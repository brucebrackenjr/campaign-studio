"""Motif presentation boundary over the canonical campaign services."""

import asyncio
import copy
import hashlib
import json
import sqlite3
from contextlib import suppress
from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from PIL import Image
from pydantic import Field, ValidationError

from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brand_updates import BrandUpdate, BrandUpdates
from campaign_studio.brief import Brief, CampaignID, Contract
from campaign_studio.brief_preview import BriefPreview
from campaign_studio.cleanup import Cleanup, require_campaign_available
from campaign_studio.costing import estimate_requests
from campaign_studio.exporting import logo_export_issue, wording_export_issue
from campaign_studio.imaging import FontProblem, require_font
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.planning import canonical_hash
from campaign_studio.rendering import blocks_output
from campaign_studio.review import RevisionTarget
from campaign_studio.state import StateError
from campaign_studio.storage import StorageError
from campaign_studio.text_limits import LIMITS, browser_contract
from campaign_studio.validation import field_issue
from campaign_studio.workspace import ActionError, image_url, provider_notice


@lru_cache(maxsize=512)
def _display_digest(path, signature):
    """Avoid rereading unchanged image bytes on every Workbench refresh.

    This cache serves display facts only. Export and download still validate their
    own bytes. File identity, size, and both modification times invalidate it.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_output_intact(storage, prefix, output):
    if not output["file"]:
        return False
    try:
        path = storage.path(f"{prefix}/{output['file']}")
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return _display_digest(path, signature) == output["sha256"]
    except OSError:
        return False


def workbench_outputs(workspace, campaign_id):
    """Summarize the current outputs opening this card will actually display."""
    current = workspace.state.current_output(campaign_id)
    if current is None:
        return None, [], 0
    selected = workspace.state.revision(current[1])
    manifest = workspace.manifest(workspace.state.run(selected["run_id"]), selected["id"])
    prefix = selected["manifest_ref"].rsplit("/", 1)[0]
    available = [
        item
        for item in manifest["outputs"]
        if display_output_intact(workspace.storage, prefix, item)
    ]
    damaged = sum(bool(item["file"]) for item in manifest["outputs"]) - len(available)
    return manifest, available, damaged


class NewCampaign(Contract):
    name: str = Field(min_length=1, max_length=LIMITS["campaign_name"])
    brand: str = Field(default="", max_length=LIMITS["brand_name"])
    audience: str = Field(default="", max_length=LIMITS["audience"])


class RuleRecheck(BriefDraft):
    run_id: str


def policy(value: dict, cap: int) -> None:
    products = value.get("products")
    if not isinstance(products, list) or not 1 <= len(products) <= cap:
        message = f"Use between 1 and {cap} products for a new campaign plan."
        raise ActionError(
            "product_limit",
            message,
            422,
            field_errors=[field_issue(["products"], "product_count", message)],
        )
    markets = value.get("markets")
    if not isinstance(markets, dict) or not markets or not set(markets) <= {"en-US", "es-MX"}:
        message = "Supply English (United States), Spanish (Mexico), or both for this campaign."
        raise ActionError(
            "markets", message, 422, field_errors=[field_issue(["markets"], "markets", message)]
        )


def document_for(workspace, campaign_id):
    intake = Intake(workspace.storage)
    run = workspace.run_for(campaign_id)
    document = intake.current_document(campaign_id, workspace)
    if document is None:
        if not run:
            raise ActionError(
                "campaign_data",
                "This campaign’s saved data is unavailable. "
                "Restore it before editing the campaign again.",
            )
        document = {
            "value": workspace.state.plan(run["id"]).brief.model_dump(mode="json"),
            "version": None,
        }
    return document, run


def workbench_card(ws, entry):
    document, run = document_for(ws, entry["id"])
    brief = document.get("authored", {}).get("brief", document["value"])
    products = brief.get("products")
    products = [p for p in products if isinstance(p, dict)] if isinstance(products, list) else []
    markets = brief.get("markets") if isinstance(brief.get("markets"), dict) else {}
    brand = brief.get("brand") if isinstance(brief.get("brand"), dict) else {}
    thumbnail = None
    first_product = products[0] if products else None
    source = first_product.get("source") if first_product else None
    source = source if isinstance(source, dict) else {}
    attempts = ws.state.attempts(run["id"]) if run else []
    manifest, outputs, damaged = workbench_outputs(ws, entry["id"]) if run else (None, [], 0)
    if manifest:
        available = next(
            (
                item
                for item in outputs
                if first_product and item["product_id"] == first_product.get("id")
            ),
            None,
        )
        if available:
            thumbnail = image_url(entry["id"], manifest["revision_id"], available)
    if thumbnail is None and first_product:
        reference = source.get("path", "")
        if isinstance(reference, str) and "/Assets/Products/" in reference:
            thumbnail = "/uploads/" + reference.rsplit("/", 1)[-1]
    thumbnail_expected = False
    if first_product:
        product_id = first_product.get("id")
        thumbnail_expected = bool(
            thumbnail
            or source.get("path")
            or any(
                item["product_id"] == product_id and item["file"]
                for item in (manifest or {}).get("outputs", [])
            )
            or any(item["product_id"] == product_id and item["source_sha256"] for item in attempts)
            or (run and f"masters/{product_id}" in ws.state.plan(run["id"]).assets)
        )
    status = run["state"] if run else "draft"
    # Later edits invalidate readiness, not already produced results or active work.
    if status in {"planned", "confirmed"} and _unplanned_draft(
        document, run, ws.state.current_output(entry["id"])
    ):
        status = "draft"
    return {
        **entry,
        "status": "damaged" if damaged else status,
        "outcome_unknown": bool(
            run and any(a["state"] in {"unknown", "dispatching"} for a in attempts)
        ),
        "planned_images": len(products) * len(markets) * 3,
        "available_images": len(outputs),
        "saved_expected_images": len(manifest["outputs"]) if manifest else 0,
        "damaged_images": damaged,
        "thumbnail": thumbnail,
        "thumbnail_label": (
            f"{first_product.get('name') or 'Product'} · source image"
            if thumbnail and thumbnail.startswith("/uploads/")
            else f"{brand.get('name') or 'Campaign'} campaign artwork"
        ),
        "thumbnail_expected": thumbnail_expected,
        "output_expected": any(item.get("file") for item in (manifest or {}).get("outputs", [])),
        "name": brief.get("name") or "Untitled campaign",
        "brand": brand.get("name") or "",
        "products": len(products),
        "markets": len(markets),
        "updated": ws.state.saved_activity(entry["id"], document.get("saved_at")),
        "revision": (ws.state.revision_number(manifest["revision_id"]) if manifest else 0),
    }


def attempt_details(workspace, attempts):
    """Expose saved receipt facts and numeric usage, never private response content."""
    result = []
    for attempt in attempts:
        value = {key: attempt[key] for key in ("product_id", "state", "created_at", "error_kind")}
        value["source_available"] = workspace.state.source_is_intact(attempt)
        try:
            value["response_retained"] = workspace.storage.path(attempt["response_ref"]).is_file()
        except OSError, StorageError:
            value["response_retained"] = False
        # Retained bytes are not reconstructable imagery after a known decode failure.
        value["response_recoverable"] = (
            value["response_retained"]
            and attempt["state"] in {"received", "decoded"}
            and attempt["error_kind"] != "saved_response_decode"
        )
        try:
            usage = json.loads(attempt["usage_json"] or "null")
        except TypeError, ValueError:
            usage = None
        value["usage"] = {
            key: usage[key]
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0
        }
        result.append(value)
    return result


def local_readiness(workspace, key_available):
    """A bounded local check; no campaign mutation or provider construction."""
    rows = []
    try:
        require_font()
        rows.append(
            {
                "id": "font",
                "label": "Bundled Noto Sans",
                "ready": True,
                "message": "Available for local composition",
            }
        )
    except FontProblem as error:
        rows.append(
            {"id": "font", "label": "Bundled Noto Sans", "ready": False, "message": str(error)}
        )
    reference = f"private/readiness-{uuid4().hex}.tmp"
    try:
        with workspace.storage.connect() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("Database integrity failed")
        workspace.storage.write(reference, b"Campaign Studio local readiness\n")
        workspace.storage.path(reference).unlink()
        rows.append(
            {
                "id": "storage",
                "label": "Local campaign storage",
                "ready": True,
                "message": "Database readable; local output write verified",
            }
        )
    except OSError, StorageError, sqlite3.Error:
        rows.append(
            {
                "id": "storage",
                "label": "Local campaign storage",
                "ready": False,
                "message": "Check free space and write permissions for the configured "
                "CAMPAIGN_STUDIO_DATA_DIR, then check readiness again. "
                "Do not replace or delete existing campaign data.",
            }
        )
    finally:
        with suppress(OSError, StorageError):
            workspace.storage.path(reference).unlink(missing_ok=True)
    try:
        inventory = workspace.storage.summary()
    except OSError, StorageError:
        inventory = None
        rows.append(
            {
                "id": "inventory",
                "label": "Workspace size",
                "ready": False,
                "message": "Workspace usage could not be measured completely. "
                "Check storage permissions before creating new files.",
            }
        )
    return {
        "storage": inventory,
        "checks": rows,
        "ready": all(row["ready"] for row in rows),
        "key_available": bool(key_available),
        "provider_checked": False,
    }


def _unplanned_draft(document, run, output=None):
    """Display the same strict authored-plan freshness on Home and the campaign."""
    base = document.get("authored_base", {})
    images_changed = bool(
        output
        and output[0] != run["id"]
        and output != (base.get("run_id"), base.get("revision_id"))
    )
    return (
        images_changed
        or bool(document.get("base_run") and document.get("planned_run") != run["id"])
        or bool(
            document.get("authored")
            and canonical_hash(document["authored"]["brief"])
            != document.get("planned_authored_hash", canonical_hash(document["value"]))
        )
    )


def campaign_data(workspace, campaign_id):
    document, run = document_for(workspace, campaign_id)
    value = copy.deepcopy(document["value"])
    result = {
        "value": value,
        "version": document["version"],
        "attachment_hints": document.get("attachment_hints", {}),
        "base_revision": workspace.state.effective_revision(run["id"]) if run else None,
        "run": None,
        "plan": None,
        "manifest": None,
        "concept_previews": {},
        "campaign_metrics": workspace.state.campaign_metrics(campaign_id),
    }
    result["project"] = AuthoredProjects(workspace).read(campaign_id)
    result["product_images"] = {}
    for product in result["project"]["authored"]["brief"].get("products", []):
        if isinstance(product, dict) and isinstance(product.get("id"), str):
            photo = workspace.retained_product_source(campaign_id, product["id"])
            if photo:
                result["product_images"][product["id"]] = {
                    "source": photo,
                    "url": f"/campaigns/{campaign_id}/sources/{product['id']}/1x1?retained=true",
                }
    result["image_cleanup"] = Cleanup(workspace.state).status(campaign_id)
    if run:
        plan = workspace.state.plan(run["id"])
        for product in plan.brief.products:
            if product.source.role == "concept" and workspace.source_preview_reference(
                campaign_id, product.id
            ):
                result["concept_previews"][product.id] = (
                    f"/campaigns/{campaign_id}/sources/{product.id}/1x1"
                )
        notice = provider_notice(workspace.state.attempts(run["id"]))
        result["notice"] = {"message": notice.message, "code": notice.code} if notice else None
        result["plan_brief"] = plan.brief.model_dump(mode="json")
        result["unplanned_draft"] = _unplanned_draft(
            document, run, workspace.state.current_output(campaign_id)
        )
        result["run"] = {
            key: run[key] for key in ("id", "state", "allowance", "current_revision", "created_at")
        }
        result["plan"] = {
            "digest": plan.digest,
            "outputs": len(plan.outputs),
            "generation_products": plan.generation_products,
            "generation_requests": {k: v.model_dump() for k, v in plan.generation_requests.items()},
            "findings": [f.model_dump() for f in plan.findings],
            "transmitted_data": plan.transmitted_data,
            "cost_status": plan.cost_status,
            "cost_estimate": plan.cost_estimate,
        }
        attempts = workspace.state.attempts(run["id"])
        result["run_attempts"] = attempt_details(workspace, attempts)
        pending = workspace.state.pending_generation(run["id"], plan)
        result["retry_plan"] = None
        if (
            run["state"] in {"partial", "paused", "stopped"}
            and pending
            and not result["unplanned_draft"]
        ):
            requests = {key: plan.generation_requests[key] for key in pending}
            result["retry_plan"] = {
                "run_id": run["id"],
                "digest": plan.digest,
                "generation_products": pending,
                "previous_attempts": len(attempts),
                "allowance": max(run["allowance"], len(attempts) + len(pending)),
                "cost_estimate": estimate_requests(requests),
            }
        current_output = workspace.state.current_output(campaign_id)
        output_run = workspace.state.run(current_output[0]) if current_output else None
        manifest = workspace.manifest(output_run, current_output[1]) if current_output else None
        if manifest:
            record = workspace.state.revision(current_output[1])
            output_plan = workspace.state.revision_plan(record)
            output_brief = output_plan.brief.model_dump(mode="json")
            result["source_sizes"] = {}
            attempts = workspace.state.attempts(output_run["id"])
            for product in output_plan.brief.products:
                asset = (
                    output_plan.assets.get(f"products/{product.id}")
                    if product.source.role == "finished_scene"
                    else output_plan.assets.get(f"masters/{product.id}")
                )
                if asset:
                    result["source_sizes"][product.id] = [asset.width, asset.height]
                else:
                    attempt = next(
                        (
                            a
                            for a in attempts
                            if a["product_id"] == product.id
                            and a["state"] == "decoded"
                            and workspace.state.source_is_intact(a)
                        ),
                        None,
                    )
                    if attempt:
                        with Image.open(workspace.storage.path(attempt["source_ref"])) as image:
                            result["source_sizes"][product.id] = list(image.size)
            result["output_brief"] = output_brief
            result["output_run_id"] = output_run["id"]
            result["revision_number"] = workspace.state.revision_number(current_output[1])
            result["metadata"] = workspace.state.metadata_snapshot(current_output[1])
            result["attempts"] = attempt_details(
                workspace, workspace.state.attempts(output_run["id"])
            )
            result["output_plan"] = {
                "generation_requests": {
                    k: v.model_dump() for k, v in output_plan.generation_requests.items()
                },
                "transmitted_data": output_plan.transmitted_data,
                "allowance": output_run["allowance"],
            }
            prefix = record["manifest_ref"].rsplit("/", 1)[0]
            damaged = []
            for item in manifest["outputs"]:
                item["url"] = image_url(campaign_id, manifest["revision_id"], item)
                if item["file"]:
                    try:
                        payload = workspace.storage.path(f"{prefix}/{item['file']}").read_bytes()
                        intact = hashlib.sha256(payload).hexdigest() == item["sha256"]
                    except OSError:
                        intact = False
                    if not intact:
                        damaged.append(item["id"])
                        item["url"] = None
                        item["omission"] = "Saved image missing or changed. Use local recovery."
            manifest["metrics"]["completed_images"] = sum(
                bool(item["url"]) for item in manifest["outputs"]
            )
            # A missing saved file is damage, not an intentional production omission.
            # Keep the immutable completeness contract and expose integrity separately.
            result["file_integrity"] = {"damaged_outputs": damaged}
            result["can_describe"] = run["state"] != "running"
            authored_products = {
                p.get("id")
                for p in result["project"]["authored"]["brief"].get("products", [])
                if isinstance(p, dict)
            }
            result["editable_products"] = {
                p.id: p.id in authored_products
                and workspace.state.product_inputs_ready(output_run["id"], output_plan, p.id)
                for p in output_plan.brief.products
            }
            result["can_edit"] = result["can_describe"] and any(
                result["editable_products"].values()
            )
            result["logo_editable_products"] = {
                p.id: result["can_describe"]
                and p.id in authored_products
                and workspace.state.product_inputs_ready(
                    output_run["id"], output_plan, p.id, include_logo=False
                )
                for p in output_plan.brief.products
            }
            result["manifest"] = manifest
            # Only omissions under the displayed decision count as rule withholding.
            # Unrelated blocking findings and a newer unstarted plan are not evidence.
            missing_ids = {item["id"] for item in manifest["outputs"] if not item.get("url")}
            result["rule_withheld_outputs"] = [
                output.id
                for output in output_plan.outputs
                if output.id in missing_ids
                and any(
                    finding.context and finding.context.phrase and blocks_output(finding, output)
                    for finding in output_plan.findings
                )
            ]
        available = {
            item["id"]
            for item in (result.get("manifest") or {}).get("outputs", [])
            if result.get("output_run_id") == run["id"] and item.get("url")
        }
        missing = [
            o
            for o in plan.outputs
            if o.id not in available and not any(blocks_output(f, o) for f in plan.findings)
        ]
        received = {
            a["product_id"]
            for a in result["run_attempts"]
            if a["source_available"] or a["response_recoverable"]
        }
        ready = {
            p.id
            for p in plan.brief.products
            if workspace.state.product_inputs_ready(run["id"], plan, p.id)
        }
        result["local_recovery"] = {
            "output_ids": [o.id for o in missing if o.product_id in ready | received],
            "saved_result": any(o.product_id in received for o in missing),
        }
    result["wording_issue"] = wording_export_issue(workspace.state, campaign_id)
    result["logo_issue"] = logo_export_issue(workspace.state, campaign_id)
    return result


def routes(templates, settings):
    router = APIRouter()

    def workspace(request):
        return request.app.state.workspace

    def idle(request):
        if any(not task.done() for task in request.app.state.operations.values()):
            raise ActionError(
                "busy", "An image operation is active. Follow its progress before saving."
            )

    @router.post("/api/motif/readiness")
    async def readiness(request: Request):
        return await asyncio.to_thread(
            local_readiness, workspace(request), request.app.state.key_available()
        )

    @router.get("/campaign")
    @router.get("/production")
    @router.get("/studio")
    @router.get("/handoff")
    async def campaignless_page():
        # Page navigation needs an explicit campaign, selected from Home.
        return RedirectResponse("/", status_code=303)

    @router.get("/favicon.ico", status_code=204)
    async def favicon():
        return Response(status_code=204)

    @router.get("/", response_class=HTMLResponse)
    @router.get("/campaigns/{campaign_id}/campaign", response_class=HTMLResponse)
    @router.get("/campaigns/{campaign_id}/production", response_class=HTMLResponse)
    @router.get("/campaigns/{campaign_id}/studio", response_class=HTMLResponse)
    @router.get("/campaigns/{campaign_id}/handoff", response_class=HTMLResponse)
    async def shell(
        request: Request, campaign_id: CampaignID | None = None, page: str = "campaign"
    ):
        page = request.url.path.rsplit("/", 1)[-1] if campaign_id else "campaign"
        if page not in {"campaign", "production", "studio", "handoff"}:
            raise ActionError("page", "Choose Campaign, Production, Studio, or Handoff.", 404)
        if campaign_id:
            if Cleanup(workspace(request).state).status(campaign_id)["campaign_state"] == "pending":
                # Saved URLs can outlive a confirmed deletion. Home exposes the
                # existing exact-intent retry; data routes remain unavailable.
                return RedirectResponse("/", status_code=303)
            document_for(workspace(request), campaign_id)
        return templates.TemplateResponse(
            request=request,
            name="motif.html",
            context={
                "csrf": request.app.state.csrf,
                "text_limits": browser_contract(),
                "campaign_id": campaign_id,
                "page": {"studio": "review"}.get(page, page) if campaign_id else "library",
                "cap": settings.product_limit,
            },
        )

    @router.get("/api/motif/workspace")
    async def catalog(request: Request):
        ws = workspace(request)
        entries = []
        for entry in ws.catalog():
            try:
                entries.append(workbench_card(ws, entry))
            except (
                ActionError,
                StateError,
                StorageError,
                ValidationError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
            ):
                # A deletion can commit after the catalog identities were read.
                # Skip that retired entry; keep genuine per-campaign damage visible.
                try:
                    require_campaign_available(ws.storage, entry["id"])
                except StateError:
                    continue
                entries.append(
                    {
                        **entry,
                        "status": "damaged",
                        "products": 0,
                        "markets": 0,
                        "updated": None,
                        "thumbnail": None,
                        "recovery_message": "This campaign’s saved files could not be read. "
                        "Restore its files, or delete the campaign from the Workbench.",
                    }
                )
        from campaign_studio.bundled import setup_notice

        return {
            "setup_notice": setup_notice(ws),
            "campaigns": entries,
            "pending_cleanup": Cleanup(ws.state).pending(),
            "product_limit": settings.product_limit,
            "key_available": request.app.state.key_available(),
        }

    @router.get("/api/motif/campaigns/{campaign_id}")
    async def campaign(request: Request, campaign_id: CampaignID):
        return await asyncio.to_thread(campaign_data, workspace(request), campaign_id)

    @router.get(
        "/campaigns/{campaign_id}/runs/{run_id}/revisions/{revision_id}/sources/{product_id}"
    )
    async def selected_source(
        request: Request, campaign_id: CampaignID, run_id: str, revision_id: str, product_id: str
    ):
        ws = workspace(request)
        target = RevisionTarget(
            campaign_id=campaign_id, run_id=run_id, revision_id=revision_id, mode="saved_version"
        )
        ws.state.metadata_snapshot(revision_id, target=target)
        revision = ws.state.revision(revision_id)
        plan = ws.state.revision_plan(revision)
        product = next((p for p in plan.brief.products if p.id == product_id), None)
        if product is None:
            raise ActionError("source", "This source is unavailable in the selected version.", 404)
        asset = (
            plan.assets.get(f"products/{product.id}")
            if product.source.role != "concept"
            else plan.assets.get(f"masters/{product.id}")
        )
        if asset:
            reference, expected = asset.reference, asset.sha256
        else:
            attempt = next(
                (
                    a
                    for a in ws.state.attempts(run_id)
                    if a["product_id"] == product.id
                    and a["state"] == "decoded"
                    and ws.state.source_is_intact(a)
                ),
                None,
            )
            if not attempt:
                raise ActionError(
                    "source", "This saved source is unavailable. Recover local work first.", 404
                )
            reference, expected = attempt["source_ref"], attempt["source_sha256"]
        try:
            payload = ws.storage.path(reference).read_bytes()
        except OSError:
            raise ActionError("source", "This saved source is unavailable.", 404) from None
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ActionError(
                "source", "This saved source changed. Restore it before using it.", 409
            )
        return Response(
            payload, media_type="image/png" if payload.startswith(b"\x89PNG") else "image/jpeg"
        )

    @router.post("/api/motif/campaigns")
    async def create(request: Request, action: NewCampaign):
        idle(request)
        if not action.name.strip():
            raise ActionError("name", "Name this campaign before creating it.", 422)
        intake = Intake(workspace(request).storage)
        with intake.storage.exclusive_run():
            campaign_id, _ = intake.new(
                name=action.name, audience=action.audience, brand=action.brand
            )
        return {"campaign_id": campaign_id}

    @router.post("/api/motif/import")
    async def import_campaign(request: Request):
        idle(request)
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ActionError("import", "Choose a JSON brief containing one campaign.", 422)
        intake = Intake(workspace(request).storage)
        campaign_id = intake.new_campaign_id()
        value = intake.import_value(campaign_id, raw)
        policy(value, settings.product_limit)
        with intake.storage.exclusive_run():
            intake.create(
                campaign_id,
                value,
                attachment_hints=intake.attachment_hints(raw, value),
                imported_bytes=await request.body(),
            )
        return {"campaign_id": campaign_id}

    @router.get("/api/motif/campaigns/{campaign_id}/authored")
    async def authored(request: Request, campaign_id: CampaignID):
        return await asyncio.to_thread(AuthoredProjects(workspace(request)).read, campaign_id)

    @router.post("/api/motif/campaigns/{campaign_id}/authored")
    async def save_authored(request: Request, campaign_id: CampaignID, action: AuthoredSave):
        return await asyncio.to_thread(
            AuthoredProjects(workspace(request)).save, campaign_id, action
        )

    @router.post("/api/motif/campaigns/{campaign_id}/brand/apply")
    async def apply_brand(request: Request, campaign_id: CampaignID, action: BrandUpdate):
        idle(request)
        ws = workspace(request)

        def apply():
            # A retired editor preview may still be finishing. Match the existing
            # delivery boundary's lock order before entering local publication.
            with request.app.state.previews.lock:
                brief, plan, impact = BrandUpdates(ws).prepare(campaign_id, action)
                request.app.state.runner.revise(
                    action.run_id,
                    brief,
                    action.revision_id,
                    plan.digest,
                    brand_edit=action,
                )
                result = campaign_data(ws, campaign_id)
                result["brand_update_impact"] = impact
                return result

        return await asyncio.to_thread(apply)

    @router.post("/api/motif/campaigns/{campaign_id}/plan")
    async def plan(request: Request, campaign_id: CampaignID):
        idle(request)
        ws = workspace(request)
        document, previous_run = document_for(ws, campaign_id)
        policy(document.get("authored", {}).get("brief", document["value"]), settings.product_limit)
        body = await request.json()
        if not isinstance(body, dict):
            raise ActionError("plan", "Save the brief before reviewing its plan.", 422)
        intake = Intake(ws.storage)
        # The request's raw source is captured before preparation updates its
        # owned attachment paths. This reports actual fitting, not a new policy.
        before = document.get("authored", {}).get("brief", document["value"])
        previous_logo = (
            ws.state.plan(previous_run["id"]).brief.brand.logo_path if previous_run else None
        )
        prepared = await asyncio.to_thread(intake.prepare, campaign_id, body.get("version"), ws)
        result = await asyncio.to_thread(campaign_data, ws, campaign_id)
        try:
            _, fitted = await asyncio.to_thread(
                intake.fit_replacement_logo,
                Brief.model_validate(before),
                previous_logo,
            )
        except ValidationError:
            fitted = []
        # A fitted rectangle is disclosed only if it is the one in this actual plan.
        current = {output.id: output.framing.logo_placement for output in prepared.outputs}
        result["fitted_outputs"] = [
            item
            for item in fitted
            if current.get(item["id"]) is not None
            and current[item["id"]].model_dump() == item["placement"]
        ]
        return result

    @router.post("/api/motif/campaigns/{campaign_id}/brief-preview")
    async def brief_preview(request: Request, campaign_id: CampaignID, action: BriefPreview):
        return await asyncio.to_thread(request.app.state.previews.render_brief, campaign_id, action)

    @router.get("/api/motif/campaigns/{campaign_id}/brief-preview/{token}.png")
    async def brief_preview_image(request: Request, campaign_id: CampaignID, token: str):
        payload = await asyncio.to_thread(
            request.app.state.previews.brief_image, campaign_id, token
        )
        if payload is None:
            raise ActionError("expired_preview", "Refresh the current Brief preview.", 404)
        return Response(payload, media_type="image/png")

    @router.post("/api/motif/campaigns/{campaign_id}/recheck")
    async def recheck(request: Request, campaign_id: CampaignID, action: RuleRecheck):
        idle(request)
        ws = workspace(request)
        await asyncio.to_thread(
            Intake(ws.storage).recheck_rules, campaign_id, action, action.run_id, ws
        )
        return await asyncio.to_thread(campaign_data, ws, campaign_id)

    return router
