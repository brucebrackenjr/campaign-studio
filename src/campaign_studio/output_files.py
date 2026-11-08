"""Recover the exact files registered by an interrupted output save."""

import json
import re


def discard_temporaries(storage, db, reference):
    # write_new and write record each temporary before creating it. Match that
    # ownership record exactly, including campaign names with brackets.
    prefix = reference + "."
    temporaries = db.execute(
        "SELECT reference FROM file_owners WHERE substr(reference,1,?)=?",
        (len(prefix), prefix),
    ).fetchall()
    for (temporary,) in temporaries:
        if re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}\.tmp", temporary):
            storage.unlink(storage.path(temporary), missing_ok=True)
            db.execute("DELETE FROM file_owners WHERE reference=?", (temporary,))


def discard_unpublished_outputs(storage, revision_id=None):
    with storage.connect() as db:
        rows = db.execute(
            "SELECT reference FROM staging_output_files WHERE ? IS NULL OR revision_id=?",
            (revision_id, revision_id),
        ).fetchall()
        for (reference,) in rows:
            discard_temporaries(storage, db, reference)
            storage.unlink(storage.path(reference), missing_ok=True)
            db.execute("DELETE FROM file_owners WHERE reference=?", (reference,))


def sync_draft_comparisons(state, db, project, draft, retiring=()):
    """Retain only fields needed to merge an outstanding draft with an image Apply."""
    owners = db.execute(
        "SELECT id,draft_comparisons FROM runs WHERE campaign_id=? ORDER BY rowid DESC",
        (project.campaign_id,),
    ).fetchall()
    if not owners:
        return
    run = owners[0][0]
    active = state._current_output(db, project.campaign_id)
    required = set()
    if draft and active:
        base = draft.get("authored_base") or {}
        if (
            draft.get("authored")
            and base.get("run_id")
            and (base.get("run_id"), base.get("revision_id")) != active
        ):
            receipt = db.execute(
                "SELECT json_extract(manifest_json,'$.edit_receipt.authored_base') FROM "
                "revisions WHERE id=?",
                (active[1],),
            ).fetchone()[0]
            applied = json.loads(receipt) if receipt else {}
            # An unsaved draft can also be persisted by a logo or rule operation
            # that advances its token. Its original comparison then applies again.
            required.add(base.get("revision_id"))
            required.add(
                applied.get("revision_id")
                if applied and applied.get("version") == draft.get("version")
                else base.get("revision_id")
            )
        if (
            draft.get("planned_run") != run
            and draft.get("base_run") == run
            and state._effective_revision(db, run)
        ):
            required.add(draft.get("base_revision"))
    required.discard(None)
    saved = {
        key: value
        for _, old in owners
        if old
        for key, value in json.loads(old).items()
        if key in required
    }
    for revision in required.intersection(retiring):
        plan = db.execute("SELECT plan_json FROM revisions WHERE id=?", (revision,)).fetchone()
        if plan and plan[0]:
            brief = json.loads(plan[0])["brief"]
            saved[revision] = {
                "brand": {k: brief["brand"].get(k) for k in ("logo_path", "logo_provenance")},
                "markets": brief["markets"],
                "products": [
                    {k: p.get(k, {}) for k in ("id", "framing", "copy_overrides")}
                    for p in brief["products"]
                ],
            }
    value = json.dumps(saved) if saved else None
    for owner, old in owners:
        desired = value if owner == run else None
        if old != desired:
            db.execute("UPDATE runs SET draft_comparisons=? WHERE id=?", (desired, owner))


def prune_retired_edits(state, db, project):
    """Delete superseded edits once their output, retry, and handoff consumers are done."""
    campaign = project.campaign_id
    active = state._current_output(db, campaign)
    current = active[1] if active else None
    if current is None:
        latest = db.execute(
            "SELECT id FROM runs WHERE campaign_id=? ORDER BY rowid DESC LIMIT 1", (campaign,)
        ).fetchone()
        current = state._effective_revision(db, latest[0]) if latest else None
    # Earlier runs once pointed at their last product image indefinitely. The
    # active campaign and preserved legacy exports carry the remaining references.
    db.execute(
        "UPDATE runs SET current_revision=NULL WHERE campaign_id=? AND current_revision IS NOT ? "
        "AND current_revision IN (SELECT id FROM revisions WHERE images_retired=1)",
        (campaign, current),
    )
    rows = db.execute(
        "SELECT r.id FROM revisions r JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=? "
        "AND (r.images_retired=1 OR r.image_count=0) AND r.id IS NOT ? "
        "AND r.id IS NOT runs.current_revision AND r.id IS NOT runs.confirmed_revision "
        "AND NOT EXISTS (SELECT 1 FROM package_exports p WHERE p.revision_id=r.id) "
        "AND NOT EXISTS (SELECT 1 FROM withheld_output_sources w WHERE w.revision_id=r.id)",
        (campaign, current),
    ).fetchall()
    from campaign_studio.campaign_drafts import read as read_draft

    draft = read_draft(state.storage, campaign, db=db)
    sync_draft_comparisons(state, db, project, draft, {r[0] for r in rows})
    db.executemany("DELETE FROM creative_metadata WHERE revision_id=?", rows)
    db.executemany("DELETE FROM revisions WHERE id=?", rows)
