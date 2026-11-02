"""Connected Studio state tests; all imagery and transports remain local."""

import asyncio
import copy
import hashlib
import io

import pytest
from campaign_fixtures import authored_edit
from fastapi.testclient import TestClient
from PIL import Image
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.brief import Box, Framing
from campaign_studio.config import Settings
from campaign_studio.imaging import crop_box, protects_subject
from campaign_studio.intake import Intake
from campaign_studio.review import CreativeUpdate
from campaign_studio.state import State, StateError


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


def test_frame_clear_preview_save_and_region_only_review_reset(tmp_path):
    from campaign_studio.brief import Brief
    from campaign_studio.planning import make_plan
    from campaign_studio.sample import write_sample

    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Framing must stay local"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        _, original = write_sample(ws.storage)
        value = original.model_dump(mode="json")
        value["products"][0]["source"]["subject_box"] = {
            "left": 0.45,
            "top": 0.45,
            "right": 0.55,
            "bottom": 0.55,
        }
        brief = Brief.model_validate(value)
        run_id = ws.state.create(make_plan(brief, ws.storage), 0)
        ws.state.confirm(run_id, ws.state.plan(run_id).digest)
        first = asyncio.run(app.state.runner.execute(run_id))
        before_sources = {
            key: hashlib.sha256(ws.storage.path(asset.reference).read_bytes()).hexdigest()
            for key, asset in ws.state.plan(run_id).assets.items()
        }
        for output in first["outputs"]:
            ws.state.update_creative(
                first["revision_id"],
                output["id"],
                CreativeUpdate(expected_version=0, description="Supplied description."),
            )
        chosen = first["outputs"][0]
        body = {
            "run_id": run_id,
            "revision_id": first["revision_id"],
            "product_id": chosen["product_id"],
            "market": chosen["market"],
            "ratio": chosen["ratio"],
            "framing": {
                "zoom": 1,
                "focal_x": 0.5,
                "focal_y": 0.5,
                "copy_placement": "top",
                "subject_box": None,
                "inherit_subject": False,
            },
        }
        base = f"/api/campaigns/{brief.campaign_id}/framing"
        preview = client.post(base + "/preview", json=body)
        assert preview.status_code == 200, preview.text
        result = preview.json()
        assert result["impact"]["render_outputs"] == [chosen["id"]]
        assert result["impact"]["generation_products"] == []
        png = client.get(result["images"][0]["url"]).content
        overlay = client.get(result["images"][0]["overlay_url"]).content
        with Image.open(io.BytesIO(overlay)) as image:
            assert image.mode == "RGBA" and image.getchannel("A").getextrema() == (0, 255)
        saved = client.post(base + "/apply", json={**body, "preview_digest": result["digest"]})
        assert saved.status_code == 200, saved.text
        second = ws.manifest(ws.state.run(run_id))
        target = next(o for o in second["outputs"] if o["id"] == chosen["id"])
        prefix = ws.state.revision(second["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
        with (
            Image.open(io.BytesIO(png)) as expected,
            Image.open(ws.storage.path(f"{prefix}/{target['file']}")) as actual,
        ):
            assert expected.tobytes() == actual.tobytes()
        assert {o["id"]: o["pixel_sha256"] for o in first["outputs"]} == {
            o["id"]: o["pixel_sha256"] for o in second["outputs"]
        }
        metadata = ws.state.metadata_snapshot(second["revision_id"])["creatives"]
        assert metadata[chosen["id"]]["description"] == "Supplied description."
        assert all(
            row["description"] == "Supplied description."
            for key, row in metadata.items()
            if key != chosen["id"]
        )
        reopened = State(ws.storage)
        layout = reopened.plan(run_id).outputs[0].framing
        assert layout.subject_box is None and not layout.inherit_subject
        assert reopened.plan(run_id).brief.products[0].source.subject_box is not None
        assert all(
            row["description"] == "Supplied description."
            for row in reopened.metadata_snapshot(first["revision_id"])["creatives"].values()
        )
        assert before_sources == {
            key: hashlib.sha256(ws.storage.path(asset.reference).read_bytes()).hexdigest()
            for key, asset in reopened.plan(run_id).assets.items()
        }
        assert reopened.attempts(run_id) == []


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
        document["value"]["name"] = "Empty, saved, and reopened"
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


def test_activity_is_committed_work_not_plan_read_or_failed_save(tmp_path, monkeypatch):
    state, runner, run_id, _ = setup_case(tmp_path)
    intake = Intake(state.storage)
    plan = state.plan(run_id)
    cid = plan.brief.campaign_id
    assert state.saved_activity(cid) is None  # An unexecuted plan is not saved work.
    value = plan.brief.model_dump(mode="json")
    monkeypatch.setattr("campaign_studio.intake.timestamp", lambda: "2026-10-29T12:30:00-07:00")
    saved = intake.write(cid, value, None)
    assert state.saved_activity(cid, saved["saved_at"]) == "2026-10-29T12:30:00-07:00"
    monkeypatch.setattr("campaign_studio.intake.timestamp", lambda: "2026-10-29T20:00:00+00:00")
    unchanged = intake.write(cid, copy.deepcopy(value), saved["version"])
    assert unchanged == saved
    changed = copy.deepcopy(value)
    changed["name"] = "A committed rename"
    original_write = state.storage.write
    monkeypatch.setattr(
        state.storage, "write", lambda *args: (_ for _ in ()).throw(OSError("full"))
    )
    with pytest.raises(OSError):
        intake.write(cid, changed, saved["version"])
    assert intake.read(cid) == saved
    monkeypatch.setattr(state.storage, "write", original_write)
    committed = intake.write(cid, changed, saved["version"])
    # A caller losing the response still reopens the actual committed timestamp/value.
    assert Intake(state.storage).read(cid) == committed
    assert State(state.storage).saved_activity(cid, committed["saved_at"]) == committed["saved_at"]
    assert state.attempts(run_id) == []


def test_activity_tracks_revision_and_metadata_with_timestamp_offsets(tmp_path, monkeypatch):
    state, runner, run_id, _ = setup_case(tmp_path)
    cid = state.plan(run_id).brief.campaign_id
    monkeypatch.setattr("campaign_studio.state.timestamp", lambda: "2026-10-29T13:00:00-07:00")
    manifest = asyncio.run(runner.execute(run_id))
    assert state.saved_activity(cid, "2026-10-29T19:59:59+00:00") == "2026-10-29T13:00:00-07:00"
    creative = manifest["outputs"][0]["id"]
    monkeypatch.setattr("campaign_studio.state.timestamp", lambda: "2026-10-29T20:01:00+00:00")
    state.update_creative(
        manifest["revision_id"],
        creative,
        CreativeUpdate(
            expected_version=0,
            description="Supplied description.",
        ),
    )
    assert State(state.storage).saved_activity(cid) == "2026-10-29T20:01:00+00:00"
    with pytest.raises(StateError):
        state.update_creative(
            manifest["revision_id"],
            creative,
            CreativeUpdate(
                expected_version=0,
                description="Changed description.",
            ),
        )
    assert state.saved_activity(cid, "invalid") == "2026-10-29T20:01:00+00:00"
