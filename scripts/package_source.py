"""Build the portable source ZIP from Git-managed distribution files."""

import argparse
import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path):
    top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, text=True)
    if Path(top.strip()).resolve() != ROOT:
        raise ValueError("Build the source ZIP from Motif’s own Git checkout.")
    committed_at = subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, text=True
    )
    # Keep repeated packages stable while showing the source commit's actual date.
    archive_date = datetime.fromtimestamp(int(committed_at.strip()), UTC).timetuple()[:6]
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
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes(), compress_type=ZIP_DEFLATED)
    with destination.open("rb") as stream:
        print(f"{hashlib.file_digest(stream, 'sha256').hexdigest()}  {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    build(parser.parse_args().destination)
