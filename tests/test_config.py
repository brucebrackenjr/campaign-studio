import json
import os
import subprocess
import sys


def distribution(directory):
    (directory / "src/campaign_studio").mkdir(parents=True)
    (directory / "src/campaign_studio/cli.py").touch()
    (directory / "pyproject.toml").write_text('[project]\nname="campaign-studio"\n')


def run_config(directory, environment, *, install=None):
    install = install or directory
    distribution(install)

    return subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import os
import sys
sys.prefix = sys.argv[1]
from campaign_studio.config import Settings
settings = Settings.from_environment(data_dir=sys.argv[2])
assert 'api_key' not in Settings.model_fields
assert 'synthetic-dotenv-only' not in repr(settings)
assert 'synthetic-dotenv-only' not in settings.model_dump_json()
loaded = os.environ.get('OPENAI_API_KEY') == 'synthetic-dotenv-only'
print(json.dumps({'port': settings.port, 'synthetic_key_loaded': loaded}))
""",
            str(install / ".venv"),
            str(directory / "Workspace"),
        ],
        cwd=directory,
        env={
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PYTHONPATH"}
        }
        | environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_distribution_dotenv_loads_without_exposing_key_and_environment_wins(tmp_path):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=synthetic-dotenv-only\nCAMPAIGN_STUDIO_PORT=8111\n"
    )
    result = run_config(tmp_path, {"CAMPAIGN_STUDIO_PORT": "8222"})
    if os.name == "nt":
        assert result.returncode != 0 and "encrypted storage" in result.stderr
        assert "synthetic-dotenv-only" not in result.stderr
        return
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"port": 8222, "synthetic_key_loaded": True}
    assert result.stderr == ""


def test_dotenv_ignores_working_directory(tmp_path):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=synthetic-dotenv-only\nCAMPAIGN_STUDIO_PORT=8111\n"
    )
    child = tmp_path / "child"
    child.mkdir()
    result = run_config(tmp_path, {}, install=child)
    assert json.loads(result.stdout) == {"port": 8000, "synthetic_key_loaded": False}


def test_dotenv_symlink_is_rejected_before_loading(tmp_path):
    target = tmp_path / "settings.txt"
    target.write_text("OPENAI_API_KEY=synthetic-dotenv-only\n")
    (tmp_path / ".env").symlink_to(target)
    distribution(tmp_path)
    from unittest.mock import patch

    import pytest

    from campaign_studio.config import ConfigurationError, Settings

    with (
        patch.object(sys, "prefix", str(tmp_path / ".venv")),
        pytest.raises(ConfigurationError, match="symbolic link"),
    ):
        Settings.from_environment(data_dir=tmp_path / "Workspace")
