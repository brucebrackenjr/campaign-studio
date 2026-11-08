import os
import shutil
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import pytest

COMMIT_DATES = ("2026-07-07T19:32:17-07:00", "2026-11-07T19:32:17-08:00")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/package_source.py"


def git(repo, *arguments, **environment):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Package test",
            "-c",
            "user.email=package-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=repo,
        env={**os.environ, **environment},
        check=True,
        text=True,
        capture_output=True,
    )


def checkout(tmp_path, date):
    repo = tmp_path / "source"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "scripts/package_source.py")
    (repo / "README.md").write_text("Portable source package.\n")
    (repo / ".env.example").write_text("OPENAI_API_KEY=\n")
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "Fixture", GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return repo


def package(repo, path, timezone="UTC", *, check=True):
    return subprocess.run(
        [sys.executable, str(repo / "scripts/package_source.py"), str(path)],
        cwd=repo,
        env={**os.environ, "TZ": timezone},
        check=check,
        text=True,
        capture_output=True,
    )


def extra_fields(data):
    fields = {}
    while data:
        tag, size = struct.unpack_from("<HH", data)
        assert len(data) >= 4 + size
        assert tag not in fields
        fields[tag] = data[4 : 4 + size]
        data = data[4 + size :]
    return fields


@pytest.mark.parametrize("date", COMMIT_DATES)
def test_source_commit_time_is_recorded_in_local_and_central_zip_headers(tmp_path, date):
    repo = checkout(tmp_path, date)
    destination = tmp_path / "source.zip"
    package(repo, destination)
    committed_at = datetime.fromisoformat(date)
    expected_epoch = int(committed_at.timestamp())
    with ZipFile(destination) as archive, destination.open("rb") as stream:
        assert archive.testzip() is None
        assert archive.namelist() == [
            "Motif/.env.example",
            "Motif/README.md",
            "Motif/scripts/package_source.py",
        ]
        for entry in archive.infolist():
            # DOS can represent only even seconds; both extra fields retain 17.
            assert entry.date_time == (*committed_at.timetuple()[:5], 16)
            stream.seek(entry.header_offset)
            local_header = stream.read(30)
            assert local_header[:4] == b"PK\x03\x04"
            name_size, extra_size = struct.unpack_from("<HH", local_header, 26)
            stream.read(name_size)
            local_extra = stream.read(extra_size)
            assert local_extra == entry.extra
            fields = extra_fields(entry.extra)
            assert struct.unpack("<Bi", fields[0x5455]) == (1, expected_epoch)
            assert struct.unpack("<ii", fields[0x5855]) == (expected_epoch, expected_epoch)
            reserved, tag, size, modified, accessed, created = struct.unpack(
                "<IHHQQQ", fields[0x000A]
            )
            assert (reserved, tag, size) == (0, 1, 24)
            assert modified == accessed == created
            assert modified // 10_000_000 - 11_644_473_600 == expected_epoch
            assert (
                archive.read(entry) == (repo / entry.filename.removeprefix("Motif/")).read_bytes()
            )


def test_rebuilding_ignores_checkout_file_times_and_build_machine_timezone(tmp_path):
    repo = checkout(tmp_path, COMMIT_DATES[1])
    first = tmp_path / "first.zip"
    package(repo, first, "America/Los_Angeles")
    for path in repo.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            os.utime(path, (946684800, 946684800))
    second = tmp_path / "second.zip"
    package(repo, second, "Asia/Tokyo")
    assert first.read_bytes() == second.read_bytes()
    assert package(repo, first, check=False).returncode != 0
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires native macOS extraction tools.")
@pytest.mark.parametrize("date", COMMIT_DATES)
@pytest.mark.parametrize("extractor", ["unzip", "ditto"])
def test_macos_extraction_keeps_exact_commit_modification_time(tmp_path, date, extractor):
    repo = checkout(tmp_path, date)
    destination = tmp_path / "source.zip"
    package(repo, destination, "UTC")
    extracted = tmp_path / "extracted"
    arguments = (
        ["/usr/bin/unzip", "-q", str(destination), "-d", str(extracted)]
        if extractor == "unzip"
        else ["/usr/bin/ditto", "-x", "-k", str(destination), str(extracted)]
    )
    subprocess.run(
        arguments,
        env={**os.environ, "TZ": "America/Los_Angeles"},
        check=True,
        capture_output=True,
    )
    expected_epoch = int(datetime.fromisoformat(date).timestamp())
    files = [p for p in extracted.rglob("*") if p.is_file()]
    assert len(files) == 3
    for path in files:
        assert path.stat().st_mtime_ns == expected_epoch * 1_000_000_000
        assert path.read_bytes() == (repo / path.relative_to(extracted / "Motif")).read_bytes()


@pytest.mark.parametrize(
    "name",
    [
        "Workspace/private/state.sqlite3",
        "private/notes.txt",
        ".env",
        "keys/test.pem",
        ".venv/config",
    ],
)
def test_private_files_are_refused_even_when_git_tracked(tmp_path, name):
    repo = checkout(tmp_path, COMMIT_DATES[1])
    private = repo / name
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(b"Do not distribute this private fixture.")
    git(repo, "add", "--force", name)
    destination = tmp_path / "refused.zip"
    result = package(repo, destination, check=False)
    assert result.returncode != 0
    assert "Private file is outside the distribution" in result.stderr
    with ZipFile(destination) as archive:
        assert f"Motif/{name}" not in archive.namelist()


def test_tracked_symlink_is_refused(tmp_path):
    repo = checkout(tmp_path, COMMIT_DATES[1])
    secret = tmp_path / "outside.txt"
    secret.write_text("Outside the distribution.\n")
    (repo / "linked.txt").symlink_to(secret)
    git(repo, "add", "linked.txt")
    result = package(repo, tmp_path / "refused.zip", check=False)
    assert result.returncode != 0
    assert "Expected a regular distribution file: linked.txt" in result.stderr
