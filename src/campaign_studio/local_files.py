"""Native file publication and locks used by the local workspace."""

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def has_link(path):
    for part in (path, *path.parents):
        try:
            if is_link(part.lstat()):
                return True
        except FileNotFoundError:
            continue
    return False


def local_root(path):
    if os.name != "nt":
        return path.expanduser().absolute()
    from ctypes import WinDLL
    from ctypes.wintypes import LPCWSTR, UINT

    raw = str(path.expanduser())
    supplied = Path(raw.removeprefix("\\\\?\\"))
    if (
        ".." in supplied.parts
        or (supplied.drive and not supplied.is_absolute())
        or (supplied.root and not supplied.drive)
        or (raw.startswith("\\\\?\\") and not supplied.is_absolute())
    ):
        raise ValueError("Choose a local Windows drive folder without traversal.")
    absolute = supplied.absolute()
    if len(absolute.drive) != 2 or absolute.drive[1] != ":":
        raise ValueError("Choose a local Windows drive folder; network shares are unsupported.")
    drive_type = WinDLL("kernel32").GetDriveTypeW
    drive_type.argtypes, drive_type.restype = (LPCWSTR,), UINT
    kind = drive_type(absolute.anchor)
    if kind in (0, 1):  # Unknown drive or no mounted volume.
        raise ValueError("Choose an existing local Windows drive for the workspace.")
    if kind == 4:  # DRIVE_REMOTE includes mapped shares.
        raise ValueError("Choose a local Windows drive folder; network shares are unsupported.")
    # Use the extended drive path consistently, including when a temporary exceeds MAX_PATH.
    return Path("\\\\?\\" + str(absolute))


def display_path(path):
    return str(path).removeprefix("\\\\?\\") if os.name == "nt" else str(path)


@contextmanager
def file_lock(stream, *, wait):
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EDEADLK):
                raise BlockingIOError("The file is locked.") from error
            raise
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def publish_file(temporary: Path, destination: Path, *, replace: bool):
    """Publish a flushed file within its volume, preserving create-only semantics."""
    if os.name == "nt":
        from ctypes import WinDLL, WinError, get_last_error
        from ctypes.wintypes import BOOL, DWORD, LPCWSTR

        move = WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (LPCWSTR, LPCWSTR, DWORD)
        move.restype = BOOL
        flags = 0x8 | int(replace)  # MOVEFILE_WRITE_THROUGH and optional REPLACE_EXISTING
        if not move(str(temporary), str(destination), flags):
            raise WinError(get_last_error())
        return
    if replace:
        os.replace(temporary, destination)
    else:
        os.link(temporary, destination)
    try:
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        if not replace and destination.samefile(temporary):
            destination.unlink()
        raise
