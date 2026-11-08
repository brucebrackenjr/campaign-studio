"""One current composition in SQLite; finished images are created only for delivery."""

import copy
import hashlib
import io
from uuid import uuid4

from pydantic import ValidationError

from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import read as read_draft
from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.composition_migration import migrate_current
from campaign_studio.costing import estimate_requests
from campaign_studio.imaging import compose, crop_box, load_image, protects_subject, save_srgb
from campaign_studio.plan_storage import completed_run, dump_completed
from campaign_studio.planning import (
    Asset,
    Finding,
    canonical_hash,
    compare_plans,
    contextualize_findings,
    expand_outputs,
    make_plan,
)
from campaign_studio.rendering import blocks_output
from campaign_studio.state import timestamp


def imagery_key(brief, product):
    """Only inputs that determine the source photograph, never copy or placement."""
    from campaign_studio.plan_storage import generation_inputs

    return canonical_hash(generation_inputs(brief, product))


def source_records(state, run_id, plan):
    records = {}
    attempts = state.attempts(run_id)
    for product in plan.brief.products:
        asset = plan.assets.get(
            f"products/{product.id}"
            if product.source.role == "finished_scene"
            else f"masters/{product.id}"
        )
        if asset is None:
            attempt = next(
                (
                    a
                    for a in reversed(attempts)
                    if a["product_id"] == product.id
                    and a["state"] == "decoded"
                    and state.source_is_intact(a)
                ),
                None,
            )
            if attempt:
                image, _ = load_image(state.storage.path(attempt["source_ref"]))
                try:
                    asset = Asset(
                        reference=attempt["source_ref"],
                        sha256=attempt["source_sha256"],
                        width=image.width,
                        height=image.height,
                    )
                finally:
                    image.close()
        if asset:
            records[product.id] = {
                "asset": asset.model_dump(),
                "imagery_key": imagery_key(plan.brief, product),
                "run_id": run_id,
                "input_sha256": plan.assets.get(f"products/{product.id}").sha256
                if f"products/{product.id}" in plan.assets
                else None,
            }
    return records


def preview_brief(value):
    """Isolate unfinished products for display, without changing the stored brief."""
    from campaign_studio.validation import validation_issues

    try:
        return Brief.model_validate(value), []
    except ValidationError as error:
        errors = error.errors(include_url=False)
        # Shared campaign errors still apply to every preview. Only field errors
        # inside individual products may be isolated from their unaffected peers.
        if any(
            len(item["loc"]) < 2
            or item["loc"][0] != "products"
            or not isinstance(item["loc"][1], int)
            for item in errors
        ):
            raise
        excluded = {item["loc"][1] for item in errors}
        projected = copy.deepcopy(value)
        projected["products"] = [
            product for index, product in enumerate(value["products"]) if index not in excluded
        ]
        if not projected["products"]:
            raise
        replay = projected.get("replay")
        if isinstance(replay, dict) and isinstance(replay.get("sources"), dict):
            kept = {product["id"] for product in projected["products"]}
            replay["sources"] = {
                pid: source for pid, source in replay["sources"].items() if pid in kept
            }
            if not replay["sources"]:
                projected["replay"] = None
        # Run the full remaining contract again, including cross-field rules.
        # A projection cannot authorize production, download, or persistence.
        return Brief.model_validate(projected), validation_issues(error)


def snapshot(workspace, campaign_id, expected_version=None, *, brief=None, preview=False):
    from campaign_studio.workspace import ActionError

    migrate_current(workspace.state, campaign_id)
    with workspace.storage.exclusive_draft():
        document = read_draft(workspace.storage, campaign_id)
        if not document:
            raise ActionError(
                "campaign_data", "This campaign has no saved editing instructions.", 404
            )
        if expected_version is not None and document["version"] != expected_version:
            raise ActionError(
                "composition_conflict",
                "This campaign changed in another tab. Reload its current settings.",
            )
        value = copy.deepcopy(document["authored"]["brief"])
        version = document["version"]
        records = copy.deepcopy(document.get("source_assets", {}))
        replay_bindings = copy.deepcopy(document.get("replay_bindings", {}))
        metadata = copy.deepcopy(document.get("creative_metadata", {}))
    issues = []
    if preview:
        displayed, issues = preview_brief(value)
        brief = brief or displayed
    else:
        brief = brief or Brief.model_validate(value)
    if brief.replay:
        products = {product.id: product for product in brief.products}
        retained = {
            pid: master
            for pid, master in brief.replay.sources.items()
            if (records.get(pid, {}).get("imagery_key") or replay_bindings.get(pid))
            in {None, imagery_key(brief, products[pid])}
        }
        if retained.keys() != brief.replay.sources.keys():
            brief = brief.model_copy(
                update={
                    "replay": brief.replay.model_copy(update={"sources": retained, "partial": True})
                    if retained
                    else None
                }
            )
    plan = make_plan(brief, workspace.storage)
    sources = {}
    for product in brief.products:
        asset = (
            plan.assets.get(f"products/{product.id}")
            if product.source.role == "finished_scene"
            else plan.assets.get(f"masters/{product.id}")
        )
        record = records.get(product.id, {})
        input_asset = plan.assets.get(f"products/{product.id}")
        matching_input = product.source.role != "prepared_product" or (
            input_asset is not None and input_asset.sha256 == record.get("input_sha256")
        )
        if (
            asset is None
            and matching_input
            and record.get("imagery_key") == imagery_key(brief, product)
        ):
            candidate = Asset.model_validate(record["asset"])
            try:
                workspace.storage.project(campaign_id).require(candidate.reference)
                if (
                    hashlib.sha256(
                        workspace.storage.path(candidate.reference).read_bytes()
                    ).hexdigest()
                    == candidate.sha256
                ):
                    asset = candidate
            except OSError, ValueError:
                pass  # The source needs repair or an explicitly confirmed new Production request.
        if asset is not None:
            sources[product.id] = asset
            if product.source.role != "finished_scene":
                plan.assets[f"masters/{product.id}"] = asset
            plan.generation_requests.pop(product.id, None)
            plan.transmitted_data.pop(product.id, None)
    plan.digest = canonical_hash(
        {
            "brief": brief.model_dump(mode="json"),
            "assets": {key: asset.model_dump() for key, asset in plan.assets.items()},
            "generation_requests": {
                key: request.model_dump() for key, request in plan.generation_requests.items()
            },
        }
    )
    plan.cost_estimate = estimate_requests(plan.generation_requests)
    plan.cost_status = "unknown" if plan.generation_requests else "no_new_requests"
    for output in plan.outputs:
        source = sources.get(output.product_id)
        box = output.framing.subject_box
        if (
            source
            and box
            and not protects_subject(
                (source.width, source.height),
                crop_box((source.width, source.height), output.ratio, output.framing),
                box,
            )
            and not any(f.item == output.id and f.rule == "subject_crop" for f in plan.findings)
        ):
            plan.findings.append(
                Finding(
                    item=output.id,
                    rule="subject_crop",
                    severity="blocking",
                    evidence="This crop cuts protected product detail.",
                    remedy="Adjust the crop or the marked product area before downloading.",
                )
            )
    contextualize_findings(brief, plan.outputs, plan.findings)
    if issues:
        # Findings created from the temporary subset must still point at the
        # producer's original product positions, including any unfinished rows.
        from campaign_studio.validation import field_issue

        positions = {
            product["id"]: index
            for index, product in enumerate(value["products"])
            if isinstance(product, dict) and isinstance(product.get("id"), str)
        }
        for finding in plan.findings:
            context = finding.context
            if context is None:
                continue
            for index, field in enumerate(context.fields):
                if len(field.path) > 1 and field.path[0] == "products":
                    original = positions[brief.products[field.path[1]].id]
                    context.fields[index] = type(field).model_validate(
                        field_issue(
                            ["products", original, *field.path[2:]], field.code, field.message
                        )
                    )
            if context.phrase:
                for match in context.phrase.matches:
                    if len(match.path) > 1 and match.path[0] == "products":
                        match.path[1] = positions[brief.products[match.path[1]].id]
    return {
        "plan": plan,
        "version": version,
        "sources": sources,
        "metadata": metadata,
        "campaign_metrics": workspace.state.campaign_metrics(campaign_id),
        "composition_issues": issues,
    }


def finish_sources(state, run_id, plan):
    """Commit source readiness and compact the completed request record, never render PNGs."""
    campaign_id = plan.brief.campaign_id
    records = source_records(state, run_id, plan)
    complete = len(records) == len(plan.brief.products)
    with state.storage.exclusive_draft(), state.storage.connect() as db:
        document = read_draft(state.storage, campaign_id, db=db)
        if document is None:
            document = {
                "value": plan.brief.model_dump(mode="json"),
                "authored": {"brief": plan.brief.model_dump(mode="json")},
                "version": uuid4().hex,
                "composition_schema": 1,
            }
        document.setdefault("planned_run", run_id)
        document.setdefault(
            "planned_authored_hash", canonical_hash(plan.brief.model_dump(mode="json"))
        )
        previous_sources = document.setdefault("source_assets", {})
        changed_sources = {
            product_id
            for product_id, record in records.items()
            if product_id in previous_sources
            and previous_sources[product_id].get("asset", {}).get("sha256")
            != record["asset"]["sha256"]
        }
        for creative_id, description in document.get("creative_metadata", {}).items():
            if creative_id.split("/", 1)[0] in changed_sources:
                description.update(
                    version=description.get("version", 0) + 1,
                    reset_reason="source_changed",
                )
        previous_sources.update(records)
        document["authored_base"] = {
            "run_id": run_id,
            "revision_id": None,
            "metadata_snapshot_id": None,
        }
        # Source completion does not overwrite edits made while a provider was running.
        document["version"] = uuid4().hex
        document["saved_at"] = timestamp()
        write_draft(state.storage, campaign_id, document, db=db)
        attempts = state.attempts(run_id)
        db.execute("UPDATE runs SET saved_attempts=? WHERE id=?", (len(attempts), run_id))
        if complete:
            attempts = state.attempts(run_id)
            old_payload = state.run(run_id)["plan_json"]
            payload = (
                old_payload
                if completed_run(old_payload) is not None
                else dump_completed(plan, {item["product_id"] for item in attempts})
            )
            db.execute(
                "UPDATE runs SET state='complete',plan_json=?,current_revision=NULL,"
                "confirmed_revision=NULL,saved_attempts=? WHERE id=?",
                (payload, len(attempts), run_id),
            )
        else:
            db.execute(
                "UPDATE runs SET state=CASE WHEN stop_requested THEN 'stopped' "
                "ELSE 'partial' END WHERE id=?",
                (run_id,),
            )
    return {
        "campaign_id": campaign_id,
        "metrics": {
            **state.campaign_metrics(campaign_id),
            "completed_images": 0,
            "sources_ready": len(records),
        },
        "complete": complete,
        "sources_ready": len(records),
        "version": document["version"],
        "revision_id": document["version"],
        "outputs": [],
    }


def fit_changed_logo(workspace, campaign_id, previous, value):
    """Fit saved placements to replacement artwork without changing unrelated form values."""
    from campaign_studio.brief import FORMATS, LogoPlacement
    from campaign_studio.imaging import ImageProblem, fit_logo_placement, placed_logo_bounds

    brand, old_brand = value.get("brand"), previous.get("brand")
    if not isinstance(brand, dict) or not isinstance(old_brand, dict):
        return
    reference = brand.get("logo_path")
    if not reference or reference == old_brand.get("logo_path"):
        return
    workspace.storage.project(campaign_id).require(reference)
    try:
        logo, _ = load_image(workspace.storage.path(reference))
        size = logo.size
        logo.close()
    except ValueError, OSError:
        return  # Current preview and export report an invalid image attachment.
    products = value.get("products")
    for product in products if isinstance(products, list) else []:
        if not isinstance(product, dict) or not isinstance(product.get("framing"), dict):
            continue
        for formats in product["framing"].values():
            if not isinstance(formats, dict):
                continue
            for ratio, framing in formats.items():
                if (
                    ratio not in FORMATS
                    or not isinstance(framing, dict)
                    or not framing.get("logo_placement")
                ):
                    continue
                try:
                    requested = LogoPlacement.model_validate(framing["logo_placement"])
                except ValueError:
                    continue  # Preserve incomplete raw typing for correction.
                try:
                    placed_logo_bounds(size, FORMATS[ratio], requested)
                except ImageProblem:
                    framing["logo_placement"] = fit_logo_placement(
                        size, FORMATS[ratio], requested
                    ).model_dump(mode="json")


def edit_value(workspace, campaign_id, value, edit):
    from campaign_studio.brief import Framing
    from campaign_studio.workspace import ActionError, FrameEdit, LogoEdit, TextEdit

    value = copy.deepcopy(value)
    selected = next((p for p in value["products"] if p["id"] == edit.product_id), None)
    if selected is None or edit.market not in value["markets"]:
        raise ActionError("creative", "Choose an image from this campaign.", 422)
    framing = Framing.model_validate(
        selected.get("framing", {}).get(edit.market, {}).get(edit.ratio, {})
    ).model_dump(mode="json")
    if isinstance(edit, TextEdit):
        if set(edit.values) != set(edit.field_scopes):
            raise ActionError("copy_fields", "Choose a scope for each wording field.", 422)
        for field, text in edit.values.items():
            if field == "cta" and text == "":
                text = None
            if edit.field_scopes[field] == "all_products":
                value["markets"][edit.market]["copy"][field] = text
                for product in value["products"]:
                    product.setdefault("copy_overrides", {}).get(edit.market, {}).pop(field, None)
            else:
                selected.setdefault("copy_overrides", {}).setdefault(edit.market, {})[field] = text
        for field, placement in edit.placements.items():
            if placement is None:
                framing["text_placements"].pop(field, None)
            else:
                framing["text_placements"][field] = placement.model_dump(mode="json")
    elif isinstance(edit, FrameEdit):
        framing.update(edit.framing.model_dump(mode="json", exclude_unset=True))
    elif isinstance(edit, LogoEdit):
        if edit.replacement:
            workspace.storage.project(campaign_id).upload_name(edit.replacement)
            value["brand"]["logo_path"] = edit.replacement
            if edit.provenance:
                value["brand"]["logo_provenance"] = edit.provenance.model_dump(mode="json")
        # Opening Logo omits placement so the preview must retain the saved
        # rectangle. An explicit null is the separate Reset action.
        if "placement" in edit.model_fields_set:
            framing["logo_placement"] = (
                edit.placement.model_dump(mode="json") if edit.placement else None
            )
    selected.setdefault("framing", {}).setdefault(edit.market, {})[edit.ratio] = framing
    if isinstance(edit, LogoEdit) and edit.replacement:
        fit_changed_logo(workspace, campaign_id, {"brand": {"logo_path": None}}, value)
    return value


def candidate(workspace, campaign_id, edit):
    current = snapshot(workspace, campaign_id, edit.revision_id, preview=True)
    old = current["plan"]
    brief = Brief.model_validate(
        edit_value(workspace, campaign_id, old.brief.model_dump(mode="json"), edit)
    )
    new = snapshot(workspace, campaign_id, edit.revision_id, brief=brief, preview=True)["plan"]
    impact = compare_plans(old, new).model_dump()
    impact["logo_available"] = "logo" in new.assets
    return brief, new, impact


def save(workspace, campaign_id, edit):
    from campaign_studio.workspace import ActionError

    request_hash = canonical_hash(
        edit.model_dump(mode="json", exclude_unset=True, exclude={"preview_digest"})
    )
    document = read_draft(workspace.storage, campaign_id)
    receipt = document.get("composition_receipt", {})
    if (
        receipt.get("request_hash") == request_hash
        and receipt.get("version") == document["version"]
    ):
        return document["version"]
    value = edit_value(workspace, campaign_id, document["authored"]["brief"], edit)
    from campaign_studio.authored import AuthoredProjects

    AuthoredProjects.check_envelope({"brief": value})
    with workspace.storage.exclusive_draft():
        document = read_draft(workspace.storage, campaign_id)
        receipt = document.get("composition_receipt", {})
        if (
            receipt.get("request_hash") == request_hash
            and receipt.get("version") == document["version"]
        ):
            return document["version"]
        if document["version"] != edit.revision_id:
            raise ActionError(
                "composition_conflict",
                "This campaign changed in another tab. Your edits are still available.",
            )
        reconcile_descriptions(document, document["authored"]["brief"], value)
        document["authored"]["brief"] = value
        document["version"] = uuid4().hex
        document["saved_at"] = timestamp()
        document["composition_receipt"] = {
            "request_hash": request_hash,
            "version": document["version"],
            "previous_version": edit.revision_id,
        }
        write_draft(workspace.storage, campaign_id, document)
        return document["version"]


def acknowledgement(workspace, campaign_id):
    """Return saved settings without opening or checking any image source file."""
    from pydantic import ValidationError

    from campaign_studio.authored import AuthoredProjects
    from campaign_studio.review import initial_metadata, public_metadata
    from campaign_studio.validation import validation_issues

    project = AuthoredProjects(workspace).read(campaign_id)
    document = read_draft(workspace.storage, campaign_id)
    value = copy.deepcopy(project["authored"]["brief"])
    version = project["version"]
    issues = []
    try:
        slots = []
        brief, issues = preview_brief(value)
        for output in expand_outputs(brief):
            slots.append(
                {
                    "id": output.id,
                    "product_id": output.product_id,
                    "market": output.market,
                    "ratio": output.ratio,
                    "width": output.width,
                    "height": output.height,
                    "copy": output.text.model_dump(mode="json"),
                    "framing": output.framing.model_dump(mode="json"),
                }
            )
    except ValidationError as error:
        slots = None
        issues = validation_issues(error)
    descriptions = {
        item["id"]: public_metadata(
            document.get("creative_metadata", {}).get(
                item["id"], initial_metadata({**item, "file": None})
            )
        )
        for item in (slots or [])
    }
    metadata = {"schema_version": 1, "revision_id": version, "creatives": descriptions}
    return {
        "version": version,
        "composition_version": version,
        "base_revision": version,
        "value": value,
        "output_brief": value,
        "plan_brief": value,
        "project": project,
        "composition_slots": slots,
        "composition_issues": issues,
        "metadata": {**metadata, "snapshot_id": canonical_hash(metadata)},
    }


def preview_image(workspace, campaign_id, version, product_id, market, ratio):
    from campaign_studio.workspace import ActionError

    current = snapshot(workspace, campaign_id, version, preview=True)
    plan = current["plan"]
    output = next(
        (
            o
            for o in plan.outputs
            if (o.product_id, o.market, o.ratio) == (product_id, market, ratio)
        ),
        None,
    )
    if output is None or product_id not in current["sources"]:
        raise ActionError(
            "source", "This image needs a source. Open Production to prepare it.", 404
        )
    if any(
        blocks_output(finding, output) and not (finding.context and finding.context.phrase)
        for finding in plan.findings
    ):
        raise ActionError("preview_blocked", "Correct the image findings to show its preview.", 422)
    scene, _ = load_image(workspace.storage.path(current["sources"][product_id].reference))
    logo = cutout = None
    try:
        if "logo" in plan.assets:
            logo, _ = load_image(workspace.storage.path(plan.assets["logo"].reference))
        product = next(p for p in plan.brief.products if p.id == product_id)
        if product.source.role == "prepared_product":
            cutout, _ = load_image(
                workspace.storage.path(plan.assets[f"products/{product_id}"].reference)
            )
        result = compose(
            scene,
            logo,
            output.text,
            output.ratio,
            plan.brief.brand.text_color,
            cutout,
            output.framing,
        )
        try:
            with io.BytesIO() as buffer:
                save_srgb(result, buffer, compress_level=3)
                return buffer.getvalue()
        finally:
            result.close()
    finally:
        scene.close()
        if logo:
            logo.close()
        if cutout:
            cutout.close()


def save_metadata(workspace, campaign_id, version, creative_id, action):
    from campaign_studio.motif import campaign_data
    from campaign_studio.review import public_metadata
    from campaign_studio.workspace import ActionError

    request_hash = canonical_hash(
        {"version": version, "creative_id": creative_id, "action": action.model_dump(mode="json")}
    )
    previous = read_draft(workspace.storage, campaign_id)
    receipt = previous.get("metadata_receipt", {})
    if (
        receipt.get("request_hash") == request_hash
        and receipt.get("version") == previous["version"]
    ):
        return campaign_data(workspace, campaign_id)
    current = snapshot(workspace, campaign_id, version, preview=True)
    if creative_id not in {o.id for o in current["plan"].outputs}:
        raise ActionError("creative", "Choose an image from this campaign.", 404)
    with workspace.storage.exclusive_draft():
        document = read_draft(workspace.storage, campaign_id)
        if document["version"] != version:
            raise ActionError(
                "composition_conflict",
                "This campaign changed. Reload it before saving the description.",
            )
        metadata = document.setdefault("creative_metadata", {})
        value = metadata.get(creative_id, {"version": 0, "description": None})
        if value["version"] != action.expected_version:
            raise ActionError(
                "metadata_conflict", "This description changed. Reload it before saving."
            )
        value.update(
            description=action.description,
            version=value["version"] + 1,
            updated_at=timestamp(),
            reset_reason=None,
        )
        metadata[creative_id] = public_metadata(value)
        document["version"] = uuid4().hex
        document["saved_at"] = value["updated_at"]
        document["metadata_receipt"] = {
            "request_hash": request_hash,
            "version": document["version"],
        }
        write_draft(workspace.storage, campaign_id, document)
    return campaign_data(workspace, campaign_id)


def reconcile_descriptions(document, before, after):
    """Keep producer descriptions attached to their creative through ordinary edits."""
    old_products, new_products = before.get("products"), after.get("products")
    markets = after.get("markets")
    if (
        not isinstance(old_products, list)
        or not isinstance(new_products, list)
        or not isinstance(markets, dict)
        or any(not isinstance(p, dict) for p in [*old_products, *new_products])
    ):
        return  # Temporarily incomplete typing must not erase saved descriptions.
    previous = {p.get("id"): p for p in old_products}
    current = {p.get("id"): p for p in new_products}
    metadata = document.get("creative_metadata", {})
    for creative_id, description in list(metadata.items()):
        product_id, market, _ = creative_id.split("/")
        if product_id not in current or market not in markets:
            del metadata[creative_id]
            continue
        old_source = previous.get(product_id, {}).get("source", {})
        new_source = current[product_id].get("source", {})
        if not isinstance(old_source, dict) or not isinstance(new_source, dict):
            continue
        # Copy, logo, color, and crop changes do not erase producer-written prose.
        # Only a changed source gets a contextual note; it does not block export.
        if (old_source.get("role"), old_source.get("path")) != (
            new_source.get("role"),
            new_source.get("path"),
        ):
            description.update(
                reset_reason="source_changed",
                version=description.get("version", 0) + 1,
                updated_at=timestamp(),
            )


def retain_replay_bindings(document, previous):
    """Remember which original creative inputs a supplied replay photograph satisfies."""
    replay = previous.get("replay")
    if not isinstance(replay, dict) or not isinstance(replay.get("sources"), dict):
        return
    products = previous.get("products")
    if not isinstance(products, list):
        return
    bindings = document.setdefault("replay_bindings", {})
    for product in products:
        if not isinstance(product, dict) or product.get("id") not in replay["sources"]:
            continue
        source = product.get("source")
        if not isinstance(source, dict):
            continue
        bindings.setdefault(
            product["id"],
            canonical_hash(
                {
                    "audience": previous.get("audience"),
                    "creative_direction": previous.get("creative_direction"),
                    "role": source.get("role"),
                    "description": source.get("description")
                    if source.get("role") == "concept"
                    else "",
                }
            ),
        )
