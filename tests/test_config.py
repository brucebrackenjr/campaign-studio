import json
import subprocess
import sys


def run_config(directory, environment):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import os
from campaign_studio.config import Settings
settings = Settings.from_environment()
assert 'api_key' not in Settings.model_fields
assert 'synthetic-dotenv-only' not in repr(settings)
assert 'synthetic-dotenv-only' not in settings.model_dump_json()
loaded = os.environ.get('OPENAI_API_KEY') == 'synthetic-dotenv-only'
print(json.dumps({'port': settings.port, 'synthetic_key_loaded': loaded}))
""",
        ],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_current_directory_dotenv_loads_without_exposing_key_and_environment_wins(tmp_path):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=synthetic-dotenv-only\nCAMPAIGN_STUDIO_PORT=8111\n"
    )
    result = run_config(tmp_path, {"CAMPAIGN_STUDIO_PORT": "8222"})
    assert json.loads(result.stdout) == {"port": 8222, "synthetic_key_loaded": True}
    assert result.stderr == ""


def test_dotenv_never_searches_parent_directories(tmp_path):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=synthetic-dotenv-only\nCAMPAIGN_STUDIO_PORT=8111\n"
    )
    child = tmp_path / "child"
    child.mkdir()
    result = run_config(child, {})
    assert json.loads(result.stdout) == {"port": 8000, "synthetic_key_loaded": False}


def test_dotenv_symlink_is_rejected_before_loading(tmp_path):
    target = tmp_path / "settings.txt"
    target.write_text("OPENAI_API_KEY=synthetic-dotenv-only\n")
    (tmp_path / ".env").symlink_to(target)
    result = subprocess.run(
        [sys.executable, "-m", "campaign_studio", "smoke"],
        cwd=tmp_path,
        env={},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert "synthetic-dotenv-only" not in result.stderr
