"""One current editable campaign record in SQLite, with no JSON file mirror.

The authored brief contains Campaign fields and every current Studio instruction.
The value key returned to callers is an in-memory compatibility view of that same
brief, not a second stored document. Accepted-plan helpers below exist only to
read and migrate workspaces created before composition_schema=1.
"""

import copy
import hashlib
import json
import re
from contextlib import nullcontext, suppress

from campaign_studio.storage import StorageError


def _accepted(db, campaign_id):
    from campaign_studio.plan_storage import load_plan
    from campaign_studio.state import State

    current = State._current_output(db, campaign_id)
    if current is None:
        return None
    row = db.execute("SELECT plan_json FROM revisions WHERE id=?", (current[1],)).fetchone()
    if row is None or row[0] is None:
        return None
    plan = load_plan(row[0])
    if plan.brief.campaign_id != campaign_id:
        raise StorageError("The saved image settings belong to another campaign.")
    return plan


def _settings(plan):
    if plan is None:
        return {}
    products = {product.id: product for product in plan.brief.products}
    settings = {}
    for output in plan.outputs:
        framing = products[output.product_id].framing.get(output.market, {}).get(output.ratio)
        settings[(output.product_id, output.market, output.ratio)] = (
            framing.model_dump(mode="json") if framing is not None else None
        )
    return settings


def _products(brief):
    products = brief.get("products")
    return [p for p in products if isinstance(p, dict)] if isinstance(products, list) else []


def _browser_numbers(value):
    if isinstance(value, dict):
        return {key: _browser_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_browser_numbers(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _hydrate(brief, settings):
    for product in _products(brief):
        for (product_id, market, ratio), framing in settings.items():
            if product.get("id") == product_id and framing is not None:
                frames = product.setdefault("framing", {})
                if not isinstance(frames, dict):
                    continue
                formats = frames.setdefault(market, {})
                if isinstance(formats, dict):
                    formats[ratio] = _browser_numbers(framing)


def _without_accepted_settings(brief, settings):
    for product in _products(brief):
        frames = product.get("framing")
        if not isinstance(product.get("id"), str) or not isinstance(frames, dict):
            continue
        removed = False
        for market, formats in list(frames.items()):
            if not isinstance(formats, dict):
                continue
            removed_market = False
            for ratio in list(formats):
                if (product.get("id"), market, ratio) in settings:
                    del formats[ratio]
                    removed = removed_market = True
            if removed_market and not formats:
                del frames[market]
        if removed and not frames:
            product.pop("framing", None)


def _pending_defaults(authored, conflicts=()):
    targets = authored.get("form_drafts", {}).get("brand_color_defaults", [])
    if not isinstance(targets, list):
        return
    products = {p["id"]: p for p in _products(authored["brief"]) if isinstance(p.get("id"), str)}
    for target in targets:
        if (
            not isinstance(target, dict)
            or not all(
                isinstance(target.get(key), str) for key in ("product_id", "market", "ratio")
            )
            or target in conflicts
        ):
            continue
        product = products.get(target.get("product_id"), {})
        placements = product.get("framing", {})
        for key in (target.get("market"), target.get("ratio"), "text_placements"):
            placements = placements.get(key, {}) if isinstance(placements, dict) else {}
        if not isinstance(placements, dict):
            continue
        for placement in placements.values():
            if isinstance(placement, dict):
                placement["color"] = None


def _default_conflicts(authored):
    """Retain only contradictory reset identities, never the image's color settings."""
    targets = authored.get("form_drafts", {}).get("brand_color_defaults", [])
    if not isinstance(targets, list):
        return []
    products = {p["id"]: p for p in _products(authored["brief"]) if isinstance(p.get("id"), str)}
    conflicts = []
    for target in targets:
        if not isinstance(target, dict) or not all(
            isinstance(target.get(key), str) for key in ("product_id", "market", "ratio")
        ):
            continue
        placements = products.get(target.get("product_id"), {})
        for key in ("framing", target.get("market"), target.get("ratio"), "text_placements"):
            placements = placements.get(key, {}) if isinstance(placements, dict) else {}
        if isinstance(placements, dict) and any(
            isinstance(p, dict) and p.get("color") is not None for p in placements.values()
        ):
            conflicts.append(copy.deepcopy(target))
    return conflicts


def _empty_value(campaign_id, raw, brand_id):
    """A safe display/validation baseline; raw text never becomes file authority."""
    brand = raw.get("brand") if isinstance(raw.get("brand"), dict) else {}

    def text(item, fallback=""):
        return item if isinstance(item, str) else fallback

    return {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "name": text(raw.get("name"), "Untitled campaign"),
        "audience": text(raw.get("audience")),
        "creative_direction": text(raw.get("creative_direction")),
        "brand": {
            "id": brand_id,
            "name": text(brand.get("name")),
            "logo_path": "",
            "logo_provenance": {
                "label": "Not provided",
                "rights": "Not provided",
                "synthetic": False,
            },
            "palette": ["#FFFFFF"],
            "text_color": "#FFFFFF",
        },
        "copy_provenance": {
            "label": "User-supplied wording",
            "rights": "Not provided",
            "synthetic": False,
        },
        "markets": {
            market: {"copy": {"message": "", "cta": None}, "phrase_rules": None}
            for market in ("en-US", "es-MX")
        },
        "products": [],
        "replay": None,
    }


def _value(storage, db, campaign_id, record, authored, accepted):
    raw = authored["brief"]
    if not record.get("authored_saved"):
        return copy.deepcopy(raw)
    if accepted is not None:
        return accepted.brief.model_dump(mode="json")
    from campaign_studio.intake import Intake

    try:
        # This structural check permits incomplete text but never follows paths.
        Intake.validate_draft(raw)
        storage.project(campaign_id).check_brief(raw, inspect_files=False)
        value = copy.deepcopy(raw)
        if isinstance(value.get("brand"), dict):
            value["brand"]["id"] = record["brand_id"]
        return value
    except ValueError, KeyError, TypeError, AttributeError:
        return _empty_value(campaign_id, raw, record["brand_id"])


def read(storage, campaign_id, *, db=None):
    """Return the legacy API view; its derived ``value`` is never persisted."""
    with nullcontext(db) if db is not None else storage.connect() as connection:
        row = connection.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if row is None:
            # Output-folder upgrade still needs the old draft's source references.
            # Read only the exact registered legacy file until its later migration.
            reference = storage.project(campaign_id).draft
            owned = connection.execute(
                "SELECT 1 FROM file_owners WHERE reference=? AND campaign_id=?",
                (reference, campaign_id),
            ).fetchone()
            path = storage.path(reference)
            if owned and path.is_file():
                document = json.loads(path.read_bytes())
                if document["value"]["campaign_id"] != campaign_id:
                    raise StorageError("The saved campaign document has a different identity.")
                return document
            return None
        record = json.loads(row[0])
        authored = copy.deepcopy(record["authored"])
        if authored["brief"].get("campaign_id") != campaign_id:
            raise StorageError("The saved campaign document has a different identity.")
        accepted = _accepted(connection, campaign_id)

        if record.get("composition_schema") != 1:
            _hydrate(authored["brief"], _settings(accepted))
        _pending_defaults(authored, record.get("default_color_conflicts", []))
        result = {
            key: copy.deepcopy(value)
            for key, value in record.items()
            if key
            not in {
                "authored",
                "authored_saved",
                "brand_id",
                "legacy_sha256",
                "default_color_conflicts",
            }
        }
        result["value"] = (
            copy.deepcopy(authored["brief"])
            if record.get("composition_schema") == 1
            else _value(storage, connection, campaign_id, record, authored, accepted)
        )
        if record.get("authored_saved") or record.get("composition_schema") == 1:
            result["authored"] = authored
        return result


def write(storage, campaign_id, document, *, db=None, create=False, legacy_sha256=None):
    """Replace this campaign's current record, never append a saved-edit history."""
    with nullcontext(db) if db is not None else storage.connect() as connection:
        old = connection.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if create and old is not None:
            raise FileExistsError("This campaign already has a saved document.")
        prior = json.loads(old[0]) if old else {}
        authored = copy.deepcopy(document.get("authored") or {"brief": document["value"]})
        brief = authored["brief"]
        if brief.get("campaign_id") != campaign_id:
            raise StorageError("A campaign's identity cannot be changed.")
        brand_id = prior.get("brand_id") or brief["brand"]["id"]
        conflicts = _default_conflicts(authored)
        accepted_settings = _settings(_accepted(connection, campaign_id))
        from campaign_studio.planning import canonical_hash

        admitted = document.get("planned_authored_hash")
        was_admitted = admitted is not None and admitted == canonical_hash(brief)
        if document.get("composition_schema") != 1:
            _without_accepted_settings(brief, accepted_settings)
        record = {
            key: copy.deepcopy(value)
            for key, value in document.items()
            if key not in {"value", "authored"}
        }
        record.update(authored=authored, authored_saved="authored" in document, brand_id=brand_id)
        if record.get("planned_run") and "planned_authored_hash" not in record:
            record["planned_authored_hash"] = canonical_hash(document.get("value", brief))
        if was_admitted:
            view = copy.deepcopy(authored)
            _hydrate(view["brief"], accepted_settings)
            _pending_defaults(view, conflicts)
            record["planned_authored_hash"] = canonical_hash(view["brief"])
        if conflicts:
            record["default_color_conflicts"] = conflicts
        else:
            record.pop("default_color_conflicts", None)
        marker = legacy_sha256 or prior.get("legacy_sha256")
        if marker:
            record["legacy_sha256"] = marker
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        connection.execute(
            "INSERT INTO campaign_drafts(campaign_id,document_json) VALUES (?,?) "
            "ON CONFLICT(campaign_id) DO UPDATE SET document_json=excluded.document_json",
            (campaign_id, payload),
        )


def strip_accepted_settings(storage, campaign_id, *, db=None):
    """First production moves initial imported editing choices into the accepted plan."""
    with nullcontext(db) if db is not None else storage.connect() as connection:
        row = connection.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if row is None:
            return
        record = json.loads(row[0])
        from campaign_studio.planning import canonical_hash

        was_admitted = record.get("planned_authored_hash") == canonical_hash(
            record["authored"]["brief"]
        )
        conflicts = _default_conflicts(record["authored"])
        if conflicts:
            record["default_color_conflicts"] = conflicts
        _without_accepted_settings(
            record["authored"]["brief"], _settings(_accepted(connection, campaign_id))
        )
        if was_admitted:
            view = copy.deepcopy(record["authored"])
            _hydrate(view["brief"], _settings(_accepted(connection, campaign_id)))
            _pending_defaults(view, record.get("default_color_conflicts", []))
            record["planned_authored_hash"] = canonical_hash(view["brief"])
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False)
        if payload != row[0]:
            connection.execute(
                "UPDATE campaign_drafts SET document_json=? WHERE campaign_id=?",
                (payload, campaign_id),
            )


def complete_base(
    storage,
    campaign_id,
    run_id,
    revision_id,
    original_plan,
    accepted_plan,
    *,
    db,
    metadata_snapshot_id=None,
    accepted_run_id=None,
):
    """Advance a pre-production draft before its completed execution plan is compacted.

    This shares the publication transaction. Distinct pending Campaign fields and
    form buffers remain unchanged; no comparison brief is added to the database.
    """
    from campaign_studio.authored import AuthoredProjects
    from campaign_studio.planning import canonical_hash

    row = db.execute(
        "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
    ).fetchone()
    if row is None:
        return
    record = json.loads(row[0])
    base = record.get("authored_base") or {
        "run_id": record.get("base_run") or record.get("planned_run"),
        "revision_id": record.get("base_revision"),
    }
    if base.get("run_id") != run_id or base.get("revision_id") is not None:
        return
    if any(plan.brief.campaign_id != campaign_id for plan in (original_plan, accepted_plan)):
        raise StorageError("The completed image settings belong to another campaign.")
    authored = copy.deepcopy(record["authored"])
    _hydrate(authored["brief"], _settings(original_plan))
    _pending_defaults(authored, record.get("default_color_conflicts", []))
    admitted = record.get("planned_authored_hash") in {
        canonical_hash(record["authored"]["brief"]),
        canonical_hash(authored["brief"]),
    }
    AuthoredProjects.reconcile_brief_fields(
        authored,
        original_plan.brief.model_dump(mode="json"),
        accepted_plan.brief.model_dump(mode="json"),
    )
    _hydrate(authored["brief"], _settings(accepted_plan))
    _pending_defaults(authored, record.get("default_color_conflicts", []))
    accepted_run_id = accepted_run_id or run_id
    record["authored_base"] = {
        "run_id": accepted_run_id,
        "revision_id": revision_id,
        "metadata_snapshot_id": metadata_snapshot_id,
    }
    if record.get("base_run") == run_id and record.get("base_revision") is None:
        record["base_run"] = accepted_run_id
        record["base_revision"] = revision_id
    if admitted and record.get("planned_run") == run_id:
        record["planned_authored_hash"] = canonical_hash(authored["brief"])
    _without_accepted_settings(authored["brief"], _settings(accepted_plan))
    record["authored"] = authored
    db.execute(
        "UPDATE campaign_drafts SET document_json=? WHERE campaign_id=?",
        (json.dumps(record, ensure_ascii=False, allow_nan=False), campaign_id),
    )


def migrate_drafts(storage):
    """Import each owned old draft once, then remove only its verified file copy."""
    with storage.connect() as db:
        campaigns = [row[0] for row in db.execute("SELECT id FROM campaigns")]
    for campaign_id in campaigns:
        project = storage.project(campaign_id)
        reference = project.draft
        with storage.connect() as db:
            if db.execute(
                "SELECT 1 FROM workspace_migrations WHERE name=?",
                (f"campaign_outputs:{campaign_id}",),
            ).fetchone():
                continue
            if not db.execute(
                "SELECT 1 FROM file_owners WHERE reference=? AND campaign_id=?",
                (reference, campaign_id),
            ).fetchone():
                continue
            path = storage.path(reference)
            row = db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if row is None:
                if not path.is_file():
                    continue
                payload = path.read_bytes()
                document = json.loads(payload)
                write(
                    storage,
                    campaign_id,
                    document,
                    db=db,
                    legacy_sha256=hashlib.sha256(payload).hexdigest(),
                )
            else:
                document = json.loads(row[0])
                if not document.get("legacy_sha256"):
                    continue
        # The database commit precedes deletion. A stopped cleanup retries here.
        with storage.connect() as db:
            record = json.loads(
                db.execute(
                    "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
                ).fetchone()[0]
            )
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != record["legacy_sha256"]:
                raise StorageError("The old campaign draft changed during its database upgrade.")
            storage.unlink(path)
        with storage.connect() as db:
            prefix = reference + "."
            for (temporary,) in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=? AND substr(reference,1,?)=?",
                (campaign_id, len(prefix), prefix),
            ).fetchall():
                if re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}\.tmp", temporary):
                    storage.unlink(storage.path(temporary), missing_ok=True)
                    db.execute("DELETE FROM file_owners WHERE reference=?", (temporary,))
            db.execute("DELETE FROM file_owners WHERE reference=?", (reference,))
            record.pop("legacy_sha256", None)
            db.execute(
                "UPDATE campaign_drafts SET document_json=? WHERE campaign_id=?",
                (json.dumps(record, ensure_ascii=False, allow_nan=False), campaign_id),
            )
        with suppress(OSError):
            path.parent.rmdir()
