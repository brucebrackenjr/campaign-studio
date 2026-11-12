"""Selection changes the exact package, without weakening saved-campaign checks."""

import io
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_deferred_delivery import case as case
from test_deferred_delivery import thirty_image_case, unpack

from campaign_studio import delivery
from campaign_studio.app import create_app
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.state import StateError


def test_selected_zip_contains_only_requested_images_and_accurate_scope(case, monkeypatch):
    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    ids = [snapshot["plan"].outputs[i].id for i in (0, 15, 29)]
    handoff = Handoff(state)
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: snapshot)
    render = delivery.render_png
    rendered = []

    def record(saved, paths, output, destination, **kwargs):
        rendered.append(output.id)
        return render(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", record)
    with handoff.create_current_zip(
        snapshot["plan"].brief.campaign_id,
        DeliveryAction(version=snapshot["version"], output_ids=list(reversed(ids))),
    ) as download:
        manifest, contents = unpack(download)
    assert rendered == ids
    assert manifest["complete"]
    assert manifest["metrics"]["requested_images"] == 3
    assert manifest["metrics"]["completed_images"] == 3
    assert [o["id"] for o in manifest["outputs"]] == ids
    assert manifest["export_scope"] == {
        "kind": "selection",
        "output_ids": ids,
        "campaign_image_count": 30,
    }
    assert len([name for name in contents if name.endswith(".png")]) == 3
    assert set(manifest["product_names"]) == {o.split("/")[0] for o in ids}
    assert {s["product_id"] for s in manifest["sources"]} == set(manifest["product_names"])
    assert manifest["campaign_metrics"] == snapshot["campaign_metrics"]
    report = contents[f"{manifest['package_name']}/index.html"].decode()
    assert "Selected images package" in report
    assert "3 selected images from 30 campaign images" in report
    assert "spend below describe the whole campaign" in report
    assert "Complete campaign package" not in report
    assert not state.delivery_jobs


@pytest.mark.parametrize("selection", [[], ["unknown/en-US/1x1"], ["same", "same"]])
def test_invalid_selection_never_falls_back_to_all(case, selection):
    _, snapshot = case
    with pytest.raises(StateError) as error:
        delivery.delivery_manifest(snapshot, output_ids=selection)
    assert error.value.code == "export_selection"


@pytest.mark.parametrize("selection", [[], ["same", "same"], ["x"] * 31, [123]])
def test_selection_request_contract_rejects_invalid_values(selection):
    with pytest.raises(ValidationError):
        DeliveryAction(version="a", output_ids=selection)


def test_selected_missing_image_is_not_silently_omitted(case):
    _, snapshot = case
    output = snapshot["plan"].outputs[0]
    del snapshot["sources"][output.product_id]
    for allow_partial in (False, True):
        with pytest.raises(StateError) as error:
            delivery.delivery_manifest(
                snapshot, output_ids=[output.id], allow_partial=allow_partial
            )
        assert error.value.code == "export_selection"


def test_available_subset_does_not_list_unselected_missing_sources(case):
    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    first = snapshot["plan"].outputs[0]
    snapshot["sources"] = {first.product_id: snapshot["sources"][first.product_id]}
    with pytest.raises(StateError) as error:
        delivery.delivery_manifest(snapshot)
    assert error.value.code == "partial_export"
    full = delivery.delivery_manifest(snapshot, allow_partial=True)
    assert full["metrics"]["completed_images"] == 6
    assert full["metrics"]["requested_images"] == 30
    selected = delivery.delivery_manifest(snapshot, output_ids=[first.id])
    assert selected["complete"]
    assert len(selected["outputs"]) == 1 and selected["outputs"][0]["omission"] is None
    assert selected["export_scope"]["campaign_image_count"] == 30


def test_selected_retry_reuses_verified_images_and_rejects_changed_selection(case, monkeypatch):
    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    ids = [o.id for o in snapshot["plan"].outputs[:2]]
    handoff = Handoff(state)
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: snapshot)
    render, counts, failing = delivery.render_png, Counter(), True

    def controlled(saved, paths, output, destination, **kwargs):
        counts[output.id] += 1
        if output.id == ids[1] and failing:
            raise OSError("Injected selected-image failure")
        return render(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", controlled)
    campaign = snapshot["plan"].brief.campaign_id
    action = DeliveryAction(version=snapshot["version"], output_ids=ids, operation_id="9" * 32)
    with pytest.raises(StateError, match="Retry failed images"):
        handoff.create_current_zip(campaign, action)
    progress = handoff.current_progress(campaign, action.operation_id)
    assert progress["completed"] == 1 and progress["total"] == 2
    failing = False
    with handoff.create_current_zip(campaign, action) as download:
        assert len(unpack(download)[0]["outputs"]) == 2
    assert counts[ids[0]] == 1
    assert set(counts) == set(ids)
    failing = True
    with pytest.raises(StateError):
        handoff.create_current_zip(campaign, action)
    changed = action.model_copy(update={"output_ids": [ids[0]]})
    with pytest.raises(StateError) as error:
        handoff.create_current_zip(campaign, changed)
    assert error.value.code == "composition_conflict"
    assert not state.delivery_jobs


def test_http_subset_and_downloadable_example_import(case):
    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Local export and import must not create a provider"),
    )
    campaign = snapshot["plan"].brief.campaign_id
    ids = [snapshot["plan"].outputs[i].id for i in (2, 16)]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        path = f"/api/campaigns/{campaign}/delivery/zip"
        body = {"version": snapshot["version"], "output_ids": ids}
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text
        with ZipFile(io.BytesIO(response.content)) as archive:
            assert len([n for n in archive.namelist() if n.endswith(".png")]) == 2
        assert client.post(path, json={**body, "output_ids": []}).status_code == 422
        assert client.post(path, json={**body, "output_ids": ["no-such-image"]}).status_code == 409
        assert client.post(path, json={**body, "version": "stale"}).status_code == 409
        example = client.get("/static/motif/example-brief.json")
        assert example.status_code == 200
        brief = Brief.model_validate(example.json())
        assert brief.brand.logo_path == "" and brief.replay is None
        assert all(p.source.role == "finished_scene" for p in brief.products)
        created = [client.post("/api/motif/import", json=example.json()) for _ in range(2)]
        assert all(r.status_code == 200 for r in created), [r.text for r in created]
        assert created[0].json()["campaign_id"] != created[1].json()["campaign_id"]
        assert not list(Path(state.storage.root).rglob("Exports"))


def test_selected_export_preserves_global_wording_protection(case):
    _, snapshot = case
    from campaign_studio.brief import PhraseRule

    brief = snapshot["plan"].brief
    brief.markets["en-US"].phrase_rules = [PhraseRule(id="blocked", phrase="blocked")]
    snapshot["plan"].outputs[1].text.cta = "This is blocked."
    with pytest.raises(StateError) as error:
        delivery.delivery_manifest(snapshot, output_ids=[snapshot["plan"].outputs[0].id])
    assert error.value.code == "prohibited_phrase"
