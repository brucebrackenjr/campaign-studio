import json
from pathlib import Path

import pytest

from campaign_studio.cli import main
from campaign_studio.config import ConfigurationError, Settings
from campaign_studio.storage import Storage


@pytest.fixture
def locations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("campaign_studio.config.sys.platform", "darwin")
    (launch / "src/campaign_studio").mkdir(parents=True)
    (launch / "src/campaign_studio/cli.py").touch()
    (launch / "pyproject.toml").write_text('[project]\nname="campaign-studio"\n')
    monkeypatch.setattr("sys.prefix", str(launch / ".venv"))
    monkeypatch.chdir(launch)
    for name in ("CAMPAIGN_STUDIO_DATA_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home, launch


def test_default_is_stable_and_other_workspaces_require_explicit_selection(locations, monkeypatch):
    home, launch = locations
    expected = launch / "Workspace"
    assert Settings.from_environment().data_dir == expected
    another = launch / "another"
    another.mkdir()
    monkeypatch.chdir(another)
    assert Settings.from_environment().data_dir == expected
    explicit = another / ".campaign-studio"
    explicit.mkdir()
    (explicit / "saved.txt").write_text("existing work")
    assert Settings.from_environment().data_dir == expected
    assert not expected.exists()
    monkeypatch.setenv("CAMPAIGN_STUDIO_DATA_DIR", str(explicit))
    assert Settings.from_environment().data_dir == explicit
    assert Settings.from_environment(data_dir=expected).data_dir == expected
    assert (explicit / "saved.txt").read_text() == "existing work"


def test_portable_workspace_leaves_existing_user_data_untouched(locations, monkeypatch, capsys):
    home, launch = locations
    monkeypatch.setattr("campaign_studio.config.sys.platform", "linux")
    old = home / ".local/share/motif/private/state.sqlite3"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"existing")
    assert Settings.from_environment().data_dir == launch / "Workspace"
    explicit = launch / "Workspace"
    assert main(["workspace"]) == 0
    assert json.loads(capsys.readouterr().out)["location"] == str(explicit)
    assert not explicit.exists() and old.read_bytes() == b"existing"


def test_unrelated_interpreter_never_guesses_workspace(locations, monkeypatch):
    _, launch = locations
    monkeypatch.setattr("sys.prefix", str(launch / "unrelated"))
    with pytest.raises(ConfigurationError, match="could not identify"):
        Settings.from_environment()
    assert Settings.from_environment(data_dir=launch / "chosen").data_dir == launch / "chosen"


def test_copied_launcher_cannot_open_previous_environment(locations, monkeypatch):
    _, old = locations
    moved_launcher = old.parent / "second" / ".venv" / "bin" / "campaign-studio"
    monkeypatch.setattr("sys.argv", [str(moved_launcher), "serve"])
    with pytest.raises(ConfigurationError, match="Recreate .venv"):
        Settings.from_environment()
    assert not (old / "Workspace").exists()


def test_storage_inventory_does_not_follow_symlinks_or_read_content(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "actual.txt").write_bytes(b"1234")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"not counted")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    summary = Storage(root).summary()
    assert summary["used_bytes"] == 4
    assert summary["file_count"] == 1
    assert "not counted" not in json.dumps(summary)
