"""Install the two curated campaigns once, through the ordinary local lifecycle."""

import asyncio
import copy
import hashlib
import json
import re
import time
from contextlib import ExitStack
from importlib.resources import files
from pathlib import PurePosixPath
from uuid import uuid4

from campaign_studio.brief import Brief
from campaign_studio.bundled_outputs import install_prepared_outputs, load_prepared_outputs
from campaign_studio.intake import BriefDraft, Intake, draft_bytes
from campaign_studio.local_files import file_lock
from campaign_studio.planning import canonical_hash
from campaign_studio.state import StateError, timestamp
from campaign_studio.storage import StorageError
from campaign_studio.workspace import ActionError

RECEIPT = "private/bundled-campaigns.json"


def load_campaigns(source=None, *, product_limit=5):
    """Read only explicitly listed public resources, never paths from an imported brief."""
    source = source or files("campaign_studio").joinpath("resources/campaigns")
    manifest = json.loads(source.joinpath("manifest.json").read_bytes())
    if manifest.get("version") != 2 or len(manifest.get("campaigns", [])) != 2:
        raise StorageError("The bundled campaign package is incomplete.")
    campaigns = []
    for entry in manifest["campaigns"]:
        slug = entry["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise StorageError("The bundled campaign package has an invalid folder.")
        if entry["brief"] != f"{slug}/brief.json" or "prepared_outputs" not in entry:
            raise StorageError("The bundled campaign package has an invalid brief entry.")
        original_bytes = source.joinpath(entry["brief"]).read_bytes()
        raw = json.loads(original_bytes)
        brief = Brief.model_validate(raw)
        if len(brief.products) > product_limit:
            raise StorageError("The bundled campaigns exceed the configured product limit.")
        references = {brief.brand.logo_path}
        references.update(p.source.path for p in brief.products if p.source.role != "concept")
        if references != entry["files"].keys():
            raise StorageError("The bundled campaign attachments do not match its brief.")
        assets = {}
        for reference, expected in entry["files"].items():
            path = PurePosixPath(reference)
            if (
                len(path.parts) != 2
                or path.parts[0] != "assets"
                or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name)
            ):
                raise StorageError("The bundled campaign package has an invalid attachment.")
            payload = source.joinpath(slug, reference).read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected:
                raise StorageError("A bundled campaign attachment does not match its package.")
            assets[reference] = payload
        prepared = load_prepared_outputs(source, entry, raw)
        campaigns.append((entry, raw, assets, prepared, original_bytes))
    return manifest, campaigns


def setup_pending(workspace, *, allow_new=True):
    path = workspace.storage.path(RECEIPT)
    if path.exists():
        state = json.loads(path.read_bytes()).get("state")
        if state not in {"started", "complete", "skipped"}:
            raise StorageError(
                "The example setup record could not be read. Restore its saved copy."
            )
        return state == "started"
    with workspace.storage.connect() as db:
        return allow_new and not db.execute("SELECT 1 FROM campaigns LIMIT 1").fetchone()


def setup_notice(workspace):
    path = workspace.storage.path(RECEIPT)
    if not path.exists():
        return None
    receipt = json.loads(path.read_bytes())
    preserved = {
        item["id"] for item in receipt.get("campaigns", []) if item.get("stage") == "preserved"
    }
    with workspace.storage.connect() as db:
        existing = {row[0] for row in db.execute("SELECT id FROM campaigns")}
    if preserved & existing:
        return "An interrupted example setup was left unfinished to preserve your later changes."
    return None


def _record(storage, receipt):
    storage.write(RECEIPT, json.dumps(receipt, ensure_ascii=False, indent=2).encode())


def _checkpoint(record, document):
    record["draft_hash"] = canonical_hash(document)
    record["draft_version"] = document["version"]


def _existing_upload(storage, campaign_id, payload, role):
    # Reuse only this campaign's registered upload with these exact bytes. An
    # interruption after upload but before its receipt must not accumulate copies.
    digest = hashlib.sha256(payload).hexdigest()
    group = "Brand" if role == "logo" else "Products"
    project = storage.project(campaign_id)
    with storage.connect() as db:
        refs = [
            row[0]
            for row in db.execute(
                "SELECT u.reference FROM uploads u JOIN file_owners f "
                "ON f.reference=u.reference WHERE f.campaign_id=?",
                (campaign_id,),
            )
        ]
    for ref in refs:
        if not ref.startswith(project.ref(f"Assets/{group}") + "/"):
            continue
        path = storage.path(project.check(ref))
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            return ref
    return None


def _resume_initial_draft(storage, record, original, original_bytes):
    """Finish a registered example whose initial draft was never published."""
    from campaign_studio.cleanup import require_campaign_available

    campaign_id = record["id"]
    with storage.exclusive_draft():
        require_campaign_available(storage, campaign_id)
        project = storage.project(campaign_id)
        original_ref = project.ref("Brief/Imports/original.json")
        initial_refs = {original_ref, project.draft}
        if storage.path(project.draft).exists():
            return
        with storage.connect() as db:
            owned = {
                row[0]
                for row in db.execute(
                    "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
                )
            }
            if db.execute("SELECT 1 FROM runs WHERE campaign_id=?", (campaign_id,)).fetchone():
                return
        # A stopped atomic write may leave a registered temporary file. Leave
        # it owned and untouched; only finish the two already registered files.
        temporary = re.compile(
            "(?:" + "|".join(map(re.escape, initial_refs)) + r")\.[a-f0-9]{32}\.tmp"
        )
        if not initial_refs.issubset(owned) or any(
            ref not in initial_refs and not temporary.fullmatch(ref) for ref in owned
        ):
            return
        original_path = storage.path(original_ref)
        if original_path.exists() and (
            not original_path.is_file() or original_path.read_bytes() != original_bytes
        ):
            return
        document = {
            "value": copy.deepcopy(record["value"]),
            "version": uuid4().hex,
            "saved_at": timestamp(),
            "attachment_hints": Intake.attachment_hints(original, record["value"]),
        }
        if not original_path.exists():
            storage.write_new(original_ref, original_bytes)
        storage.write_new(project.draft, draft_bytes(document))


async def _install_one(workspace, record, definition, receipt):
    storage, intake = workspace.storage, Intake(workspace.storage)
    entry, original, assets, prepared, original_bytes = definition
    campaign_id = record["id"]
    with storage.connect() as db:
        exists = db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    stage = record["stage"]
    if stage not in {
        "create",
        "attachments",
        "saving",
        "planning",
        "outputs",
        "complete",
        "preserved",
    }:
        raise StorageError(
            "The example setup checkpoint could not be read. Restore its saved copy."
        )
    if stage in {"complete", "preserved"}:
        return
    if not exists and stage != "create":
        record["stage"] = "preserved"  # A deleted example stays deleted.
        return
    try:
        if stage == "create" and exists:
            _resume_initial_draft(storage, record, original, original_bytes)
        existing_document = intake.read(campaign_id) if exists else None
    except ActionError, StateError, ValueError, KeyError, TypeError, AttributeError:
        record["stage"] = "preserved"
        return
    if stage == "create":
        if exists:
            document = existing_document
            if not document or "authored" in document or document["value"] != record["value"]:
                record["stage"] = "preserved"
                return
        else:
            document = intake.create(
                campaign_id,
                copy.deepcopy(record["value"]),
                attachment_hints=intake.attachment_hints(original, record["value"]),
                imported_bytes=original_bytes,
            )
        record["stage"] = "attachments"
        _checkpoint(record, document)
        _record(storage, receipt)
    else:
        document = existing_document

    # Authored edits (including raw editor buffers) belong to the user. Setup
    # only continues a draft whose exact last checkpoint is still present.
    same = document is not None and canonical_hash(document) == record.get("draft_hash")
    saved_before_receipt = (
        record["stage"] == "saving"
        and document is not None
        and "authored" not in document
        and document["value"] == record["value"]
    )
    planned_before_receipt = (
        record["stage"] == "planning"
        and document is not None
        and "authored" not in document
        and document["version"] == record.get("draft_version")
        and bool(document.get("planned_run"))
        and document["value"]
        == workspace.state.plan(document["planned_run"]).brief.model_dump(mode="json")
    )
    if not (same or saved_before_receipt or planned_before_receipt):
        record["stage"] = "preserved"
        return
    if record["stage"] == "attachments":
        value = copy.deepcopy(record["value"])
        for reference, role, target, field in [
            (original["brand"]["logo_path"], "logo", value["brand"], "logo_path"),
            *[
                (old["source"]["path"], "product", product["source"], "path")
                for old, product in zip(original["products"], value["products"], strict=True)
                if product["source"]["role"] != "concept"
            ],
        ]:
            target[field] = (
                _existing_upload(storage, campaign_id, assets[reference], role)
                or (intake.upload(assets[reference], None, campaign_id, role)["reference"])
            )
        record.update(stage="saving", value=value)
        _record(storage, receipt)  # Intended value is durable before its saved draft.
    if record["stage"] == "saving":
        if not saved_before_receipt:
            document = intake.save(
                campaign_id,
                BriefDraft(value=record["value"], version=document["version"]),
                workspace,
            )
        record["stage"] = "planning"
        _checkpoint(record, document)
        _record(storage, receipt)
    if record["stage"] == "planning":
        plan = intake.prepare(campaign_id, document["version"], workspace)
        if prepared is not None and plan.generation_products:
            raise StorageError("A prepared bundled campaign unexpectedly requires new imagery.")
        record["stage"] = "outputs"
        _checkpoint(record, intake.read(campaign_id))
        _record(storage, receipt)
    if record["stage"] == "outputs":
        run = workspace.run_for(campaign_id)
        if prepared is not None and not run["current_revision"]:
            await asyncio.to_thread(
                install_prepared_outputs, workspace.state, run["id"], original, prepared
            )
        record["stage"] = "complete"
        record.pop("value", None)


async def install_campaigns(workspace, *, source=None, product_limit=5, allow_new=True):
    """Resume untouched first-start work; never recreate deleted or edited examples."""
    storage = workspace.storage
    with storage.path("private/bundled-setup.lock").open("a+b") as lock, ExitStack() as held:
        try:
            held.enter_context(file_lock(lock, wait=False))
        except BlockingIOError:
            return None  # Another startup owns the same setup receipt.
        if not allow_new and not storage.path(RECEIPT).exists():
            _record(storage, {"state": "skipped", "campaigns": []})
            return None
        if not setup_pending(workspace, allow_new=allow_new):
            if not storage.path(RECEIPT).exists():
                _record(storage, {"state": "skipped", "campaigns": []})
            return None
        manifest, definitions = load_campaigns(source, product_limit=product_limit)
        path = storage.path(RECEIPT)
        if path.exists():
            receipt = json.loads(path.read_bytes())
        else:
            # Reserve enough space before the first campaign. Per-write capacity
            # checks remain authoritative if disk usage changes during installation.
            needed = sum(
                3 * sum(map(len, assets.values()))
                + 2 * 1024 * 1024
                + (sum(item["bytes"] for item in prepared.descriptor["outputs"]) if prepared else 0)
                for _, _, assets, prepared, _ in definitions
            )
            storage.require_capacity(needed)
            intake = Intake(storage)
            receipt = {"version": manifest["version"], "state": "started", "campaigns": []}
            for entry, original, _, _, _ in definitions:
                cid = intake.new_campaign_id()
                receipt["campaigns"].append(
                    {
                        "slug": entry["slug"],
                        "id": cid,
                        "stage": "create",
                        "value": intake.import_value(cid, original),
                    }
                )
            _record(storage, receipt)
        started = time.monotonic()
        for definition in definitions:
            slug = definition[0]["slug"]
            record = next((item for item in receipt["campaigns"] if item["slug"] == slug), None)
            if record is None:
                cid = Intake(storage).new_campaign_id()
                record = {
                    "slug": slug,
                    "id": cid,
                    "stage": "create",
                    "value": Intake(storage).import_value(cid, definition[1]),
                }
                receipt["campaigns"].append(record)
                _record(storage, receipt)
            if "stage" not in record:
                record["stage"] = "preserved"  # Older interrupted receipts have no safe checkpoint.
            await _install_one(workspace, record, definition, receipt)
            _record(storage, receipt)
        receipt.update(state="complete", elapsed_seconds=round(time.monotonic() - started, 3))
        _record(storage, receipt)
        return receipt
