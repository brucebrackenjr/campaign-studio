"""Move v0.2.0 outputs into the campaign's current output folder on first open."""

import hashlib
import json
import re
from contextlib import suppress

from campaign_studio.cleanup import Cleanup
from campaign_studio.output_files import discard_temporaries
from campaign_studio.plan_storage import dump_plan, load_plan
from campaign_studio.projects import matches_output_file, output_file, product_folders
from campaign_studio.storage import StorageError


def _copy_once(project, destination, payload):
    # A crash before the SQL cutover leaves the original intact. Only reuse
    # bytes from a prior attempt when both the owner and the contents match.
    storage = project.storage
    project.claim(destination)
    with storage.connect() as db:
        discard_temporaries(storage, db, destination)
    path = storage.path(destination)
    if path.is_file():
        if path.read_bytes() != payload:
            raise StorageError("The destination of a saved-image upgrade has changed.")
    else:
        storage.write_new(destination, payload)


def _move_images(project, plan, manifest, base, revision_id, retired, staged):
    storage = project.storage
    planned = {output.id: output for output in plan.outputs}
    folders = product_folders(plan.brief.products)
    for item in manifest["outputs"]:
        if not item["file"]:
            continue
        out = planned[item["id"]]
        expected = f"images/{out.product_id}/{out.ratio.replace(':', 'x')}/{out.market}.png"
        if item["file"] != expected:
            raise StorageError("A saved image has an unexpected location.")
        old = f"{base}/{expected}"
        relative = output_file(
            out.product_id, out.ratio, out.market, revision_id, folder=folders[out.product_id]
        )
        item["file"] = relative
        if retired:
            continue
        # Previously damaged or missing files keep their recorded hash so
        # downstream verification can still distinguish them from valid ones.
        source = storage.path(project.require(old))
        if not source.exists():
            project.claim(project.ref(f"Outputs/{relative}"))
            continue
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        key = (out.id, digest)
        if key not in staged:
            _copy_once(project, project.ref(f"Outputs/{relative}"), payload)
            staged[key] = relative
        item["file"] = staged[key]


def _migrate_campaign(state, campaign_id, pending_files):
    storage = state.storage
    project = storage.project(campaign_id)
    cleaner = Cleanup(state)
    with storage.connect() as db:
        rows = db.execute(
            "SELECT r.id,r.run_id,r.plan_ref,r.manifest_ref,r.images_retired "
            "FROM revisions r JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=? "
            "AND r.plan_json IS NULL "
            "AND r.manifest_ref LIKE '%/Revisions/%' ORDER BY r.rowid",
            (campaign_id,),
        ).fetchall()
        converted = {
            row[0]
            for row in db.execute(
                "SELECT r.id FROM revisions r JOIN runs ON r.run_id=runs.id "
                "WHERE runs.campaign_id=? AND "
                "r.manifest_ref LIKE '%/Outputs/manifest.json'",
                (campaign_id,),
            )
        }
        runs = dict(db.execute("SELECT id,plan_ref FROM runs WHERE campaign_id=?", (campaign_id,)))
        owned = {
            ref
            for (ref,) in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
            )
        }
    prefix = project.ref("Runs") + "/"
    # The ownership registry also covers recognized partial writes from an
    # interrupted v0.2.0 render that never published a database revision.
    legacy = {
        ref
        for ref in owned
        if ref.startswith(prefix)
        and re.fullmatch(r"[0-9a-f]{32}/Revisions/[0-9a-f]{32}(?:/.+)?", ref[len(prefix) :])
        and ref[len(prefix) :].split("/")[0] in runs
        and ref[len(prefix) :].split("/")[2] not in converted
    }
    if not legacy and not rows:
        return
    directories = {ref for ref in legacy if len(ref[len(prefix) :].split("/")) == 3}
    preserve = cleaner._current_inputs(campaign_id) | pending_files
    updates, aliases = [], []
    current = state.current_output(campaign_id)
    required = {current[1]} if current else set()
    draft_path = storage.path(project.draft)
    if draft_path.is_file():
        draft = json.loads(draft_path.read_bytes())
        required.update(
            (draft.get("base_revision"), draft.get("authored_base", {}).get("revision_id"))
        )
    staged = {}
    outputs = project.ref("Outputs")
    plan_ref, manifest_ref = f"{outputs}/plan.json", f"{outputs}/manifest.json"
    for rev, run, old_plan, old_manifest, retired in rows:
        base = f"{project.run(run)}/Revisions/{rev}"
        if old_plan != f"{base}/plan.json" or old_manifest != f"{base}/manifest.json":
            raise StorageError("The location of a saved image set does not match its campaign.")
        try:
            plan_bytes = storage.path(project.require(old_plan)).read_bytes()
            plan = load_plan(plan_bytes)
            manifest = json.loads(storage.path(project.require(old_manifest)).read_bytes())
            if (
                plan.brief.campaign_id != campaign_id
                or manifest.get("campaign_id") != campaign_id
                or manifest.get("revision_id") != rev
            ):
                raise StorageError("A saved image set does not match its campaign.")
        except OSError, ValueError, AttributeError:
            if not retired or rev in required or runs[run] == old_plan:
                raise
            # A retired export may still need one of its original documents.
            # Keep its evidence in place. The usual pruning step removes an
            # unreferenced retired revision and its obsolete file registrations.
            with storage.connect() as db:
                pinned = db.execute(
                    "SELECT 1 FROM package_exports WHERE revision_id=? UNION ALL "
                    "SELECT 1 FROM runs WHERE confirmed_revision=? LIMIT 1",
                    (rev, rev),
                ).fetchone()
            if pinned:
                preserve.update(ref for ref in legacy if ref == base or ref.startswith(base + "/"))
                directories.discard(base)
            continue
        preserve.update(asset.reference for asset in plan.assets.values())
        _move_images(project, plan, manifest, base, rev, retired, staged)
        if runs[run] == old_plan:
            aliases.append((dump_plan(plan), run))
        updates.append(
            (
                plan_ref,
                manifest_ref,
                dump_plan(plan),
                json.dumps(manifest, indent=2),
                sum(bool(item["file"]) for item in manifest["outputs"]),
                rev,
            )
        )
    # Validate and record exactly what can be deleted. A user-created file in a
    # former revision folder is never included, and keeps its containing folder.
    obsolete = legacy - preserve - directories
    files = cleaner._files(obsolete, include_missing=True)
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany(
            "UPDATE revisions SET plan_ref=?,manifest_ref=?,plan_json=?,manifest_json=?,"
            "image_count=? WHERE id=?",
            updates,
        )
        db.executemany("UPDATE runs SET plan_json=? WHERE id=?", aliases)
        retirement = cleaner.plan_retirement(db, campaign_id) if updates else None
        intent = (
            cleaner.record_intent(
                db,
                {
                    "kind": "migration",
                    "campaign_id": campaign_id,
                    "files": files,
                    "directories": sorted(directories),
                    "owned_directories": sorted(directories),
                },
            )
            if files or directories
            else None
        )
    for identity in (intent, retirement):
        if identity:
            cleaner._resume(identity)


def prune_empty_export_dirs(storage):
    """Remove only empty legacy ZIP directories. rmdir preserves every existing entry."""
    with storage.connect() as db:
        campaigns = [cid for (cid,) in db.execute("SELECT id FROM campaigns")]
    for cid in campaigns:
        project = storage.project(cid)
        for directory in ("Exports/archives", "Exports"):
            # Unknown files, folders, aliases, and inaccessible paths are preserved.
            with suppress(OSError, StorageError):
                storage.path(project.ref(directory)).rmdir()


def _migrate_product_folders(state, campaign_id, pending_files):
    """Upgrade the rc2 product-ID directories once, preserving the actual PNG bytes."""
    storage = state.storage
    project = storage.project(campaign_id)
    cleaner = Cleanup(state)
    with storage.connect() as db:
        rows = db.execute(
            "SELECT r.id,r.plan_json,r.manifest_json,r.images_retired FROM revisions r "
            "JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=? "
            "AND r.plan_json IS NOT NULL ORDER BY r.rowid",
            (campaign_id,),
        ).fetchall()
        withheld = db.execute(
            "SELECT creative_id,revision_id,file FROM withheld_output_sources WHERE campaign_id=?",
            (campaign_id,),
        ).fetchall()
    if not rows:
        return
    current = state.current_output(campaign_id)
    products, records = {}, []
    preserve = cleaner._current_inputs(campaign_id) | pending_files
    for revision, plan_json, manifest_json, retired in rows:
        plan = load_plan(plan_json)
        manifest = json.loads(manifest_json)
        if (
            plan.brief.campaign_id != campaign_id
            or manifest.get("campaign_id") != campaign_id
            or manifest.get("revision_id") != revision
        ):
            raise StorageError("The image folder upgrade found a different campaign.")
        products.update((product.id, product) for product in plan.brief.products)
        preserve.update(asset.reference for asset in plan.assets.values())
        records.append((revision, plan, manifest, retired))
    current_manifest = next(
        (manifest for revision, _, manifest, _ in records if current and revision == current[1]),
        None,
    )
    folders = product_folders(products.values(), current_manifest)
    needed = {file for _, _, file in withheld}
    replacements, updates = {}, []
    for revision, plan, manifest, retired in records:
        planned = {output.id: output for output in plan.outputs}
        changed = False
        for item in manifest["outputs"]:
            old = item.get("file")
            if not old:
                continue
            output = planned[item["id"]]
            if not matches_output_file(old, output.product_id, output.ratio, output.market):
                raise StorageError("A saved image has an unexpected location.")
            pieces = old.split("/")
            pieces[1] = folders[output.product_id]
            new = "/".join(pieces)
            if old == new:
                continue
            changed = True
            item["file"] = new
            replacements[old] = new
            if not retired:
                needed.add(old)
        if changed:
            updates.append((json.dumps(manifest, indent=2), revision))
    if not updates:
        return
    obsolete = set()
    for old, new in replacements.items():
        if old not in needed:
            continue
        source_ref = project.require(project.ref(f"Outputs/{old}"))
        destination = project.ref(f"Outputs/{new}")
        source = storage.path(source_ref)
        if source.exists():
            _copy_once(project, destination, source.read_bytes())
        else:
            # Preserve damage as damage; missing images must not become verified images.
            project.claim(destination)
        obsolete.add(source_ref)
    files = cleaner._files(obsolete - preserve, include_missing=True)
    with storage.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("UPDATE revisions SET manifest_json=? WHERE id=?", updates)
        db.executemany(
            "UPDATE withheld_output_sources SET file=? WHERE campaign_id=? AND creative_id=?",
            (
                (replacements[file], campaign_id, creative)
                for creative, _, file in withheld
                if file in replacements
            ),
        )
        intent = (
            cleaner.record_intent(
                db, {"kind": "migration", "campaign_id": campaign_id, "files": files}
            )
            if files
            else None
        )
    if intent:
        cleaner._resume(intent)


def _remove_output_sidecars(state, campaign_id, pending_files):
    """Remove only registered, redundant output documents after checking SQLite."""
    storage = state.storage
    project = storage.project(campaign_id)
    outputs = project.ref("Outputs")
    documents = {f"{outputs}/{name}.json" for name in ("plan", "manifest")}
    with storage.connect() as db:
        owned = {
            reference
            for (reference,) in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
            )
            if reference in documents
            or any(
                re.fullmatch(re.escape(document) + r"\.[0-9a-f]{32}\.tmp", reference)
                for document in documents
            )
        }
        if not owned:
            return
        current = state._current_output(db, campaign_id)
        row = db.execute(
            "SELECT r.id,r.plan_json,r.manifest_json FROM revisions r JOIN runs "
            "ON runs.id=r.run_id WHERE runs.campaign_id=? "
            "AND (? IS NULL OR r.id=?) ORDER BY r.rowid DESC LIMIT 1",
            (campaign_id, current[1] if current else None, current[1] if current else None),
        ).fetchone()
    if row is None:
        raise StorageError("Restore the campaign's saved database records before upgrading it.")
    revision, plan_json, manifest_json = row
    plan = state._checked_plan(project, plan_json)
    manifest = json.loads(manifest_json)
    planned = {output.id: output for output in plan.outputs}
    if (
        manifest.get("campaign_id") != campaign_id
        or manifest.get("revision_id") != revision
        or sorted(item["id"] for item in manifest["outputs"]) != sorted(planned)
        or any(
            item["file"]
            and not matches_output_file(
                item["file"],
                planned[item["id"]].product_id,
                planned[item["id"]].ratio,
                planned[item["id"]].market,
            )
            for item in manifest["outputs"]
        )
    ):
        raise StorageError("The saved image settings do not match this campaign.")
    cleaner = Cleanup(state)
    files = cleaner._files(owned - pending_files, include_missing=True)
    if files:
        with storage.connect() as db:
            intent = cleaner.record_intent(
                db, {"kind": "migration", "campaign_id": campaign_id, "files": files}
            )
        cleaner._resume(intent)


def migrate_outputs(state):
    """Run under the runner and draft locks, before allowing new edits."""
    cleaner = Cleanup(state)
    with state.storage.connect() as db:
        pending = db.execute(
            "SELECT id,kind,campaign_id,value_json FROM cleanup_intents ORDER BY rowid"
        ).fetchall()
    skip = set()
    reserved = {}
    for identity, kind, campaign_id, document in pending:
        result = cleaner._resume(identity)
        if kind == "campaign":
            skip.add(campaign_id)  # An approved deletion retains its original, exact inventory.
        elif result["state"] != "complete":
            reserved.setdefault(campaign_id, set()).update(
                item["reference"] for item in json.loads(document)["files"]
            )
    with state.storage.connect() as db:
        initial = db.execute(
            "SELECT 1 FROM workspace_migrations WHERE name='campaign_outputs'"
        ).fetchone()
        if initial:
            campaigns = [row[0] for row in db.execute("SELECT id FROM campaigns")]
        else:
            campaigns = [
                row[0]
                for row in db.execute(
                    "SELECT c.id FROM campaigns c JOIN workspace_migrations m "
                    "ON m.name='campaign_outputs:' || c.id"
                )
            ]
    upgraded = []
    for campaign_id in campaigns:
        if campaign_id in skip:
            continue
        marker = f"campaign_outputs:{campaign_id}"
        try:
            _migrate_campaign(state, campaign_id, reserved.get(campaign_id, set()))
            _migrate_product_folders(state, campaign_id, reserved.get(campaign_id, set()))
            _remove_output_sidecars(state, campaign_id, reserved.get(campaign_id, set()))
        except OSError, ValueError, KeyError, TypeError, AttributeError:
            # A campaign keeps its original files and can retry after a restore.
            # Healthy campaigns can complete their own independent cutovers.
            with state.storage.connect() as db:
                db.execute("INSERT OR IGNORE INTO workspace_migrations VALUES (?)", (marker,))
        else:
            with state.storage.connect() as db:
                db.execute("DELETE FROM workspace_migrations WHERE name=?", (marker,))
            upgraded.append(campaign_id)
    return upgraded
