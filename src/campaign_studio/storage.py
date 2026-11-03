"""Private local files, atomic publication, and the serial-run boundary."""

import ntpath
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4

from campaign_studio.local_files import (
    display_path,
    file_lock,
    has_link,
    is_link,
    local_root,
    publish_file,
)

WORKSPACE_SCHEMA = 9


class CapacityError(ValueError):
    """Safe numeric context for an actionable local limit, without parsing prose."""

    def __init__(self, kind: str, measured: dict, projected_bytes: int):
        self.details = {
            "kind": kind,
            "projected_bytes": projected_bytes,
            **{
                key: measured[key]
                for key in ("used_bytes", "budget_bytes", "free_bytes", "reserve_bytes")
            },
        }
        message = (
            "The workspace’s storage limit has been reached. Existing work stays available. "
            "Delete an unwanted campaign before creating new files."
            if kind == "workspace_budget"
            else "There is not enough free space on this computer for this operation. "
            "Free disk space, then try again. Saved work is preserved."
        )
        super().__init__(message)


class StorageError(ValueError):
    """A storage operation cannot satisfy the local boundary."""

    def __init__(self, message, *, code="state"):
        super().__init__(message)
        self.code = code


class EarlierWorkspace(StorageError):
    """The existing workspace must be opened by its original Motif version."""


class IncompleteWorkspace(StorageError):
    """The available workspace database cannot index the saved campaign files."""


def _reject_symlinks(path: Path) -> None:
    if has_link(path):
        raise StorageError(
            "Symbolic links, junctions, and other reparse points are not allowed here."
        )


def delivery_component(value: str, fallback: str) -> str:
    """A readable, bounded path component safe for macOS, Windows, and ZIP tools."""
    value = unicodedata.normalize("NFKC", value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    value = re.sub(r'[<>:"/\\|?*#%]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value.encode("utf-8")[:80].decode("utf-8", errors="ignore").rstrip(" .") or fallback
    if ntpath.isreserved(value):
        value = "_" + value
    return value


class Storage:
    def __init__(
        self,
        root: Path,
        *,
        location_notice: str = "",
        budget_bytes: int = 10_000_000_000,
        reserve_bytes: int = 1_000_000_000,
    ):
        try:
            self.root = local_root(root)
        except ValueError as error:
            raise StorageError(str(error)) from error
        self.location_notice = location_notice
        self.budget_bytes = budget_bytes
        self.reserve_bytes = reserve_bytes

    def summary(self) -> dict:
        """Measure owned regular files without following aliases or reading their content."""
        _reject_symlinks(self.root)
        total = 0
        count = 0

        def unreadable(error):
            raise StorageError(
                "Workspace usage could not be measured completely. "
                "Check storage permissions before creating new files."
            ) from error

        try:
            root_stat = self.root.stat()
        except FileNotFoundError:
            root_stat = None
        except OSError as error:
            unreadable(error)
        if root_stat is not None:
            if not stat.S_ISDIR(root_stat.st_mode):
                raise StorageError("Runtime storage must be a directory.")
            for directory, names, files in os.walk(
                self.root, followlinks=False, onerror=unreadable
            ):
                for name in list(names):
                    try:
                        inspected = (Path(directory) / name).lstat()
                        if is_link(inspected):
                            names.remove(name)
                    except FileNotFoundError:
                        names.remove(name)
                    except OSError as error:
                        unreadable(error)
                for name in files:
                    try:
                        inspected = (Path(directory) / name).lstat()
                        if stat.S_ISREG(inspected.st_mode) and not is_link(inspected):
                            total += inspected.st_size
                            count += 1
                    except FileNotFoundError:
                        pass  # A concurrent atomic temporary file may have disappeared.
                    except OSError as error:
                        unreadable(error)
        existing = self.root
        while True:
            try:
                existing.stat()
                break
            except FileNotFoundError:
                if existing == existing.parent:
                    raise StorageError("Choose an available local drive.") from None
                existing = existing.parent
        return {
            "location": display_path(self.root),
            "location_notice": self.location_notice,
            "used_bytes": total,
            "budget_bytes": self.budget_bytes,
            "reserve_bytes": self.reserve_bytes,
            "over_budget": total >= self.budget_bytes,
            "file_count": count,
            "free_bytes": shutil.disk_usage(existing).free,
        }

    def require_capacity(self, projected_bytes: int) -> dict:
        """Admit one large operation; essential control and received-result writes bypass this.

        Call once at the serial operation boundary, not for every file or pixel.
        Recheck before each paid attempt because another process can consume disk.
        """
        if projected_bytes < 0:
            raise ValueError("A storage estimate cannot be negative.")
        measured = self.summary()
        if measured["used_bytes"] + projected_bytes > self.budget_bytes:
            raise CapacityError("workspace_budget", measured, projected_bytes)
        if measured["free_bytes"] < self.reserve_bytes + projected_bytes:
            raise CapacityError("disk_space", measured, projected_bytes)
        return measured

    def reserve_namespaces(self, patterns: tuple[str, ...], *, occupied=None) -> str:
        """Reserve immutable directories before writing; collisions never replace old files."""
        for _ in range(100):
            identity = uuid4().hex
            if occupied and occupied(identity):
                continue
            created = []
            try:
                for pattern in patterns:
                    path = self.path(pattern.format(id=identity))
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path.mkdir(mode=0o700)
                    created.append(path)
                return identity
            except FileExistsError:
                for path in reversed(created):
                    path.rmdir()  # Only empty directories created by this attempt.
        raise StorageError("A new storage identity could not be reserved. Try again.")

    def temporary_file(self, reference):
        """Record temporary project bytes so a crash leaves an exact cleanup target."""
        for _ in range(100):
            temp = f"{reference}.{uuid4().hex}.tmp"
            path = self.path(temp)
            if path.exists():
                continue
            registered = False
            if reference.startswith("Campaigns/"):
                with self.connect() as db:
                    if db.execute("PRAGMA user_version").fetchone()[0] == WORKSPACE_SCHEMA:
                        owner = db.execute(
                            "SELECT campaign_id FROM file_owners WHERE reference=?", (reference,)
                        ).fetchone()
                        if owner:
                            if not db.execute(
                                "INSERT OR IGNORE INTO file_owners VALUES (?,?)", (temp, owner[0])
                            ).rowcount:
                                continue
                            registered = True
            return path, registered
        raise StorageError("A temporary file could not be reserved. Try again.")

    def finish_temporary(self, path, registered):
        if registered and not path.exists():
            with self.connect() as db:
                db.execute(
                    "DELETE FROM file_owners WHERE reference=?",
                    (path.relative_to(self.root).as_posix(),),
                )

    def write_new(self, reference: str, data: bytes) -> None:
        """Publish a new owned file durably without replacing an existing identifier."""
        destination = self.path(reference)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary, registered = self.temporary_file(reference)
        created = False
        try:
            with temporary.open("xb") as output:
                created = True
                os.chmod(temporary, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            publish_file(temporary, destination, replace=False)
        finally:
            if created:
                temporary.unlink(missing_ok=True)
            self.finish_temporary(temporary, registered)

    def require_project_layout(self):
        database = self.path("private/state.sqlite3")
        names = {p.name for p in self.root.iterdir()} if self.root.is_dir() else set()
        # The old lowercase campaigns directory also matches Campaigns on a case-insensitive disk.
        legacy = bool(names & {"Projects", "campaigns", "exports"}) or any(
            self.path(name).exists() for name in ("private/brief-drafts", "private/uploads")
        )
        version, tables = 0, set()
        old_schema = False
        if database.is_file() and database.stat().st_size:
            with closing(
                sqlite3.connect(
                    Path(display_path(database)).as_uri() + "?mode=ro&immutable=1", uri=True
                )
            ) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")
                }
                columns = {r[1] for r in db.execute("PRAGMA table_info(campaigns)")}
                old_schema = "campaigns" in tables and "folder" not in columns
        if old_schema or (legacy and version == 0) or version not in (0, WORKSPACE_SCHEMA):
            raise EarlierWorkspace(
                "This workspace uses an earlier Motif layout. Open it with the Motif version "
                "that created it. To start a fresh workspace, use a new folder:\n"
                "uv run --locked campaign-studio serve --data-dir ./motif-projects"
            )
        campaign_files = "Campaigns" in names and any(self.path("Campaigns").iterdir())
        required = {
            "campaigns",
            "runs",
            "revisions",
            "attempts",
            "creative_metadata",
            "package_exports",
            "file_owners",
            "uploads",
            "cleanup_intents",
        }
        if (campaign_files or version or tables) and (
            version != WORKSPACE_SCHEMA or not required.issubset(tables)
        ):
            raise IncompleteWorkspace(
                "The workspace database is missing or incomplete. Stop Motif and restore the "
                "complete Workspace folder from backup, including private/state.sqlite3 "
                "and Campaigns."
            )

    def require_writable(self):
        """Check before the HTTP server starts, without marking a fresh workspace as used."""
        _reject_symlinks(self.root)
        parent = self.root
        while not parent.exists():
            if parent == parent.parent:
                raise StorageError("Choose an available local drive.")
            parent = parent.parent
        if not parent.is_dir():
            raise StorageError("Choose a writable local directory for this workspace.")
        for directory in (parent, self.path("private"), self.path("Campaigns")):
            if directory.is_dir():
                with tempfile.TemporaryFile(dir=directory):
                    pass
        database = self.path("private/state.sqlite3")
        if database.exists():
            with database.open("r+b"):
                pass

    def project(self, campaign_id):
        from campaign_studio.projects import ProjectFiles

        with self.connect() as db:
            row = db.execute("SELECT folder FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            raise StorageError(
                "We couldn’t find this campaign in your workspace.", code="not_found"
            )
        return ProjectFiles(self, campaign_id, row[0])

    def initialize(self) -> None:
        _reject_symlinks(self.root)
        self.require_project_layout()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise StorageError("Runtime storage must be a directory.")
        for name in ("Campaigns", "private", "smoke"):
            self.path(name).mkdir(exist_ok=True, mode=0o700)
        with self.connect() as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("The local database failed its integrity check.")

    def path(self, reference: str) -> Path:
        """Resolve application-owned relative references, rejecting aliases and symlinks.

        This guards ordinary local inputs, not a hostile process changing files
        concurrently. Only the current user's process should own runtime storage.
        """
        parts = reference.split("/")
        # Delivery names are human-readable; private application references stay
        # deliberately ASCII. Both retain the same traversal and symlink boundary.
        delivery = parts[0] == "Campaigns"
        valid = (
            all(
                bool(part)
                and part == part.strip(" .")
                and not re.search(r'[<>:"\\|?*#%]', part)
                and not any(unicodedata.category(c).startswith("C") for c in part)
                for part in parts
            )
            if delivery
            else all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in parts)
        )
        if (
            not reference
            or PurePosixPath(reference).is_absolute()
            or not valid
            or any(ntpath.isreserved(part) for part in parts)
        ):
            raise StorageError("Use a plain relative storage reference without traversal.")
        candidate = self.root.joinpath(*parts)
        _reject_symlinks(candidate)
        if not candidate.resolve().is_relative_to(self.root.resolve()):
            raise StorageError("The storage reference escapes the runtime directory.")
        return candidate

    def write(self, reference: str, data: bytes) -> None:
        """Publish a complete private file after flushing its bytes to disk."""
        destination = self.path(reference)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary, registered = self.temporary_file(reference)
        created = False
        try:
            with temporary.open("xb") as output:
                created = True
                os.chmod(temporary, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            publish_file(temporary, destination, replace=True)
        finally:
            if created:
                temporary.unlink(missing_ok=True)
            self.finish_temporary(temporary, registered)

    @contextmanager
    def exclusive_draft(self) -> Iterator[None]:
        """Short authored-file CAS, independent of a provider's long run lock.

        Code needing both locks takes runner first, then draft. Never hold this
        guard across provider or image-render waits, or acquire it recursively.
        """
        with self.path("private/authored.lock").open("a+b") as lock, file_lock(lock, wait=True):
            yield

    @contextmanager
    def exclusive_run(self) -> Iterator[None]:
        """One serial operation across processes using this runtime root."""
        with self.path("private/runner.lock").open("a+b") as lock, ExitStack() as held:
            try:
                held.enter_context(file_lock(lock, wait=False))
            except BlockingIOError as error:
                raise StorageError(
                    "Another campaign operation is already active.", code="busy"
                ) from error
            yield

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A short-lived connection owned by the calling thread.

        State owns the run, attempt, and revision tables. This connection manages
        only the local transaction and safe database file.
        """
        database = self.path("private/state.sqlite3")
        with database.open("ab"):
            pass
        database.chmod(0o600)
        connection = sqlite3.connect(database, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()
