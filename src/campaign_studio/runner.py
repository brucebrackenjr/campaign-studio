"""One serial campaign operation with durable intent and explicit local recovery."""

import asyncio
import hashlib
from contextlib import suppress

from campaign_studio.brief import Brief
from campaign_studio.capacity import production_bytes
from campaign_studio.imaging import ImageProblem, require_font
from campaign_studio.planning import Plan
from campaign_studio.provider import (
    OpenAIImages,
    ProviderFailure,
    read_saved_response,
    recover_source,
)
from campaign_studio.state import State, StateError
from campaign_studio.storage import StorageError


class SavedSourceError(StateError):
    """A verified input is unavailable; output-only recovery cannot repair it."""


class Runner:
    def __init__(self, state: State):
        self.state = state
        state.reconcile_interrupted()
        # Restore accurate accounting after a shutdown or manual folder changes.
        # Existing campaigns can still be opened if a drive is temporarily unreadable.
        with suppress(OSError, StorageError):
            state.storage.refresh_edit_capacity()

    def _verify_sources(self, plan: Plan) -> None:
        for key, asset in plan.assets.items():
            if key == "logo":
                continue
            try:
                payload = self.state.storage.path(asset.reference).read_bytes()
            except OSError as error:
                raise SavedSourceError("A saved source is unavailable.") from error
            if hashlib.sha256(payload).hexdigest() != asset.sha256:
                raise SavedSourceError(
                    "A saved source changed; the plan must be rebuilt before production."
                )

    def _recover_attempt(self, attempt: dict) -> None:
        body = read_saved_response(self.state.storage, attempt["response_ref"])
        usage = body.get("usage")
        # Usage describes the returned request, not successful local image work.
        # The same attempt is updated on recovery; later failures retain its usage.
        self.state.finish_attempt(
            attempt["id"], "received", usage=usage if isinstance(usage, dict) else None
        )
        recover_source(self.state.storage, body, attempt["source_ref"])
        self.state.finish_attempt(attempt["id"], "decoded")

    def _recover_received(self, run_id: str) -> None:
        for attempt in self.state.attempts(run_id):
            if attempt["state"] not in {"received", "dispatching", "unknown", "decoded"}:
                continue
            if attempt["state"] == "decoded" and self.state.source_is_intact(attempt):
                continue
            if not self.state.storage.path(attempt["response_ref"]).is_file():
                continue
            try:
                self._recover_attempt(attempt)
            except ImageProblem:
                self.state.finish_attempt(attempt["id"], "received", error="saved_response_decode")
            except StorageError, OSError:
                self.state.finish_attempt(attempt["id"], "received", error="saved_response_storage")

    async def execute(self, run_id: str, provider: OpenAIImages | None = None) -> dict:
        with self.state.storage.exclusive_run():
            run = self.state.run(run_id)
            if run["state"] == "complete":
                return {"complete": True, "sources_ready": True, "outputs": []}
            if run["state"] != "confirmed":
                raise StateError("The saved plan needs explicit confirmation before running.")
            # Execution holds the runner lock. Authored-file checks use the short
            # draft guard so work can still be saved during provider/render waits.
            with self.state.storage.connect() as db:
                latest = db.execute(
                    "SELECT id FROM runs WHERE campaign_id=? ORDER BY rowid DESC LIMIT 1",
                    (run["campaign_id"],),
                ).fetchone()
            if latest[0] != run_id:
                raise StateError(
                    "The campaign brief changed. Review a fresh production plan before starting."
                )
            with self.state.storage.exclusive_draft():
                self.state.require_authored_plan(run_id)
            plan = self.state.plan(run_id, current=False)
            self.state.require_image_cleanup(run_id)
            require_font()
            self._verify_sources(plan)
            if self.state.pending_generation(run_id, plan) and provider is None:
                raise StateError("This plan requires an explicitly supplied image provider.")
            await asyncio.to_thread(
                self.state.storage.require_capacity,
                production_bytes(plan, len(self.state.pending_generation(run_id, plan))),
            )
            self.state.set_status(run_id, "running")
            try:
                self._recover_received(run_id)
                for product_id in plan.generation_products:
                    if any(
                        item["product_id"] == product_id
                        and item["state"] == "decoded"
                        and self.state.source_is_intact(item)
                        for item in self.state.attempts(run_id)
                    ):
                        continue
                    if self.state.run(run_id)["stop_requested"]:
                        break
                    name = next(p.name for p in plan.brief.products if p.id == product_id)
                    self.state.live_progress[run_id] = {
                        "stage": f"Generating an image for {name}",
                        "product_id": product_id,
                        "completed": 0,
                        "completed_ids": [],
                    }
                    await asyncio.to_thread(
                        self.state.storage.require_capacity,
                        production_bytes(plan, len(self.state.pending_generation(run_id, plan))),
                    )
                    # Stop may arrive while disk inventory runs in its worker.
                    # Leave normally; reserve still enforces the final dispatch guard.
                    if self.state.run(run_id)["stop_requested"]:
                        break
                    with self.state.storage.exclusive_draft():
                        if not self.state.attempts(run_id):
                            self.state.require_authored_plan(run_id)
                        attempt = self.state.reserve(run_id, product_id)
                    try:
                        receipt = await provider.generate(
                            plan.generation_requests[product_id],
                            self.state.storage,
                            attempt["response_ref"],
                        )
                        self.state.finish_attempt(
                            attempt["id"], "received", request_id=receipt.request_id
                        )
                        self._recover_attempt(attempt)
                    except ProviderFailure as error:
                        usage = None
                        # A failed request may have no readable response or usage.
                        with suppress(ValueError, OSError):
                            usage = read_saved_response(
                                self.state.storage, attempt["response_ref"]
                            ).get("usage")
                        self.state.finish_attempt(
                            attempt["id"],
                            error.outcome,
                            error=error.kind,
                            usage=usage if isinstance(usage, dict) else None,
                        )
                        break
                    except ImageProblem:
                        self.state.finish_attempt(
                            attempt["id"], "received", error="saved_response_decode"
                        )
                        break
                    except StorageError, OSError:
                        self.state.finish_attempt(
                            attempt["id"], "received", error="saved_response_storage"
                        )
                        break
                    except asyncio.CancelledError:
                        self.state.finish_attempt(
                            attempt["id"], "unknown", error="canceled_local_wait"
                        )
                        raise
                    except Exception as error:
                        status = (
                            "received"
                            if self.state.storage.path(attempt["response_ref"]).is_file()
                            else "unknown"
                        )
                        self.state.finish_attempt(attempt["id"], status, error=type(error).__name__)
                        break
                from campaign_studio.compositions import finish_sources

                return await asyncio.to_thread(finish_sources, self.state, run_id, plan)
            except BaseException:
                if self.state.run(run_id)["state"] != "complete":
                    self.state.set_status(run_id, "paused")
                raise

    def recover_local(self, run_id: str, revision_id: str | None = None) -> dict:
        with self.state.storage.exclusive_run():
            self.state.require_local_recovery(run_id)
            self._recover_received(run_id)
            run = self.state.run(run_id)
            if run["state"] == "complete" and revision_id is None:
                from campaign_studio.workspace import Workspace

                current = Workspace(self.state).current_composition(run["campaign_id"])
                # Repairing verified source bytes does not change any saved editing
                # instruction. It must not invalidate every open Studio tab.
                return {
                    "campaign_id": run["campaign_id"],
                    "complete": len(current["sources"]) == len(current["plan"].brief.products),
                    "sources_ready": len(current["sources"]),
                    "version": current["version"],
                    "revision_id": current["version"],
                    "outputs": [],
                    "metrics": {
                        **self.state.campaign_metrics(run["campaign_id"]),
                        "completed_images": 0,
                    },
                }
            plan = self.state.plan(run_id)
            if revision_id:
                revision = self.state.revision(revision_id)
                if revision["run_id"] != run_id:
                    raise StateError("The requested revision belongs to another run.")
                plan = self.state.revision_plan(revision)
            self._verify_sources(plan)
            from campaign_studio.compositions import finish_sources

            return finish_sources(self.state, run_id, plan)

    def revise(
        self,
        run_id: str,
        brief: Brief,
        expected_revision: str,
        expected_plan_digest: str,
        *,
        logo_edit=None,
        text_edit=None,
        frame_edit=None,
        brand_edit=None,
    ) -> dict:
        raise StateError(
            "Editing changes save as current instructions. "
            "Finished images are created during download.",
            code="autosave",
        )
