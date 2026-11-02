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
    monkeypatch.chdir(launch)
    for name in ("CAMPAIGN_STUDIO_DATA_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home, launch


def test_default_is_stable_and_other_workspaces_require_explicit_selection(locations, monkeypatch):
    home, launch = locations
    expected = home / "Library/Application Support/Motif"
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


def test_linux_xdg_and_cli_summary_do_not_create_or_migrate_data(locations, monkeypatch, capsys):
    home, launch = locations
    monkeypatch.setattr("campaign_studio.config.sys.platform", "linux")
    assert Settings.from_environment().data_dir == home / ".local/share/motif"
    xdg = home / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert Settings.from_environment().data_dir == xdg / "motif"
    explicit = launch / "chosen"
    assert main(["workspace", "--data-dir", str(explicit)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["location"] == str(explicit)
    assert summary["used_bytes"] == summary["file_count"] == 0
    assert summary["free_bytes"] > 0
    assert not explicit.exists()
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    with pytest.raises(ConfigurationError, match="absolute"):
        Settings.from_environment()


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
