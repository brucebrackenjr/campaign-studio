import os
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
        "private/NUL.txt",
        "Campaigns/demo/CONIN$",
        "Campaigns/demo/LPT¹.png",
        "Campaigns/demo/NUL .png",
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
    if os.name != "nt":
        assert storage.path("private/state.sqlite3").stat().st_mode & 0o777 == 0o600


def test_startup_does_not_create_smoke_directory(tmp_path):
    store = Storage(tmp_path / "workspace")
    store.initialize()
    assert not (store.root / "smoke").exists()


def test_startup_removes_only_empty_legacy_smoke_directory(storage):
    smoke = storage.root / "smoke"
    smoke.mkdir()
    storage.initialize()
    assert not smoke.exists()
    smoke.mkdir()
    proof = smoke / "result.txt"
    proof.write_text("existing check results")
    storage.initialize()
    assert proof.read_text() == "existing check results"


def test_reinitializing_preserves_existing_files(storage):
    from campaign_studio.state import State

    State(storage)
    source = storage.path("Campaigns/source.txt")
    source.write_text("original")
    storage.initialize()
    assert source.read_text() == "original"


def test_readable_delivery_names_keep_private_and_path_boundaries(storage, tmp_path):
    reference = (
        "Campaigns/Autumn-12345678/Exports/Autumn – Café/images/Maple chair/"
        "English (United States)/Square - 1080x1080.png"
    )
    storage.write(reference, b"verified image")
    assert storage.path(reference).read_bytes() == b"verified image"
    for unsafe in (
        "Campaigns/Autumn-12345678/Exports/../private/key",
        "Campaigns/Autumn-12345678/Exports//file",
        "Campaigns/Autumn-12345678/Exports/.hidden",
        "Campaigns/Autumn-12345678/Exports/CON.txt",
        "Campaigns/Autumn-12345678/Exports/a\\b",
        "Campaigns/Autumn-12345678/Exports/a:stream",
        "Campaigns/Autumn-12345678/Exports/a#b",
        "Campaigns/Autumn-12345678/Exports/a%2fb",
        "Campaigns/Autumn-12345678/Exports/a\nfile",
        "private/Readable name",
        "campaigns/Café",
    ):
        with pytest.raises(StorageError):
            storage.path(unsafe)
    outside = tmp_path / "outside"
    outside.mkdir()
    (storage.root / "Campaigns/Autumn-12345678/Exports" / "linked folder").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(StorageError):
        storage.write("Campaigns/Autumn-12345678/Exports/linked folder/file.png", b"no")
    assert not list(outside.iterdir())


def test_newer_workspace_is_rejected_without_modifying_database(storage):
    from campaign_studio.state import State
    from campaign_studio.storage import WORKSPACE_SCHEMA

    State(storage)
    with storage.connect() as db:
        db.execute(f"PRAGMA user_version={WORKSPACE_SCHEMA + 1}")
    path = storage.path("private/state.sqlite3")
    before = path.read_bytes()
    with pytest.raises(StorageError, match="newer version"):
        State(Storage(storage.root))
    assert path.read_bytes() == before


def test_schema15_workspace_upgrades_and_keeps_current_campaign(tmp_path):
    from campaign_studio.campaign_drafts import read
    from campaign_studio.intake import Intake
    from campaign_studio.state import State
    from campaign_studio.storage import WORKSPACE_SCHEMA

    storage = Storage(tmp_path / "workspace")
    State(storage)
    cid, document = Intake(storage).new(name="Saved before upgrade")
    with storage.connect() as db:
        db.execute("PRAGMA user_version=15")
    State(storage)
    assert read(storage, cid)["authored"]["brief"]["name"] == "Saved before upgrade"
    with storage.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == WORKSPACE_SCHEMA
