"""Move owned production-plan documents into SQLite before retiring their files."""

import json
import re

from campaign_studio.cleanup import Cleanup
from campaign_studio.plan_storage import completed_run, dump_plan
from campaign_studio.storage import StorageError


def _migrate_campaign(state, campaign_id):
    storage = state.storage
    project = storage.project(campaign_id)
    cleaner = Cleanup(state)
    with storage.connect() as db:
        runs = db.execute(
            "SELECT id,plan_ref,plan_json FROM runs WHERE campaign_id=?", (campaign_id,)
        ).fetchall()
        revisions = db.execute(
            "SELECT r.id,r.plan_json FROM revisions r JOIN runs ON runs.id=r.run_id "
            "WHERE runs.campaign_id=?",
            (campaign_id,),
        ).fetchall()
        owned = {
            reference
            for (reference,) in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
            )
        }
        pending = {
            item["reference"]
            for (payload,) in db.execute(
                "SELECT value_json FROM cleanup_intents WHERE campaign_id=?", (campaign_id,)
            )
            for item in json.loads(payload)["files"]
        }
    run_updates, revision_updates, documents = [], [], set()
    for run_id, reference, payload in runs:
        if payload is None:
            # The former retry pointer may name an accepted revision document.
            expected = (
                rf"{re.escape(project.run(run_id))}/(?:plan|retry-plan)\.json|"
                rf"{re.escape(project.run(run_id))}/Revisions/[0-9a-f]{{32}}/plan\.json"
            )
            if reference not in owned or not re.fullmatch(expected, reference):
                raise StorageError("The production plan is not an owned campaign document.")
            payload = storage.path(project.require(reference)).read_bytes()
        receipt = completed_run(payload)
        if receipt is not None:
            if receipt.campaign_id != campaign_id:
                raise StorageError("The generation receipt belongs to another campaign.")
        else:
            plan = state._checked_plan(project, payload)
            run_updates.append((dump_plan(plan), run_id))
        documents.update(
            {
                reference,
                f"{project.run(run_id)}/plan.json",
                f"{project.run(run_id)}/retry-plan.json",
            }
        )
    for revision_id, payload in revisions:
        if payload is None:
            # A damaged historical output may intentionally remain available only
            # through its legacy export. Its output migration owns that recovery.
            continue
        plan = state._checked_plan(project, payload)
        revision_updates.append((dump_plan(plan), revision_id))
    obsolete = {
        reference
        for reference in owned
        if reference in documents
        or any(
            re.fullmatch(re.escape(document) + r"\.[0-9a-f]{32}\.tmp", reference)
            for document in documents
        )
    }
    files = cleaner._files(obsolete - pending, include_missing=True)
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("UPDATE runs SET plan_json=? WHERE id=?", run_updates)
        db.executemany("UPDATE revisions SET plan_json=? WHERE id=?", revision_updates)
        intent = (
            cleaner.record_intent(
                db, {"kind": "migration", "campaign_id": campaign_id, "files": files}
            )
            if files
            else None
        )
    # Once committed, interrupted unlinking cannot remove the only copy of a plan.
    if intent:
        cleaner._resume(intent)


def migrate_plans(state):
    """Run after output conversion and before draft conversion, under both workspace locks."""
    with state.storage.connect() as db:
        campaigns = [
            cid
            for (cid,) in db.execute(
                "SELECT id FROM campaigns WHERE NOT EXISTS (SELECT 1 FROM workspace_migrations "
                "WHERE name='campaign_outputs:' || campaigns.id) AND NOT EXISTS "
                "(SELECT 1 FROM cleanup_intents WHERE campaign_id=campaigns.id AND kind='campaign')"
            )
        ]
    upgraded = []
    for campaign_id in campaigns:
        try:
            _migrate_campaign(state, campaign_id)
        except OSError, ValueError, KeyError, TypeError:
            with state.storage.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO workspace_migrations VALUES (?)",
                    (f"campaign_outputs:{campaign_id}",),
                )
        else:
            upgraded.append(campaign_id)
    return upgraded


def compact_completed_runs(state):
    """Upgrade completed inputs only after their drafts and accepted plans are readable."""
    with state.storage.connect() as db:
        campaigns = [
            cid
            for (cid,) in db.execute(
                "SELECT DISTINCT campaign_id FROM runs WHERE state='complete' AND NOT EXISTS "
                "(SELECT 1 FROM workspace_migrations WHERE name='campaign_outputs:' || "
                "runs.campaign_id) AND NOT EXISTS (SELECT 1 FROM cleanup_intents WHERE "
                "campaign_id=runs.campaign_id AND kind='campaign')"
            )
        ]
    upgraded = []
    for campaign_id in campaigns:
        try:
            with state.storage.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                runs = db.execute(
                    "SELECT id FROM runs WHERE campaign_id=? AND state='complete' ORDER BY rowid",
                    (campaign_id,),
                ).fetchall()
                for (run_id,) in runs:
                    state._complete_run(db, run_id)
        except OSError, ValueError, KeyError, TypeError:
            with state.storage.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO workspace_migrations VALUES (?)",
                    (f"campaign_outputs:{campaign_id}",),
                )
        else:
            upgraded.append(campaign_id)
    return upgraded
