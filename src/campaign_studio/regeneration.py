"""Explicit replacement requests retain accepted sources until guarded publication."""

import hashlib
import json
from uuid import uuid4

from pydantic import Field

from campaign_studio.brief import Contract, Identifier
from campaign_studio.campaign_drafts import read, write
from campaign_studio.costing import estimate_requests
from campaign_studio.generation import ReplacementIntent, request_for
from campaign_studio.planning import Asset, canonical_hash
from campaign_studio.state import timestamp
from campaign_studio.workspace import ActionError


class RegenerationReview(Contract):
    version: str = Field(min_length=1, max_length=128)
    product_id: Identifier


class RegenerationConfirm(RegenerationReview):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def intent_for(run):
    value = json.loads(run["plan_json"]).get("replacement")
    return ReplacementIntent.model_validate(value) if value else None


def eligible(workspace, document, product, source):
    """Uploaded photos and supplied replay imagery are never inferred to be generated."""
    if product.source.role == "finished_scene" or source is None:
        return False
    replay = document["authored"]["brief"].get("replay") or {}
    if product.id in replay.get("sources", {}):
        return False
    record = document.get("source_assets", {}).get(product.id, {})
    if record.get("asset", {}).get("sha256") != source.sha256 or not record.get("run_id"):
        return False
    return any(
        attempt["product_id"] == product.id
        and attempt["state"] == "decoded"
        and attempt["source_ref"] == source.reference
        and attempt["source_sha256"] == source.sha256
        and workspace.state.source_is_intact(attempt)
        for attempt in workspace.state.attempts(record["run_id"])
    )


def _review(workspace, campaign_id, action, operation_id):
    current = workspace.current_composition(campaign_id, action.version)
    document = read(workspace.storage, campaign_id)
    if document["version"] != action.version:
        raise ActionError(
            "composition_conflict", "This campaign changed. Review regeneration again."
        )
    plan = current["plan"]
    product = next((p for p in plan.brief.products if p.id == action.product_id), None)
    source = current["sources"].get(action.product_id)
    if product is None or not eligible(workspace, document, product, source):
        raise ActionError(
            "regeneration_source",
            "Regenerate is available only for an intact image generated in this campaign. "
            "Use Edit product to deliberately replace supplied imagery.",
            422,
        )
    request = request_for(plan.brief, product)
    basis = {
        "operation_id": operation_id,
        "version": action.version,
        "product_id": product.id,
        "source_sha256": source.sha256,
        "source_reference": source.reference,
        "request": request.model_dump(mode="json"),
        "input_assets": {key: asset.sha256 for key, asset in plan.assets.items()},
    }
    digest = canonical_hash(basis)
    plan = plan.model_copy(deep=True)
    plan.replacement = ReplacementIntent(
        operation_id=operation_id,
        product_id=product.id,
        document_version=action.version,
        source_sha256=source.sha256,
        source_reference=source.reference,
        confirmation_digest=digest,
    )
    plan.generation_requests = {product.id: request}
    plan.transmitted_data = {
        product.id: "Audience, scene direction, and product description. "
        "Existing photographs, logo, and wording stay local."
    }
    plan.cost_estimate = estimate_requests(plan.generation_requests)
    plan.cost_status = "unknown"
    return plan, {
        "operation_id": operation_id,
        "version": action.version,
        "product_id": product.id,
        "product_name": product.name,
        "digest": digest,
        "request": request.model_dump(mode="json"),
        "cost_estimate": plan.cost_estimate,
        "background_only": product.source.role == "prepared_product",
    }


def review(workspace, campaign_id, action):
    return _review(workspace, campaign_id, action, uuid4().hex)[1]


def existing(workspace, campaign_id, action):
    # Completed receipts retain intent too. An old confirmation never becomes a
    # fresh paid request after a later regeneration supersedes the current receipt.
    with workspace.storage.connect() as db:
        rows = db.execute(
            "SELECT id FROM runs WHERE campaign_id=? ORDER BY rowid DESC", (campaign_id,)
        ).fetchall()
    for row in rows:
        run = workspace.state.run(row[0])
        intent = intent_for(run)
        if intent is None or intent.operation_id != action.operation_id:
            continue
        if (
            intent.confirmation_digest != action.digest
            or intent.product_id != action.product_id
            or intent.document_version != action.version
        ):
            raise ActionError(
                "regeneration_confirmation", "This confirmation changed. Review regeneration again."
            )
        return run
    return None


def prepare(workspace, campaign_id, action):
    prior = existing(workspace, campaign_id, action)
    if prior:
        return prior
    plan, reviewed = _review(workspace, campaign_id, action, action.operation_id)
    if reviewed["digest"] != action.digest:
        raise ActionError(
            "regeneration_confirmation", "This image or request changed. Review regeneration again."
        )
    with workspace.storage.exclusive_run(), workspace.storage.exclusive_draft():
        prior = existing(workspace, campaign_id, action)
        if prior:
            return prior
        require_current(workspace.state, campaign_id, plan.replacement)
        run_id = workspace.state.create(plan, 1)
        document = read(workspace.storage, campaign_id)
        document["planned_run"] = run_id
        document["planned_authored_hash"] = canonical_hash(document["authored"]["brief"])
        document["regeneration_receipt"] = {
            "run_id": run_id,
            "product_id": action.product_id,
            "outcome": "pending",
        }
        # Planning a replacement is not an accepted composition change.
        write(workspace.storage, campaign_id, document)
    return workspace.state.run(run_id)


def require_current(state, campaign_id, intent):
    document = read(state.storage, campaign_id)
    record = (document or {}).get("source_assets", {}).get(intent.product_id, {}).get("asset", {})
    if (
        not document
        or document["version"] != intent.document_version
        or record.get("sha256") != intent.source_sha256
        or record.get("reference") != intent.source_reference
    ):
        raise ActionError(
            "composition_conflict",
            "This campaign or its accepted image changed. "
            "The current image is kept; review regeneration again.",
        )
    try:
        state.storage.project(campaign_id).require(intent.source_reference)
        digest = hashlib.sha256(
            state.storage.path(intent.source_reference).read_bytes()
        ).hexdigest()
    except OSError, ValueError:
        digest = None
    if digest != intent.source_sha256:
        raise ActionError(
            "composition_conflict", "The accepted source changed. No replacement was applied."
        )
    return document


def not_started(state, run_id):
    """A refused confirmation/dispatch keeps its identity without claiming a request."""
    run = state.run(run_id)
    intent = intent_for(run)
    if intent is None or state.attempts(run_id):
        return
    with state.storage.exclusive_draft(), state.storage.connect() as db:
        document = read(state.storage, run["campaign_id"], db=db)
        document["regeneration_receipt"] = {
            "run_id": run_id,
            "product_id": intent.product_id,
            "outcome": "not_started",
        }
        write(state.storage, run["campaign_id"], document, db=db)
        db.execute("UPDATE runs SET state='paused',saved_attempts=0 WHERE id=?", (run_id,))


def finish(state, run_id, plan):
    """A paid receipt is retained even when Stop or a concurrent edit prevents adoption."""
    from campaign_studio.compositions import imagery_key
    from campaign_studio.imaging import load_image
    from campaign_studio.plan_storage import dump_completed

    intent = plan.replacement
    campaign_id = plan.brief.campaign_id
    attempts = state.attempts(run_id)
    attempt = next(
        (
            a
            for a in reversed(attempts)
            if a["product_id"] == intent.product_id
            and a["state"] == "decoded"
            and state.source_is_intact(a)
        ),
        None,
    )
    asset = None
    if attempt:
        # Decode before taking the short publication guard. The attempt and
        # response remain durable regardless of whether this image is adopted.
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
    with state.storage.exclusive_draft(), state.storage.connect() as db:
        document = read(state.storage, campaign_id, db=db)
        outcome = "failed"
        if asset:
            try:
                require_current(state, campaign_id, intent)
            except ActionError:
                outcome = "conflict"
                asset = None
            else:
                if not state.source_is_intact(attempt):
                    asset = None
        # Stop is a separate database writer. Take its serialization boundary
        # only after decoding/current-source checks, then read Stop again in the
        # same transaction that commits the accepted image and terminal state.
        db.execute("BEGIN IMMEDIATE")
        stopped = bool(
            db.execute("SELECT stop_requested FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        )
        if stopped:
            outcome = "stopped"
        elif asset:
            product = next(p for p in plan.brief.products if p.id == intent.product_id)
            input_asset = plan.assets.get(f"products/{product.id}")
            document.setdefault("source_assets", {})[product.id] = {
                "asset": asset.model_dump(),
                "imagery_key": imagery_key(plan.brief, product),
                "run_id": run_id,
                "input_sha256": input_asset.sha256 if input_asset else None,
            }
            for creative_id, metadata in document.get("creative_metadata", {}).items():
                if (
                    creative_id.split("/", 1)[0] == product.id
                    and asset.sha256 != intent.source_sha256
                ):
                    metadata.update(
                        version=metadata.get("version", 0) + 1, reset_reason="source_changed"
                    )
            document["authored_base"] = {
                "run_id": run_id,
                "revision_id": None,
                "metadata_snapshot_id": None,
            }
            document["version"] = uuid4().hex
            document["saved_at"] = timestamp()
            outcome = "replaced"
        document["regeneration_receipt"] = {
            "run_id": run_id,
            "product_id": intent.product_id,
            "outcome": outcome,
        }
        write(state.storage, campaign_id, document, db=db)
        status = "complete" if outcome == "replaced" else "stopped" if stopped else "partial"
        payload = (
            dump_completed(plan, {a["product_id"] for a in attempts})
            if status == "complete"
            else state.run(run_id)["plan_json"]
        )
        db.execute(
            "UPDATE runs SET state=?,saved_attempts=?,plan_json=?,"
            "confirmed_revision=NULL WHERE id=?",
            (status, len(attempts), payload, run_id),
        )
    return {
        "campaign_id": campaign_id,
        "complete": outcome == "replaced",
        "replacement_outcome": outcome,
        "version": document["version"],
        "sources_ready": len(document.get("source_assets", {})),
        "outputs": [],
    }


def public_status(document, run):
    if not run or intent_for(run) is None:
        return None
    intent = intent_for(run)
    receipt = document.get("regeneration_receipt", {})
    outcome = receipt.get("outcome", "pending") if receipt.get("run_id") == run["id"] else "pending"
    if outcome == "pending" and run["state"] in {"paused", "partial"}:
        outcome = "failed"
    messages = {
        "not_started": (
            "No image request was sent. The current image was kept. Review regeneration again."
        ),
        "pending": "The current image stays available while its replacement is generated.",
        "replaced": "The regenerated image is now used in this product’s campaign formats.",
        "stopped": (
            "Regeneration stopped. The current image was kept. "
            "An in-flight request may still be charged."
        ),
        "failed": (
            "The current image was kept. No request is retried automatically. "
            "Review the production record before another request."
        ),
        "conflict": (
            "A replacement was generated, but the campaign changed. The current image was kept. "
            "The unused result and its recorded usage remain in the production record; "
            "no request was resent."
        ),
    }
    return {
        "product_id": intent.product_id,
        "outcome": outcome,
        "message": messages[outcome],
        "operation_id": intent.operation_id,
        "reviewed_version": intent.document_version,
    }
