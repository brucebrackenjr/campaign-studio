"""Crash actual app processes; do not substitute State reinstantiation for restart."""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from campaign_fixtures import authored_edit
from test_provider_runner import generated_case

from campaign_studio.state import State
from campaign_studio.storage import Storage


def wait_until(check, limit=30):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.05)
    pytest.fail("App subprocess did not reach the expected checkpoint")


@contextmanager
def process(root, mode="normal"):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    log = (root / f"server-{mode}.log").open("a")
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("durability_worker.py")),
            str(root),
            str(port),
            mode,
        ],
        cwd=root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)

    def ready():
        assert child.poll() is None, (root / f"server-{mode}.log").read_text()
        try:
            return client.get("/")
        except httpx.TransportError:
            return None

    try:
        home = wait_until(ready)
        client.headers["X-Campaign-Token"] = re.search(
            r'<meta name="campaign-token" content="([^"]+)"', home.text
        )[1]
        yield child, client
    finally:
        client.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        log.close()


def fixture(root):
    state, _, run_id, body = generated_case(root)
    (root / "response.json").write_text(json.dumps(body))
    return state, run_id


def model(client):
    response = client.get("/api/motif/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde")
    assert response.status_code == 200, response.text
    return response.json()


def start(client, state, run_id):
    response = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/start",
        json={"run_id": run_id, "digest": state.plan(run_id).digest, "allowance": 1},
    )
    assert response.status_code == 202, response.text


def settle(client):
    def done():
        response = client.get("/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/status")
        assert response.status_code == 200
        value = response.json()
        return value if not value["active"] else None

    return wait_until(done)


def recover(client, run_id):
    response = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/recover", json={"run_id": run_id}
    )
    assert response.status_code == 202, response.text
    return settle(client)


def dispatch_count(root):
    return len((root / "dispatches.jsonl").read_text().splitlines())


def test_killed_after_intent_restarts_unknown_without_duplicate_dispatch(tmp_path):
    state, run_id = fixture(tmp_path)
    with process(tmp_path, "intent") as (child, client):
        start(client, state, run_id)
        wait_until(lambda: (tmp_path / "checkpoint").exists())
        assert state.attempts(run_id)[0]["state"] == "dispatching"
        child.kill()
        child.wait(timeout=10)
    with process(tmp_path) as (_, client):
        assert state.attempts(run_id)[0]["state"] == "unknown"
        assert state.run(run_id)["state"] == "paused"
        assert dispatch_count(tmp_path) == 1
        recovered = recover(client, run_id)
        assert not recovered["active"]
        data = model(client)
        assert data["manifest"]["metrics"]["completed_images"] == 6
        assert not data["manifest"]["complete"]
        assert data["retry_plan"]["previous_attempts"] == 1
        assert data["retry_plan"]["allowance"] == 2
        assert len(state.attempts(run_id)) == dispatch_count(tmp_path) == 1


def test_killed_during_composition_recovers_and_reopens_draft_revision_metadata(tmp_path):
    state, run_id = fixture(tmp_path)
    with process(tmp_path, "composition") as (child, client):
        start(client, state, run_id)
        wait_until(lambda: (tmp_path / "checkpoint").exists())
        assert state.attempts(run_id)[0]["state"] == "decoded"
        assert state.run(run_id)["current_revision"] is None
        revisions = next((tmp_path / "runtime" / "Campaigns").glob(f"*/Runs/{run_id}/Revisions"))
        assert len(list(revisions.rglob("*.png"))) == 1
        assert state.revision_count(run_id) == 0
        child.kill()
        child.wait(timeout=10)
    with process(tmp_path) as (_, client):
        recover(client, run_id)
        data = model(client)
        assert data["manifest"]["complete"]
        assert data["manifest"]["metrics"]["completed_images"] == 12
        revision = data["manifest"]["revision_id"]
        base = (
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde"
            f"/runs/{run_id}/revisions/{revision}/metadata"
        )
        notes = client.post(
            base + "/product-1/en-US/1x1",
            json={
                "mode": "current",
                "expected_version": 0,
                "description": "Un círculo azul en un espacio claro.",
            },
        )
        assert notes.status_code == 200, notes.text
        data["value"]["name"] = "Saved draft after recovery"
        action = authored_edit(client, data["value"]["campaign_id"])
        action["authored"]["brief"] = data["value"]
        saved = client.post(
            "/api/motif/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/authored",
            json=action,
        )
        assert saved.status_code == 200, saved.text
        hashes = {
            o["id"]: hashlib.sha256(client.get(o["url"]).content).hexdigest()
            for o in data["manifest"]["outputs"]
        }
    with process(tmp_path) as (_, client):
        reopened = model(client)
        assert reopened["project"]["authored"]["brief"] == data["value"]
        assert reopened["version"] == saved.json()["version"]
        assert reopened["manifest"]["revision_id"] == revision
        assert reopened["metadata"] == notes.json()
        assert hashes == {
            o["id"]: hashlib.sha256(client.get(o["url"]).content).hexdigest()
            for o in reopened["manifest"]["outputs"]
        }
        assert len(state.attempts(run_id)) == dispatch_count(tmp_path) == 1


@pytest.mark.parametrize("valid_image", [False, True])
def test_killed_after_response_recovers_before_first_revision(tmp_path, valid_image):
    state, run_id = fixture(tmp_path)
    body = json.loads((tmp_path / "response.json").read_text())
    if not valid_image:
        body["data"] = [None]
    (tmp_path / "response.json").write_text(json.dumps(body))
    with process(tmp_path, "received") as (child, client):
        start(client, state, run_id)
        wait_until(lambda: (tmp_path / "checkpoint").exists())
        attempt = state.attempts(run_id)[0]
        assert attempt["state"] == "dispatching" and attempt["usage_json"] is None
        assert json.loads(state.storage.path(attempt["response_ref"]).read_bytes()) == body
        child.kill()
        child.wait(timeout=10)
    with process(tmp_path) as (_, client):
        assert state.attempts(run_id)[0]["state"] == "received"
        assert state.revision_count(run_id) == 0
        for _ in range(2):
            recover(client, run_id)
            attempt = state.attempts(run_id)[0]
            assert attempt["state"] == ("decoded" if valid_image else "received")
            assert json.loads(attempt["usage_json"]) == body["usage"]
            data = model(client)
            assert data["manifest"]["metrics"]["completed_images"] == (12 if valid_image else 6)
            totals = data["campaign_metrics"]
            assert totals["attempted_requests"] == 1
            assert totals["usage_cost_estimate"]["estimated_usd"] == 0.00108
        assert dispatch_count(tmp_path) == len(state.attempts(run_id)) == 1


def test_committed_copy_save_with_lost_response_reopens_once_and_rejects_old_retry(tmp_path):
    state, run_id = fixture(tmp_path)
    with process(tmp_path, "response") as (child, client):
        start(client, state, run_id)
        settle(client)
        before = model(client)
        values = before["value"]["markets"]["es-MX"]["copy"] | {"message": "Para cada día."}
        edit = {
            "ratio": "1:1",
            "run_id": run_id,
            "revision_id": before["manifest"]["revision_id"],
            "market": "es-MX",
            "product_id": "product-1",
            "field_scopes": {"message": "all_products", "cta": "all_products"},
            "values": values,
        }
        preview = client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/preview", json=edit
        )
        assert preview.status_code == 200, preview.text
        edit["preview_digest"] = preview.json()["digest"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.post,
                "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply",
                json=edit,
                headers={"X-Test-Hold-Response": "yes"},
            )
            wait_until(lambda: (tmp_path / "checkpoint").exists())
            assert state.run(run_id)["current_revision"] != edit["revision_id"]
            child.kill()
            child.wait(timeout=10)
            with pytest.raises(httpx.TransportError):
                pending.result(timeout=10)
    with process(tmp_path) as (_, client):
        saved = model(client)
        assert saved["value"]["markets"]["es-MX"]["copy"] == values
        assert "revisions" not in saved
        assert state.revision_count(run_id) == 2
        retry = client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply", json=edit
        )
        assert retry.status_code == 409
        after = model(client)
        assert after["manifest"]["revision_id"] == saved["manifest"]["revision_id"]
        assert "revisions" not in after
        assert state.revision_count(run_id) == 2
        assert (
            sum(
                a["sha256"] != b["sha256"]
                for a, b in zip(
                    before["manifest"]["outputs"], saved["manifest"]["outputs"], strict=True
                )
            )
            == 6
        )
        assert len(State(Storage(tmp_path / "runtime")).attempts(run_id)) == 1
        assert dispatch_count(tmp_path) == 1
