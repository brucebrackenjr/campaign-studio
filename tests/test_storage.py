import sqlite3

import pytest

from campaign_studio.storage import Storage, StorageError


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "runtime")
    store.initialize()
    return store


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "/etc/passwd",
        "../outside",
        "smoke/../../outside",
        "smoke//item",
        "smoke/./item",
        "smoke\\outside",
        "C:/outside",
        ".hidden",
    ],
)
def test_rejects_unsafe_references(storage, reference):
    with pytest.raises(StorageError):
        storage.path(reference)


def test_rejects_symlinked_file_and_directory(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "original.txt"
    original.write_text("preserve me")
    (storage.root / "linked").symlink_to(outside, target_is_directory=True)
    (storage.root / "file.txt").symlink_to(original)
    for reference in ("linked/original.txt", "linked/new.txt", "file.txt"):
        with pytest.raises(StorageError):
            storage.path(reference)
    assert original.read_text() == "preserve me"


def test_rejects_symlinked_runtime_root_before_writing(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(StorageError):
        Storage(alias).initialize()
    assert list(target.iterdir()) == []


def test_sqlite_connections_commit_rollback_and_close(storage):
    with storage.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO probe VALUES (?)", ("saved",))
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError), storage.connect() as connection:
        connection.execute("INSERT INTO probe VALUES (?)", ("discarded",))
        raise RuntimeError("simulated local operation failure")
    with storage.connect() as connection:
        assert connection.execute("SELECT value FROM probe").fetchall() == [("saved",)]
    assert storage.path("private/state.sqlite3").stat().st_mode & 0o777 == 0o600


def test_reinitializing_preserves_existing_files(storage):
    source = storage.path("Projects/source.txt")
    source.write_text("original")
    storage.initialize()
    assert source.read_text() == "original"


def test_readable_delivery_names_keep_private_and_path_boundaries(storage, tmp_path):
    reference = (
        "Projects/Autumn-12345678/Exports/Autumn – Café/images/Maple chair/"
        "English (United States)/Square - 1080x1080.png"
    )
    storage.write(reference, b"verified image")
    assert storage.path(reference).read_bytes() == b"verified image"
    for unsafe in (
        "Projects/Autumn-12345678/Exports/../private/key",
        "Projects/Autumn-12345678/Exports//file",
        "Projects/Autumn-12345678/Exports/.hidden",
        "Projects/Autumn-12345678/Exports/CON.txt",
        "Projects/Autumn-12345678/Exports/a\\b",
        "Projects/Autumn-12345678/Exports/a:stream",
        "Projects/Autumn-12345678/Exports/a#b",
        "Projects/Autumn-12345678/Exports/a%2fb",
        "Projects/Autumn-12345678/Exports/a\nfile",
        "private/Readable name",
        "campaigns/Café",
    ):
        with pytest.raises(StorageError):
            storage.path(unsafe)
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / "Projects/Autumn-12345678/Exports" / "linked folder").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(StorageError):
        storage.write("Projects/Autumn-12345678/Exports/linked folder/file.png", b"no")
    assert not list(outside.iterdir())
