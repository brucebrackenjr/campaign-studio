"""Deliberate owned-file cleanup and retirement of older image sets.

The database commits an exact file list before unlinking campaign content.
Empty campaign directories may also contain disposable Finder metadata.
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
from campaign_studio.local_files import is_link
from campaign_studio.planning import canonical_hash
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
        draft = self.storage.path(self.storage.project(campaign_id).draft)
        document = draft if draft.is_file() else None
        # A display name is optional recovery metadata, never deletion authority.
        # The registered file inventory and its confirmation token still decide
        # exactly which files can be removed when a saved draft is damaged.
        name = f"Campaign {campaign_id[:8]}"
        try:
            value = json.loads(document.read_bytes()) if document else {}
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

    def _files(self, references):
        found = []
        for reference in sorted(set(references)):
            path = self.storage.path(reference)
            try:
                info = path.lstat()
            except FileNotFoundError:
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

        project = self.storage.project(campaign_id)
        path = self.storage.path(project.draft)
        if not path.exists():
            return set()
        document = json.loads(path.read_bytes())
        brief = document["value"]
        inputs = set(
            ref
            for ref in attachment_paths(document.get("authored", {"brief": brief}))
            if isinstance(ref, str)
        )
        inputs.update(
            master["path"] for master in (brief.get("replay") or {}).get("sources", {}).values()
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

    def _revision_files(self, campaign_id, manifest_ref):
        prefix = manifest_ref.rsplit("/", 1)[0] + "/images/"
        return self._files(ref for ref in self._registered(campaign_id) if ref.startswith(prefix))

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
        runs, name = self._campaign(campaign_id)
        project = self.storage.project(campaign_id)
        project_root = self.storage.path(f"Campaigns/{project.folder}")
        references = self._registered(campaign_id)
        directories = {project_root}
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
        images = [
            f["reference"]
            for f in files
            if "/Revisions/" in f["reference"] and "/images/" in f["reference"]
        ]
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
            stored = inventory
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
            for revision in inventory.get("retire", []):
                db.execute("UPDATE revisions SET images_retired=1 WHERE id=?", (revision,))
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
            project = self.storage.project(inventory["campaign_id"])
            registered = set(self._registered(project.campaign_id))
            if any(item["reference"] not in registered for item in inventory["files"]):
                raise StorageError("The ownership of a file in this deletion changed.")
            if inventory["kind"] == "retirement":
                self._require_unshared(project.campaign_id, inventory["files"])
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
            project_root = self.storage.path(f"Campaigns/{project.folder}")
            parents = {self.storage.path(ref) for ref in inventory.get("directories", [])}
            parents.update(
                parent
                for item in inventory["files"]
                for parent in self.storage.path(item["reference"]).parents
                if parent.is_relative_to(project_root)
            )
            for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
                if not parent.is_relative_to(project_root):
                    raise StorageError("The saved cleanup directory is outside its project.")
                if inventory["kind"] == "campaign" and parent.is_dir():
                    # Finder adds this file when a folder is opened. Remove it only
                    # after all other contents are gone; preserve actual user files.
                    contents = list(parent.iterdir())
                    if len(contents) == 1 and _finder_metadata(contents[0]):
                        contents[0].unlink()
                with suppress(OSError):
                    parent.rmdir()  # Unregistered files remain where the producer put them.
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
                if inventory["kind"] == "retirement":
                    db.executemany(
                        "DELETE FROM file_owners WHERE reference=? AND campaign_id=?",
                        ((item["reference"], project.campaign_id) for item in inventory["files"]),
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
        inputs = self._current_inputs(campaign_id)
        if any(item["reference"] in inputs for item in files):
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
            identity: self._revision_files(campaign_id, row[1])
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
                else self._revision_files(campaign_id, row[1])
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
                "directories": [row[1].rsplit("/", 1)[0] + "/images" for row in retire],
            }
        )
