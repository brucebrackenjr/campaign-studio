"""Deliberate owned-file cleanup and retirement of older image sets.

The database commits an exact file list before unlinking campaign content.
Empty campaign directories may also contain disposable Finder metadata.
"""

import hashlib
import json
import os
import sqlite3
import stat
import threading
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from campaign_studio.brief import valid_campaign_id
from campaign_studio.local_files import is_link
from campaign_studio.planning import canonical_hash
from campaign_studio.projects import matches_output_file
from campaign_studio.state import StateError, timestamp
from campaign_studio.storage import StorageError


def _finder_metadata(path):
    """Recognize Finder's folder settings, without following aliases."""
    if path.name != ".DS_Store":
        return False
    try:
        info = path.lstat()
        if is_link(info) or not stat.S_ISREG(info.st_mode):
            return False
        with path.open("rb") as stream:
            return stream.read(8) == b"\x00\x00\x00\x01Bud1"
    except FileNotFoundError:
        return False


def pending_campaign(db, campaign_id):
    return (
        db.execute(
            "SELECT 1 FROM cleanup_intents WHERE campaign_id=? AND kind='campaign'",
            (campaign_id,),
        ).fetchone()
        is not None
    )


UPGRADE_RECOVERY_MESSAGE = (
    "This campaign could not finish upgrading its saved images. Check that its files are "
    "present and writable, restore any missing files from your Workspace backup, and "
    "restart Motif to retry."
)


def upgrade_pending(db, campaign_id):
    return (
        db.execute(
            "SELECT 1 FROM workspace_migrations WHERE name=?", (f"campaign_outputs:{campaign_id}",)
        ).fetchone()
        is not None
    )


def require_campaign_registered(storage, campaign_id):
    if not valid_campaign_id(campaign_id):
        raise StateError("We couldn’t find this campaign in your workspace.", code="not_found")
    with storage.connect() as db:
        if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
            raise StateError("We couldn’t find this campaign in your workspace.", code="not_found")


def require_campaign_available(storage, campaign_id, *, registered=True):
    if registered:
        require_campaign_registered(storage, campaign_id)
    elif not valid_campaign_id(campaign_id):
        raise StateError("We couldn’t find this campaign in your workspace.", code="not_found")
    with storage.connect() as db:
        if pending_campaign(db, campaign_id):
            raise StateError("This campaign is being deleted. Retry its pending cleanup.")
        if upgrade_pending(db, campaign_id):
            raise StateError(UPGRADE_RECOVERY_MESSAGE, code="workspace_upgrade")


class Cleanup:
    def __init__(self, state):
        self.state = state
        self.storage = state.storage

    def plan_upload_retirement(self, db, project, detached):
        """Queue only registered uploads detached by this accepted authored save."""
        candidates = []
        for reference in detached:
            if (
                not isinstance(reference, str)
                or not db.execute(
                    "SELECT 1 FROM uploads u JOIN file_owners f ON f.reference=u.reference "
                    "WHERE u.reference=? AND f.campaign_id=?",
                    (reference, project.campaign_id),
                ).fetchone()
            ):
                continue
            # This excludes originals, run inputs, generated sources, and receipts.
            project.upload_name(reference)
            candidates.append(reference)
        files = self._files(candidates, include_missing=True)
        if not files:
            return None
        return self.record_intent(
            db, {"kind": "upload", "campaign_id": project.campaign_id, "files": files}
        )

    def resume_upload(self, identity):
        # A save has already committed and released its draft lock. Never acquire
        # the run lock from inside that lock or wait for an in-flight production.
        guard = self.state.__dict__.setdefault("delivery_guard", threading.RLock())
        if not guard.acquire(blocking=False):
            return {"state": "pending", "cleanup_id": identity}
        try:
            with self.storage.exclusive_run(), self.storage.exclusive_draft():
                return self._resume(identity)
        except OSError, ValueError, sqlite3.Error:
            return {"state": "pending", "cleanup_id": identity}
        finally:
            guard.release()

    def _referenced_uploads(self, references):
        """Exact references veto deletion; document text never authorizes it."""
        retained = set()
        with self.storage.connect() as db:
            for table, column in (
                ("campaign_drafts", "document_json"),
                ("runs", "plan_json"),
                ("runs", "draft_comparisons"),
                ("revisions", "plan_json"),
                ("revisions", "manifest_json"),
                ("creative_metadata", "value_json"),
            ):
                for (payload,) in db.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
                ):
                    # Unknown/corrupt saved data is not evidence that a file is unused.
                    value = json.loads(payload)
                    pending = [value]
                    while pending:
                        item = pending.pop()
                        if isinstance(item, dict):
                            pending.extend(item.values())
                        elif isinstance(item, list):
                            pending.extend(item)
                        elif isinstance(item, str) and item in references:
                            retained.add(item)
            for reference in references - retained:
                if db.execute(
                    "SELECT 1 FROM attempts WHERE source_ref=? OR response_ref=? "
                    "UNION ALL SELECT 1 FROM package_exports WHERE reference=? LIMIT 1",
                    (reference, reference, reference),
                ).fetchone():
                    retained.add(reference)
        return retained

    def _prepare_upload_retirement(self, identity, inventory):
        references = {item["reference"] for item in inventory["files"]}
        retained = self._referenced_uploads(references)
        # A legacy external plan may still use an original upload. Keep it until
        # that record is migrated, rather than infer its contents from a path.
        with self.storage.connect() as db:
            if db.execute(
                "SELECT 1 FROM runs WHERE campaign_id=? AND plan_json IS NULL "
                "UNION ALL SELECT 1 FROM revisions r JOIN runs n ON n.id=r.run_id "
                "WHERE n.campaign_id=? AND r.plan_json IS NULL LIMIT 1",
                (inventory["campaign_id"], inventory["campaign_id"]),
            ).fetchone():
                retained.update(references)
            if retained:
                inventory["files"] = [
                    item for item in inventory["files"] if item["reference"] not in retained
                ]
                db.execute(
                    "UPDATE cleanup_intents SET value_json=? WHERE id=?",
                    (json.dumps(inventory), identity),
                )
        # Export admission uses the same guard as cleanup. This also covers the
        # interval after its snapshot is read but before source bytes are copied.
        for job in self.state.__dict__.get("delivery_jobs", {}).values():
            if not job.closed and any(
                asset.reference in references - retained
                for asset in (
                    *job.snapshot["plan"].assets.values(),
                    *job.snapshot["sources"].values(),
                )
            ):
                raise StorageError(
                    "A download still uses this detached image. Retry cleanup later."
                )

    def _campaign(self, campaign_id):
        require_campaign_registered(self.storage, campaign_id)
        with self.storage.connect() as db:
            if pending_campaign(db, campaign_id):
                raise StateError("This campaign is being deleted. Retry its pending cleanup.")
            runs = db.execute("SELECT id FROM runs WHERE campaign_id=?", (campaign_id,)).fetchall()
        # A display name is optional recovery metadata, never deletion authority.
        # The registered file inventory and its confirmation token still decide
        # exactly which files can be removed when a saved draft is damaged.
        name = f"Campaign {campaign_id[:8]}"
        try:
            from campaign_studio.campaign_drafts import read as read_draft

            document = read_draft(self.storage, campaign_id)
            value = document or {}
            authored = value.get("authored", {}).get("brief")
            if isinstance(authored, dict):
                name = authored.get("name") or "Untitled campaign"
            elif document:
                name = value.get("value", value).get("name") or "Untitled campaign"
            elif runs:
                name = self.state.plan(runs[-1][0]).brief.name
        except ValueError, KeyError, TypeError, AttributeError, OSError:
            pass
        return [row[0] for row in runs], str(name or "Untitled campaign")

    def _files(self, references, *, include_missing=False):
        found = []
        for reference in sorted(set(references)):
            path = self.storage.path(reference)
            try:
                info = path.lstat()
            except FileNotFoundError:
                if include_missing:
                    found.append({"reference": reference, "bytes": 0, "sha256": None})
                continue
            if not stat.S_ISREG(info.st_mode):
                raise StorageError("Cleanup found an unsupported file. Review its location.")
            found.append(
                {
                    "reference": reference,
                    "bytes": info.st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return found

    def _current_inputs(self, campaign_id):
        from campaign_studio.authored import attachment_paths
        from campaign_studio.campaign_drafts import read as read_draft

        document = read_draft(self.storage, campaign_id)
        if document is None:
            return set()
        brief = document["value"]
        inputs = set(
            ref
            for ref in attachment_paths(document.get("authored", {"brief": brief}))
            if isinstance(ref, str)
        )
        inputs.update(
            master["path"] for master in (brief.get("replay") or {}).get("sources", {}).values()
        )
        inputs.update(
            record["asset"]["reference"]
            for record in document.get("source_assets", {}).values()
            if isinstance(record, dict)
            and isinstance(record.get("asset"), dict)
            and isinstance(record["asset"].get("reference"), str)
        )
        return inputs

    def _registered(self, campaign_id):
        project = self.storage.project(campaign_id)
        with self.storage.connect() as db:
            return [
                project.check(row[0])
                for row in db.execute(
                    "SELECT reference FROM file_owners WHERE campaign_id=? ORDER BY reference",
                    (campaign_id,),
                )
            ]

    def _record_fingerprint(self, campaign_id):
        with self.storage.connect() as db:
            rows = {}
            for table in ("runs", "attempts", "revisions", "creative_metadata", "package_exports"):
                if table == "runs":
                    query = "SELECT * FROM runs WHERE campaign_id=? ORDER BY rowid"
                elif table == "creative_metadata":
                    query = (
                        "SELECT m.* FROM creative_metadata m JOIN revisions r ON "
                        "r.id=m.revision_id "
                        "JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=? ORDER BY m.rowid"
                    )
                else:
                    query = (
                        f"SELECT t.* FROM {table} t JOIN runs ON runs.id=t.run_id "
                        "WHERE runs.campaign_id=? ORDER BY t.rowid"
                    )
                rows[table] = db.execute(query, (campaign_id,)).fetchall()
            rows["owners"] = db.execute(
                "SELECT * FROM file_owners WHERE campaign_id=? ORDER BY reference", (campaign_id,)
            ).fetchall()
            rows["draft"] = db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
        return canonical_hash(rows)

    def _campaign_inventory(self, campaign_id):
        runs, name = self._campaign(campaign_id)
        project = self.storage.project(campaign_id)
        project_root = self.storage.path(f"Campaigns/{project.folder}")
        references = self._registered(campaign_id)
        directories = {project_root}
        # Removing the last managed file can leave one of these application
        # directories with only Finder metadata. Do not treat it as user content.
        for directory_name in ("Brief", "Assets", "Runs", "Outputs", "Exports"):
            path = project_root / directory_name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode) and not is_link(info):
                directories.add(path)
        leaves = []
        for ref in references:
            path = self.storage.path(ref)
            if path.is_dir():
                directories.add(path)
            else:
                leaves.append(ref)
            directories.update(
                parent for parent in path.parents if parent.is_relative_to(project_root)
            )
        managed = set(references)
        remaining = 0
        if project_root.is_dir():

            def unreadable(error):
                raise error

            for directory, names, filenames in os.walk(
                project_root, onerror=unreadable, followlinks=False
            ):
                aliases = [name for name in names if is_link((Path(directory) / name).lstat())]
                names[:] = [name for name in names if name not in aliases]
                other = [*filenames, *aliases]
                remaining += sum(
                    (Path(directory) / name).relative_to(self.storage.root).as_posix()
                    not in managed
                    and not (
                        Path(directory) in directories and _finder_metadata(Path(directory) / name)
                    )
                    for name in other
                )
        return {
            "kind": "campaign",
            "unmanaged_file_count": remaining,
            "campaign_id": campaign_id,
            "name": name,
            "runs": runs,
            "files": self._files(leaves),
            "directories": sorted(p.relative_to(self.storage.root).as_posix() for p in directories),
            "record_fingerprint": self._record_fingerprint(campaign_id),
        }

    @staticmethod
    def _public(inventory):
        files = inventory["files"]
        images = [f["reference"] for f in files if "/Outputs/images/" in f["reference"]]
        packages = {
            f["reference"].split("/Exports/", 1)[1].split("/")[0]
            if "/archives/" not in f["reference"]
            else f["reference"]
            for f in files
            if ("/Exports/" in f["reference"])
        }
        sources = {
            f["reference"]
            for f in files
            if f["reference"].lower().endswith((".png", ".jpg", ".jpeg"))
            and f["reference"] not in images
            and "/Exports/" not in f["reference"]
        }
        return {
            "kind": inventory["kind"],
            "campaign_id": inventory["campaign_id"],
            "name": inventory.get("name"),
            "token": canonical_hash(inventory),
            "file_count": len(files),
            "removable_bytes": sum(x["bytes"] for x in files),
            "unmanaged_file_count": inventory.get("unmanaged_file_count", 0),
            "run_count": len(inventory.get("runs", [])),
            "source_file_count": len(sources),
            "output_png_count": len(images),
            "package_count": len(packages),
        }

    def preview_campaign(self, campaign_id):
        with self.storage.exclusive_run():
            return self._public(self._campaign_inventory(campaign_id))

    def delete_campaign(self, campaign_id, token, *, defer_cleanup=False):
        # Export admission takes this guard before reading the draft. Use that
        # same order so no new temporary job can enter after deletion commits.
        delivery_guard = self.state.__dict__.setdefault("delivery_guard", threading.RLock())
        with delivery_guard, self.storage.exclusive_run(), self.storage.exclusive_draft():
            self._require_no_pending(campaign_id)
            inventory = self._campaign_inventory(campaign_id)
            if canonical_hash(inventory) != token:
                raise StateError(
                    "This campaign changed. Review what will be deleted again.",
                    code="cleanup_changed",
                )
            return self._commit_and_resume(inventory, defer_cleanup=defer_cleanup)

    def _require_no_pending(self, campaign_id):
        with self.storage.connect() as db:
            if db.execute(
                "SELECT 1 FROM cleanup_intents WHERE campaign_id=?", (campaign_id,)
            ).fetchone():
                raise StateError("Finish this campaign’s pending cleanup before starting another.")

    def _commit_and_resume(self, inventory, *, defer_cleanup=False):
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Description writes use their own short SQL transaction, so include
            # their final state in this CAS, after the filesystem inventory.
            if "record_fingerprint" in inventory and inventory[
                "record_fingerprint"
            ] != self._record_fingerprint(inventory["campaign_id"]):
                raise StateError(
                    "This campaign changed. Review cleanup again.", code="cleanup_changed"
                )
            # A temporary display name identifies an interrupted deletion. It is
            # removed with the intent; no brief or provider body is copied here.
            identity = self.record_intent(db, inventory)
        if inventory["kind"] == "campaign":
            # Confirmed deletion also ends an idle failed download immediately,
            # even when the filesystem cleanup will happen in a later request.
            # Active work observes cancellation at its next image/ZIP boundary.
            for job in list(self.state.__dict__.get("delivery_jobs", {}).values()):
                if job.campaign_id == inventory["campaign_id"]:
                    job.cancel()
        if defer_cleanup:
            return {"state": "committed", "cleanup_id": identity}
        return self._resume(identity)

    def _resume(self, identity):
        with self.storage.connect() as db:
            row = db.execute(
                "SELECT value_json FROM cleanup_intents WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            return {"state": "complete", "cleanup_id": identity}
        inventory = json.loads(row[0])
        try:
            if inventory["kind"] == "upload":
                self._prepare_upload_retirement(identity, inventory)
            project = self.storage.project(inventory["campaign_id"])
            references = [item["reference"] for item in inventory["files"]]
            references.extend(inventory.get("owned_directories", []))
            with self.storage.connect() as db:
                for reference in references:
                    project.check(reference)
                    if (
                        db.execute(
                            "SELECT 1 FROM file_owners WHERE reference=? AND campaign_id=?",
                            (reference, project.campaign_id),
                        ).fetchone()
                        is None
                    ):
                        raise StorageError("The ownership of a file in this deletion changed.")
            if inventory["kind"] in ("retirement", "migration"):
                self._require_unshared(project.campaign_id, inventory["files"])
            for item in inventory["files"]:
                path = self.storage.path(item["reference"])
                try:
                    payload = path.read_bytes()
                except FileNotFoundError:
                    continue
                if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    raise StorageError("An approved cleanup file changed.")
                self.storage.unlink(path)
            # Only empty parents of the approved files; never recursive removal.
            project_root = self.storage.path(f"Campaigns/{project.folder}")
            parents = {self.storage.path(ref) for ref in inventory.get("directories", [])}
            parents.update(
                parent
                for ref in references
                for parent in self.storage.path(ref).parents
                if parent.is_relative_to(project_root)
            )
            for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
                if not parent.is_relative_to(project_root):
                    raise StorageError("The saved cleanup directory is outside its project.")
                if inventory["kind"] in ("campaign", "migration") and parent.is_dir():
                    # Finder adds this file when a folder is opened. Remove it only
                    # after all other contents are gone; preserve actual user files.
                    contents = list(parent.iterdir())
                    if len(contents) == 1 and _finder_metadata(contents[0]):
                        self.storage.unlink(contents[0])
                with suppress(OSError):
                    parent.rmdir()  # Unregistered files remain where the producer put them.
            with self.storage.connect() as db:
                if inventory["kind"] == "campaign":
                    cid = inventory["campaign_id"]
                    db.execute(
                        "DELETE FROM workspace_migrations WHERE name=?",
                        (f"campaign_outputs:{cid}",),
                    )
                    db.execute("DELETE FROM withheld_output_sources WHERE campaign_id=?", (cid,))
                    for table in ("creative_metadata", "package_exports", "revisions", "attempts"):
                        if table == "creative_metadata":
                            db.execute(
                                "DELETE FROM creative_metadata WHERE revision_id IN (SELECT "
                                "r.id FROM revisions r JOIN runs ON runs.id=r.run_id WHERE "
                                "runs.campaign_id=?)",
                                (cid,),
                            )
                        else:
                            db.execute(
                                f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs "
                                f"WHERE campaign_id=?)",
                                (cid,),
                            )
                    db.execute("DELETE FROM runs WHERE campaign_id=?", (cid,))
                    db.execute("DELETE FROM file_owners WHERE campaign_id=?", (cid,))
                if inventory["kind"] in ("retirement", "migration", "upload"):
                    db.executemany(
                        "DELETE FROM file_owners WHERE reference=? AND campaign_id=?",
                        ((ref, project.campaign_id) for ref in references),
                    )
                db.execute("DELETE FROM cleanup_intents WHERE id=?", (identity,))
                if inventory["kind"] == "campaign":
                    db.execute("DELETE FROM campaigns WHERE id=?", (inventory["campaign_id"],))
            return {
                "state": "complete",
                "cleanup_id": identity,
                "unmanaged_file_count": inventory.get("unmanaged_file_count", 0),
                "removed_bytes": sum(item["bytes"] for item in inventory["files"]),
            }
        except OSError, ValueError, sqlite3.Error:
            return {
                "state": "pending",
                "cleanup_id": identity,
                "message": "Some approved files could not be removed. Check local storage, "
                "then retry cleanup. Other campaigns remain available.",
            }

    def retry(self, campaign_id, identity):
        guard = self.state.__dict__.setdefault("delivery_guard", threading.RLock())
        with (
            guard,
            self.storage.exclusive_run(),
            self.storage.exclusive_draft(),
            self.storage.connect() as db,
        ):
            row = db.execute(
                "SELECT campaign_id FROM cleanup_intents WHERE id=?", (identity,)
            ).fetchone()
            if row is None or row[0] != campaign_id:
                raise StateError("This pending cleanup is unavailable for the selected campaign.")
            return self._resume(identity)

    def resume_all(self):
        guard = self.state.__dict__.setdefault("delivery_guard", threading.RLock())
        try:
            with guard, self.storage.exclusive_run(), self.storage.exclusive_draft():
                with self.storage.connect() as db:
                    identities = [
                        row[0]
                        for row in db.execute("SELECT id FROM cleanup_intents ORDER BY rowid")
                    ]
                return [self._resume(identity) for identity in identities]
        except StorageError:
            return []  # Another operation owns the workspace; it must finish first.

    def status(self, campaign_id):
        require_campaign_registered(self.storage, campaign_id)
        with self.storage.connect() as db:
            pending = [
                {"id": row[0], "kind": row[1]}
                for row in db.execute(
                    "SELECT id,kind FROM cleanup_intents WHERE campaign_id=?", (campaign_id,)
                )
            ]
        return {
            "campaign_state": "pending"
            if any(p["kind"] == "campaign" for p in pending)
            else "available",
            "pending": pending,
        }

    def pending(self):
        with self.storage.connect() as db:
            rows = db.execute(
                "SELECT id,kind,campaign_id,value_json FROM cleanup_intents ORDER BY rowid"
            ).fetchall()
        return [
            {
                "id": row[0],
                "kind": row[1],
                "campaign_id": row[2],
                "name": json.loads(row[3]).get("name", "Saved image cleanup"),
            }
            for row in rows
        ]

    @staticmethod
    def _retiring_sets(rows, accepted_partial=None):
        """Keep only the newest usable result and a distinct complete fallback."""
        if not rows:
            return []
        complete = next((row[0] for row in rows if row[2]), None)
        keep = {
            identity
            for identity in (accepted_partial or rows[0][0], complete)
            if identity is not None
        }
        return [row for row in rows if row[0] not in keep]

    def _require_unshared(self, campaign_id, files):
        if not files:
            return
        inputs = self._current_inputs(campaign_id)
        if any(item["reference"] in inputs for item in files):
            raise StateError(
                "An older output is used as an input by this or another campaign. "
                "Replace that input before creating new images."
            )

    @staticmethod
    def _output_files(document):
        files = {}
        for item in json.loads(document)["outputs"]:
            file = item["file"]
            if file and not matches_output_file(
                file, item["product_id"], item["ratio"], item["market"]
            ):
                raise StorageError("A saved image has an unexpected location.")
            files[item["id"]] = file
        return files

    def plan_retirement(self, db, campaign_id):
        """Record exactly the files displaced by this publication before its commit."""
        records = db.execute(
            "SELECT r.id,r.manifest_json,r.complete,r.run_id,r.rowid FROM revisions r "
            "JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=? "
            "AND r.images_retired=0 AND r.image_count>0 ORDER BY r.rowid DESC",
            (campaign_id,),
        ).fetchall()
        active = self.state._current_output(db, campaign_id)
        partial = db.execute(
            "SELECT r.id,r.manifest_json,r.rowid FROM revisions r JOIN runs ON runs.id=r.run_id "
            "WHERE r.id=runs.current_revision AND r.id=? AND r.complete=0",
            (active[1] if active else None,),
        ).fetchone()
        previous_sources = {
            creative: (revision, file, run)
            for creative, revision, file, run in db.execute(
                "SELECT w.creative_id,w.revision_id,w.file,r.run_id FROM withheld_output_sources w "
                "JOIN revisions r ON r.id=w.revision_id WHERE w.campaign_id=?",
                (campaign_id,),
            )
        }
        outputs = {row[0]: self._output_files(row[1]) for row in records}
        retired = {row[0] for row in self._retiring_sets(records, partial[0] if partial else None)}
        sources = {}
        if partial:
            # A rule reversal can need the PNG of an omitted creative. Keep one
            # recoverable file per omitted creative, independently of the other
            # files once produced in its historical revision.
            current = outputs.get(partial[0])
            if current is None:
                current = self._output_files(partial[1])  # A decision can withhold every image.
            missing = {creative for creative, file in current.items() if not file}
            for revision, _, _, run, order in records:
                if run != active[0] or order >= partial[2]:
                    continue
                for creative in missing - sources.keys():
                    if file := outputs[revision].get(creative):
                        sources[creative] = (revision, file, run)
            for creative in missing - sources.keys():
                source = previous_sources.get(creative)
                if source and source[2] == active[0]:
                    sources[creative] = source
            allowed = {file for file in current.values() if file}
            allowed.update(source[1] for source in sources.values())
            # Keep an older complete set accessible only while all of its files
            # still belong to the current result or its omitted creatives.
            for revision, _, complete, run, _ in records:
                if complete and (
                    run != active[0] or not allowed.issuperset(outputs[revision].values())
                ):
                    retired.add(revision)
        for creative in previous_sources.keys() - sources.keys():
            db.execute(
                "DELETE FROM withheld_output_sources WHERE campaign_id=? AND creative_id=?",
                (campaign_id, creative),
            )
        db.executemany(
            "INSERT OR REPLACE INTO withheld_output_sources VALUES (?,?,?,?)",
            (
                (campaign_id, creative, revision, file)
                for creative, (revision, file, run) in sources.items()
                if previous_sources.get(creative) != (revision, file, run)
            ),
        )
        keep = {source[1] for source in sources.values()}
        remove = {source[1] for source in previous_sources.values()}
        for revision, items in outputs.items():
            paths = remove if revision in retired else keep
            paths.update(file for file in items.values() if file)
        project = self.storage.project(campaign_id)
        obsolete = [
            project.require(project.ref(f"Outputs/{file}")) for file in sorted(remove - keep)
        ]
        self._require_unshared(campaign_id, [{"reference": file} for file in obsolete])
        files = self._files(obsolete, include_missing=True)
        db.executemany("UPDATE revisions SET images_retired=1 WHERE id=?", ((r,) for r in retired))
        if not files:
            return None
        return self.record_intent(
            db, {"kind": "retirement", "campaign_id": campaign_id, "files": files}
        )

    @staticmethod
    def record_intent(db, inventory):
        identity = uuid4().hex
        db.execute(
            "INSERT INTO cleanup_intents VALUES (?,?,?,?,?)",
            (
                identity,
                inventory["kind"],
                inventory["campaign_id"],
                json.dumps(inventory),
                timestamp(),
            ),
        )
        return identity
