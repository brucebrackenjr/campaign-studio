"""Missing sources do not prevent metadata edits or authorize extra provider requests."""

import asyncio
import json
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.compositions import save_metadata
from campaign_studio.config import Settings
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.motif import campaign_data
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import CreativeUpdate
from campaign_studio.runner import Runner
from campaign_studio.state import State, StateError
from campaign_studio.workspace import ActionError, FrameEdit, TextEdit, Workspace


@pytest.fixture
def partial(tmp_path):
    state, runner, run_id, response = generated_case(tmp_path)
    calls = []

    def fail(request):
        calls.append(json.loads(request.content))
        return httpx.Response(500, json={"error": {"message": "Synthetic failure"}})

    first = asyncio.run(
        runner.execute(run_id, OpenAIImages("synthetic", transport=httpx.MockTransport(fail)))
    )
    assert first["metrics"]["completed_images"] == 0 and first["sources_ready"] == 1
    assert len(calls) == 1
    return state, runner, run_id, first, response, calls


def edit(state, run_id, *, scope="product-2", message="Para cada movimiento."):
    ws = Workspace(state)
    cid = state.run(run_id)["campaign_id"]
    return TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=ws.current_composition(cid)["version"],
        market="es-MX",
        product_id="product-2",
        field_scopes={
            field: "all_products" if scope == "shared" else "product"
            for field in ("message", "cta")
        },
        values={"message": message, "cta": "Conoce más"},
    )


def correct(state, runner, run_id, **kwargs):
    ws = Workspace(state)
    cid = state.run(run_id)["campaign_id"]
    version = ws.save_composition(cid, edit(state, run_id, **kwargs))
    return ws.current_composition(cid, version)


def omitted(manifest):
    return [item for item in manifest["outputs"] if item["file"] is None]


def export(state, cid, allow_partial=True):
    version = Workspace(state).current_composition(cid)["version"]
    with (
        Handoff(state).create_current_zip(
            cid, DeliveryAction(version=version, allow_partial=allow_partial)
        ) as download,
        ZipFile(download.stream) as archive,
    ):
        name = next(n for n in archive.namelist() if n.endswith("/manifest.json"))
        return json.loads(archive.read(name))


def test_partial_copy_framing_descriptions_reopen_and_explicit_export(partial, monkeypatch):
    state, runner, run, first, _, calls = partial
    cid = first["campaign_id"]
    ws = Workspace(state)
    current = campaign_data(ws, cid)
    assert current["editable_products"] == {"product-1": False, "product-2": True}
    save_metadata(
        ws,
        cid,
        current["version"],
        "product-2/en-US/1x1",
        CreativeUpdate(expected_version=0, description="Supplied description."),
    )
    monkeypatch.setattr(
        runner, "_recover_received", lambda *a: pytest.fail("Implicit provider recovery")
    )
    correct(state, runner, run)
    version = ws.current_composition(cid)["version"]
    ws.save_composition(
        cid,
        FrameEdit(
            revision_id=version,
            product_id="product-2",
            market="es-MX",
            ratio="1:1",
            framing={"zoom": 1.15, "focal_x": 0.613},
        ),
    )
    reopened = Workspace(State(state.storage))
    snapshot = reopened.current_composition(cid)
    assert snapshot["plan"].brief.products[1].framing["es-MX"]["1:1"].zoom == 1.15
    assert snapshot["plan"].brief.products[1].framing["es-MX"]["1:1"].focal_x == 0.613
    with pytest.raises(StateError, match="explicitly"):
        export(state, cid, False)
    delivered = export(state, cid)
    assert len(omitted(delivered)) == 6 and len([o for o in delivered["outputs"] if o["file"]]) == 6
    assert all(
        o["copy"]["message"] == "Para cada movimiento."
        for o in delivered["outputs"]
        if o["product_id"] == "product-2" and o["market"] == "es-MX"
    )
    assert (
        next(o for o in delivered["outputs"] if o["id"] == "product-2/en-US/1x1")[
            "accessible_description"
        ]
        == "Supplied description."
    )
    assert len(calls) == len(state.attempts(run)) == 1 and state.revision_count(run) == 0
    assert not list(state.storage.root.rglob("Outputs"))


def test_shared_copy_autosaves_even_before_every_product_has_source(partial):
    state, runner, run, first, _, calls = partial
    snapshot = correct(state, runner, run, scope="shared", message="One current shared message.")
    assert all(
        o.text.message == "One current shared message."
        for o in snapshot["plan"].outputs
        if o.market == "es-MX"
    )
    assert set(snapshot["sources"]) == {"product-2"}
    manifest = export(state, first["campaign_id"])
    assert len(omitted(manifest)) == 6 and len(calls) == 1


def test_selected_source_damage_blocks_delivery_without_erasing_saved_metadata(partial):
    state, runner, run, first, _, calls = partial
    ws = Workspace(state)
    cid = first["campaign_id"]
    asset = ws.current_composition(cid)["sources"]["product-2"]
    original = state.storage.path(asset.reference).read_bytes()
    state.storage.path(asset.reference).write_bytes(b"Synthetic damaged source")
    ws.save_composition(cid, edit(state, run))
    with pytest.raises(StateError):
        export(state, cid)
    state.storage.path(asset.reference).write_bytes(original)
    assert export(state, cid)["outputs"][6]["copy"]["message"]
    assert len(calls) == 1


def test_retained_response_recovers_only_explicitly_and_keeps_current_edits(partial):
    state, runner, run, first, response, calls = partial
    attempt = state.attempts(run)[0]
    state.storage.write(attempt["response_ref"], json.dumps(response).encode())
    state.finish_attempt(attempt["id"], "received")
    cid = first["campaign_id"]
    ws = Workspace(state)
    current = campaign_data(ws, cid)
    save_metadata(
        ws,
        cid,
        current["version"],
        "product-2/en-US/1x1",
        CreativeUpdate(expected_version=0, description="Saved before recovery."),
    )
    correct(state, runner, run)
    assert state.attempts(run)[0]["state"] == "received"
    assert set(ws.current_composition(cid)["sources"]) == {"product-2"}
    assert len(omitted(export(state, cid))) == 6
    recovered = Runner(State(state.storage)).recover_local(run)
    assert recovered["complete"] and recovered["sources_ready"] == 2 and recovered["outputs"] == []
    current = Workspace(State(state.storage)).current_composition(cid)
    assert all(
        o.text.message == "Para cada movimiento."
        for o in current["plan"].outputs
        if o.product_id == "product-2" and o.market == "es-MX"
    )
    assert current["metadata"]["product-2/en-US/1x1"]["description"] == "Saved before recovery."
    assert export(state, cid)["complete"]
    assert len(calls) == len(state.attempts(run)) == 1 and state.effective_revision(run) is None


def test_explicit_provider_retry_uses_pinned_request_and_preserves_latest_copy(partial):
    state, runner, run, first, response, calls = partial
    request = state.plan(run).generation_requests["product-1"]
    correct(state, runner, run)
    with pytest.raises(StateError, match="authored brief changed"):
        state.confirm(run, state.plan(run).digest, retry_allowance=2)
    from campaign_studio.authored import AuthoredProjects
    from campaign_studio.intake import Intake

    ws = Workspace(state)
    cid = first["campaign_id"]
    fresh = Intake(state.storage).prepare(cid, AuthoredProjects(ws).read(cid)["version"], ws)
    new_run = ws.run_for(cid)["id"]
    state.confirm(new_run, fresh.digest)

    def succeed(received):
        calls.append(json.loads(received.content))
        return httpx.Response(200, json=response)

    result = asyncio.run(
        runner.execute(new_run, OpenAIImages("synthetic", transport=httpx.MockTransport(succeed)))
    )
    assert result["complete"] and calls[0] == calls[1]
    assert len(calls) == 2
    assert len(state.attempts(run)) == len(state.attempts(new_run)) == 1
    assert state.source_generation(new_run)["product-1"]["request"] == request
    assert state.campaign_metrics(cid)["attempted_requests"] == 2
    delivered = export(state, first["campaign_id"])
    assert all(
        o["copy"]["message"] == "Para cada movimiento."
        for o in delivered["outputs"]
        if o["product_id"] == "product-2" and o["market"] == "es-MX"
    )


def test_stale_edit_rejected_and_unstarted_production_does_not_replace_current_copy(partial):
    state, runner, run, first, _, _ = partial
    cid = first["campaign_id"]
    ws = Workspace(state)
    stale = edit(state, run)
    correct(state, runner, run, message="Saved current words.")
    with pytest.raises(ActionError, match="another tab"):
        ws.save_composition(cid, stale)
    before = ws.current_composition(cid)
    pending = state.create(make_plan(before["plan"].brief, state.storage), 1)
    assert state.run(pending)["state"] == "planned" and state.attempts(pending) == []
    after = ws.current_composition(cid)
    assert after["version"] == before["version"]
    assert after["plan"].brief == before["plan"].brief
    assert len(omitted(export(state, cid))) == 6


def test_http_partial_preview_autosave_description_and_export(partial):
    state, _, run, first, _, _ = partial
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("No provider for local edits"),
    )
    cid = first["campaign_id"]
    base = f"/api/campaigns/{cid}"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        body = edit(state, run).model_dump(mode="json")
        saved = client.post(base + "/text/save", json=body)
        assert saved.status_code == 200, saved.text
        shown = client.get(f"/api/motif/campaigns/{cid}").json()
        image = next(o for o in shown["manifest"]["outputs"] if o["id"] == "product-2/es-MX/1x1")
        assert client.get(image["url"]).status_code == 200
        described = client.post(
            base + f"/composition/{shown['version']}/metadata/product-2/es-MX/1x1",
            json={
                "mode": "current",
                "expected_version": 0,
                "description": "Current supplied description.",
            },
        )
        assert described.status_code == 200, described.text
        current = client.get(f"/api/motif/campaigns/{cid}").json()
        result = client.post(
            base + "/delivery/zip", json={"version": current["version"], "allow_partial": True}
        )
        assert result.status_code == 200 and result.content.startswith(b"PK")
        assert len(state.attempts(run)) == 1


def test_unrelated_damaged_source_preserves_edits_but_blocks_delivery_until_repaired(tmp_path):
    state, runner, run, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run))
    ws = Workspace(state)
    cid = brief.campaign_id
    snapshot = ws.current_composition(cid)
    path = state.storage.path(snapshot["sources"]["product-1"].reference)
    original = path.read_bytes()
    path.unlink()
    correct(state, runner, run)
    current = ws.current_composition(cid)
    assert set(current["sources"]) == {"product-2"}
    with pytest.raises(StateError, match="image or text issues"):
        Handoff(state).current_png(cid, "product-2/es-MX/1x1", current["version"])
    path.write_bytes(original)
    with Handoff(state).current_png(cid, "product-2/es-MX/1x1", current["version"]) as result:
        assert result.stream.read(8) == b"\x89PNG\r\n\x1a\n"
    assert state.attempts(run) == []
