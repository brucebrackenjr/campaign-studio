"""Build the portable source ZIP from Git-managed distribution files."""

import argparse
import hashlib
import struct
import subprocess
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path):
    top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True)
    if Path(top.strip()).resolve() != ROOT:
        raise ValueError("Build the source ZIP from Motif’s own Git checkout.")
    committed_at = datetime.fromisoformat(
        subprocess.check_output(
            ["git", "show", "-s", "--format=%cI", "HEAD"], cwd=ROOT, text=True
        ).strip()
    )
    # DOS timestamps have no timezone. Use the commit's recorded local time as
    # the fallback, with absolute UTC timestamps for extractors that support them.
    archive_date = committed_at.timetuple()[:6]
    committed_epoch = int(committed_at.timestamp())
    if not 1980 <= committed_at.year <= 2107:
        raise ValueError("The source commit date is outside ZIP's supported date range.")
    # Info-ZIP UT stores modification time in signed Unix seconds. NTFS stores
    # 100-nanosecond intervals since 1601, and also covers dates beyond 2038.
    timestamp_extra = (
        struct.pack("<HHBi", 0x5455, 5, 1, committed_epoch)
        if committed_epoch <= 0x7FFFFFFF
        else b""
    )
    # Apple's ditto still reads the older Info-ZIP UX timestamp field. Include
    # only its two timestamps, never machine-specific user or group IDs.
    if committed_epoch <= 0x7FFFFFFF:
        timestamp_extra += struct.pack("<HHii", 0x5855, 8, committed_epoch, committed_epoch)
    filetime = (committed_epoch + 11_644_473_600) * 10_000_000
    # Normalize all archive times to this source snapshot, not checkout access
    # or creation times, so rebuilding the same source produces the same bytes.
    timestamp_extra += struct.pack("<HHIHHQQQ", 0x000A, 32, 0, 1, 24, *([filetime] * 3))
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    with ZipFile(destination, "x") as archive:
        for name in sorted(n.decode("utf-8") for n in names if n):
            path = ROOT / name
            if (
                any(
                    (p.is_symlink() or p.is_junction())
                    for p in (path, *path.parents)
                    if p.is_relative_to(ROOT)
                )
                or not path.is_file()
            ):
                raise ValueError(f"Expected a regular distribution file: {name}")
            if (
                any(
                    part.casefold()
                    in {"workspace", ".git", ".venv", "private", ".env", "__pycache__"}
                    for part in Path(name).parts
                )
                or (path.name.lower().startswith(".env.") and path.name != ".env.example")
                or path.suffix.lower() in {".dpapi", ".sqlite3", ".db", ".pem", ".key"}
            ):
                raise ValueError(f"Private file is outside the distribution: {name}")
            entry = ZipInfo(f"Motif/{name}", archive_date)
            entry.extra = timestamp_extra
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes(), compress_type=ZIP_DEFLATED)
    with destination.open("rb") as stream:
        print(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    build(parser.parse_args().destination)
