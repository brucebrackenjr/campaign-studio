"""Local task pages and deliberate workflow actions; no arbitrary file serving."""

import asyncio
import base64
import binascii
import hashlib
import io
import time
from contextlib import ExitStack
from mimetypes import guess_type
from urllib.parse import quote, unquote
from zipfile import ZipFile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from campaign_studio.brief import FORMATS, CampaignID, Contract
from campaign_studio.capacity import production_bytes
from campaign_studio.cleanup import Cleanup
from campaign_studio.credentials import invalid_key
from campaign_studio.exporting import delivery_paths, public_manifest, require_export_rules
from campaign_studio.handoff import DeliveryAction, Handoff, MetadataAction, PackageAction
from campaign_studio.imaging import (
    FontProblem,
    crop_box,
    load_image,
    require_font,
    save_srgb,
    text_blocks,
)
from campaign_studio.intake import UPLOAD_LIMIT, Intake
from campaign_studio.plan_storage import load_plan
from campaign_studio.regeneration import RegenerationConfirm, RegenerationReview
from campaign_studio.rendering import blocks_output
from campaign_studio.review import RevisionTarget
from campaign_studio.runner import SavedSourceError
from campaign_studio.scene import SceneEdit, render_scene
from campaign_studio.state import StateError
from campaign_studio.storage import CapacityError, StorageError
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


class NewProductUpload(Contract):
    version: str | None = Field(max_length=64)
    base: dict
    product: dict
    image_base64: str = Field(max_length=((UPLOAD_LIMIT + 2) // 3) * 4)


class CancelDelivery(Contract):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ZipAttachment(StreamingResponse):
    def __init__(self, download):
        self.download = download
        chunks = iter(lambda: download.stream.read(256 * 1024), b"")
        super().__init__(
            chunks,
            media_type="application/zip",
            headers={
                "Content-Disposition": attachment_header(download.filename),
                "Content-Length": str(download.size),
                "Cache-Control": "no-store",
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes disconnects and failures while the response is being sent.
            self.download.close()


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

    @router.delete("/api/openai-key")
    async def remove_key(request: Request):
        return await asyncio.to_thread(request.app.state.credentials.remove)

    async def action_body(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ActionError("action_shape", "Send one action with its named fields.", 422)
        return body

    def services(request):
        return request.app.state.workspace

    def after_previews(request, operation, *args):
        # Background previews must finish before an upload or export takes the
        # workspace mutation lock. Keep this order so a preview cannot slip in
        # between waiting and the mutation itself.
        with request.app.state.previews.lock:
            return operation(*args)

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

    def status(request, campaign_id, run_id=None):
        workspace = services(request)
        from campaign_studio.motif import attempt_details, campaign_data

        run = workspace.state.run(run_id) if run_id else ensure_run(request, campaign_id)
        if run["campaign_id"] != campaign_id:
            raise ActionError("campaign_identity", "This request belongs to another campaign.")
        # Source-operation accounting remains readable while a separate tab saves
        # a newer composition. Its progress must not depend on preview freshness.
        data = None
        try:
            data = campaign_data(workspace, campaign_id)
            manifest = data["manifest"]
        except (ActionError, StateError) as error:
            if error.code not in {"composition_conflict", "production_complete"}:
                raise
            manifest = None
        run = workspace.state.run(run["id"])
        attempts = workspace.state.attempts(run["id"])
        attempt_facts = attempt_details(workspace, attempts)
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
        # Production prepares one source per product, independently of the
        # number of markets and formats. Keep its count separate from preview
        # coverage, and include decoded results before finish_sources publishes
        # them into the current campaign document.
        if run["state"] != "complete":
            source_plan = load_plan(run["plan_json"])
            product_ids = {product.id for product in source_plan.brief.products}
            source_ids = set()
            for product in source_plan.brief.products:
                asset = source_plan.assets.get(
                    f"products/{product.id}"
                    if product.source.role == "finished_scene"
                    else f"masters/{product.id}"
                )
                if asset is None:
                    continue
                try:
                    payload = workspace.storage.path(asset.reference).read_bytes()
                    if hashlib.sha256(payload).hexdigest() == asset.sha256:
                        source_ids.add(product.id)
                except OSError, StorageError:
                    pass
            source_ids.update(
                attempt["product_id"]
                for attempt in attempt_facts
                if attempt["product_id"] in product_ids
                and attempt["state"] == "decoded"
                and attempt["source_available"]
            )
        else:
            product_ids = {
                product["id"]
                for product in ((data or {}).get("value", {}).get("products") or [])
                if isinstance(product, dict) and isinstance(product.get("id"), str)
            }
            source_ids = set((data or {}).get("source_urls", {})) & product_ids
        timing = request.app.state.operation_times.get(run["id"])
        elapsed = (
            round((timing.get("finished") or time.monotonic()) - timing["started"], 1)
            if timing
            else (manifest["metrics"].get("elapsed_processing_seconds") if manifest else None)
        )
        return {
            "run_id": run["id"],
            "state": run["state"],
            "active": active,
            "attempted": len(attempts),
            "campaign_metrics": workspace.state.campaign_metrics(campaign_id),
            "attempts": [
                {
                    key: attempt[key]
                    for key in (
                        "product_id",
                        "state",
                        "created_at",
                        "error_kind",
                        "source_available",
                        "response_recoverable",
                    )
                }
                for attempt in attempt_facts
            ],
            "remaining": max(0, run["allowance"] - len(attempts)),
            "completed": len(completed_ids),
            "total": len(manifest["outputs"]) if manifest else 0,
            "sources_completed": len(source_ids),
            "sources_total": len(product_ids),
            "stage": progress.get(
                "stage",
                "Source images ready" if run["state"] == "complete" else "Preparing source images",
            ),
            "elapsed_seconds": elapsed,
            "stoppable": bool(
                active
                and run["state"] != "complete"
                and progress.get("stage", "").startswith("Generating")
                and not run["stop_requested"]
                and (
                    source_plan.replacement is not None
                    or (
                        len(attempts) < run["allowance"]
                        and (
                            set(source_plan.generation_products)
                            - source_ids
                            - {progress.get("product_id")}
                        )
                    )
                )
            ),
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
            except (ActionError, StateError) as error:
                request.app.state.operation_errors[run_id] = {
                    "code": error.code,
                    "message": str(error),
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
        try:
            plan = workspace.plan(campaign_id)
        except ValidationError as error:
            raise ActionError("logo", "No logo is selected yet.", 404) from error
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
            after_previews,
            request,
            Intake(services(request).storage).upload,
            await request.body(),
            request.headers.get("content-type", "").split(";", 1)[0],
            campaign_id,
            request.headers.get("X-Image-Role"),
            unquote(request.headers.get("X-Asset-Name", "")[:2048]),
            request.headers.get("X-Source-Role"),
        )

    @router.post("/api/uploads/validate")
    async def validate_upload(request: Request):
        return await asyncio.to_thread(
            Intake.validate_upload,
            await request.body(),
            "product",
            request.headers.get("X-Source-Role", "finished_scene"),
        )

    @router.post("/api/motif/campaigns/{campaign_id}/products/photo")
    async def add_product_photo(
        request: Request, campaign_id: CampaignID, action: NewProductUpload
    ):
        from campaign_studio.authored import AuthoredBase
        from campaign_studio.motif import campaign_data

        base = AuthoredBase.model_validate(action.base).model_dump()
        try:
            payload = base64.b64decode(action.image_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ActionError("upload_image", "Choose a valid PNG or JPEG image.", 422) from error
        ws = services(request)
        project = await asyncio.to_thread(
            Intake(ws.storage).add_product,
            ws,
            campaign_id,
            action.version,
            base,
            action.product,
            payload,
        )
        result = await asyncio.to_thread(campaign_data, ws, campaign_id, verify_saved_files=False)
        return {"project": project, "campaign": result}

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
        download = await run_in_threadpool(
            after_previews,
            request,
            Handoff(services(request).state).create_zip,
            target,
            action,
        )
        return ZipAttachment(download)

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
            after_previews,
            request,
            Handoff(services(request).state).download,
            target,
            artifact_id,
        )
        with ZipFile(io.BytesIO(payload)) as archive:
            filename = archive.namelist()[0].split("/", 1)[0] + ".zip"
        return Response(
            payload,
            media_type="application/zip",
            headers={"Content-Disposition": attachment_header(filename)},
        )

    @router.get("/api/campaigns/{campaign_id}/delivery/progress/{operation_id}")
    async def current_delivery_progress(
        request: Request, campaign_id: CampaignID, operation_id: str
    ):
        return Handoff(services(request).state).current_progress(campaign_id, operation_id)

    @router.post("/api/campaigns/{campaign_id}/delivery/cancel")
    async def cancel_delivery(request: Request, campaign_id: CampaignID, action: CancelDelivery):
        return await asyncio.to_thread(
            Handoff(services(request).state).cancel_current_delivery,
            campaign_id,
            action.operation_id,
        )

    @router.post("/api/campaigns/{campaign_id}/delivery/zip")
    async def current_zip(request: Request, campaign_id: CampaignID, action: DeliveryAction):
        download = await asyncio.to_thread(
            Handoff(services(request).state).create_current_zip, campaign_id, action
        )
        return ZipAttachment(download)

    @router.post("/api/campaigns/{campaign_id}/delivery/report-preview")
    async def current_report(request: Request, campaign_id: CampaignID, action: DeliveryAction):
        return await asyncio.to_thread(
            Handoff(services(request).state).create_current_preview, campaign_id, action
        )

    @router.get(
        "/reports/current/{campaign_id}/{version}/{partial}/{created}/{fingerprint}/{reference:path}"
    )
    async def current_report_file(
        request: Request,
        campaign_id: CampaignID,
        version: str,
        partial: int,
        created: int,
        fingerprint: str,
        reference: str,
    ):
        payload = await asyncio.to_thread(
            Handoff(services(request).state).current_preview_file,
            campaign_id,
            version,
            bool(partial),
            created,
            fingerprint,
            reference,
        )
        return Response(payload, media_type=guess_type(reference)[0] or "application/octet-stream")

    @router.get(
        "/api/campaigns/{campaign_id}/composition/{version}/images/{product_id}/{market}/{ratio}"
    )
    async def current_image(
        request: Request,
        campaign_id: CampaignID,
        version: str,
        product_id: str,
        market: str,
        ratio: str,
    ):
        from campaign_studio.compositions import preview_image

        payload = await asyncio.to_thread(
            preview_image,
            services(request),
            campaign_id,
            version,
            product_id,
            market,
            ratio.replace("x", ":"),
        )
        return Response(
            payload, media_type="image/png", headers={"Cache-Control": "private, no-cache"}
        )

    @router.get("/api/campaigns/{campaign_id}/composition/{version}/sources/{product_id}")
    async def current_source(
        request: Request,
        campaign_id: CampaignID,
        version: str,
        product_id: str,
        asset: str | None = None,
    ):
        def read_source():
            ws = services(request)
            current = ws.preview_composition(campaign_id, version)
            source = current["sources"].get(product_id)
            if not source:
                raise ActionError("source", "Prepare this source image in Production.", 404)
            reference = ws.storage.project(campaign_id).require(source.reference)
            payload = ws.storage.path(reference).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != source.sha256 or (asset is not None and digest != asset):
                raise ActionError(
                    "preview_source_changed", "The source changed. Reload this image."
                )
            return payload

        payload = await asyncio.to_thread(read_source)
        return Response(
            payload, media_type="image/png" if payload.startswith(b"\x89PNG") else "image/jpeg"
        )

    @router.get(
        "/api/campaigns/{campaign_id}/composition/{version}/thumbnails/{product_id}/{market}/{ratio}"
    )
    async def current_thumbnail(
        request: Request,
        campaign_id: CampaignID,
        version: str,
        product_id: str,
        market: str,
        ratio: str,
        edge: int = 640,
    ):
        from campaign_studio.compositions import preview_image

        if edge not in {160, 640}:
            raise ActionError("thumbnail_size", "Choose a supported thumbnail size.", 422)

        payload = await asyncio.to_thread(
            preview_image,
            services(request),
            campaign_id,
            version,
            product_id,
            market,
            ratio.replace("x", ":"),
            edge,
        )
        return Response(
            payload, media_type="image/png", headers={"Cache-Control": "private, no-cache"}
        )

    @router.get("/api/campaigns/{campaign_id}/composition/{version}/source-thumbnails/{product_id}")
    async def current_source_thumbnail(
        request: Request, campaign_id: CampaignID, version: str, product_id: str
    ):
        def thumbnail():
            ws = services(request)
            current = ws.preview_composition(campaign_id, version)
            source = current["sources"].get(product_id)
            if source is None:
                raise ActionError("source", "Prepare this source image in Production.", 404)
            from campaign_studio.preview_cache import preview_cache

            key = (str(ws.storage.root), campaign_id, "source-thumbnail", source.sha256)
            cached = preview_cache.image(key)
            if cached is not None:
                return cached
            reference = ws.storage.project(campaign_id).require(source.reference)
            verified = ws.storage.path(reference).read_bytes()
            if hashlib.sha256(verified).hexdigest() != source.sha256:
                raise ActionError(
                    "preview_source_changed", "The source image changed. Retry its preview."
                )
            with load_image(io.BytesIO(verified))[0] as image:
                image.thumbnail((640, 640))
                with io.BytesIO() as buffer:
                    save_srgb(image, buffer, compress_level=3)
                    payload = buffer.getvalue()
            preview_cache.keep_image(key, payload)
            return payload

        payload = await asyncio.to_thread(thumbnail)
        return Response(
            payload, media_type="image/png", headers={"Cache-Control": "private, no-cache"}
        )

    @router.get(
        "/api/campaigns/{campaign_id}/composition/{version}/download/{product_id}/{market}/{ratio}"
    )
    async def current_png(
        request: Request,
        campaign_id: CampaignID,
        version: str,
        product_id: str,
        market: str,
        ratio: str,
    ):
        download = await asyncio.to_thread(
            Handoff(services(request).state).current_png,
            campaign_id,
            f"{product_id}/{market}/{ratio}",
            version,
        )
        response = ZipAttachment(download)
        response.media_type = "image/png"
        response.headers["content-type"] = "image/png"
        return response

    @router.post(
        "/api/campaigns/{campaign_id}/composition/{version}/metadata/{product_id}/{market}/{ratio}"
    )
    async def current_metadata(
        request: Request,
        campaign_id: CampaignID,
        version: str,
        product_id: str,
        market: str,
        ratio: str,
        action: MetadataAction,
    ):
        from campaign_studio.compositions import save_metadata

        return await asyncio.to_thread(
            save_metadata,
            services(request),
            campaign_id,
            version,
            f"{product_id}/{market}/{ratio}",
            action,
        )

    @router.get("/uploads/{name}")
    async def uploaded_image(request: Request, name: str):
        return FileResponse(Intake(services(request).storage).uploaded_path(name))

    @router.post("/api/campaigns/{campaign_id}/regeneration/preview")
    async def regeneration_preview(
        request: Request, campaign_id: CampaignID, action: RegenerationReview
    ):
        from campaign_studio.regeneration import review

        require_idle(request)
        return await asyncio.to_thread(review, services(request), campaign_id, action)

    @router.post("/api/campaigns/{campaign_id}/regeneration/status")
    async def regeneration_status(
        request: Request, campaign_id: CampaignID, action: RegenerationConfirm
    ):
        from campaign_studio.regeneration import existing

        prior = await asyncio.to_thread(existing, services(request), campaign_id, action)
        if prior is None:
            return {
                "found": False,
                "active": False,
                "message": (
                    "No confirmed request is recorded yet. This check sent no image request."
                ),
            }
        return {**status(request, campaign_id, prior["id"]), "found": True}

    @router.post("/api/campaigns/{campaign_id}/regeneration/start")
    async def regeneration_start(
        request: Request, campaign_id: CampaignID, action: RegenerationConfirm
    ):
        from campaign_studio.regeneration import existing, prepare

        workspace = services(request)
        prior = await asyncio.to_thread(existing, workspace, campaign_id, action)
        if prior and (prior["state"] not in {"planned", "confirmed"} or busy(request)):
            return JSONResponse(
                {**status(request, campaign_id), "disposition": "already_confirmed"},
                status_code=202,
            )
        require_idle(request)
        require_font()
        if not request.app.state.key_available():
            raise ActionError(MISSING_KEY.code, MISSING_KEY.message)
        run = await asyncio.to_thread(prepare, workspace, campaign_id, action)
        if run["state"] not in {"planned", "confirmed"} or busy(request):
            return JSONResponse(
                {**status(request, campaign_id), "disposition": "already_confirmed"},
                status_code=202,
            )
        plan = workspace.state.plan(run["id"], current=False)
        try:
            await asyncio.to_thread(workspace.storage.require_capacity, production_bytes(plan, 1))
            workspace.state.confirm(run["id"], plan.digest)
            provider = request.app.state.provider_factory()
        except Exception:
            from campaign_studio.regeneration import not_started

            await asyncio.to_thread(not_started, workspace.state, run["id"])
            raise
        launch(request, run["id"], lambda: request.app.state.runner.execute(run["id"], provider))
        return JSONResponse(
            {**status(request, campaign_id), "disposition": "started"}, status_code=202
        )

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
        if plan.replacement:
            raise ActionError(
                "regeneration_review",
                "Review a fresh regeneration confirmation before another image request.",
            )
        if plan.source_blocked:
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

    @router.post("/api/campaigns/{campaign_id}/scene/preview")
    async def scene_preview(request: Request, campaign_id: CampaignID, edit: SceneEdit):
        require_idle(request)
        return await asyncio.to_thread(
            render_scene, request.app.state.previews, services(request), campaign_id, edit
        )

    @router.post("/api/campaigns/{campaign_id}/scene/save")
    async def scene_save(request: Request, campaign_id: CampaignID, edit: SceneEdit):
        from campaign_studio.compositions import acknowledgement

        ws = services(request)
        await asyncio.to_thread(ws.save_composition, campaign_id, edit)
        return await asyncio.to_thread(acknowledgement, ws, campaign_id)

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
            "composition_issues": impact["composition_issues"],
            "blocked": bool(blocking),
            "findings": [finding.model_dump() for finding in blocking],
        }

    @router.post("/api/campaigns/{campaign_id}/text/save")
    @router.post("/api/campaigns/{campaign_id}/text/apply")
    async def text_apply(request: Request, campaign_id: CampaignID, edit: TextEdit):
        from campaign_studio.compositions import acknowledgement

        ws = services(request)
        await asyncio.to_thread(ws.save_composition, campaign_id, edit)
        return await asyncio.to_thread(acknowledgement, ws, campaign_id)

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

    @router.post("/api/campaigns/{campaign_id}/framing/save")
    @router.post("/api/campaigns/{campaign_id}/framing/apply")
    async def frame_apply(request: Request, campaign_id: CampaignID, edit: FrameEdit):
        from campaign_studio.compositions import acknowledgement

        ws = services(request)
        await asyncio.to_thread(ws.save_composition, campaign_id, edit)
        return await asyncio.to_thread(acknowledgement, ws, campaign_id)

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

    @router.post("/api/campaigns/{campaign_id}/logo/save")
    @router.post("/api/campaigns/{campaign_id}/logo/apply")
    async def logo_apply(request: Request, campaign_id: CampaignID, edit: LogoEdit):
        from campaign_studio.compositions import acknowledgement

        ws = services(request)
        await asyncio.to_thread(ws.save_composition, campaign_id, edit)
        return await asyncio.to_thread(acknowledgement, ws, campaign_id)

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

    @router.post(
        "/api/campaigns/{campaign_id}/runs/{run_id}/revisions/{revision_id}/report-preview"
    )
    async def create_report_preview(
        request: Request,
        campaign_id: CampaignID,
        run_id: str,
        revision_id: str,
        action: PackageAction,
    ):
        target = RevisionTarget(
            campaign_id=campaign_id, run_id=run_id, revision_id=revision_id, mode="current"
        )
        return await asyncio.to_thread(
            after_previews,
            request,
            Handoff(services(request).state).create_preview,
            target,
            action,
        )

    def image_response(
        request: Request,
        campaign_id: CampaignID,
        revision_id: str,
        product_id: str,
        market: str,
        ratio: str,
        download: bool = False,
    ):
        workspace = services(request)
        thumbnail_hash = None
        if revision_id.startswith("current-"):
            if download:
                raise ActionError("image", "Open the current image before downloading.", 404)
            current = workspace.state.current_output(campaign_id)
            if current is None:
                raise ActionError("image", "This campaign has no saved images.", 404)
            thumbnail_hash = revision_id.removeprefix("current-")
            revision_id = current[1]
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
                require_export_rules(workspace.state, campaign_id, revision_id)
            with workspace.storage.connect() as db:
                row = db.execute(
                    "SELECT r.run_id,r.images_retired,r.image_count,r.id=runs.current_revision "
                    "FROM revisions r JOIN runs ON runs.id=r.run_id "
                    "WHERE r.id=? AND runs.campaign_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM cleanup_intents c WHERE c.campaign_id=runs.campaign_id "
                    "AND c.kind='campaign')",
                    (revision_id, campaign_id),
                ).fetchone()
            if row and row[1]:
                raise ActionError(
                    "retired_revision",
                    "This image is no longer available. "
                    "Open the current images or an earlier exported package.",
                    410,
                )
            if row is None or not (row[2] or row[3]):
                raise ActionError("image", "This saved image is unavailable.", 404)
            manifest = workspace.manifest(workspace.state.run(row[0]), revision_id)
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
            if item is None or (thumbnail_hash is not None and item["sha256"] != thumbnail_hash):
                raise ActionError(
                    "missing_image",
                    "This output is unavailable. Open Studio to inspect the current images.",
                    404,
                )
            prefix = workspace.state.revision(revision_id)["manifest_ref"].rsplit("/", 1)[0]
            try:
                payload = workspace.storage.path(f"{prefix}/{item['file']}").read_bytes()
            except OSError:
                payload = None
            if payload is None or hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise ActionError(
                    "changed_image",
                    "This saved image is missing or changed. Use local recovery in Production.",
                    409,
                )
            if download:
                public = public_manifest(workspace.state, revision_id)
                filename = " - ".join(delivery_paths(public)[item["id"]].split("/")[1:])
                return Response(
                    payload,
                    media_type="image/png",
                    headers={
                        "Content-Disposition": attachment_header(filename),
                    },
                )
            return Response(payload, media_type="image/png")

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
        args = (request, campaign_id, revision_id, product_id, market, ratio, download)
        if download:
            return await asyncio.to_thread(after_previews, request, image_response, *args)
        return image_response(*args)

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
            try:
                current = workspace.preview_composition(campaign_id)
            except ValidationError as error:
                raise ActionError(
                    "missing_source", "This product has no available source preview.", 404
                ) from error
            plan = current["plan"]
            asset = current["sources"].get(product_id)
            reference = asset.reference if asset else None
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

    @router.get(
        "/reports/preview/{campaign_id}/{run_id}/{revision_id}/"
        "{partial}/{created}/{fingerprint}/{reference:path}"
    )
    async def preview_file(
        request: Request,
        campaign_id: CampaignID,
        run_id: str,
        revision_id: str,
        partial: int,
        created: int,
        fingerprint: str,
        reference: str,
    ):
        if partial not in {0, 1} or not 0 <= created <= 253402300799:
            raise StateError("This report address is invalid.", code="report_not_found")
        target = RevisionTarget(
            campaign_id=campaign_id, run_id=run_id, revision_id=revision_id, mode="current"
        )
        payload = await asyncio.to_thread(
            after_previews,
            request,
            Handoff(services(request).state).preview_file,
            target,
            bool(partial),
            created,
            fingerprint,
            reference,
        )
        return Response(payload, media_type=guess_type(reference)[0] or "application/octet-stream")

    @router.get("/reports/{report_id}/{reference:path}")
    async def report_file(request: Request, report_id: str, reference: str):
        payload = await asyncio.to_thread(
            after_previews,
            request,
            Handoff(services(request).state).report_file,
            report_id,
            reference,
        )
        return Response(payload, media_type=guess_type(reference)[0] or "application/octet-stream")

    return router
