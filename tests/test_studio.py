"""Connected Studio state tests; all imagery and transports remain local."""

import copy

import pytest
from campaign_fixtures import authored_edit
from fastapi.testclient import TestClient
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.brief import Box, Framing
from campaign_studio.config import Settings
from campaign_studio.imaging import crop_box, protects_subject
from campaign_studio.review import CreativeUpdate
from campaign_studio.state import State


@pytest.mark.parametrize(
    "size,ratio,zoom,position,expected",
    [
        ((1000, 1000), "1:1", 1, (0, 1), (0, 0, 1000, 1000)),
        ((1000, 1000), "9:16", 1, (0, 1), (0, 0, 562.5, 1000)),
        ((1000, 1000), "9:16", 1, (1, 0), (437.5, 0, 1000, 1000)),
        ((1000, 1000), "16:9", 1, (0.5, 0.5), (0, 218.75, 1000, 781.25)),
        ((1000, 1000), "1:1", 2, (0.333, 0.667), (166.5, 333.5, 666.5, 833.5)),
        ((800, 400), "1:1", 1, (0.251, 0.9), (100.4, 0, 500.4, 400)),
    ],
)
def test_float_crop_geometry(size, ratio, zoom, position, expected):
    assert crop_box(
        size,
        ratio,
        Framing(
            zoom=zoom,
            focal_x=position[0],
            focal_y=position[1],
        ),
    ) == pytest.approx(expected)


def test_subpixel_axis_threshold_and_default_layout():
    small = crop_box((500, 500), "1:1", Framing(zoom=1.001, focal_x=0, focal_y=1))
    larger = crop_box((501, 501), "1:1", Framing(zoom=1.001, focal_x=0, focal_y=1))
    assert small[0] == pytest.approx(small[1])  # No meaningful room; both centered.
    assert larger[0] == 0 and larger[1] > 0.5
    old = Framing.model_validate({"focal_x": 0.6, "copy_placement": "bottom"})
    assert old.zoom == 1 and old.subject_box is None and old.inherit_subject


@pytest.mark.parametrize("edge", ["left", "top", "right", "bottom"])
@pytest.mark.parametrize("overhang,allowed", [(0.499, True), (0.5, True), (0.501, False)])
def test_subject_tolerance_is_source_pixels(edge, overhang, allowed):
    bounds = {"left": 0.2, "top": 0.2, "right": 0.8, "bottom": 0.8}
    bounds[edge] += overhang / 1000 * (-1 if edge in {"left", "top"} else 1)
    assert protects_subject((1000, 1000), (200, 200, 800, 800), Box(**bounds)) is allowed


def test_frame_clear_preview_save_and_description_survives_restart(tmp_path):
    from test_workflow import delivery

    from campaign_studio.campaign_drafts import read, write
    from campaign_studio.compositions import save_metadata
    from campaign_studio.motif import campaign_data
    from campaign_studio.workspace import FrameEdit, Workspace

    state, _, run, brief = setup_case(tmp_path)
    ws = Workspace(state)
    cid = brief.campaign_id
    doc = read(ws.storage, cid)
    box = {"left": 0.45, "top": 0.45, "right": 0.55, "bottom": 0.55}
    doc["authored"]["brief"]["products"][0]["source"]["subject_box"] = box
    write(ws.storage, cid, doc)
    first = campaign_data(ws, cid)
    for output in first["manifest"]["outputs"]:
        save_metadata(
            ws,
            cid,
            read(ws.storage, cid)["version"],
            output["id"],
            CreativeUpdate(expected_version=0, description="Supplied description."),
        )
    before_sources = {p: p.read_bytes() for p in ws.storage.root.rglob("*.png")}
    chosen = first["manifest"]["outputs"][0]
    body = FrameEdit(
        revision_id=read(ws.storage, cid)["version"],
        product_id=chosen["product_id"],
        market=chosen["market"],
        ratio=chosen["ratio"],
        framing=Framing(subject_box=None, inherit_subject=False),
    )
    ws.save_composition(cid, body)
    reopened = Workspace(State(ws.storage))
    current = reopened.current_composition(cid)
    layout = current["plan"].outputs[0].framing
    assert layout.subject_box is None and not layout.inherit_subject
    assert current["plan"].brief.products[0].source.subject_box is not None
    assert all(m["description"] == "Supplied description." for m in current["metadata"].values())
    manifest, _, _, _ = delivery(state, cid)
    assert all(o["accessible_description"] == "Supplied description." for o in manifest["outputs"])
    assert before_sources == {p: p.read_bytes() for p in ws.storage.root.rglob("*.png")}
    assert not list(ws.storage.root.glob("Campaigns/*/Outputs"))
    assert state.revision_count(run) == 0


def test_empty_draft_reopens_but_cannot_plan(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("No provider for draft work"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        cid = client.post("/api/motif/campaigns", json={"name": "Empty Studio"}).json()[
            "campaign_id"
        ]
        document = client.get(f"/api/motif/campaigns/{cid}").json()
        assert document["value"]["products"] == []
        document["value"]["name"] = "Empty, saved, reopened"
        action = authored_edit(client, cid)
        action["authored"]["brief"] = document["value"]
        saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
        assert saved.status_code == 200, saved.text
        assert (
            client.get(f"/api/motif/campaigns/{cid}").json()["project"]["authored"]["brief"]
            == document["value"]
        )
        blocked = client.post(
            f"/api/motif/campaigns/{cid}/plan",
            json={
                "version": saved.json()["version"],
            },
        )
        assert blocked.status_code == 422
        assert blocked.json()["field_errors"][0]["path"] == ["products"]
        assert not app.state.workspace.run_for(cid)


def test_activity_is_committed_autosave_not_read_or_failed_save(tmp_path, monkeypatch):
    import campaign_studio.authored as authored_module
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.campaign_drafts import read
    from campaign_studio.workspace import Workspace

    state, _, run, brief = setup_case(tmp_path)
    cid = brief.campaign_id
    project = AuthoredProjects(Workspace(state))
    current = project.read(cid)
    value = copy.deepcopy(current["authored"])
    value["brief"]["name"] = "Saved rename"
    action = AuthoredSave(version=current["version"], base=current["base"], authored=value)
    monkeypatch.setattr(authored_module, "timestamp", lambda: "2026-10-29T12:30:00-07:00")
    saved = project.save(cid, action)
    doc = read(state.storage, cid)
    assert state.saved_activity(cid, doc["saved_at"]) == "2026-10-29T12:30:00-07:00"
    assert project.save(cid, action)["version"] == saved["version"]
    current = project.read(cid)
    value["brief"]["name"] = "Must not be saved"
    action = AuthoredSave(version=current["version"], base=current["base"], authored=value)
    monkeypatch.setattr(
        authored_module, "write_draft", lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
    )
    with pytest.raises(OSError):
        project.save(cid, action)
    assert read(state.storage, cid) == doc
    assert state.attempts(run) == []


def test_activity_tracks_metadata_autosave_with_timestamp_offsets(tmp_path, monkeypatch):
    from campaign_studio.campaign_drafts import read
    from campaign_studio.compositions import save_metadata
    from campaign_studio.workspace import ActionError, Workspace

    state, _, _, brief = setup_case(tmp_path)
    ws = Workspace(state)
    cid = brief.campaign_id
    version = read(state.storage, cid)["version"]
    monkeypatch.setattr(
        "campaign_studio.compositions.timestamp", lambda: "2026-10-29T20:01:00+00:00"
    )
    action = CreativeUpdate(expected_version=0, description="Supplied description.")
    save_metadata(ws, cid, version, "product-1/en-US/1x1", action)
    doc = read(state.storage, cid)
    assert State(state.storage).saved_activity(cid, doc["saved_at"]) == "2026-10-29T20:01:00+00:00"
    with pytest.raises(ActionError):
        save_metadata(
            ws,
            cid,
            version,
            "product-1/en-US/1x1",
            CreativeUpdate(expected_version=0, description="Stale description."),
        )
    assert read(state.storage, cid) == doc
