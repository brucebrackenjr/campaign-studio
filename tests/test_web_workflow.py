import asyncio
import io
import re
import time
from zipfile import ZipFile

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


def test_http_source_ready_copy_autosave_stale_guard_and_offline_export(ui):
    client, workspace = ui
    data = sample_plan(client)
    cid = data["campaignId"]
    base = f"/api/campaigns/{cid}"
    before = page_data(client, client.get(f"/campaigns/{cid}/studio"))
    assert before["manifest"]["complete"] and before["run"] is None
    assert len(before["manifest"]["outputs"]) == 12
    assert all(client.get(item["url"]).status_code == 200 for item in before["manifest"]["outputs"])
    assert not list(workspace.storage.root.glob("Campaigns/*/Outputs"))
    values = before["brief"]["markets"]["es-MX"]["copy"] | {"message": "Para cada día."}
    edit = {
        "ratio": "1:1",
        "revision_id": before["currentRevision"],
        "market": "es-MX",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products", "cta": "all_products"},
        "values": values,
    }
    preview = client.post(base + "/text/preview", json=edit)
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["impact"]["render_outputs"]) == 6
    assert preview.json()["impact"]["generation_products"] == []
    saved = client.post(base + "/text/save", json=edit)
    assert saved.status_code == 200, saved.text
    assert (
        client.post(base + "/text/save", json=edit).status_code == 200
    )  # Lost acknowledgement retry.
    stale = client.post(
        base + "/text/save", json=edit | {"values": values | {"message": "Outdated tab"}}
    )
    assert stale.status_code == 409
    after = page_data(client, client.get(f"/campaigns/{cid}/studio"))
    assert after["brief"]["markets"]["es-MX"]["copy"]["message"] == "Para cada día."
    assert all(
        item["copy"]["message"] == "Para cada día."
        for item in after["manifest"]["outputs"]
        if item["market"] == "es-MX"
    )
    body = {"version": after["version"]}
    preview = client.post(base + "/delivery/report-preview", json=body)
    assert preview.status_code == 200, preview.text
    overview = client.get(preview.json()["url"])
    assert overview.status_code == 200 and 'href="manifest.json"' in overview.text
    manifest = client.get(preview.json()["url"].removesuffix("index.html") + "manifest.json").json()
    assert len([o for o in manifest["outputs"] if o["file"]]) == 12
    package = client.post(base + "/delivery/zip", json=body)
    assert package.status_code == 200, package.text
    with ZipFile(io.BytesIO(package.content)) as archive:
        assert sum(n.endswith(".png") for n in archive.namelist()) == 12
    assert not list(workspace.storage.root.glob("Campaigns/*/Exports"))
    assert not list(workspace.storage.root.glob("Campaigns/*/Outputs"))
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    # Removing a source affects every format that depends on it. A preview is
    # never treated as a replacement source, and export cannot silently use it.
    source = workspace.current_composition(cid)["sources"]["product-1"]
    workspace.storage.path(source.reference).unlink()
    missing = page_data(client, client.get(f"/campaigns/{cid}/studio"))
    assert sum(bool(item["url"]) for item in missing["manifest"]["outputs"]) == 6
    assert client.post(base + "/delivery/zip", json=body).status_code == 409


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
    from uuid import uuid4

    from campaign_studio.campaign_drafts import write
    from campaign_studio.planning import canonical_hash

    run_id = workspace.state.create(make_plan(Brief.model_validate(value), workspace.storage), 1)
    prepared = workspace.state.plan(run_id).brief.model_dump(mode="json")
    write(
        workspace.storage,
        brief.campaign_id,
        {
            "authored": {"brief": prepared},
            "composition_schema": 1,
            "version": uuid4().hex,
            "planned_run": run_id,
            "planned_authored_hash": canonical_hash(prepared),
            "authored_base": {"run_id": run_id, "revision_id": None, "metadata_snapshot_id": None},
        },
    )
    return run_id


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
        f"/api/campaigns/{data['manifest']['campaign_id']}/delivery/zip",
        json={"allow_partial": True, "version": data["version"]},
    )
    assert package.status_code == 200
    with ZipFile(io.BytesIO(package.content)) as archive:
        assert sum(name.endswith(".png") for name in archive.namelist()) == 6
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


def test_source_status_remains_readable_when_a_concurrent_save_changes_composition(
    tmp_path, monkeypatch
):
    from test_workflow import setup_case

    from campaign_studio.workspace import ActionError

    state, runner, run_id, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run_id))
    app = create_app(Settings(data_dir=state.storage.root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with monkeypatch.context() as patch:
            patch.setattr(
                "campaign_studio.motif.campaign_data",
                lambda *args: (_ for _ in ()).throw(
                    ActionError("composition_conflict", "Changed while reading")
                ),
            )
            response = client.get(f"/api/campaigns/{brief.campaign_id}/status")
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "complete" and response.json()["attempted"] == 0
        current = client.get(f"/api/campaigns/{brief.campaign_id}/status")
        assert current.status_code == 200
        assert current.json()["completed"] == 6
        assert state.run(run_id)["current_revision"] is None
