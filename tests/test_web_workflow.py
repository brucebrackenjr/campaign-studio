import asyncio
import json
import re
import time
from pathlib import Path

import httpx
import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample


def page_data(client, response):
    """Read the current client shell and expose its data for workflow assertions."""
    assert response.status_code == 200
    assert "/static/motif/actions.js" in response.text
    campaign = re.search(r'data-campaign="([^"]+)"', response.text)[1]
    result = client.get(f"/api/motif/campaigns/{campaign}")
    assert result.status_code == 200, result.text
    data = result.json()
    run, plan, manifest = data.get("run") or {}, data.get("plan") or {}, data.get("manifest")
    page = response.request.url.path.rsplit("/", 1)[-1]
    return data | {
        "campaignId": campaign,
        "runId": run.get("id")
        if page in {"create", "production", "campaign"}
        else data.get("output_run_id") or run.get("id"),
        "digest": plan.get("digest"),
        "allowance": run.get("allowance"),
        "brief": data.get("output_brief")
        or data.get("plan_brief")
        or data["project"]["authored"]["brief"],
        "currentRevision": manifest["revision_id"] if manifest else None,
        "intake": data["project"]["authored"]["brief"],
        "intakeVersion": data["version"],
        "editingBrief": not run or response.request.url.params.get("edit") == "brief",
    }


def enable_actions(client):
    response = client.get("/")
    client.headers["X-Campaign-Token"] = re.search(
        r'<meta name="campaign-token" content="([^"]+)"', response.text
    )[1]


def wait_for_work(client, campaign):
    for _ in range(600):
        response = client.get(f"/api/campaigns/{campaign}/status")
        assert response.status_code == 200
        if not response.json()["active"]:
            return response.json()
        time.sleep(0.02)
    pytest.fail("Local operation did not settle")


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        provider_factory=lambda: pytest.fail("Unexpected provider construction"),
        key_available=lambda: False,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        enable_actions(client)
        yield client, app.state.workspace


def sample_plan(client):
    seed_campaign(client.app.state.workspace)
    return page_data(
        client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/production")
    )


def start(client, data):
    return client.post(
        f"/api/campaigns/{data['campaignId']}/start",
        json={"run_id": data["runId"], "digest": data["digest"], "allowance": data["allowance"]},
    )


def test_empty_tasks_and_mutation_protection(ui):
    client, workspace = ui
    for page in ("campaign", "production", "studio", "handoff"):
        response = client.get(f"/{page}")
        assert response.url.path == "/" and "/static/motif/actions.js" in response.text
    token = client.headers.pop("X-Campaign-Token")
    assert client.post("/api/motif/campaigns", json={}).status_code == 403
    client.headers["X-Campaign-Token"] = token
    assert (
        client.post(
            "/api/motif/campaigns", json={}, headers={"origin": "http://127.0.0.1:9000"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/motif/campaigns", content="{}", headers={"content-type": "text/plain"}
        ).status_code
        == 415
    )
    created = client.post(
        "/api/motif/campaigns",
        json={"name": "Request validation", "brand": "Brand", "audience": "Audience"},
    )
    assert created.status_code == 200
    campaign_id = created.json()["campaign_id"]
    for suffix in ("logo", "sources/product-1/1x1"):
        response = client.get(f"/campaigns/{campaign_id}/{suffix}")
        assert response.status_code == 404
        assert "Page not found" in response.text and "Open workbench" in response.text
    assert (
        client.post(
            "/api/motif/import",
            content="{",
            headers={"content-type": "application/json"},
        ).status_code
        == 400
    )
    assert client.post("/api/motif/campaigns/missing/plan", json=[]).status_code == 404
    assert workspace.runs() == []
    assert client.post("/api/campaigns/missing/export", json={}).status_code == 404
    assert client.get("/private/state.sqlite3").status_code == 404


def test_http_local_run_copy_preview_stale_guard_and_offline_export(ui):
    client, workspace = ui
    data = sample_plan(client)
    assert (
        page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio"))[
            "manifest"
        ]
        is None
    )
    assert (
        page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/handoff"))[
            "manifest"
        ]
        is None
    )
    assert start(client, data).status_code == 202
    assert start(client, data).status_code in (200, 202)
    result = wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    assert result["state"] == "complete" and result["attempted"] == 0
    before = page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio"))
    assert before["manifest"]["metrics"]["completed_images"] == 12
    assert all(client.get(item["url"]).status_code == 200 for item in before["manifest"]["outputs"])
    values = before["brief"]["markets"]["es-MX"]["copy"] | {"message": "Para cada día."}
    edit = {
        "ratio": "1:1",
        "run_id": data["runId"],
        "revision_id": before["currentRevision"],
        "market": "es-MX",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products", "cta": "all_products"},
        "values": values,
    }
    preview = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/preview", json=edit
    )
    assert preview.status_code == 200
    assert len(preview.json()["impact"]["render_outputs"]) == 6
    assert preview.json()["impact"]["generation_products"] == []
    assert (
        client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply",
            json=edit | {"preview_digest": "stale"},
        ).status_code
        == 409
    )
    applied = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply",
        json=edit | {"preview_digest": preview.json()["digest"]},
    )
    assert applied.status_code == 200
    after = page_data(client, client.get(f"/campaigns/{data['campaignId']}/studio"))
    assert after["manifest"]["complete"]
    assert (
        sum(
            a["sha256"] != b["sha256"]
            for a, b in zip(
                before["manifest"]["outputs"], after["manifest"]["outputs"], strict=True
            )
        )
        == 6
    )
    assert (
        client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply",
            json=edit | {"preview_digest": preview.json()["digest"]},
        ).status_code
        == 409
    )
    same = edit | {"revision_id": after["currentRevision"]}
    unchanged = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/preview", json=same
    )
    assert unchanged.status_code == 200 and unchanged.json()["impact"]["render_outputs"] == []
    refused = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply",
        json=same | {"preview_digest": unchanged.json()["digest"]},
    )
    assert refused.status_code == 409 and refused.json()["code"] == "no_changes"
    body = {
        "run_id": data["runId"],
        "revision_id": after["currentRevision"],
        "allow_partial": False,
    }
    exported = client.post("/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export", json=body)
    assert exported.status_code == 200
    assert exported.json()["included"] == 12
    assert (
        client.post("/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export", json=body).json()[
            "location"
        ]
        == exported.json()["location"]
    )
    overview = client.get(exported.json()["url"])
    assert overview.status_code == 200 and 'href="manifest.json"' in overview.text
    assert "unsafe-inline" in overview.headers["content-security-policy"]
    damaged_package = Path(exported.json()["location"])
    exported_manifest = json.loads((damaged_package / "manifest.json").read_text())
    damaged_output = next(
        item
        for item in exported_manifest["outputs"]
        if item["id"] == after["manifest"]["outputs"][0]["id"]
    )
    (damaged_package / damaged_output["file"]).unlink()
    repaired = client.post("/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export", json=body)
    assert repaired.status_code == 200 and repaired.json()["included"] == 12
    assert repaired.json()["location"] != exported.json()["location"]
    assert len(list(Path(repaired.json()["location"]).rglob("*.png"))) == 12
    assert len(list(damaged_package.rglob("*.png"))) == 11
    assert workspace.state.attempts(data["runId"]) == []
    revision = workspace.state.revision(after["currentRevision"])
    prefix = revision["manifest_ref"].rsplit("/", 1)[0]
    workspace.storage.path(f"{prefix}/{after['manifest']['outputs'][0]['file']}").unlink()
    missing = page_data(
        client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio")
    )
    assert missing["manifest"]["outputs"][0]["url"] is None
    assert "missing or changed" in missing["manifest"]["outputs"][0]["omission"]
    assert missing["manifest"]["metrics"]["completed_images"] == 11
    assert (
        client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/recover",
            json={"run_id": data["runId"]},
        ).status_code
        == 202
    )
    restored = wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    assert restored["state"] == "complete" and restored["completed"] == 12
    assert restored["revision_id"] != after["currentRevision"]
    assert restored["attempted"] == 0


def concept_plan(workspace):
    _, brief = write_sample(workspace.storage)
    value = brief.model_dump()
    value["products"][0]["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "Synthetic mocked concept.",
        "provenance": {
            "label": "Synthetic test fixture",
            "rights": "Local test",
            "synthetic": True,
        },
    }
    return workspace.state.create(make_plan(Brief.model_validate(value), workspace.storage), 1)


def test_missing_key_blocks_dispatch_and_local_recovery_still_works(ui):
    client, workspace = ui
    run_id = concept_plan(workspace)
    data = page_data(
        client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/production")
    )
    response = start(client, data)
    assert response.status_code == 409 and response.json()["code"] == "missing_key"
    assert "API key in Production" in response.json()["message"]
    assert "launch environment" in response.json()["message"]
    assert workspace.state.attempts(run_id) == []
    refused = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/recover", json={"run_id": run_id}
    )
    assert refused.status_code == 409 and refused.json()["code"] == "recovery_not_started"
    assert workspace.state.run(run_id)["state"] == "planned"
    assert workspace.state.revision_count(run_id) == 0

    # Previously admitted work stopped before dispatch can still recover local
    # supplied images without a key. It is not an alternative start route.
    class ForbiddenProvider:
        async def generate(self, *args):
            pytest.fail("Stopped work must not dispatch")

    workspace.state.confirm(run_id, data["digest"])
    workspace.state.stop(run_id)
    asyncio.run(Runner(workspace.state).execute(run_id, ForbiddenProvider()))
    assert workspace.state.run(run_id)["state"] == "stopped"
    assert (
        client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/recover", json={"run_id": run_id}
        ).status_code
        == 202
    )
    status = wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    assert status["completed"] == 6 and status["attempted"] == 0
    export = client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/handoff")
    data = page_data(client, export)
    assert not data["manifest"]["complete"]
    assert sum(bool(item["url"]) for item in data["manifest"]["outputs"]) == 6
    assert all(item.get("omission") for item in data["manifest"]["outputs"] if not item["url"])
    package = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export",
        json={
            "run_id": run_id,
            "revision_id": data["currentRevision"],
            "allow_partial": True,
        },
    )
    assert package.status_code == 200 and package.json()["included"] == 6
    assert workspace.state.attempts(run_id) == []


@pytest.mark.parametrize(
    ("code", "notice"), [(401, "rejected_key"), (403, "access_denied"), (500, "provider_failure")]
)
def test_provider_failures_have_safe_distinct_messages_and_do_not_retry(tmp_path, code, notice):
    calls = []

    def transport(request):
        calls.append(True)
        return httpx.Response(
            code,
            json={"error": {"message": "PRIVATE DIAGNOSTIC DO NOT DISPLAY", "type": "test_error"}},
        )

    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        provider_factory=lambda: OpenAIImages(
            "synthetic-test-credential", transport=httpx.MockTransport(transport)
        ),
        key_available=lambda: True,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        enable_actions(client)
        concept_plan(app.state.workspace)
        data = page_data(
            client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/production")
        )
        assert start(client, data).status_code == 202
        status = wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
        assert status["notice_code"] == notice
        assert status["completed"] == 6
        page = client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/production")
        assert (
            "PRIVATE DIAGNOSTIC" not in page.text and "synthetic-test-credential" not in page.text
        )
        assert len(calls) == 1
        assert "Saved" in status["notice"]
