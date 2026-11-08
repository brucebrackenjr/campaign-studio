"""Dummy-only local credential persistence and deliberate mocked dispatch."""

import io
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_web_workflow import wait_for_work

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.credentials import (
    BODY_LIMIT,
    KEY,
    CredentialError,
    LocalCredentials,
    read_saved_key,
)
from campaign_studio.local_files import local_root
from campaign_studio.provider import OpenAIImages

DUMMY = "dummy-only-new-credential"
OLD = "dummy-only-old-credential"


def setup(tmp_path, existing=b"# Other settings\nCAMPAIGN_STUDIO_PORT=8000\n"):
    target = tmp_path / ".env"
    if existing is not None:
        target.write_bytes(existing)
    environment = {KEY: OLD}
    return LocalCredentials(target, environment=environment), target, environment


@pytest.mark.skipif(os.name == "nt", reason="Windows saves DPAPI ciphertext instead of dotenv")
def test_private_save_preserves_unrelated_multiline_settings_and_removes_duplicate_keys(tmp_path):
    unrelated = "# Keep this comment\nOTHER='two\nlines'\nEMPTY=\n"
    helper, target, environment = setup(
        tmp_path, (unrelated + "OPENAI_API_KEY=old\nexport OPENAI_API_KEY='duplicate'\n").encode()
    )
    special = "dummy-'quoted'\\path#=value"
    result = helper.save(special)
    text = target.read_text()
    assert text.startswith(unrelated) and text.count("OPENAI_API_KEY=") == 1
    assert dotenv_values(stream=io.StringIO(text), interpolate=False)[KEY] == special
    assert environment[KEY] == special and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert result == {
        "saved": True,
        "saved_at": helper.saved_at,
        "available": True,
        "managed_by_environment": False,
        "can_save": True,
    }
    assert special not in repr(helper) + json.dumps(result) + json.dumps(helper.status())
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("value", ["", "dummy with spaces", "dummy\nsecond", "clé", "x" * 4097, {}])
def test_invalid_key_preserves_file_and_process_without_echo(tmp_path, value):
    helper, target, environment = setup(tmp_path)
    before = target.read_bytes()
    with pytest.raises(CredentialError) as error:
        helper.save(value)
    assert error.value.code == "credential_input"
    assert target.read_bytes() == before and environment[KEY] == OLD
    assert "dummy" not in str(error.value)


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "fifo", "parent_alias", "writable"]
)
def test_unsafe_destinations_refuse_without_mutation(tmp_path, kind):
    if os.name == "nt" and kind in {"fifo", "writable"}:
        pytest.skip("POSIX FIFO and mode-bit rules do not apply to Windows")
    parent = tmp_path / "launch"
    parent.mkdir()
    original = tmp_path / "original"
    original.write_bytes(b"original settings")
    target = parent / ".env"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "parent_alias":
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        target = alias / ".env"
    else:
        target.write_bytes(b"unchanged")
        target.chmod(0o666)
    environment = {KEY: OLD}
    with pytest.raises(CredentialError) as error:
        LocalCredentials(target, environment=environment).save(DUMMY)
    assert error.value.code == "credential_save_failed"
    assert original.read_bytes() == b"original settings" and environment[KEY] == OLD
    assert not list(parent.glob("*.tmp"))
    if kind == "writable":
        assert target.read_bytes() == b"unchanged"


@pytest.mark.skipif(os.name == "nt", reason="Windows replaces damaged DPAPI data, not dotenv")
def test_invalid_or_oversize_local_file_is_not_rewritten_or_logged(tmp_path, caplog, capsys):
    for payload in [b"OTHER='private malformed file", b"#" * 131073]:
        helper, target, environment = setup(tmp_path, payload)
        with pytest.raises(CredentialError) as error:
            helper.save(DUMMY)
        assert error.value.code in {"credential_file_invalid", "credential_save_failed"}
        assert target.read_bytes() == payload and environment[KEY] == OLD
    assert "private malformed file" not in caplog.text + capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", ["write", "external_edit", "directory_once", "directory_always", "rollback"]
)
def test_persistence_failures_preserve_working_key_and_report_uncertain_only_when_needed(
    tmp_path, monkeypatch, failure
):
    if os.name == "nt" and failure in {"directory_once", "directory_always", "rollback"}:
        pytest.skip("Windows uses MoveFileExW rather than POSIX directory fsync and rollback")
    helper, target, environment = setup(tmp_path)
    before = target.read_bytes()
    original_sync, original_replace = os.fsync, helper._replace
    directory_calls = 0

    def sync(descriptor):
        nonlocal directory_calls
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if not is_directory and failure == "write":
            raise OSError(DUMMY)
        if not is_directory and failure == "external_edit":
            target.write_bytes(b"OTHER=external edit\n")
        if is_directory:
            directory_calls += 1
            if failure in {"directory_always", "rollback"} or (
                failure == "directory_once" and directory_calls == 1
            ):
                raise OSError(DUMMY)
        return original_sync(descriptor)

    calls = 0

    def replace(*args):
        nonlocal calls
        calls += 1
        if failure == "rollback" and calls == 2:
            raise OSError(DUMMY)
        return original_replace(*args)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(helper, "_replace", replace)
    with pytest.raises(CredentialError) as error:
        helper.save(DUMMY)
    assert error.value.code == (
        "credential_save_uncertain"
        if failure in {"directory_always", "rollback"}
        else "credential_save_failed"
    )
    assert DUMMY not in str(error.value) and environment[KEY] == OLD
    expected = b"OTHER=external edit\n" if failure == "external_edit" else before
    if failure != "rollback":
        assert target.read_bytes() == expected
    else:
        assert dotenv_values(target, interpolate=False)[KEY] == DUMMY
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_save_is_busy_until_durable_activation(tmp_path, monkeypatch):
    import campaign_studio.credentials as credentials

    helper, target, environment = setup(tmp_path)
    entered, release = Event(), Event()
    owner, method = (credentials, "publish_file") if os.name == "nt" else (helper, "_replace")
    original = getattr(owner, method)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, method, held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        flight = pool.submit(helper.save, DUMMY)
        assert entered.wait(5)
        assert environment[KEY] == OLD
        second = LocalCredentials(target, environment=environment)
        with pytest.raises(CredentialError) as error:
            second.save("dummy-other")
        assert error.value.code == "credential_busy"
        release.set()
        assert flight.result(10)["saved"]
    assert environment[KEY] == DUMMY
    assert second.save("dummy-other")["saved"]
    assert environment[KEY] == read_saved_key(target) == "dummy-other"


@pytest.mark.parametrize("explicit", [None, "", "dummy-environment"])
def test_launch_source_is_pinned_and_explicit_environment_wins(tmp_path, monkeypatch, explicit):
    from test_config import distribution

    distribution(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(KEY, raising=False)
    if os.name == "nt":
        target = local_root(tmp_path / "runtime") / "private/openai-key.dpapi"
        target.parent.mkdir(parents=True)
        LocalCredentials(target, environment={}).save("dummy-local")
    else:
        target = tmp_path / ".env"
        target.write_text("OPENAI_API_KEY=dummy-local\n", encoding="utf-8")
    if explicit is not None:
        monkeypatch.setenv(KEY, explicit)
    settings = Settings.from_environment(data_dir=tmp_path / "runtime")
    assert settings._credential_file == target
    assert settings._credential_environment is (explicit is not None)
    assert "dummy" not in repr(settings) + settings.model_dump_json()
    helper = LocalCredentials(
        settings._credential_file,
        managed_by_environment=settings._credential_environment,
        environment=os.environ,
    )
    child = tmp_path / "later-cwd"
    child.mkdir()
    monkeypatch.chdir(child)
    if explicit is None:
        assert helper.save(DUMMY)["saved"] and not (child / ".env").exists()
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
        }
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        reopened = subprocess.run(
            [
                sys.executable,
                "-c",
                "from campaign_studio.config import Settings; "
                "import sys,os,pathlib; sys.prefix=str(pathlib.Path.cwd()/'.venv'); "
                "s=Settings.from_environment(data_dir='runtime'); "
                "assert os.environ['OPENAI_API_KEY']=='dummy-only-new-credential'; "
                "assert not s._credential_environment; print('reopened')",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert reopened.stdout == "reopened\n" and not reopened.stderr
    else:
        before = target.read_bytes()
        with pytest.raises(CredentialError) as error:
            helper.save(DUMMY)
        assert error.value.code == "credential_environment_managed"
        assert target.read_bytes() == before and os.environ[KEY] == explicit


def test_injected_service_cannot_write_implicit_launch_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(KEY, raising=False)
    settings = Settings.from_environment(data_dir=tmp_path / "runtime")
    app = create_app(settings, provider_factory=lambda: pytest.fail("Provider forbidden"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        status = client.get("/api/openai-key").json()
        assert not status["can_save"]
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post("/api/openai-key", json={"api_key": DUMMY})
        assert response.status_code == 503
        assert response.json()["code"] == "credential_setup_unavailable"
    assert not (tmp_path / ".env").exists()


def test_api_save_is_private_and_only_deliberate_start_uses_dummy_key(
    tmp_path, monkeypatch, caplog
):
    state, _, run, response_body = generated_case(tmp_path)
    helper = LocalCredentials(tmp_path / ".env", environment={})
    constructions, requests = [], []

    def transport(request):
        requests.append(request.url.path)
        assert request.headers["authorization"] == f"Bearer {DUMMY}"
        return httpx.Response(200, json=response_body)

    def factory(key):
        constructions.append(True)
        assert key == DUMMY
        return OpenAIImages(key, transport=httpx.MockTransport(transport))

    monkeypatch.setattr("campaign_studio.app.OpenAIImages", factory)
    app = create_app(Settings(data_dir=state.storage.root), credentials=helper)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post("/api/openai-key", json={"api_key": DUMMY})
        assert response.status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.post(
                "/api/openai-key",
                json={"api_key": DUMMY},
                headers={"Origin": "https://other.example"},
            ).status_code
            == 403
        )
        for body in [
            [],
            {"api_key": DUMMY, "path": "/not/allowed"},
            {"api_key": {"secret": DUMMY}},
        ]:
            result = client.post("/api/openai-key", json=body)
            assert result.status_code == 422 and DUMMY not in result.text
        invalid = client.post(
            "/api/openai-key",
            content=b'{"api_key": "dummy-only-malformed',
            headers={"Content-Type": "application/json"},
        )
        assert invalid.status_code == 422 and "dummy-only" not in invalid.text
        result = client.post(
            "/api/openai-key",
            content=b"x" * (BODY_LIMIT + 1),
            headers={"Content-Type": "application/json"},
        )
        assert result.status_code == 413 and DUMMY not in result.text
        start = "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/start"
        action = {"run_id": run, "digest": state.plan(run).digest, "allowance": 1}
        assert client.post(start, json=action).json()["code"] == "missing_key"
        before = state.run(run)
        result = client.post("/api/openai-key", json={"api_key": DUMMY})
        assert result.status_code == 200 and result.json()["saved"]
        assert result.headers["Cache-Control"] == "no-store" and DUMMY not in result.text
        assert client.get("/api/openai-key").json() == {
            "saved_at": helper.saved_at,
            "available": True,
            "managed_by_environment": False,
            "can_save": True,
        }
        assert not constructions and not requests and state.run(run) == before
        assert not state.attempts(run)
        assert client.post(start, json=action).status_code == 202
        complete = wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
        assert complete["completed"] == 12 and len(constructions) == len(requests) == 1
        assert len(state.attempts(run)) == 1
    assert DUMMY not in caplog.text
    assert all(
        DUMMY.encode() not in p.read_bytes() for p in state.storage.root.rglob("*") if p.is_file()
    )


def test_key_save_time_survives_restart_and_failed_save_does_not_advance(tmp_path):
    helper, target, environment = setup(tmp_path, existing=None)
    assert helper.status()["saved_at"] is None
    result = helper.save(DUMMY)
    assert isinstance(result["saved_at"], float)
    reopened = LocalCredentials(target, environment=environment)
    assert abs(reopened.status()["saved_at"] - result["saved_at"]) < 1
    with pytest.raises(CredentialError):
        helper.save("not a valid key")
    assert helper.status()["saved_at"] == result["saved_at"]
    assert DUMMY not in json.dumps(result)


def test_environment_key_load_time_changes_only_with_process_setup(tmp_path, monkeypatch):
    monkeypatch.setattr("campaign_studio.credentials.time.time", lambda: 100.0)
    credentials = LocalCredentials(
        managed_by_environment=True, environment={"OPENAI_API_KEY": DUMMY}
    )
    assert credentials.loaded_at == 100.0
    assert credentials.status()["saved_at"] is None
    monkeypatch.setattr("campaign_studio.credentials.time.time", lambda: 200.0)
    assert credentials.loaded_at == 100.0
    restarted = LocalCredentials(managed_by_environment=True, environment={"OPENAI_API_KEY": DUMMY})
    assert restarted.loaded_at == 200.0
    assert restarted.status()["saved_at"] is None
    assert LocalCredentials(environment={"OPENAI_API_KEY": DUMMY}).loaded_at is None
    assert LocalCredentials(managed_by_environment=True, environment={}).loaded_at is None


@pytest.mark.skipif(os.name == "nt", reason="Windows removes a DPAPI key file")
def test_remove_key_preserves_other_settings_and_does_not_restore_on_restart(tmp_path):
    other = b"# Keep settings\nCAMPAIGN_STUDIO_PORT=8000\nOTHER='two\nlines'\n"
    helper, target, environment = setup(
        tmp_path, other + b"OPENAI_API_KEY=old\nexport OPENAI_API_KEY=duplicate\n"
    )
    result = helper.remove()
    assert result["removed"] is True and result["available"] is False
    assert KEY not in environment and target.read_bytes() == other
    assert not read_saved_key(target)
    assert not list(tmp_path.glob("*.tmp"))
    helper.save(DUMMY)
    assert helper.current_key() == DUMMY


def test_remove_key_is_guarded_and_never_calls_provider(tmp_path):
    helper = LocalCredentials(local_root(tmp_path) / "local-key", environment={})
    helper.save(DUMMY)
    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        credentials=helper,
        provider_factory=lambda: pytest.fail("Provider forbidden"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.request("DELETE", "/api/openai-key", json={}).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.request(
                "DELETE", "/api/openai-key", json={}, headers={"Origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert helper.current_key() == DUMMY
        result = client.request("DELETE", "/api/openai-key", json={})
        assert result.status_code == 200 and result.json()["removed"] is True
        assert DUMMY not in result.text
        assert client.get("/api/openai-key").json()["available"] is False
        if os.name == "nt":
            assert not helper.destination.exists()
        else:
            assert not read_saved_key(helper.destination)


def test_remove_failure_or_environment_key_preserves_current_key(tmp_path, monkeypatch):
    helper, target, environment = setup(tmp_path)
    helper.save(DUMMY)
    before = target.read_bytes()

    def fail(_value):
        raise OSError("blocked")

    monkeypatch.setattr(helper, "_persist", fail)
    with pytest.raises(CredentialError) as error:
        helper.remove()
    assert error.value.code == "credential_remove_failed"
    assert target.read_bytes() == before and environment[KEY] == DUMMY
    helper.managed_by_environment = True
    with pytest.raises(CredentialError) as error:
        helper.remove()
    assert error.value.code == "credential_environment_managed"
    assert target.read_bytes() == before and environment[KEY] == DUMMY
