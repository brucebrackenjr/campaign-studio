"""One serial campaign operation with durable intent and explicit local recovery."""

import asyncio
import hashlib
import json
import time
from contextlib import suppress

from campaign_studio.brief import Brief
from campaign_studio.capacity import production_bytes, revision_bytes
from campaign_studio.imaging import ImageProblem, image_suffix, require_font
from campaign_studio.planning import Plan, compare_plans, make_local_plan
from campaign_studio.provider import (
    OpenAIImages,
    ProviderFailure,
    read_saved_response,
    recover_source,
)
from campaign_studio.rendering import render_revision
from campaign_studio.state import State, StateError
from campaign_studio.storage import StorageError


class SavedSourceError(StateError):
    """A verified input is unavailable; output-only recovery cannot repair it."""


class Runner:
    def __init__(self, state: State):
        self.state = state
        state.reconcile_interrupted()

    def _verify_sources(self, plan: Plan) -> None:
        for asset in plan.assets.values():
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
            started = time.monotonic()
            run = self.state.run(run_id)
            if run["state"] == "complete":
                revision = self.state.revision(run["current_revision"])
                return json.loads(self.state.storage.path(revision["manifest_ref"]).read_bytes())
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
            from campaign_studio.cleanup import Cleanup

            retirement_files = await asyncio.to_thread(
                Cleanup(self.state).prepare_retirement, plan.brief.campaign_id
            )
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
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        render_revision,
                        self.state,
                        run_id,
                        plan,
                        operation_started=started,
                        retirement_files=retirement_files,
                    )
                )
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # A thread cannot be canceled. Keep the instance lock until it finishes.
                    await worker
                    raise
                if not result["complete"] and self.state.run(run_id)["stop_requested"]:
                    self.state.set_status(run_id, "stopped")
                return result
            except BaseException:
                if self.state.run(run_id)["state"] != "complete":
                    self.state.set_status(run_id, "paused")
                raise

    def recover_local(self, run_id: str, revision_id: str | None = None) -> dict:
        with self.state.storage.exclusive_run():
            started = time.monotonic()
            self.state.require_local_recovery(run_id)
            self._recover_received(run_id)
            plan = self.state.plan(run_id)
            if revision_id:
                revision = self.state.revision(revision_id)
                if revision["run_id"] != run_id:
                    raise StateError("The requested revision belongs to another run.")
                plan = Plan.model_validate_json(
                    self.state.storage.path(revision["plan_ref"]).read_bytes()
                )
            self._verify_sources(plan)
            return render_revision(
                self.state,
                run_id,
                plan,
                # Recovery rebuilds outputs from verified sources, including when
                # an existing output file is missing, changed, or damaged.
                operation_started=started,
            )

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
    ) -> dict:
        with self.state.storage.exclusive_run():
            started = time.monotonic()
            # The runner lock excludes production and another Apply. A newer
            # unstarted plan does not replace the accepted image being edited.
            self.state.require_current_revision(run_id, expected_revision)
            old = self.state.plan(run_id)
            authored_base = None
            if any(edit is not None for edit in (logo_edit, text_edit, frame_edit)):
                from campaign_studio.authored import AuthoredProjects
                from campaign_studio.workspace import Workspace

                authored_base = AuthoredProjects(Workspace(self.state)).edit_authored_base(
                    old.brief.campaign_id, expected_revision
                )
            # Web edits re-derive the brief under this lock; sample callers supply a complete brief.
            new = None
            if logo_edit is not None:
                from campaign_studio.workspace import Workspace

                brief, new, _ = Workspace(self.state).logo(brief.campaign_id, logo_edit)
            if text_edit is not None:
                from campaign_studio.workspace import Workspace

                brief, new, _ = Workspace(self.state).text(brief.campaign_id, text_edit)
            if frame_edit is not None:
                from campaign_studio.workspace import Workspace

                brief, new, _ = Workspace(self.state).frame(brief.campaign_id, frame_edit)
            if new is None:
                new = make_local_plan(old, brief, self.state.storage)
            if new.digest != expected_plan_digest:
                raise StateError("The edit preview changed; preview its effects again.")
            impact = compare_plans(old, new)
            if logo_edit is not None:
                previous = self.state.revision(expected_revision)
                manifest = json.loads(
                    self.state.storage.path(previous["manifest_ref"]).read_bytes()
                )
                available = {o["id"] for o in manifest["outputs"] if o["file"]}
                impact.render_outputs = [o for o in impact.render_outputs if o in available]
            if {product.id for product in old.brief.products} != {
                product.id for product in new.brief.products
            } or set(old.brief.markets) != set(new.brief.markets):
                raise StateError("Product or market selection changes require a new saved plan.")
            if (
                impact.generation_products
                or old.brief.replay != new.brief.replay
                or old.brief.audience != new.brief.audience
                or old.brief.creative_direction != new.brief.creative_direction
            ):
                raise StateError("An imagery change requires a new saved plan and confirmation.")
            for product in new.brief.products:
                previous = next(
                    (item for item in old.brief.products if item.id == product.id), None
                )
                if (
                    previous is None
                    or product.source != previous.source
                    or old.assets.get(f"products/{product.id}")
                    != new.assets.get(f"products/{product.id}")
                ):
                    raise StateError("Source changes require a new saved plan and confirmation.")
            # Corrections never recover another output or alter attempt outcomes.
            self.state.require_edit_inputs(run_id, new, impact.render_outputs)
            retirement_files = None
            if logo_edit is not None:
                from campaign_studio.cleanup import Cleanup

                self.state.require_image_cleanup(run_id)
                retirement_files = Cleanup(self.state).prepare_retirement(brief.campaign_id)
            if (
                "logo" in new.assets
                and new.assets["logo"] != old.assets.get("logo")
                and not any(
                    item.item == "logo" and item.severity == "blocking" for item in new.findings
                )
            ):
                asset = new.assets["logo"]
                payload = self.state.storage.path(asset.reference).read_bytes()
                reference = f"campaigns/{run_id}/inputs/logo-{asset.sha256}{image_suffix(payload)}"
                self.state.storage.require_capacity(revision_bytes(new) + 2 * len(payload))
                destination = self.state.storage.path(reference)
                if destination.exists():
                    if hashlib.sha256(destination.read_bytes()).hexdigest() != asset.sha256:
                        raise StateError("The saved logo snapshot changed. Reopen current work.")
                else:
                    self.state.storage.write_new(reference, payload)
                value = new.brief.model_dump()
                value["brand"]["logo_path"] = reference
                new = make_local_plan(old, Brief.model_validate(value), self.state.storage)
            result = render_revision(
                self.state,
                run_id,
                new,
                reuse_revision=expected_revision,
                operation_started=started,
                preserve_omissions=logo_edit is not None
                or (text_edit is not None and not text_edit.values),
                retirement_files=retirement_files,
                edit_receipt={
                    "kind": (
                        "logo"
                        if logo_edit is not None
                        else "text"
                        if text_edit is not None
                        else "framing"
                    ),
                    "base_revision": expected_revision,
                    "preview_digest": expected_plan_digest,
                    **({"authored_base": authored_base} if authored_base else {}),
                    **(
                        {"replacement_draft_version": logo_edit.draft_version}
                        if logo_edit is not None and logo_edit.replacement
                        else {}
                    ),
                }
                if any(edit is not None for edit in (logo_edit, text_edit, frame_edit))
                else None,
            )
            if (
                any(edit is not None for edit in (logo_edit, text_edit, frame_edit))
                and self.state.effective_revision(run_id) != result["revision_id"]
            ):
                # A failed candidate remains useful diagnostics, not an applied
                # image or authority to replace the campaign's authored logo.
                raise StateError(
                    "Could not apply. Your edits are still here.", code="apply_not_accepted"
                )
            if logo_edit is not None and logo_edit.replacement:
                from campaign_studio.intake import Intake
                from campaign_studio.workspace import Workspace

                Intake(self.state.storage).persist_logo_revision(
                    brief.campaign_id, Workspace(self.state)
                )
            return result
