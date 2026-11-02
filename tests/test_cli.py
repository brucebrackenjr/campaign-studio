import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import pytest

from campaign_studio.cleanup import Cleanup
from campaign_studio.cli import main
from campaign_studio.intake import Intake
from campaign_studio.state import State
from campaign_studio.storage import Storage


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    # Never read the developer's real project .env in configuration tests.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    monkeypatch.chdir(tmp_path)


def test_invalid_configuration_is_actionable_without_echoing_input(monkeypatch, capsys):
    monkeypatch.setenv("CAMPAIGN_STUDIO_PORT", "sensitive-supplied-value")
    assert main(["smoke"]) == 2
    error = capsys.readouterr().err
    assert "port" in error
    assert "sensitive-supplied-value" not in error


def test_empty_storage_setting_is_rejected(monkeypatch, capsys):
    monkeypatch.setenv("CAMPAIGN_STUDIO_DATA_DIR", "")
    assert main(["smoke"]) == 2
    assert "data_dir" in capsys.readouterr().err


def test_module_launcher_runs_installed_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPAIGN_STUDIO_DATA_DIR", str(tmp_path / "runtime"))
    result = subprocess.run(
        [sys.executable, "-m", "campaign_studio", "smoke"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert json.loads(result.stdout)["evidence"] == "synthetic-installation-check"


@pytest.mark.parametrize("pending_cleanup", [False, True])
def test_occupied_port_refuses_before_opening_or_resuming_workspace(
    tmp_path, monkeypatch, pending_cleanup
):
    root = tmp_path / "runtime"
    if pending_cleanup:
        storage = Storage(root)
        state = State(storage)
        campaign, _ = Intake(storage).new()
        cleanup = Cleanup(state)
        preview = cleanup.preview_campaign(campaign)
        cleanup.delete_campaign(campaign, preview["token"], defer_cleanup=True)
        with storage.connect() as db:
            before_rows = list(db.iterdump())
        before_files = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setenv("CAMPAIGN_STUDIO_PORT", str(port))
        result = subprocess.run(
            [sys.executable, "-m", "campaign_studio", "serve", "--data-dir", str(root)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 1
    assert f"Port {port} is already in use" in result.stderr
    assert "CAMPAIGN_STUDIO_PORT" in result.stderr
    assert "Application startup" not in result.stderr
    assert "Traceback" not in result.stderr
    assert str(root) not in result.stderr
    if pending_cleanup:
        assert {
            p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
        } == before_files
        with storage.connect() as db:
            assert list(db.iterdump()) == before_rows
        assert cleanup.status(campaign)["campaign_state"] == "pending"
    else:
        assert not root.exists()


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_real_server_starts_on_loopback_and_shuts_down(tmp_path, monkeypatch, stop_signal):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("CAMPAIGN_STUDIO_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CAMPAIGN_STUDIO_PORT", str(port))
    # Explicit launcher settings must win over broad Uvicorn environment defaults.
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")
    opener = build_opener(ProxyHandler({}))
    started = time.monotonic()
    ready_seconds = None
    failure = None
    shutdown_failed = False
    process = subprocess.Popen(
        [sys.executable, "-m", "campaign_studio", "serve"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Fresh setup composes the bundled campaign before the server becomes ready.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                failure = "The local server exited before becoming ready."
                break
            try:
                with opener.open(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                    assert json.load(response)["stage"] == "workflow"
                with opener.open(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                    assert "/static/motif/actions.js" in response.read().decode()
                ready_seconds = time.monotonic() - started
                break
            except URLError, TimeoutError:
                time.sleep(0.05)
        else:
            failure = "The local server did not become ready within 30 seconds."
    finally:
        process.send_signal(stop_signal)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            shutdown_failed = True
        receipt = tmp_path / "runtime/private/bundled-campaigns.json"
        try:
            setup = json.loads(receipt.read_text()) if receipt.is_file() else {}
        except OSError, ValueError:
            setup = {"state": "unreadable"}
        print(
            "CLI startup measurement: "
            + json.dumps(
                {
                    "ready_seconds": ready_seconds,
                    "total_seconds": time.monotonic() - started,
                    "returncode": process.returncode,
                    "setup_state": setup.get("state"),
                    "setup_elapsed_seconds": setup.get("elapsed_seconds"),
                    "setup_campaigns": len(setup.get("campaigns", [])),
                }
            )
        )
        print("CLI stdout:\n" + stdout)
        print("CLI stderr:\n" + stderr)
    if failure or shutdown_failed:
        pytest.fail(
            "\n".join(
                item
                for item in (
                    failure,
                    "The local server did not stop cleanly." if shutdown_failed else None,
                )
                if item
            )
        )
    # Uvicorn completes lifespan shutdown; Control-C has the conventional CLI exit code.
    assert process.returncode == (130 if stop_signal == signal.SIGINT else -signal.SIGTERM)
    assert "Application shutdown complete." in stderr
    assert "Traceback" not in stderr
    assert f"127.0.0.1:{port}" in stdout
    assert "0.0.0.0" not in stdout + stderr
