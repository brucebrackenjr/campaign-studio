"""Partial output corrections use real saved files and synthetic transport only."""

import asyncio
import copy
import json

import httpx
import pytest
from campaign_fixtures import export_ref
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.brief import Brief, Framing
from campaign_studio.config import Settings
from campaign_studio.exporting import export_package
from campaign_studio.handoff import Handoff, PackageAction
from campaign_studio.imaging import ImageProblem
from campaign_studio.motif import campaign_data
from campaign_studio.planning import make_local_plan, make_plan
from campaign_studio.previews import DraftPreviews
from campaign_studio.provider import OpenAIImages
from campaign_studio.rendering import render_revision
from campaign_studio.review import CreativeUpdate, RevisionTarget
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

    provider = OpenAIImages("synthetic", transport=httpx.MockTransport(fail))
    first = asyncio.run(runner.execute(run_id, provider))
    assert first["metrics"]["completed_images"] == 6 and len(calls) == 1
    return state, runner, run_id, first, response, calls


def edit(state, run_id, *, scope="product-2", message="Para cada movimiento."):
    return TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=state.effective_revision(run_id),
        market="es-MX",
        product_id="product-2",
        field_scopes={
            field: "all_products" if scope == "shared" else "product"
            for field in ("message", "cta")
        },
        values={"message": message, "cta": "Conoce más"},
    )


def correct(state, runner, run_id, **kwargs):
    command = edit(state, run_id, **kwargs)
    brief, plan, impact = Workspace(state).text(state.run(run_id)["campaign_id"], command)
    result = runner.revise(run_id, brief, command.revision_id, plan.digest)
    return result, impact


def omitted(manifest):
    return [item for item in manifest["outputs"] if item["file"] is None]


def target(state, run_id, revision):
    return RevisionTarget(
        campaign_id=state.run(run_id)["campaign_id"],
        run_id=run_id,
        revision_id=revision,
        mode="current",
    )


def test_partial_metadata_copy_framing_reopen_and_selected_export(partial, monkeypatch):
    state, runner, run_id, first, _, calls = partial
    cid = first["campaign_id"]
    data = campaign_data(Workspace(state), cid)
    assert data["can_describe"] and data["can_edit"]
    assert data["editable_products"] == {"product-1": False, "product-2": True}
    assert state.run(run_id)["current_revision"] is None
    english = "product-2/en-US/1x1"
    spanish = "product-2/es-MX/1x1"
    for creative in (english, spanish):
        state.update_creative(
            first["revision_id"],
            creative,
            CreativeUpdate(expected_version=0, description="Supplied description."),
            target=target(state, run_id, first["revision_id"]),
        )
    before_notes = state.metadata_snapshot(first["revision_id"])
    saved_target = target(state, run_id, first["revision_id"]).model_copy(
        update={"mode": "saved_version"}
    )
    handoff = Handoff(state)
    package = handoff.create_zip(
        saved_target, PackageAction(snapshot_id=before_notes["snapshot_id"], allow_partial=True)
    )
    preserved_zip = handoff.download(saved_target, package["artifact_id"])
    # Descriptions do not approve or create an unavailable image. The retired
    # review-marker rule does not apply to this retained metadata capability.
    state.update_creative(
        first["revision_id"],
        "product-1/en-US/1x1",
        CreativeUpdate(expected_version=0, description="Description for a pending image."),
        target=target(state, run_id, first["revision_id"]),
    )
    before_notes = state.metadata_snapshot(first["revision_id"])
    assert all(item["file"] is None for item in omitted(first))
    assert len(state.attempts(run_id)) == 1
    monkeypatch.setattr(runner, "_recover_received", lambda *args: pytest.fail("Implicit recovery"))
    command = edit(state, run_id)
    _, plan, impact = Workspace(state).text(cid, command)
    previews = DraftPreviews(state)
    images = previews.render(cid, command, plan, impact)
    assert len(images) == 3
    second, impact = correct(state, runner, run_id)
    assert len(impact["render_outputs"]) == 3 and not impact["generation_products"]
    assert omitted(second) == omitted(first)
    for old, new in zip(first["outputs"], second["outputs"], strict=True):
        assert (old["sha256"] != new["sha256"]) == (new["id"] in impact["render_outputs"])
    notes = state.metadata_snapshot(second["revision_id"])["creatives"]
    assert notes[english]["description"] == "Supplied description."
    assert (
        notes[spanish]["description"] is None
        and notes[spanish]["reset_reason"] == "creative_changed"
    )
    assert state.metadata_snapshot(first["revision_id"]) == before_notes
    frame = FrameEdit(
        run_id=run_id,
        revision_id=second["revision_id"],
        product_id="product-2",
        market="es-MX",
        ratio="1:1",
        framing=Framing(zoom=1.15, focal_x=0.613),
    )
    brief, plan, impact = Workspace(state).frame(cid, frame)
    assert impact["render_outputs"] == [spanish]
    third = runner.revise(run_id, brief, frame.revision_id, plan.digest)
    assert omitted(third) == omitted(first) and not third["complete"]
    reopened = State(state.storage)
    assert reopened.effective_revision(run_id) == third["revision_id"]
    assert reopened.plan(run_id).brief.products[1].framing["es-MX"]["1:1"].zoom == 1.15
    fourth, _ = correct(reopened, Runner(reopened), run_id, message="Una segunda corrección.")
    assert omitted(fourth) == omitted(first)
    assert reopened.plan(run_id).brief.products[1].framing["es-MX"]["1:1"].focal_x == 0.613
    assert reopened.run(run_id)["current_revision"] is None
    # Superseded partial PNGs are cleaned up. Their independent package survives,
    # while a new export from retired pixels is a clear unavailable result.
    assert Handoff(reopened).download(saved_target, package["artifact_id"]) == preserved_zip
    with pytest.raises(StateError, match="no longer available"):
        export_package(
            reopened,
            first["revision_id"],
            export_ref(reopened, first["revision_id"], "retired-new"),
            allow_partial=True,
        )

    for manifest in (fourth,):
        reference = export_package(
            reopened,
            manifest["revision_id"],
            export_ref(reopened, manifest["revision_id"], manifest["revision_id"]),
            allow_partial=True,
        )
        exported = json.loads(
            reopened.storage.path(reference).with_name("manifest.json").read_bytes()
        )
        assert len([o for o in exported["outputs"] if o["file"]]) == 6
        assert exported["revision_id"] == manifest["revision_id"]
    assert len(calls) == len(reopened.attempts(run_id)) == 1

    assert (
        campaign_data(Workspace(reopened), cid)["manifest"]["revision_id"] == fourth["revision_id"]
    )


def test_shared_missing_input_scope_and_selected_damage_are_distinct(partial):
    state, runner, run_id, first, _, calls = partial
    ws = Workspace(state)
    with pytest.raises(StateError, match="Synthetic product 1"):
        ws.text(first["campaign_id"], edit(state, run_id, scope="shared"))
    selected = state.storage.path(state.plan(run_id).assets["products/product-2"].reference)
    selected.write_bytes(b"synthetic damaged source")
    with pytest.raises(StateError, match="Synthetic product 2"):
        ws.text(first["campaign_id"], edit(state, run_id))
    data = campaign_data(ws, first["campaign_id"])
    assert data["can_describe"] and not data["can_edit"]
    state.update_creative(
        first["revision_id"],
        "product-2/en-US/1x1",
        CreativeUpdate(expected_version=0, description="Supplied description."),
        target=target(state, run_id, first["revision_id"]),
    )
    assert state.revision_count(run_id) == 1 and len(calls) == 1


def test_retained_response_is_recovered_only_explicitly_and_keeps_partial_edits(partial):
    state, runner, run_id, first, response, calls = partial
    attempt = state.attempts(run_id)[0]
    state.storage.write(attempt["response_ref"], json.dumps(response).encode())
    state.finish_attempt(attempt["id"], "received")
    english = "product-2/en-US/1x1"
    state.update_creative(
        first["revision_id"],
        english,
        CreativeUpdate(expected_version=0, description="Supplied description."),
    )
    second, _ = correct(state, runner, run_id)
    assert state.attempts(run_id)[0]["state"] == "received"
    assert omitted(second) == omitted(first)
    recovered = Runner(State(state.storage)).recover_local(run_id)
    assert recovered["complete"] and recovered["metrics"]["completed_images"] == 12
    assert state.run(run_id)["current_revision"] == recovered["revision_id"]
    assert (
        state.plan(run_id)
        .brief.effective_copy(state.plan(run_id).brief.products[1], "es-MX")
        .message
        == "Para cada movimiento."
    )
    assert (
        state.metadata_snapshot(recovered["revision_id"])["creatives"][english]["description"]
        == "Supplied description."
    )
    assert len(calls) == len(state.attempts(run_id)) == 1


def test_partial_retry_uses_latest_copy_pinned_request_and_cumulative_allowance(partial):
    state, runner, run_id, first, response, calls = partial
    original_digest = state.plan(run_id).digest
    original_request = state.plan(run_id).generation_requests["product-1"]
    second, _ = correct(state, runner, run_id)
    with pytest.raises(StateError, match="stale"):
        state.confirm(run_id, original_digest, retry_allowance=2)
    with pytest.raises(StateError, match="another"):
        state.confirm(run_id, state.plan(run_id).digest, retry_allowance=1)
    state.confirm(run_id, state.plan(run_id).digest, retry_allowance=2)
    assert state.plan(run_id, current=False).digest == state.plan(run_id).digest

    def succeed(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    completed = asyncio.run(
        runner.execute(run_id, OpenAIImages("synthetic", transport=httpx.MockTransport(succeed)))
    )
    assert completed["complete"] and len(calls) == 2
    assert calls[0] == calls[1]
    assert state.plan(run_id).generation_requests["product-1"] == original_request
    assert state.run(run_id)["allowance"] == len(state.attempts(run_id)) == 2
    assert all(
        o["copy"]["message"] == "Para cada movimiento."
        for o in completed["outputs"]
        if o["product_id"] == "product-2" and o["market"] == "es-MX"
    )
    assert omitted(second) == omitted(first)


def test_stale_partial_targets_reject_but_unstarted_plan_keeps_current_editable(partial):
    state, runner, run_id, first, _, _ = partial
    stale = edit(state, run_id)
    second, _ = correct(state, runner, run_id)
    with pytest.raises((StateError, ActionError), match="changed"):
        Workspace(state).text(first["campaign_id"], stale)
    with pytest.raises(StateError, match="no longer available"):
        state.update_creative(
            first["revision_id"],
            "product-2/en-US/1x1",
            CreativeUpdate(expected_version=0, description="Supplied description."),
            target=target(state, run_id, first["revision_id"]),
        )
    stale = edit(state, run_id, message="Another change")
    pending = state.create(make_plan(state.plan(run_id).brief, state.storage), 1)
    brief, preview, _ = Workspace(state).text(first["campaign_id"], stale)
    result = runner.revise(run_id, brief, second["revision_id"], preview.digest, text_edit=stale)
    assert omitted(result) == omitted(second)
    assert state.effective_revision(run_id) == result["revision_id"]
    assert state.run(pending)["state"] == "planned" and state.attempts(pending) == []
    with pytest.raises((StateError, ActionError), match="changed"):
        Workspace(state).text(first["campaign_id"], stale)


def test_local_recomposition_fixes_copy_fit_without_filling_other_omissions(partial):
    state, runner, run_id, first, _, calls = partial
    value = state.plan(run_id).brief.model_dump()
    value["products"][1]["copy_overrides"] = {"es-MX": {"message": "Oversized layout"}}
    original_framing = copy.deepcopy(value["products"][1]["framing"])
    value["products"][1]["framing"]["es-MX"] = {
        ratio: {
            "text_placements": {
                "message": {
                    "left": 0.1,
                    "top": 0.1,
                    "width": 0.2,
                    "font_size": 1000,
                }
            }
        }
        for ratio in ("1:1", "9:16", "16:9")
    }
    blocked = make_local_plan(state.plan(run_id), Brief.model_validate(value), state.storage)
    broken = render_revision(state, run_id, blocked, reuse_revision=first["revision_id"])
    assert broken["metrics"]["completed_images"] == 3
    value["products"][1]["copy_overrides"]["es-MX"]["message"] = "Para cada movimiento."
    value["products"][1]["framing"] = original_framing
    corrected = make_local_plan(state.plan(run_id), Brief.model_validate(value), state.storage)
    repaired = runner.revise(run_id, corrected.brief, broken["revision_id"], corrected.digest)
    assert repaired["metrics"]["completed_images"] == 6
    assert omitted(repaired) == omitted(first)
    assert len(calls) == 1


def test_actual_http_partial_preview_apply_and_metadata(partial):
    state, _, run_id, first, _, _ = partial
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("No provider for local editing"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        base = f"/api/campaigns/{first['campaign_id']}"
        blocked = client.post(
            base + "/text/preview", json=edit(state, run_id, scope="shared").model_dump()
        )
        assert blocked.status_code == 409 and blocked.json()["code"] == "state"
        body = edit(state, run_id).model_dump()
        preview = client.post(base + "/text/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["images"]) == 1 and not preview.json()["blocked"]
        assert client.get(preview.json()["images"][0]["url"]).status_code == 200
        body["preview_digest"] = preview.json()["digest"]
        saved = client.post(base + "/text/apply", json=body)
        assert saved.status_code == 200 and not saved.json()["complete"]
        current = state.effective_revision(run_id)
        assert current == saved.json()["revision_id"]
        result = client.post(
            f"{base}/runs/{run_id}/revisions/{current}/metadata/product-2/en-US/1x1",
            json={"mode": "current", "expected_version": 0, "description": "Supplied description."},
        )
        assert result.status_code == 200, result.text
        assert len(state.attempts(run_id)) == 1


def test_unrelated_damaged_source_does_not_block_usable_partial_target(tmp_path, monkeypatch):
    state, runner, run_id, _ = setup_case(tmp_path)
    from campaign_studio import rendering

    original = rendering.compose
    calls = 0

    def interrupted_product(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 6:
            raise ImageProblem("Synthetic first-product local failure.")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rendering, "compose", interrupted_product)
        first = asyncio.run(runner.execute(run_id))
    assert first["metrics"]["completed_images"] == 6
    state.storage.path(state.plan(run_id).assets["products/product-1"].reference).unlink()
    data = campaign_data(Workspace(state), first["campaign_id"])
    assert data["editable_products"] == {"product-1": False, "product-2": True}
    second, impact = correct(state, runner, run_id)
    assert len(impact["render_outputs"]) == 3
    assert omitted(second) == omitted(first)
    assert state.attempts(run_id) == []


def test_recovery_metadata_uses_effective_complete_not_later_failed_correction(
    tmp_path, monkeypatch
):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    creative = "product-2/es-MX/1x1"
    state.update_creative(
        first["revision_id"],
        creative,
        CreativeUpdate(expected_version=0, description="Original reviewed image."),
        target=target(state, run_id, first["revision_id"]),
    )
    before = state.metadata_snapshot(first["revision_id"])
    command = edit(state, run_id)
    brief, plan, _ = Workspace(state).text(first["campaign_id"], command)

    def fail(*args, **kwargs):
        raise ImageProblem("Synthetic local failure after successful preview")

    with monkeypatch.context() as patch:
        patch.setattr("campaign_studio.rendering.compose", fail)
        partial = runner.revise(run_id, brief, command.revision_id, plan.digest)
    assert not partial["complete"]
    assert state.effective_revision(run_id) == first["revision_id"]
    failed_notes = state.metadata_snapshot(partial["revision_id"])
    recovered = runner.recover_local(run_id)
    old = next(item for item in first["outputs"] if item["id"] == creative)
    new = next(item for item in recovered["outputs"] if item["id"] == creative)
    assert new["pixel_sha256"] == old["pixel_sha256"] and new["copy"] == old["copy"]
    restored = state.metadata_snapshot(recovered["revision_id"])["creatives"][creative]
    assert restored["description"] == "Original reviewed image."
    assert state.metadata_snapshot(first["revision_id"]) == before
    assert state.metadata_snapshot(partial["revision_id"]) == failed_notes
    assert state.attempts(run_id) == []
