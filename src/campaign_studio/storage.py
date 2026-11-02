"""Private local files, atomic publication, and the serial-run boundary."""

import fcntl
import os
import re
import shutil
import sqlite3
import stat
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import uuid4


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


def _reject_symlinks(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise StorageError("Symbolic links are not allowed in runtime storage.")


class Storage:
    def __init__(
        self,
        root: Path,
        *,
        location_notice: str = "",
        budget_bytes: int = 10_000_000_000,
        reserve_bytes: int = 1_000_000_000,
    ):
        self.root = root.expanduser().absolute()
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
                        if stat.S_ISLNK(inspected.st_mode):
                            names.remove(name)
                    except FileNotFoundError:
                        names.remove(name)
                    except OSError as error:
                        unreadable(error)
                for name in files:
                    try:
                        inspected = (Path(directory) / name).lstat()
                        if stat.S_ISREG(inspected.st_mode):
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
                existing = existing.parent
        return {
            "location": str(self.root),
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

    def write_new(self, reference: str, data: bytes) -> None:
        """Publish a new owned file durably without replacing an existing identifier."""
        destination = self.path(reference)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path(f"{reference}.{uuid4().hex}.tmp")
        created = False
        linked = False
        try:
            with temporary.open("xb") as output:
                created = True
                os.chmod(temporary, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, destination)
            linked = True
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            # A failed directory sync must not leave a file whose caller rolls
            # back its database identity. Only remove this call's own hard link.
            if linked:
                try:
                    if destination.samefile(temporary):
                        destination.unlink()
                except OSError:
                    pass  # Preserve the original publication failure.
            raise
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    def initialize(self) -> None:
        _reject_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir():
            raise StorageError("Runtime storage must be a directory.")
        for name in ("campaigns", "private", "smoke"):
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
        delivery = parts[0] == "exports"
        valid = (
            all(
                bool(part)
                and part == part.strip(" .")
                and not re.search(r'[<>:"\\|?*#%]', part)
                and not any(unicodedata.category(c).startswith("C") for c in part)
                and not re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
                for part in parts
            )
            if delivery
            else all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in parts)
        )
        if not reference or PurePosixPath(reference).is_absolute() or not valid:
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
        temporary = self.path(f"{reference}.{uuid4().hex}.tmp")
        created = False
        try:
            with temporary.open("xb") as output:
                created = True
                os.chmod(temporary, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def exclusive_draft(self) -> Iterator[None]:
        """Short authored-file CAS, independent of a provider's long run lock.

        Code needing both locks takes runner first, then draft. Never hold this
        guard across provider or image-render waits, or acquire it recursively.
        """
        with self.path("private/authored.lock").open("ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def exclusive_run(self) -> Iterator[None]:
        """One serial operation across processes using this runtime root."""
        with self.path("private/runner.lock").open("ab") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StorageError(
                    "Another campaign operation is already active.", code="busy"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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
