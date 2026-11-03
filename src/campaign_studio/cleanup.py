"""Deliberate owned-file cleanup and retirement of older image sets.

The database commits an exact file list before unlinking. Restart only resumes
those lists; it never discovers new deletion targets or sweeps upload orphans.
"""

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from campaign_studio.brief import valid_campaign_id
from campaign_studio.planning import canonical_hash
from campaign_studio.state import StateError, timestamp
from campaign_studio.storage import StorageError


def pending_campaign(db, campaign_id):
    return (
        db.execute(
            "SELECT 1 FROM cleanup_intents WHERE campaign_id=? AND kind='campaign'",
            (campaign_id,),
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


class Cleanup:
    def __init__(self, state):
        self.state = state
        self.storage = state.storage

    def _campaign(self, campaign_id):
        require_campaign_available(self.storage, campaign_id)
        with self.storage.connect() as db:
            runs = db.execute("SELECT id FROM runs WHERE campaign_id=?", (campaign_id,)).fetchall()
            owned = db.execute(
                "SELECT 1 FROM file_owners WHERE campaign_id=? LIMIT 1", (campaign_id,)
            ).fetchone()
        draft = self.storage.path(f"private/brief-drafts/{campaign_id}.json")
        document = draft if draft.is_file() else None
        value = json.loads(document.read_bytes()) if document else {}
        authored = value.get("authored", {}).get("brief")
        if isinstance(authored, dict):
            # Even an explicitly empty saved name belongs to the current project.
            name = authored.get("name")
        else:
            name = value.get("value", value).get("name")
            if not name and runs:
                name = self.state.plan(runs[-1][0]).brief.name
        return (
            [row[0] for row in runs],
            str(name or "Untitled campaign"),
            bool(runs or document or owned),
        )

    def _files(self, references):
        """Inventory exact regular files. Permission errors and aliases are refusals."""
        found = {}

        def inspect(path):
            reference = path.relative_to(self.storage.root).as_posix()
            path = self.storage.path(reference)
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode):
                raise StorageError(
                    "Cleanup found an unsupported file. No new deletion was started."
                )
            found[reference] = {
                "reference": reference,
                "bytes": info.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        def walk_error(error):
            raise error

        for reference in references:
            path = self.storage.path(reference)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                for directory, names, files in os.walk(path, onerror=walk_error):
                    for name in names:
                        self.storage.path(
                            (Path(directory) / name).relative_to(self.storage.root).as_posix()
                        )
                    for name in files:
                        inspect(Path(directory) / name)
            else:
                inspect(path)
        return [found[key] for key in sorted(found)]

    def _references(self, campaign_id, *, live_only=False):
        """Saved documents establish input dependencies; names never imply reuse."""
        documents = [f"private/brief-drafts/{campaign_id}.json"]
        with self.storage.connect() as db:
            documents += (
                []
                if live_only
                else [
                    row[0]
                    for row in db.execute(
                        "SELECT plan_ref FROM runs WHERE campaign_id=? UNION "
                        "SELECT r.plan_ref FROM revisions r JOIN runs ON runs.id=r.run_id "
                        "WHERE runs.campaign_id=?",
                        (campaign_id, campaign_id),
                    )
                ]
            )
        from campaign_studio.authored import attachment_paths

        result = set()
        for index, reference in enumerate(documents):
            try:
                document = json.loads(self.storage.path(reference).read_bytes())
            except FileNotFoundError:
                continue
            if index == 0:
                brief = document.get("value", document)
                authored = document.get("authored", {})
                paths = list(attachment_paths(authored))
                if not live_only or not authored:
                    paths.extend(attachment_paths({"brief": brief}))
            else:
                brief = document["brief"]
                paths = list(attachment_paths({"brief": brief}))
                paths.extend(asset["reference"] for asset in document["assets"].values())
            # Read saved replay images from the brief used for production.
            replay = brief.get("replay") or {}
            paths.extend(master["path"] for master in replay.get("sources", {}).values())
            for path in paths:
                if isinstance(path, str) and path.startswith(
                    (
                        "private/uploads/",
                        "campaigns/",
                        "private/runs/",
                        "exports/",
                        "replays/",
                        "samples/",
                    )
                ):
                    try:
                        self.storage.path(path)
                    except StorageError:
                        continue  # A draft can contain an unfinished image path.
                    result.add(path)
        return result

    def _file_key(self, reference):
        # On a case-insensitive volume, two spellings can point to the same file.
        try:
            info = self.storage.path(reference).stat()
        except FileNotFoundError, NotADirectoryError:
            return None
        return info.st_dev, info.st_ino

    def _other_owners(self, campaign_id):
        with self.storage.connect() as db:
            others = {row[0] for row in db.execute("SELECT DISTINCT campaign_id FROM runs")}
            owned = db.execute(
                "SELECT reference,campaign_id FROM file_owners WHERE campaign_id!=?", (campaign_id,)
            ).fetchall()
        path = self.storage.path("private/brief-drafts")
        try:
            info = path.stat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISDIR(info.st_mode):
                others.update(p.name.removesuffix(".json") for p in path.iterdir())
        references = {}
        for reference, owner in owned:
            if (key := self._file_key(reference)) is not None:
                references.setdefault(key, set()).add(owner)
        for other in others - {campaign_id}:
            if valid_campaign_id(other):
                for reference in self._references(other):
                    if (key := self._file_key(reference)) is not None:
                        references.setdefault(key, set()).add(other)
        return references

    def _shared_files(self, campaign_id, files):
        shared = self._other_owners(campaign_id)
        # Another campaign's attachment protects a file from deletion.
        return {
            item["reference"]: sorted(owners)
            for item in files
            if (owners := shared.get(self._file_key(item["reference"])))
        }

    def _directories(self, references):
        directories = set()

        def failed(error):
            raise error

        for reference in references:
            path = self.storage.path(reference)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            for directory, names, _ in os.walk(path, onerror=failed):
                for child in [Path(directory), *(Path(directory) / name for name in names)]:
                    relative = child.relative_to(self.storage.root).as_posix()
                    self.storage.path(relative)
                    directories.add(relative)
        return sorted(directories)

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
        return canonical_hash(rows)

    def _campaign_inventory(self, campaign_id):
        runs, name, has_content = self._campaign(campaign_id)
        references = (
            [
                f"private/brief-drafts/{campaign_id}.json",
                f"exports/{campaign_id}",
            ]
            if has_content
            else []
        )
        references += [
            prefix for run in runs for prefix in (f"private/runs/{run}", f"campaigns/{run}")
        ]
        with self.storage.connect() as db:
            references += [
                row[0]
                for row in db.execute(
                    "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
                )
            ]
        all_files = self._files(references)
        shared_owners = self._shared_files(campaign_id, all_files)
        retained = [item for item in all_files if item["reference"] in shared_owners]
        files = [item for item in all_files if item["reference"] not in shared_owners]
        return {
            "kind": "campaign",
            "campaign_id": campaign_id,
            "name": name,
            "runs": runs,
            "files": files,
            "directories": self._directories(references),
            "retained": retained,
            "shared_owners": shared_owners,
            "record_fingerprint": self._record_fingerprint(campaign_id),
        }

    @staticmethod
    def _public(inventory):
        files = inventory["files"]
        images = [
            f["reference"]
            for f in files
            if "/revisions/" in f["reference"] and "/images/" in f["reference"]
        ]
        packages = {
            tuple(f["reference"].split("/")[: 4 if "/archives/" in f["reference"] else 3])
            for f in files
            if f["reference"].startswith("exports/")
        }
        sources = {
            f["reference"]
            for f in files
            if f["reference"].lower().endswith((".png", ".jpg", ".jpeg"))
            and f["reference"] not in images
            and not f["reference"].startswith("exports/")
        }
        return {
            "kind": inventory["kind"],
            "campaign_id": inventory["campaign_id"],
            "name": inventory.get("name"),
            "token": canonical_hash(inventory),
            "file_count": len(files),
            "removable_bytes": sum(x["bytes"] for x in files),
            "shared_file_count": len(inventory.get("retained", [])),
            "shared_bytes": sum(x["bytes"] for x in inventory.get("retained", [])),
            "run_count": len(inventory.get("runs", [])),
            "source_file_count": len(sources),
            "output_png_count": len(images),
            "package_count": len(packages),
        }

    def preview_campaign(self, campaign_id):
        with self.storage.exclusive_run():
            return self._public(self._campaign_inventory(campaign_id))

    def delete_campaign(self, campaign_id, token, *, defer_cleanup=False):
        with self.storage.exclusive_run(), self.storage.exclusive_draft():
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
        identity = uuid4().hex
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
            stored = {key: value for key, value in inventory.items() if key != "retained"}
            stored["shared_file_count"] = len(inventory.get("retained", []))
            db.execute(
                "INSERT INTO cleanup_intents VALUES (?,?,?,?,?)",
                (
                    identity,
                    inventory["kind"],
                    inventory["campaign_id"],
                    json.dumps(stored),
                    timestamp(),
                ),
            )
            self._retain_shared(db, inventory.get("shared_owners", {}))
            for revision in inventory.get("retire", []):
                db.execute("UPDATE revisions SET images_retired=1 WHERE id=?", (revision,))
        if defer_cleanup:
            return {"state": "committed", "cleanup_id": identity}
        return self._resume(identity)

    @staticmethod
    def _retain_shared(db, shared):
        db.executemany(
            "INSERT OR IGNORE INTO file_owners VALUES (?,?)",
            ((reference, owner) for reference, owners in shared.items() for owner in owners),
        )

    def _resume(self, identity):
        with self.storage.connect() as db:
            row = db.execute(
                "SELECT value_json FROM cleanup_intents WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            return {"state": "complete", "cleanup_id": identity}
        inventory = json.loads(row[0])
        try:
            if inventory["kind"] == "campaign":
                # Older deletion lists could include uploads named in notes.
                # Recheck who owns each upload before removing it.
                campaign_id = inventory["campaign_id"]
                with self.storage.connect() as db:
                    owned = {
                        row[0]
                        for row in db.execute(
                            "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
                        )
                    }
                    files = [
                        item
                        for item in inventory["files"]
                        if not item["reference"].startswith("private/uploads/")
                        or item["reference"] in owned
                    ]
                    shared = self._shared_files(campaign_id, files)
                    self._retain_shared(db, shared)
                inventory["files"] = [item for item in files if item["reference"] not in shared]
                inventory["shared_file_count"] += len(shared)
            else:
                self._require_unshared(inventory["campaign_id"], inventory["files"])
            for item in inventory["files"]:
                path = self.storage.path(item["reference"])
                try:
                    payload = path.read_bytes()
                except FileNotFoundError:
                    continue
                if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    raise StorageError("An approved cleanup file changed.")
                path.unlink()
            # Only empty parents of the approved files; never recursive removal.
            parents = {
                parent
                for item in inventory["files"]
                for parent in self.storage.path(item["reference"]).parents
                if parent != self.storage.root and parent.is_relative_to(self.storage.root)
            }
            protected = {
                self.storage.path(x)
                for x in (
                    "private",
                    "campaigns",
                    "exports",
                    "incoming",
                    "private/uploads",
                    "private/brief-drafts",
                    "private/runs",
                )
            }
            parents.update(
                self.storage.path(reference) for reference in inventory.get("directories", [])
            )
            for parent in sorted(parents - protected, key=lambda p: len(p.parts), reverse=True):
                # A preserved dependency or another owned file may still use it.
                with suppress(OSError):
                    parent.rmdir()
            with self.storage.connect() as db:
                if inventory["kind"] == "campaign":
                    cid = inventory["campaign_id"]
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
                db.execute("DELETE FROM cleanup_intents WHERE id=?", (identity,))
                if inventory["kind"] == "campaign":
                    db.execute("DELETE FROM campaigns WHERE id=?", (inventory["campaign_id"],))
            return {
                "state": "complete",
                "cleanup_id": identity,
                "shared_file_count": inventory.get("shared_file_count", 0),
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
        with (
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
        try:
            with self.storage.exclusive_run(), self.storage.exclusive_draft():
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

    def _image_sets(self):
        # Ordered once, then shared by admission and the actual retirement choice.
        return (
            "SELECT r.id,r.manifest_ref,r.complete FROM revisions r "
            "JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=? "
            "AND images_retired=0 AND COALESCE(image_count,1)>0 ORDER BY r.rowid DESC"
        )

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
        shared = set(self._other_owners(campaign_id))
        shared.update(self._file_key(ref) for ref in self._references(campaign_id, live_only=True))
        shared.discard(None)
        if any(self._file_key(item["reference"]) in shared for item in files):
            raise StateError(
                "An older output is used as an input by this or another campaign. "
                "Replace that input before creating new images."
            )

    def prepare_retirement(self, campaign_id):
        """Inspect every possible retirement before creating another image set.

        A local operation can finish complete or partial. An older complete set
        may stay protected in the latter case, displacing a newer partial set.
        Both outcomes therefore use the same selection rule as publication.
        """
        with self.storage.connect() as db:
            rows = db.execute(self._image_sets(), (campaign_id,)).fetchall()
        candidates = {}
        for complete in (False, True):
            projected = [("new-publication", "", complete), *rows]
            for row in self._retiring_sets(projected):
                candidates[row[0]] = row
        result = {
            identity: self._files([row[1].rsplit("/", 1)[0] + "/images"])
            for identity, row in candidates.items()
        }
        self._require_unshared(campaign_id, [item for files in result.values() for item in files])
        return result

    def retire_after_publish(self, campaign_id, prepared=None):
        """Called under the existing serial lock, only after a new revision commits."""
        with self.storage.connect() as db:
            revisions = db.execute(self._image_sets(), (campaign_id,)).fetchall()
            current = db.execute(
                "SELECT r.id FROM runs JOIN revisions r ON r.id=runs.current_revision "
                "WHERE runs.id=(SELECT id FROM runs WHERE campaign_id=? "
                "ORDER BY rowid DESC LIMIT 1) AND r.complete=0 AND r.image_count>0",
                (campaign_id,),
            ).fetchone()
        # An unsuccessful correction cannot replace or retire an accepted partial
        # result. First-run partial generation still keeps its newest result.
        retire = self._retiring_sets(revisions, current[0] if current else None)
        if not retire:
            return None
        files = []
        for row in retire:
            files.extend(
                prepared[row[0]]
                if prepared is not None and row[0] in prepared
                else self._files([row[1].rsplit("/", 1)[0] + "/images"])
            )
        # A direct publisher or a changed dependency must never bypass the
        # admission check merely because its image list was already prepared.
        self._require_unshared(campaign_id, files)
        return self._commit_and_resume(
            {
                "kind": "retirement",
                "campaign_id": campaign_id,
                "retire": [row[0] for row in retire],
                "files": files,
                "directories": self._directories(
                    [row[1].rsplit("/", 1)[0] + "/images" for row in retire]
                ),
            }
        )
