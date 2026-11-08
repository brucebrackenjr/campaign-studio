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
from campaign_studio.brand_updates import BrandUpdate
from campaign_studio.brief import Brief, CampaignID, Contract
from campaign_studio.brief_preview import BriefPreview
from campaign_studio.cleanup import Cleanup, require_campaign_available
from campaign_studio.costing import estimate_requests
from campaign_studio.imaging import FontProblem, require_font
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.planning import canonical_hash
from campaign_studio.rendering import blocks_output
from campaign_studio.review import RevisionTarget
from campaign_studio.state import StateError
from campaign_studio.storage import StorageError
from campaign_studio.text_limits import LIMITS, browser_contract
from campaign_studio.validation import field_issue
from campaign_studio.workspace import ActionError, provider_notice


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
    try:
        data = campaign_data(workspace, campaign_id)
        manifest = data["manifest"]
        return manifest, [o for o in (manifest or {}).get("outputs", []) if o.get("url")], 0
    except ValueError, OSError:
        return None, [], 0


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
    manifest, outputs, damaged = workbench_outputs(ws, entry["id"])
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
            thumbnail = available["url"]
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
        "revision": 0,
    }


def attempt_details(workspace, attempts, *, verify_bytes=True):
    """Expose saved receipt facts and numeric usage, never private response content."""
    result = []
    for attempt in attempts:
        value = {key: attempt[key] for key in ("product_id", "state", "created_at", "error_kind")}
        if verify_bytes:
            value["source_available"] = workspace.state.source_is_intact(attempt)
            try:
                value["response_retained"] = workspace.storage.path(
                    attempt["response_ref"]
                ).is_file()
            except OSError, StorageError:
                value["response_retained"] = False
        else:
            value["source_available"] = bool(attempt["source_sha256"])
            value["response_retained"] = attempt["state"] in {"received", "decoded"}
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
        workspace.storage.unlink(workspace.storage.path(reference))
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
            workspace.storage.unlink(workspace.storage.path(reference), missing_ok=True)
    try:
        inventory = workspace.storage.refresh_edit_capacity()
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


def _retained_size(workspace, reference):
    with Image.open(workspace.storage.path(reference)) as image:
        return list(image.size)


def campaign_data(workspace, campaign_id, *, verify_saved_files=True):
    from campaign_studio.compositions import imagery_key
    from campaign_studio.review import initial_metadata, public_metadata
    from campaign_studio.validation import validation_issues

    document, run = document_for(workspace, campaign_id)
    project = AuthoredProjects(workspace).read(campaign_id)
    value = copy.deepcopy(project["authored"]["brief"])
    version = document["version"]
    result = {
        "value": value,
        "version": version,
        "composition_version": version,
        "attachment_hints": document.get("attachment_hints", {}),
        "base_revision": version,
        "project": project,
        "run": None,
        "plan": None,
        "manifest": None,
        "concept_previews": {},
        "product_images": {},
        "source_sizes": {},
        "source_urls": {},
        "campaign_metrics": workspace.state.campaign_metrics(campaign_id),
        "output_run_id": run["id"] if run else None,
        "output_brief": value,
        "plan_brief": value,
        "can_edit": False,
        "can_describe": True,
        "editable_products": {},
        "logo_editable_products": {},
        "attempts": [],
        "run_attempts": [],
        "notice": None,
        "retry_plan": None,
        "composition_issues": [],
        "unplanned_draft": False,
        "wording_issue": None,
        "logo_issue": None,
        "image_cleanup": Cleanup(workspace.state).status(campaign_id),
        "local_recovery": {"output_ids": [], "saved_result": False},
        "file_integrity": {"damaged_outputs": [], "verified": True},
    }
    for product in value.get("products", []) if isinstance(value.get("products"), list) else []:
        if not isinstance(product, dict) or not isinstance(product.get("id"), str):
            continue
        retained = workspace.retained_product_source(campaign_id, product["id"])
        if retained:
            result["product_images"][product["id"]] = {
                "source": retained,
                "url": f"/campaigns/{campaign_id}/sources/{product['id']}/1x1?retained=true",
            }
    if run:
        result["run"] = {
            key: run[key] for key in ("id", "state", "allowance", "current_revision", "created_at")
        }
        result["run_attempts"] = attempt_details(workspace, workspace.state.attempts(run["id"]))
        result["attempts"] = result["run_attempts"]
        notice = provider_notice(workspace.state.attempts(run["id"]))
        result["notice"] = {"code": notice.code, "message": notice.message} if notice else None
    try:
        current = workspace.preview_composition(campaign_id, version)
    except ValidationError as error:
        result["composition_issues"] = validation_issues(error)
        return result
    plan, sources = current["plan"], current["sources"]
    result["composition_issues"] = current["composition_issues"]
    result["unplanned_draft"] = bool(result["composition_issues"])
    expected_images = len(value["products"]) * len(plan.brief.markets) * 3
    result["plan"] = {
        "digest": plan.digest,
        "outputs": expected_images,
        "generation_products": plan.generation_products,
        "generation_requests": {k: v.model_dump() for k, v in plan.generation_requests.items()},
        "findings": [f.model_dump() for f in plan.findings],
        "transmitted_data": plan.transmitted_data,
        "cost_status": plan.cost_status,
        "cost_estimate": plan.cost_estimate,
        "changed_source_products": [
            {"id": product.id, "name": product.name}
            for product in plan.brief.products
            if product.id in plan.generation_products
            and (record := document.get("source_assets", {}).get(product.id))
            and record.get("imagery_key") != imagery_key(plan.brief, product)
        ],
    }
    # Readiness belongs to the current products, not the most recent historical
    # request. Removing a missing product can make every remaining source ready;
    # adding a concept after a completed run requires a new reviewed request.
    needs_generation = bool(plan.generation_products)
    if not needs_generation:
        result["notice"] = None
    admitted = bool(
        run
        and run["state"] != "complete"
        and document.get("planned_run") == run["id"]
        and document.get("planned_authored_hash") == canonical_hash(value)
    )
    result["unplanned_draft"] = result["unplanned_draft"] or (needs_generation and not admitted)
    if needs_generation and admitted and not result["unplanned_draft"]:
        production = workspace.state.plan(run["id"], current=False)
        result["plan"].update(
            digest=production.digest,
            generation_products=production.generation_products,
            generation_requests={
                k: v.model_dump() for k, v in production.generation_requests.items()
            },
            cost_estimate=production.cost_estimate,
        )
        result["unplanned_draft"] = document.get("planned_authored_hash") != canonical_hash(value)
        pending = [pid for pid in production.generation_products if pid not in sources]
        if (
            run["state"] in {"partial", "paused", "stopped"}
            and pending
            and not result["unplanned_draft"]
        ):
            requests = {pid: production.generation_requests[pid] for pid in pending}
            result["retry_plan"] = {
                "run_id": run["id"],
                "digest": production.digest,
                "generation_products": pending,
                "previous_attempts": len(result["run_attempts"]),
                "allowance": max(run["allowance"], len(result["run_attempts"]) + len(pending)),
                "cost_estimate": estimate_requests(requests),
            }
    result["output_plan"] = {
        "generation_requests": {k: v.model_dump() for k, v in plan.generation_requests.items()},
        "transmitted_data": plan.transmitted_data,
        "allowance": run["allowance"] if run else 0,
    }
    matches = [
        f for f in plan.findings if f.context and f.context.phrase and f.severity == "blocking"
    ]
    result["wording_issue"] = (
        {
            "code": "prohibited_phrase",
            "message": (
                "Correct the prohibited wording, or edit or remove its rule, before exporting."
            ),
            "matches": [finding.model_dump(mode="json") for finding in matches],
            "pending_rules": False,
            "pending_rule_targets": [],
        }
        if matches
        else None
    )
    logo_findings = [
        f for f in plan.findings if f.rule == "logo_presence" and f.severity == "blocking"
    ]
    result["logo_issue"] = (
        {
            "code": "logo_presence",
            "message": "Attach a logo in Brand, or turn off the logo requirement.",
        }
        if logo_findings
        else None
    )
    outputs = []
    descriptions = {}
    for output in plan.outputs:
        blocked = any(blocks_output(f, output) for f in plan.findings)
        available = output.product_id in sources and not any(
            blocks_output(f, output)
            and not (f.context and f.context.phrase)
            and f.rule != "text_fit"
            for f in plan.findings
        )
        url = (
            f"/api/campaigns/{campaign_id}/composition/{version}/images/{output.id}"
            if available
            else None
        )
        product = next(p for p in plan.brief.products if p.id == output.product_id)
        item = {
            "id": output.id,
            "product_id": output.product_id,
            "market": output.market,
            "ratio": output.ratio,
            "width": output.width,
            "height": output.height,
            "copy": output.text.model_dump(),
            "file": output.id + ".png" if available else None,
            "eligible_for_export": output.product_id in sources and not blocked,
            "url": url,
            "thumbnail_url": url,
            "sha256": None,
            "source_role": product.source.role,
            "synthetic": product.source.provenance.synthetic,
            "omission": None
            if available
            else "Resolve the listed findings."
            if blocked
            else "Prepare the source image in Production.",
        }
        outputs.append(item)
        descriptions[output.id] = public_metadata(
            current["metadata"].get(output.id, initial_metadata(item))
        )
    metadata = {"schema_version": 1, "revision_id": version, "creatives": descriptions}
    result["metadata"] = {**metadata, "snapshot_id": canonical_hash(metadata)}
    result["manifest"] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "run_id": run["id"] if run else None,
        "revision_id": version,
        "composition_version": version,
        "preview_only": True,
        "complete": not result["composition_issues"]
        and all(o["eligible_for_export"] for o in outputs),
        "outputs": outputs,
        "findings": [f.model_dump() for f in plan.findings],
        "metrics": {
            **result["campaign_metrics"],
            "completed_images": sum(bool(o["url"]) for o in outputs),
            "expected_images": expected_images,
        },
    }
    recoverable = {
        a["product_id"]
        for a in result["run_attempts"]
        if a["response_recoverable"] and not a["source_available"]
    }
    result["local_recovery"] = {
        "output_ids": [o.id for o in plan.outputs if o.product_id in recoverable],
        "saved_result": bool(recoverable),
    }
    result["editable_products"] = {
        p["id"]: p["id"] in sources
        for p in value["products"]
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    }
    result["logo_editable_products"] = result["editable_products"].copy()
    result["can_edit"] = any(result["editable_products"].values())
    result["source_sizes"] = {pid: [asset.width, asset.height] for pid, asset in sources.items()}
    result["source_urls"] = {
        pid: f"/api/campaigns/{campaign_id}/composition/{version}/sources/{pid}" for pid in sources
    }
    result["rule_withheld_outputs"] = [
        o.id
        for o in plan.outputs
        if any(f.context and f.context.phrase and blocks_output(f, o) for f in plan.findings)
    ]
    for product in plan.brief.products:
        if product.id in sources:
            url = f"/campaigns/{campaign_id}/sources/{product.id}/1x1"
            if product.source.role == "concept":
                result["concept_previews"][product.id] = url
            else:
                result["product_images"][product.id] = {
                    "source": product.source.model_dump(mode="json"),
                    "url": url,
                }
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
                message = (
                    "This campaign’s saved files could not be read. "
                    "Restore its files, or delete the campaign from the Workbench."
                )
                try:
                    require_campaign_available(ws.storage, entry["id"])
                except StateError as error:
                    if error.code != "workspace_upgrade":
                        continue
                    message = str(error)
                entries.append(
                    {
                        **entry,
                        "status": "damaged",
                        "products": 0,
                        "markets": 0,
                        "updated": None,
                        "thumbnail": None,
                        "recovery_message": message,
                    }
                )
        from campaign_studio.bundled import setup_notice

        return {
            "setup_notice": setup_notice(ws),
            "campaigns": entries,
            "pending_cleanup": Cleanup(ws.state).pending(),
            "product_limit": settings.product_limit,
            "key_available": request.app.state.key_available(),
            "key_saved_at": request.app.state.credentials.status().get("saved_at"),
            "key_loaded_at": request.app.state.credentials.loaded_at,
        }

    @router.get("/api/motif/campaigns/{campaign_id}")
    async def campaign(request: Request, campaign_id: CampaignID, after_edit: bool = False):
        return await asyncio.to_thread(
            campaign_data, workspace(request), campaign_id, verify_saved_files=not after_edit
        )

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
        ws = workspace(request)
        acknowledgement = await asyncio.to_thread(AuthoredProjects(ws).save, campaign_id, action)
        result = await asyncio.to_thread(campaign_data, ws, campaign_id, verify_saved_files=False)
        return {**result, **acknowledgement, "campaign": result}

    @router.post("/api/motif/campaigns/{campaign_id}/brand/apply")
    async def apply_brand(request: Request, campaign_id: CampaignID, action: BrandUpdate):
        raise ActionError(
            "autosave",
            "Brand changes save automatically. Refresh this page to use the current editor.",
            410,
        )

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
