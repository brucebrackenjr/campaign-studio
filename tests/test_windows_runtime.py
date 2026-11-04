"""Exercise native disk, credential, and locking boundaries on an actual Windows host."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.credentials import (
    KEY,
    CredentialError,
    LocalCredentials,
    UnreadableProtectedKey,
    read_saved_key,
)
from campaign_studio.intake import Intake
from campaign_studio.state import State
from campaign_studio.storage import Storage, StorageError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Exercises native Windows APIs")


def test_current_user_key_is_encrypted_and_reopens(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "Workspace")
    State(storage)
    monkeypatch.delenv(KEY, raising=False)
    settings = Settings.from_environment(data_dir=storage.root)
    environment = {}
    helper = LocalCredentials(settings._credential_file, environment=environment)
    dummy = "local-test-only-dummy-key"
    helper.save(dummy)
    target = settings._credential_file
    assert target.name == "openai-key.dpapi" and dummy.encode() not in target.read_bytes()
    assert read_saved_key(target) == dummy
    assert not list(storage.root.rglob(".env"))
    Settings.from_environment(data_dir=storage.root)
    assert os.environ[KEY] == dummy
    target.write_bytes(b"damaged")
    with pytest.raises(UnreadableProtectedKey):
        read_saved_key(target)
    monkeypatch.delenv(KEY)
    Settings.from_environment(data_dir=storage.root)
    assert KEY not in os.environ and target.read_bytes() == b"damaged"
    helper.save(dummy)
    assert read_saved_key(target) == dummy
    target.unlink()
    target.mkdir()
    with pytest.raises(CredentialError):
        helper.save("dummy-new")
    assert environment[KEY] == dummy


def test_workspace_lock_blocks_other_process_and_releases(tmp_path):
    storage = Storage(tmp_path / "Workspace")
    State(storage)
    script = (
        "import sys; from pathlib import Path; from campaign_studio.storage import Storage;"
        "guard=Storage(Path(sys.argv[1])).exclusive_run(); "
        "guard.__enter__(); guard.__exit__(None,None,None)"
    )
    args = [sys.executable, "-c", script, str(storage.root)]
    with storage.exclusive_run():
        refused = subprocess.run(args, capture_output=True, text=True, timeout=15)
        assert refused.returncode != 0 and "already active" in refused.stderr
    assert subprocess.run(args, capture_output=True, timeout=15).returncode == 0


def test_junction_and_long_readable_paths_preserve_unmanaged_files(tmp_path):
    storage = Storage(tmp_path / "Workspace")
    state = State(storage)
    cid, _ = Intake(storage).new()
    project = storage.project(cid)
    destination = project.ref(
        "Exports/"
        + "/".join(["Long readable folder " * 3 + str(i) for i in range(4)])
        + "/Square - 1080x1080.png"
    )
    project.write(destination, b"readable")
    assert len(str(storage.path(destination))) > 260
    with pytest.raises(FileExistsError):
        storage.write_new(destination, b"must not replace")
    assert storage.path(destination).read_bytes() == b"readable"
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "preserved.txt"
    original.write_bytes(b"mine")
    link = Path(str(storage.path(project.ref("Exports/junction"))).removeprefix("\\\\?\\"))
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
    )
    with pytest.raises(StorageError, match="junction"):
        storage.write(project.ref("Exports/junction/preserved.txt"), b"forbidden")
    preview = Cleanup(state).preview_campaign(cid)
    assert preview["unmanaged_file_count"] == 1
    assert Cleanup(state).delete_campaign(cid, preview["token"])["state"] == "complete"
    assert link.is_junction() and original.read_bytes() == b"mine"
    link.rmdir()


def test_network_and_ambiguous_device_roots_refuse_locally():
    for supplied in (r"\\server\share", r"\\?\UNC\server\share", r"C:relative", r"\rooted"):
        with pytest.raises(StorageError, match="local Windows drive"):
            Storage(Path(supplied))


def test_open_destination_refuses_replacement_without_losing_the_saved_file(tmp_path):
    storage = Storage(tmp_path / "Workspace")
    State(storage)
    cid, _ = Intake(storage).new()
    reference = storage.project(cid).ref("Assets/held-open.txt")
    storage.write(reference, b"original")
    target = storage.path(reference)

    # Windows keeps this file open without delete sharing, as another application may do.
    with target.open("rb") as reader:
        with pytest.raises(PermissionError):
            storage.write(reference, b"replacement")
        assert reader.read() == b"original"
        assert target.read_bytes() == b"original"

    storage.write(reference, b"replacement")
    assert target.read_bytes() == b"replacement"
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
