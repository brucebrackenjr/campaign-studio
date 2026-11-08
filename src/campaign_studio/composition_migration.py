"""Upgrade saved campaigns to one editable composition, then retire derived PNGs.

The SQLite transaction commits the complete editing instructions and an exact
cleanup inventory together. File removal happens afterward and can be retried.
Original inputs, source photographs, and previously downloaded packages survive.
"""

import copy
import hashlib
import json
from uuid import uuid4

from campaign_studio.campaign_drafts import read, write
from campaign_studio.cleanup import Cleanup
from campaign_studio.plan_storage import completed_run, dump_completed, load_plan
from campaign_studio.projects import matches_output_file
from campaign_studio.storage import StorageError


def _fill_missing(target, defaults):
    """Keep current explicit choices, including null values and empty collections."""
    for key, value in defaults.items():
        if key not in target:
            target[key] = copy.deepcopy(value)


def _authored(document, raw, plan):
    # The old read view hydrates saved image settings. Its current Campaign text
    # is authoritative; a validated old production plan must not replace it.
    authored = copy.deepcopy(document.get("authored", {"brief": document["value"]}))
    if raw and isinstance(raw.get("authored"), dict):
        authored = copy.deepcopy(raw["authored"])
    if plan is not None:
        prior = {product.id: product.model_dump(mode="json") for product in plan.brief.products}
        for product in authored["brief"].get("products", []):
            if not isinstance(product, dict) or product.get("id") not in prior:
                continue
            accepted = prior[product["id"]]
            _fill_missing(product, {key: accepted[key] for key in ("framing", "copy_overrides")})
        # Honor an explicit legacy request to return particular text to the
        # campaign default, after recovering its stored geometry.
        from campaign_studio.campaign_drafts import _pending_defaults

        _pending_defaults(authored, (raw or {}).get("default_color_conflicts", []))
    return authored


def _verify_sources(state, records):
    for record in records.values():
        asset = record["asset"]
        reference = asset["reference"]
        state.storage.project(state.run(record["run_id"])["campaign_id"]).require(reference)
        if (
            hashlib.sha256(state.storage.path(reference).read_bytes()).hexdigest()
            != asset["sha256"]
        ):
            raise StorageError("A source image changed. Restore it before upgrading this campaign.")


def _retirement(state, campaign_id, protected):
    """Inventory only registered files named by an actual legacy image manifest."""
    storage = state.storage
    project = storage.project(campaign_id)
    with storage.connect() as db:
        rows = db.execute(
            "SELECT r.id,r.manifest_ref,r.manifest_json,r.images_retired FROM revisions r "
            "JOIN runs ON r.run_id=runs.id WHERE runs.campaign_id=?",
            (campaign_id,),
        ).fetchall()
        owned = {
            ref
            for (ref,) in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
            )
        }
    references = set()
    for _, manifest_ref, payload, retired in rows:
        if payload is None:
            if retired:
                # A damaged, already retired package record is not the current
                # campaign. Leave its evidence untouched for its legacy reader.
                continue
            raise StorageError("Finish upgrading the previous campaign format before continuing.")
        base = manifest_ref.rsplit("/", 1)[0]
        for item in json.loads(payload)["outputs"]:
            relative = item.get("file")
            if not relative:
                continue
            if not matches_output_file(relative, item["product_id"], item["ratio"], item["market"]):
                raise StorageError("A saved campaign image has an unexpected location.")
            reference = project.check(f"{base}/{relative}")
            if reference in protected or reference not in owned:
                continue
            path = storage.path(reference)
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                # A producer may have replaced a file outside Motif. It is no
                # longer the derived file we can safely retire.
                continue
            references.add(reference)
    cleaner = Cleanup(state)
    return {
        "kind": "migration",
        "campaign_id": campaign_id,
        "files": cleaner._files(references, include_missing=True),
        "directories": [],
    }


def _retire_records(state, db, campaign_id):
    # Archived v0.2.0 packages are already user deliveries. Keep their old
    # records for compatibility, but no new save or download creates any.
    retained = {
        row[0]
        for row in db.execute(
            "SELECT p.revision_id FROM package_exports p JOIN runs ON runs.id=p.run_id "
            "WHERE runs.campaign_id=?",
            (campaign_id,),
        )
    }
    rows = db.execute(
        "SELECT id,plan_json,state FROM runs WHERE campaign_id=?", (campaign_id,)
    ).fetchall()
    for run_id, payload, status in rows:
        if status == "complete" and completed_run(payload) is None:
            attempted = {
                row[0]
                for row in db.execute("SELECT product_id FROM attempts WHERE run_id=?", (run_id,))
            }
            db.execute(
                "UPDATE runs SET plan_json=? WHERE id=?",
                (
                    dump_completed(load_plan(payload), attempted),
                    run_id,
                ),
            )
    db.execute(
        "UPDATE runs SET current_revision=NULL,confirmed_revision=NULL,draft_comparisons=NULL "
        "WHERE campaign_id=?",
        (campaign_id,),
    )
    db.execute("DELETE FROM withheld_output_sources WHERE campaign_id=?", (campaign_id,))
    revisions = [
        row[0]
        for row in db.execute(
            "SELECT r.id FROM revisions r JOIN runs ON r.run_id=runs.id WHERE runs.campaign_id=?",
            (campaign_id,),
        )
    ]
    for revision in revisions:
        if revision in retained:
            db.execute("UPDATE revisions SET images_retired=1 WHERE id=?", (revision,))
            continue
        db.execute("DELETE FROM creative_metadata WHERE revision_id=?", (revision,))
        db.execute("DELETE FROM revisions WHERE id=?", (revision,))


def migrate_current(state, campaign_id):
    """Convert once; normal reads and autosaves never create migration history."""
    from campaign_studio.compositions import source_records

    storage = state.storage
    document = read(storage, campaign_id)
    if document is not None and document.get("composition_schema") == 1:
        return document
    with storage.exclusive_run(), storage.exclusive_draft():
        document = read(storage, campaign_id)
        if document is not None and document.get("composition_schema") == 1:
            return document
        current = state.current_output(campaign_id)
        plan = state.revision_plan(state.revision(current[1])) if current else None
        if document is None:
            if plan is None:
                return None
            document = {"value": plan.brief.model_dump(mode="json")}
        with storage.connect() as db:
            raw_row = db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        raw = json.loads(raw_row[0]) if raw_row else None
        document["authored"] = _authored(document, raw, plan)
        document["source_assets"] = source_records(state, current[0], plan) if current else {}
        _verify_sources(state, document["source_assets"])
        document["creative_metadata"] = (
            state.metadata_snapshot(current[1])["creatives"] if current else {}
        )
        document["composition_schema"] = 1
        document["version"] = (
            uuid4().hex
        )  # Old browser tabs cannot overwrite the new representation.
        document["authored_base"] = {
            "run_id": current[0] if current else document.get("planned_run"),
            "revision_id": None,
            "metadata_snapshot_id": None,
        }
        document.pop("base_revision", None)
        protected = Cleanup(state)._current_inputs(campaign_id)
        protected.update(
            record["asset"]["reference"] for record in document["source_assets"].values()
        )
        inventory = _retirement(state, campaign_id, protected)
        with storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            write(storage, campaign_id, document, db=db)
            _retire_records(state, db, campaign_id)
            intent = Cleanup.record_intent(db, inventory) if inventory["files"] else None
        if intent:
            Cleanup(state)._resume(intent)
        return read(storage, campaign_id)


def migrate_compositions(state):
    """Upgrade campaigns independently; a damaged campaign does not stop the others."""
    with state.storage.connect() as db:
        campaigns = [row[0] for row in db.execute("SELECT id FROM campaigns")]
    for campaign_id in campaigns:
        try:
            migrate_current(state, campaign_id)
        except OSError, ValueError, KeyError, TypeError:
            # Original records and files remain available for repair. Opening
            # this campaign retries and reports the specific error to the UI.
            continue
