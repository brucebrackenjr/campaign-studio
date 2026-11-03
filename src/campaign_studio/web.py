"""Local task pages and deliberate workflow actions; no arbitrary file serving."""

import asyncio
import hashlib
import io
import time
from contextlib import ExitStack
from mimetypes import guess_type
from urllib.parse import quote
from zipfile import ZipFile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from campaign_studio.brief import FORMATS, CampaignID
from campaign_studio.capacity import production_bytes
from campaign_studio.cleanup import Cleanup
from campaign_studio.credentials import invalid_key
from campaign_studio.exporting import delivery_paths, public_manifest, require_export_wording
from campaign_studio.handoff import Handoff, MetadataAction, PackageAction
from campaign_studio.imaging import (
    FontProblem,
    crop_box,
    load_image,
    require_font,
    save_srgb,
    text_blocks,
)
from campaign_studio.intake import Intake
from campaign_studio.rendering import blocks_output
from campaign_studio.review import RevisionTarget
from campaign_studio.runner import SavedSourceError
from campaign_studio.storage import CapacityError
from campaign_studio.workspace import (
    MISSING_KEY,
    ActionError,
    FrameEdit,
    LogoEdit,
    RunAction,
    TextEdit,
    image_url,
    provider_notice,
)


def attachment_header(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


def routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/openai-key")
    async def key_status(request: Request):
        return {
            **request.app.state.credentials.status(),
            "available": request.app.state.key_available(),
        }

    @router.post("/api/openai-key")
    async def save_key(request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise invalid_key() from None
        if not isinstance(body, dict) or set(body) != {"api_key"}:
            raise invalid_key()
        # This local write never invokes a provider or changes a campaign plan.
        return await asyncio.to_thread(request.app.state.credentials.save, body["api_key"])

    async def action_body(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ActionError("action_shape", "Send one action with its named fields.", 422)
        return body

    def services(request):
        return request.app.state.workspace

    def busy(request):
        return any(not task.done() for task in request.app.state.operations.values())

    def require_idle(request):
        if busy(request):
            raise ActionError(
                "busy", "A campaign operation is active. View Production for progress."
            )

    def ensure_run(request, campaign_id, run_id=None):
        run = services(request).run_for(campaign_id, run_id)
        if run is None:
            raise ActionError(
                "no_plan", "This campaign needs a plan. Open Production to review it."
            )
        return run

    def action_run(request, campaign_id, body):
        if not isinstance(body, dict) or not body.get("run_id"):
            raise ActionError("stale", "Refresh the saved campaign before taking this action.")
        return ensure_run(request, campaign_id, body["run_id"])

    def manifest_with_links(workspace, run):
        manifest = workspace.manifest(run)
        if manifest:
            prefix = workspace.state.revision(manifest["revision_id"])["manifest_ref"].rsplit(
                "/", 1
            )[0]
            for item in manifest["outputs"]:
                item["url"] = image_url(run["campaign_id"], manifest["revision_id"], item)
                if item["file"]:
                    path = workspace.storage.path(f"{prefix}/{item['file']}")
                    if (
                        not path.is_file()
                        or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
                    ):
                        item["url"] = None
                        item["omission"] = (
                            "Saved image missing or changed. Use local recovery in Production."
                        )
            manifest["metrics"]["completed_images"] = sum(
                bool(item["url"]) for item in manifest["outputs"]
            )
            manifest["complete"] = all(item["url"] for item in manifest["outputs"])
        return manifest

    def status(request, campaign_id):
        workspace = services(request)
        # Recovery can retire the selected PNGs between reading the manifest and
        # checking its files. The publisher holds this same lock.
        with workspace.storage.exclusive_draft():
            run = ensure_run(request, campaign_id)
            manifest = manifest_with_links(workspace, run)
        attempts = workspace.state.attempts(run["id"])
        notice = provider_notice(attempts)
        operation_error = request.app.state.operation_errors.get(run["id"])
        active = (
            run["id"] in request.app.state.operations
            and not request.app.state.operations[run["id"]].done()
        )
        progress = workspace.state.live_progress.get(run["id"], {}) if active else {}
        # Saved output availability survives a retry. Count the union rather than
        # resetting to the new operation's zero or double-counting reused files.
        completed_ids = (
            {item["id"] for item in manifest["outputs"] if item["url"]} if manifest else set()
        )
        completed_ids.update(progress.get("completed_ids", []))
        timing = request.app.state.operation_times.get(run["id"])
        elapsed = (
            round((timing.get("finished") or time.monotonic()) - timing["started"], 1)
            if timing
            else (manifest["metrics"]["elapsed_processing_seconds"] if manifest else None)
        )
        return {
            "run_id": run["id"],
            "state": run["state"],
            "active": active,
            "attempted": len(attempts),
            "campaign_metrics": workspace.state.campaign_metrics(campaign_id),
            "attempts": [
                {key: attempt[key] for key in ("product_id", "state", "created_at", "error_kind")}
                for attempt in attempts
            ],
            "remaining": max(0, run["allowance"] - len(attempts)),
            "completed": len(completed_ids),
            "total": len(workspace.state.plan(run["id"]).outputs),
            "stage": progress.get("stage", "Preparing saved images"),
            "elapsed_seconds": elapsed,
            "stoppable": active and progress.get("stage", "").startswith("Generating"),
            "revision_id": manifest["revision_id"] if manifest else None,
            "notice": operation_error["message"]
            if operation_error
            else (notice.message if notice else None),
            "capacity": operation_error.get("capacity") if operation_error else None,
            "notice_code": operation_error["code"]
            if operation_error
            else (notice.code if notice else None),
        }

    def launch(request, run_id, operation):
        require_idle(request)
        request.app.state.operation_errors.pop(run_id, None)
        request.app.state.operation_times[run_id] = {"started": time.monotonic(), "finished": None}

        async def perform():
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except CapacityError as error:
                request.app.state.operation_errors[run_id] = {
                    "code": "storage_capacity",
                    "message": str(error),
                    "capacity": error.details,
                }
            except FontProblem as error:
                request.app.state.operation_errors[run_id] = {
                    "code": "font_unavailable",
                    "message": str(error),
                }
            except SavedSourceError:
                request.app.state.operation_errors[run_id] = {
                    "code": "source_unavailable",
                    "message": "A saved source image is missing or changed. Local recovery cannot "
                    "repair that input. Open Production, attach the intended source again, "
                    "and review a new production plan. No image request was sent.",
                }
            except Exception:
                request.app.state.operation_errors[run_id] = {
                    "code": "local_operation",
                    "message": "The local operation could not finish. Saved work is preserved. "
                    "Check local storage, then use local recovery.",
                }
            finally:
                request.app.state.operation_times[run_id]["finished"] = time.monotonic()

        request.app.state.operations[run_id] = asyncio.create_task(perform())

    @router.get("/campaigns/{campaign_id}/logo")
    async def logo_image(request: Request, campaign_id: CampaignID):
        workspace = services(request)
        plan = workspace.plan(campaign_id)
        asset = plan.assets.get("logo")
        if not asset:
            raise ActionError("logo", "No logo is selected yet.", 404)
        return FileResponse(workspace.storage.path(asset.reference))

    @router.post("/api/uploads")
    async def upload(request: Request):
        campaign_id = request.headers.get("X-Campaign-Id")
        if not campaign_id:
            raise ActionError(
                "upload_campaign", "Choose a campaign before attaching an image.", 422
            )
        return await asyncio.to_thread(
            Intake(services(request).storage).upload,
            await request.body(),
            request.headers.get("content-type", "").split(";", 1)[0],
            campaign_id,
            request.headers.get("X-Image-Role"),
        )

    @router.post("/api/campaigns/{campaign_id}/cleanup/preview")
    async def cleanup_preview(request: Request, campaign_id: CampaignID):
        return await asyncio.to_thread(
            Cleanup(services(request).state).preview_campaign, campaign_id
        )

    @router.post("/api/campaigns/{campaign_id}/cleanup/confirm")
    async def cleanup_confirm(request: Request, campaign_id: CampaignID):
        body = await request.json()
        if (
            not isinstance(body, dict)
            or set(body) != {"token"}
            or not isinstance(body["token"], str)
        ):
            raise ActionError("cleanup", "Review the campaign deletion before confirming.", 422)
        return await asyncio.to_thread(
            Cleanup(services(request).state).delete_campaign,
            campaign_id,
            body["token"],
            defer_cleanup=True,
        )

    @router.post("/api/campaigns/{campaign_id}/cleanup/{cleanup_id}/retry")
    async def cleanup_retry(request: Request, campaign_id: CampaignID, cleanup_id: str):
        return await asyncio.to_thread(
            Cleanup(services(request).state).retry, campaign_id, cleanup_id
        )

    @router.get("/api/campaigns/{campaign_id}/cleanup/status")
    async def cleanup_status(request: Request, campaign_id: CampaignID):
        return await asyncio.to_thread(Cleanup(services(request).state).status, campaign_id)

    @router.post(
        "/api/campaigns/{campaign_id}/runs/{run_id}/revisions/{revision_id}/metadata/{product_id}/{market}/{ratio}"
    )
    async def save_creative_metadata(
        request: Request,
        campaign_id: CampaignID,
        run_id: str,
        revision_id: str,
        product_id: str,
        market: str,
        ratio: str,
        action: MetadataAction,
    ):
        require_idle(request)
        target = RevisionTarget(
            campaign_id=campaign_id, run_id=run_id, revision_id=revision_id, mode=action.mode
        )
        return await asyncio.to_thread(
            Handoff(services(request).state).update,
            target,
            f"{product_id}/{market}/{ratio}",
            action,
        )

    @router.post("/api/campaigns/{campaign_id}/runs/{run_id}/revisions/{revision_id}/zip")
    async def create_zip(
        request: Request,
        campaign_id: CampaignID,
        run_id: str,
        revision_id: str,
        action: PackageAction,
    ):
        target = RevisionTarget(
            campaign_id=campaign_id, run_id=run_id, revision_id=revision_id, mode="current"
        )
        result = await asyncio.to_thread(
            Handoff(services(request).state).create_zip, target, action
        )
        result["url"] = (
            f"/api/campaigns/{campaign_id}/runs/{run_id}/revisions/{revision_id}"
            f"/packages/{result['artifact_id']}/download"
        )
        return result

    @router.get(
        "/api/campaigns/{campaign_id}/runs/{run_id}/revisions/{revision_id}/packages/{artifact_id}/download"
    )
    async def download_zip(
        request: Request, campaign_id: CampaignID, run_id: str, revision_id: str, artifact_id: str
    ):
        target = RevisionTarget(
            campaign_id=campaign_id, run_id=run_id, revision_id=revision_id, mode="saved_version"
        )
        payload = await asyncio.to_thread(
            Handoff(services(request).state).download, target, artifact_id
        )
        with ZipFile(io.BytesIO(payload)) as archive:
            filename = archive.namelist()[0].split("/", 1)[0] + ".zip"
        return Response(
            payload,
            media_type="application/zip",
            headers={"Content-Disposition": attachment_header(filename)},
        )

    @router.get("/uploads/{name}")
    async def uploaded_image(request: Request, name: str):
        return FileResponse(Intake(services(request).storage).uploaded_path(name))

    @router.post("/api/campaigns/{campaign_id}/start")
    async def start(request: Request, campaign_id: CampaignID, action: RunAction):
        workspace = services(request)
        run = ensure_run(request, campaign_id, action.run_id)
        if (
            run["id"] in request.app.state.operations
            and not request.app.state.operations[run["id"]].done()
        ):
            return JSONResponse(
                {**status(request, campaign_id), "disposition": "already_active"}, status_code=202
            )
        if run["state"] == "complete":
            return status(request, campaign_id)
        require_idle(request)
        plan = workspace.state.plan(run["id"])
        if plan.blocked:
            raise ActionError(
                "blocked", "Some campaign details need correcting before production can start."
            )
        require_font()
        workspace.state.require_image_cleanup(run["id"])
        provider = None
        pending = workspace.state.pending_generation(run["id"], plan)
        await asyncio.to_thread(
            workspace.storage.require_capacity, production_bytes(plan, len(pending))
        )
        workspace.state.require_authored_plan(run["id"])
        if action.retry and not pending:
            raise ActionError(
                "local_recovery",
                "No imagery is pending. Use local recovery to rebuild the last complete "
                "revision, or correct the blocked draft in Review.",
            )
        if pending:
            if not request.app.state.key_available():
                raise ActionError(MISSING_KEY.code, MISSING_KEY.message)
            provider = request.app.state.provider_factory()
        if not action.retry and action.allowance != run["allowance"]:
            raise ActionError(
                "allowance", "The request allowance changed. Review the updated plan."
            )
        workspace.state.confirm(
            run["id"],
            action.digest,
            retry_allowance=action.allowance if action.retry else None,
        )
        launch(request, run["id"], lambda: request.app.state.runner.execute(run["id"], provider))
        return JSONResponse(
            {**status(request, campaign_id), "disposition": "started"}, status_code=202
        )

    @router.get("/api/campaigns/{campaign_id}/status")
    async def get_status(request: Request, campaign_id: CampaignID):
        return status(request, campaign_id)

    @router.post("/api/campaigns/{campaign_id}/stop")
    async def stop(request: Request, campaign_id: CampaignID):
        body = await action_body(request)
        run = action_run(request, campaign_id, body)
        if run["state"] not in {"running", "confirmed"}:
            raise ActionError(
                "not_running",
                "There is no active request to stop. Saved work is available in Review.",
            )
        services(request).state.stop(run["id"])
        return {
            "message": "Stop requested. The current request may still finish or incur a charge."
        }

    @router.post("/api/campaigns/{campaign_id}/recover")
    async def recover(request: Request, campaign_id: CampaignID):
        body = await action_body(request)
        run = action_run(request, campaign_id, body)
        # Refuse before reporting an accepted action. The runner repeats this
        # admission under its lock after any gap before the background task.
        with services(request).storage.exclusive_run():
            services(request).state.require_local_recovery(run["id"])
        launch(
            request,
            run["id"],
            lambda: asyncio.to_thread(request.app.state.runner.recover_local, run["id"]),
        )
        return JSONResponse(status(request, campaign_id), status_code=202)

    @router.post("/api/campaigns/{campaign_id}/text/preview")
    async def text_preview(request: Request, campaign_id: CampaignID, edit: TextEdit):
        require_idle(request)
        _, plan, impact = await asyncio.to_thread(
            services(request).text, campaign_id, edit, allow_unchanged=True
        )
        selected = next(o for o in plan.outputs if o.id == edit.output_id)
        geometry = {
            block["id"]: {
                **{k: v for k, v in block.items() if k not in {"lines", "text"}},
                "color": block["color"] or plan.brief.brand.text_color,
            }
            for block in text_blocks(
                selected.text,
                selected.ratio,
                selected.framing,
                validate=False,
            )
        }
        images = await asyncio.to_thread(
            request.app.state.previews.render, campaign_id, edit, plan, impact, text_preview=True
        )
        affected = [
            output
            for output in plan.outputs
            if output.id == edit.output_id or output.id in impact["render_outputs"]
        ]
        blocking = [
            finding
            for finding in plan.findings
            if any(blocks_output(finding, output) for output in affected)
        ]
        return {
            "digest": plan.digest,
            "impact": impact,
            "images": images,
            "geometry": {"canvas_size": [selected.width, selected.height], "text_blocks": geometry},
            "blocked": bool(blocking),
            "findings": [finding.model_dump() for finding in blocking],
        }

    @router.post("/api/campaigns/{campaign_id}/text/apply")
    async def text_apply(request: Request, campaign_id: CampaignID, edit: TextEdit):
        require_idle(request)
        brief, plan, impact = await asyncio.to_thread(services(request).text, campaign_id, edit)
        if any(
            blocks_output(f, o)
            for f in plan.findings
            for o in plan.outputs
            if o.id == edit.output_id or o.id in impact["render_outputs"]
        ):
            raise ActionError(
                "text_layout", "Resolve the listed text or image problems before applying.", 422
            )
        if edit.preview_digest != plan.digest:
            raise ActionError("stale", "Review the current text preview before applying.")
        manifest = await asyncio.to_thread(
            request.app.state.runner.revise,
            edit.run_id,
            brief,
            edit.revision_id,
            edit.preview_digest,
            text_edit=edit,
        )
        return {
            "complete": manifest["complete"],
            "revision_id": manifest["revision_id"],
            "impact": impact,
        }

    @router.post("/api/campaigns/{campaign_id}/framing/preview")
    async def frame_preview(request: Request, campaign_id: CampaignID, edit: FrameEdit):
        require_idle(request)
        _, plan, impact = await asyncio.to_thread(
            services(request).frame, campaign_id, edit, allow_unchanged=True
        )
        images = await asyncio.to_thread(
            request.app.state.previews.render,
            campaign_id,
            edit,
            plan,
            impact,
            framing_preview=True,
        )
        return {
            "digest": plan.digest,
            "impact": impact,
            "images": images,
            "blocked": any(
                blocks_output(f, o)
                for f in plan.findings
                for o in plan.outputs
                if o.product_id == edit.product_id
                and o.market == edit.market
                and o.ratio == edit.ratio
            ),
            "findings": [f.model_dump() for f in plan.findings if f.severity == "blocking"],
        }

    @router.post("/api/campaigns/{campaign_id}/framing/apply")
    async def frame_apply(request: Request, campaign_id: CampaignID, edit: FrameEdit):
        require_idle(request)
        brief, plan, impact = await asyncio.to_thread(services(request).frame, campaign_id, edit)
        if any(
            blocks_output(f, o)
            for f in plan.findings
            for o in plan.outputs
            if o.id in impact["render_outputs"]
        ):
            raise ActionError(
                "framing",
                "The proposed crop cuts protected detail. Adjust it before applying.",
                422,
            )
        if edit.preview_digest != plan.digest:
            raise ActionError("stale", "Review the current framing preview before applying.")
        manifest = await asyncio.to_thread(
            request.app.state.runner.revise,
            edit.run_id,
            brief,
            edit.revision_id,
            edit.preview_digest,
            frame_edit=edit,
        )
        return {"complete": manifest["complete"], "revision_id": manifest["revision_id"]}

    @router.post("/api/campaigns/{campaign_id}/logo/preview")
    async def logo_preview(request: Request, campaign_id: CampaignID, edit: LogoEdit):
        require_idle(request)
        _, plan, impact = await asyncio.to_thread(
            services(request).logo, campaign_id, edit, allow_unchanged=True
        )
        images = await asyncio.to_thread(
            request.app.state.previews.render, campaign_id, edit, plan, impact, logo_preview=True
        )
        return {
            "digest": plan.digest,
            "impact": impact,
            "images": images,
            "logo_asset": plan.assets["logo"].model_dump()
            if impact.get("logo_available")
            else None,
            "blocked": any(
                blocks_output(f, o)
                for f in plan.findings
                for o in plan.outputs
                if (o.id in impact["render_outputs"] or o.id == edit.output_id)
                and not (not impact.get("logo_available") and f.item == "logo")
            ),
            "findings": [f.model_dump() for f in plan.findings if f.severity == "blocking"],
        }

    @router.post("/api/campaigns/{campaign_id}/logo/apply")
    async def logo_apply(request: Request, campaign_id: CampaignID, edit: LogoEdit):
        require_idle(request)
        brief, plan, impact = await asyncio.to_thread(services(request).logo, campaign_id, edit)
        if any(
            blocks_output(f, o)
            for f in plan.findings
            for o in plan.outputs
            if o.id in impact["render_outputs"]
        ):
            raise ActionError(
                "logo", "Resolve the listed logo or image problems before applying.", 422
            )
        if edit.preview_digest != plan.digest:
            raise ActionError("stale", "Review the current logo preview before applying.")
        manifest = await asyncio.to_thread(
            request.app.state.runner.revise,
            edit.run_id,
            brief,
            edit.revision_id,
            edit.preview_digest,
            logo_edit=edit,
        )
        return {
            "complete": manifest["complete"],
            "revision_id": manifest["revision_id"],
            "impact": impact,
        }

    @router.get("/api/campaigns/{campaign_id}/draft-images/{token}/{product}/{market}/{ratio}")
    async def draft_image(
        request: Request, campaign_id: CampaignID, token: str, product: str, market: str, ratio: str
    ):
        payload = await asyncio.to_thread(
            request.app.state.previews.image, campaign_id, token, product, market, ratio
        )
        if payload is None:
            raise ActionError(
                "expired_preview",
                "This draft preview expired. Edit the wording to refresh it.",
                404,
            )
        return Response(payload, media_type="image/png")

    @router.post("/api/campaigns/{campaign_id}/export")
    async def export(request: Request, campaign_id: CampaignID):
        require_idle(request)
        body = await action_body(request)
        workspace = services(request)
        run = next(
            (item for item in workspace.runs(campaign_id) if item["id"] == body.get("run_id")), None
        )
        if run is None:
            raise ActionError("stale", "Reopen this saved version before exporting.")
        if not body.get("revision_id"):
            raise ActionError(
                "nothing_to_export", "Nothing to export yet. Choose a saved revision first."
            )
        manifest = workspace.manifest(run, body.get("revision_id"))
        if not manifest or not any(item["file"] for item in manifest["outputs"]):
            raise ActionError(
                "nothing_to_export", "Nothing to export yet. Open Production to create drafts."
            )
        if not manifest["complete"] and body.get("allow_partial") is not True:
            raise ActionError(
                "partial",
                "This revision has omissions. Choose Create partial review package "
                "to include them explicitly.",
            )
        snapshot = (
            body.get("snapshot_id")
            or workspace.state.metadata_snapshot(manifest["revision_id"])["snapshot_id"]
        )
        return await asyncio.to_thread(
            Handoff(workspace.state).create_folder,
            RevisionTarget(
                campaign_id=campaign_id,
                run_id=run["id"],
                revision_id=manifest["revision_id"],
                mode="current",
            ),
            PackageAction(snapshot_id=snapshot, allow_partial=body.get("allow_partial") is True),
        )

    @router.get("/campaigns/{campaign_id}/images/{revision_id}/{product_id}/{market}/{ratio}")
    async def image(
        request: Request,
        campaign_id: CampaignID,
        revision_id: str,
        product_id: str,
        market: str,
        ratio: str,
        download: bool = False,
    ):
        workspace = services(request)
        with ExitStack() as locks:
            if download:
                locks.enter_context(workspace.storage.exclusive_run())
                locks.enter_context(workspace.storage.exclusive_draft())
                run_id = workspace.state.revision(revision_id)["run_id"]
                workspace.state.require_current_output(
                    RevisionTarget(
                        campaign_id=campaign_id,
                        run_id=run_id,
                        revision_id=revision_id,
                        mode="current",
                    )
                )
                require_export_wording(workspace.state, campaign_id)
            with workspace.storage.connect() as db:
                retired = db.execute(
                    "SELECT r.images_retired FROM revisions r JOIN runs ON runs.id=r.run_id "
                    "WHERE r.id=? AND runs.campaign_id=?",
                    (revision_id, campaign_id),
                ).fetchone()
            if retired and retired[0]:
                raise ActionError(
                    "retired_revision",
                    "This image is no longer available. "
                    "Open the current images or an earlier exported package.",
                    410,
                )
            run = next(
                (
                    item
                    for item in workspace.runs(campaign_id)
                    if any(r["id"] == revision_id for r in workspace.revisions(item["id"]))
                ),
                None,
            )
            if run is None:
                raise ActionError("image", "This saved image is unavailable.", 404)
            manifest = workspace.manifest(run, revision_id)
            item = next(
                (
                    item
                    for item in manifest["outputs"]
                    if item["product_id"] == product_id
                    and item["market"] == market
                    and item["ratio"].replace(":", "x") == ratio
                    and item["file"]
                ),
                None,
            )
            if item is None:
                raise ActionError(
                    "missing_image",
                    "This output is unavailable. Open Studio to inspect the current images.",
                    404,
                )
            prefix = workspace.state.revision(revision_id)["manifest_ref"].rsplit("/", 1)[0]
            path = workspace.storage.path(f"{prefix}/{item['file']}")
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
            ):
                raise ActionError(
                    "changed_image",
                    "This saved image is missing or changed. Use local recovery in Production.",
                    409,
                )
            if download:
                public = public_manifest(workspace.state, revision_id)
                filename = " - ".join(delivery_paths(public)[item["id"]].split("/")[1:])
                return Response(
                    path.read_bytes(),
                    media_type="image/png",
                    headers={
                        "Content-Disposition": attachment_header(filename),
                    },
                )
            return FileResponse(path, media_type="image/png")

    @router.get("/campaigns/{campaign_id}/sources/{product_id}/{ratio}")
    async def source_preview(
        request: Request,
        campaign_id: CampaignID,
        product_id: str,
        ratio: str,
        retained: bool = False,
    ):
        workspace = services(request)
        if retained:
            source = workspace.retained_product_source(campaign_id, product_id)
            reference = source["path"] if source else None
            prepared = source and source["role"] == "prepared_product"
        else:
            plan = workspace.plan(campaign_id)
            reference = workspace.source_preview_reference(campaign_id, product_id)
            product = next((p for p in plan.brief.products if p.id == product_id), None)
            prepared = product and product.source.role == "prepared_product"
        if reference is None:
            raise ActionError(
                "missing_source", "This product has no available source preview.", 404
            )
        selected = ratio.replace("x", ":")
        if selected not in FORMATS:
            raise ActionError("format", "Choose a supported format.", 404)

        def render():
            image, _ = load_image(workspace.storage.path(reference))
            if not prepared:
                image = image.crop(crop_box(image.size, selected))
            image.thumbnail((480, 480))
            buffer = io.BytesIO()
            save_srgb(image, buffer)
            image.close()
            return buffer.getvalue()

        return Response(await asyncio.to_thread(render), media_type="image/png")

    @router.get("/reports/{report_id}/{reference:path}")
    async def report_file(request: Request, report_id: str, reference: str):
        # Keep these short reads serial on the event loop, as with image delivery;
        # parallel report assets must not compete for the nonblocking run lock.
        payload = Handoff(services(request).state).report_file(report_id, reference)
        return Response(payload, media_type=guess_type(reference)[0] or "application/octet-stream")

    return router
