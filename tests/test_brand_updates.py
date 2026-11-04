"""Saved Brand intent updates current pixels without admitting unrelated drafts."""

import asyncio
import copy
import hashlib
import io
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_partial_editing import omitted, partial  # noqa: F401
from test_provider_runner import generated_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brand_updates import BrandUpdate, BrandUpdates
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.handoff import Handoff, PackageAction
from campaign_studio.intake import Intake
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import RevisionTarget
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage
from campaign_studio.workspace import ActionError, Workspace


@pytest.fixture
def case(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, original = write_sample(storage, 2, True)
    value = original.model_dump(mode="json")
    value["products"] = value["products"][:1]
    value["products"][0]["framing"] = {
        "en-US": {
            ratio: {
                "focal_x": 0.45,
                "zoom": 1.1,
                "logo_placement": {"left": 0.7, "top": 0.04, "width": 0.15},
                "text_placements": {
                    "message": {
                        "left": 0.06,
                        "top": 0.7,
                        "width": 0.8,
                        "font_size": 30,
                        "color": "#FFFFFF",
                    },
                    "cta": {
                        "left": 0.06,
                        "top": 0.85,
                        "width": 0.8,
                        "font_size": 25,
                        "color": None,
                    },
                },
            }
            for ratio in ("1:1", "9:16", "16:9")
        }
    }
    state = State(storage)
    run = state.create(make_plan(Brief.model_validate(value), storage), 0)
    state.confirm(run, state.plan(run).digest)
    runner = Runner(state)
    first = asyncio.run(runner.execute(run))
    assert first["complete"]
    return state, runner, run, first


def save(state, cid, change):
    projects = AuthoredProjects(Workspace(state))
    current = projects.read(cid)
    authored = copy.deepcopy(current["authored"])
    change(authored)
    saved = projects.save(
        cid, AuthoredSave(version=current["version"], base=current["base"], authored=authored)
    )
    return saved, authored


def command(state, run, version, targets=()):
    return BrandUpdate(
        version=version,
        run_id=run,
        revision_id=state.effective_revision(run),
        default_color_targets=list(targets),
    )


def apply(state, runner, edit):
    brief, plan, _ = BrandUpdates(Workspace(state)).prepare(
        state.run(edit.run_id)["campaign_id"], edit
    )
    return runner.revise(edit.run_id, brief, edit.revision_id, plan.digest, brand_edit=edit)


def update_brand(authored):
    authored["brief"]["brand"].update(
        name="Updated Brand", palette=["#FFFFFF", "#FFEE00"], text_color="#FFEE00"
    )


def target(ratio="1:1"):
    return {"product_id": "product-1", "market": "en-US", "ratio": ratio}


def select_default_colors(authored, ratios=("1:1",)):
    """Match the Brand control: save null colors and the selected image targets."""
    authored["form_drafts"]["brand_color_defaults"] = [target(ratio) for ratio in ratios]
    for ratio in ratios:
        for placement in authored["brief"]["products"][0]["framing"]["en-US"][ratio][
            "text_placements"
        ].values():
            placement["color"] = None


def test_brand_route_preserves_invalid_unrelated_draft_and_custom_layout(case):
    state, _, run, first = case
    cid = first["campaign_id"]
    old = state.plan(run).brief.model_dump(mode="json")

    def change(authored):
        update_brand(authored)
        authored["brief"]["audience"] = ""
        authored["brief"]["products"][0]["description"] = ""
        authored["brief"]["products"][0]["source"]["path"] = ""
        authored["brief"]["markets"]["en-US"]["copy"]["message"] = ""
        authored["form_drafts"] = {"fields": {"unrelated": "unfinished"}}

    saved, authored = save(state, cid, change)
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Brand must not request a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(
            f"/api/motif/campaigns/{cid}/brand/apply",
            json=command(state, run, saved["version"]).model_dump(),
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["project"]["authored"] == authored
        assert data["manifest"]["revision_id"] != first["revision_id"]
    after = state.plan(run).brief.model_dump(mode="json")
    assert after["brand"]["name"] == "Updated Brand"
    assert {k: v for k, v in after.items() if k != "brand"} == {
        k: v for k, v in old.items() if k != "brand"
    }
    assert state.attempts(run) == [] and len(Workspace(state).runs(cid)) == 1


def test_only_explicit_default_targets_reset_colors_and_acknowledge_subset(case):
    state, runner, run, first = case
    cid = first["campaign_id"]

    def use_default(authored):
        select_default_colors(authored, ("1:1", "16:9"))
        authored["form_drafts"]["other"] = "keep"

    saved, authored = save(state, cid, use_default)
    result = apply(state, runner, command(state, run, saved["version"], [target()]))
    changed = [
        b["id"]
        for a, b in zip(first["outputs"], result["outputs"], strict=True)
        if a["sha256"] != b["sha256"]
    ]
    # White already equals the inherited color. No other output may change.
    assert set(changed) <= {"product-1/en-US/1x1"}
    frames = state.plan(run).brief.products[0].framing["en-US"]
    assert frames["1:1"].text_placements["message"].color is None
    assert frames["16:9"].text_placements["message"].color == "#FFFFFF"
    reopened = AuthoredProjects(Workspace(state)).read(cid)["authored"]
    assert reopened["form_drafts"] == {"brand_color_defaults": [target("16:9")], "other": "keep"}
    expected = copy.deepcopy(authored["brief"])
    expected["products"][0]["framing"]["en-US"]["1:1"]["text_placements"]["message"]["color"] = None
    assert reopened["brief"] == expected


@pytest.mark.parametrize("operation", ["replace", "remove", "add"])
def test_logo_changes_use_owned_snapshots_and_preserve_exports(case, operation):
    state, runner, run, first = case
    cid = first["campaign_id"]
    if operation == "add":
        saved, _ = save(
            state, cid, lambda a: a["brief"]["brand"].update(logo_path="", require_logo=False)
        )
        first = apply(state, runner, command(state, run, saved["version"]))
    revision = first["revision_id"]
    selected = RevisionTarget(campaign_id=cid, run_id=run, revision_id=revision, mode="current")
    handoff = Handoff(state)
    snapshot = state.metadata_snapshot(revision)
    package = handoff.create_zip(selected, PackageAction(snapshot_id=snapshot["snapshot_id"]))
    old_bytes = handoff.download(selected, package["artifact_id"])
    old_frame = state.plan(run).brief.products[0].framing
    logo = ""
    if operation != "remove":
        buffer = io.BytesIO()
        Image.new("RGB", (200, 80), "#FF7700").save(buffer, format="PNG")
        logo = Intake(state.storage).upload(buffer.getvalue(), "image/png", cid)["reference"]
    saved, _ = save(
        state, cid, lambda a: a["brief"]["brand"].update(logo_path=logo, require_logo=False)
    )
    result = apply(state, runner, command(state, run, saved["version"]))
    assert result["complete"]
    final = state.plan(run)
    assert bool(final.brief.brand.logo_path) == bool(logo)
    if logo:
        assert f"/Runs/{run}/Inputs/logo-" in final.brief.brand.logo_path
    assert final.brief.products[0].framing == old_frame
    assert (
        handoff.download(
            selected.model_copy(update={"mode": "saved_version"}), package["artifact_id"]
        )
        == old_bytes
    )
    current = selected.model_copy(update={"revision_id": result["revision_id"]})
    export = handoff.create_zip(
        current,
        PackageAction(snapshot_id=state.metadata_snapshot(result["revision_id"])["snapshot_id"]),
    )
    with ZipFile(io.BytesIO(handoff.download(current, export["artifact_id"]))) as archive:
        pngs = [name for name in archive.namelist() if name.endswith(".png")]
        assert len(pngs) == 3
        assert sorted(hashlib.sha256(archive.read(name)).hexdigest() for name in pngs) == sorted(
            item["sha256"] for item in result["outputs"]
        )
    assert state.attempts(run) == []


@pytest.mark.parametrize("damage", ["source", "output", "logo"])
def test_damaged_inputs_refuse_without_changing_current(case, damage):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    plan = state.plan(run)
    reference = plan.assets["products/product-1" if damage == "source" else "logo"].reference
    if damage == "output":
        prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
        reference = f"{prefix}/{first['outputs'][0]['file']}"
    state.storage.path(reference).write_bytes(b"damaged")
    with pytest.raises((ActionError, StateError)):
        apply(state, runner, command(state, run, saved["version"]))
    assert state.effective_revision(run) == first["revision_id"] and state.attempts(run) == []


def test_competing_save_during_render_refuses_publication_and_preserves_newer_draft(
    case, monkeypatch
):
    import campaign_studio.rendering as rendering

    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    edit = command(state, run, saved["version"])
    compose = rendering.compose
    later = []

    def concurrent(*args, **kwargs):
        if not later:
            later.append(
                save(state, cid, lambda a: a["brief"].update(audience="Later saved work"))[1]
            )
        return compose(*args, **kwargs)

    monkeypatch.setattr(rendering, "compose", concurrent)
    with pytest.raises(ActionError) as error:
        apply(state, runner, edit)
    assert error.value.code == "authored_conflict"
    assert state.effective_revision(run) == first["revision_id"]
    assert AuthoredProjects(Workspace(state)).read(cid)["authored"] == later[0]
    assert state.attempts(run) == []


def test_partial_missing_product_is_not_generated_or_recovered(partial):  # noqa: F811
    state, runner, run, first, _, calls = partial
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    before_attempts = state.attempts(run)
    result = apply(state, runner, command(state, run, saved["version"]))
    assert omitted(result) == omitted(first)
    assert result["metrics"]["completed_images"] == first["metrics"]["completed_images"]
    assert state.attempts(run) == before_attempts and len(calls) == 1


def test_existing_generated_sources_reused_without_another_request(tmp_path):
    state, runner, run, response = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    )
    first = asyncio.run(runner.execute(run, provider))
    saved, _ = save(state, first["campaign_id"], update_brand)
    before = state.plan(run)
    attempts = state.attempts(run)
    result = apply(state, runner, command(state, run, saved["version"]))
    assert result["complete"] and result["metrics"]["completed_images"] == 12
    assert state.plan(run).generation_requests == before.generation_requests
    assert state.attempts(run) == attempts


def test_newer_unstarted_plan_does_not_replace_output_authority(case):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    old = state.plan(run)
    value = old.brief.model_dump(mode="json")
    value["audience"] = "Different unfinished production"
    newer = state.create(make_plan(Brief.model_validate(value), state.storage), 0)
    result = apply(state, runner, command(state, run, saved["version"]))
    assert state.current_output(cid) == (run, result["revision_id"])
    assert Workspace(state).run_for(cid)["id"] == newer
    assert state.plan(run).brief.audience == old.brief.audience
    assert state.effective_revision(newer) is None


def test_stale_version_revision_foreign_campaign_and_unknown_target_refuse(case, tmp_path):
    from uuid import uuid4

    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    edit = command(state, run, saved["version"])
    service = BrandUpdates(Workspace(state))
    with pytest.raises(ActionError, match="saved campaign changed"):
        service.prepare(cid, edit.model_copy(update={"version": "old"}))
    with pytest.raises(ActionError, match="campaign images changed"):
        service.prepare(cid, edit.model_copy(update={"revision_id": "0" * 32}))
    other_cid = str(uuid4())
    value = state.plan(run).brief.model_dump(mode="json")
    value["campaign_id"] = other_cid
    Intake(state.storage).create(other_cid, value)
    with pytest.raises((ActionError, StateError)):
        service.prepare(other_cid, edit)
    with pytest.raises(ActionError) as error:
        service.prepare(
            cid,
            command(
                state,
                run,
                saved["version"],
                [{"product_id": "not-here", "market": "en-US", "ratio": "1:1"}],
            ),
        )
    assert error.value.code == "creative"
    assert state.effective_revision(run) == first["revision_id"]


def test_publication_receipt_recovers_lost_draft_persistence(case, monkeypatch):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(
        state,
        cid,
        lambda a: (update_brand(a), select_default_colors(a)),
    )
    edit = command(state, run, saved["version"], [target()])
    monkeypatch.setattr(
        Intake,
        "persist_applied_revision",
        lambda *args: (_ for _ in ()).throw(OSError("simulated draft write failure")),
    )
    with pytest.raises(OSError, match="simulated"):
        apply(state, runner, edit)
    assert state.effective_revision(run) != first["revision_id"]
    reopened = AuthoredProjects(Workspace(State(state.storage))).read(cid)
    assert reopened["authored"]["form_drafts"]["brand_color_defaults"] == []
    assert (
        reopened["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["text_placements"][
            "message"
        ]["color"]
        is None
    )
    assert state.attempts(run) == []


def test_unrelated_invalid_framing_stays_exact_when_reset_targets_apply(case):
    state, runner, run, first = case
    cid = first["campaign_id"]

    def change(authored):
        update_brand(authored)
        authored["brief"]["products"][0]["framing"] = "unfinished"

    saved, authored = save(state, cid, change)
    result = apply(state, runner, command(state, run, saved["version"], [target()]))
    assert result["complete"]
    assert AuthoredProjects(Workspace(state)).read(cid)["authored"] == authored


def test_tall_logo_fit_and_color_reset_reconcile_only_their_fields(case):
    state, runner, run, first = case
    cid = first["campaign_id"]
    buffer = io.BytesIO()
    Image.new("RGB", (100, 1500), "#FF7700").save(buffer, format="PNG")
    logo = Intake(state.storage).upload(buffer.getvalue(), "image/png", cid)["reference"]

    def change(authored):
        authored["brief"]["brand"]["logo_path"] = logo
        authored["brief"]["products"][0]["framing"]["en-US"]["1:1"]["focal_x"] = 0.3
        select_default_colors(authored)

    saved, _ = save(state, cid, change)
    result = apply(state, runner, command(state, run, saved["version"], [target()]))
    assert result["complete"]
    accepted = state.plan(run).brief.products[0].framing["en-US"]["1:1"]
    assert accepted.focal_x == 0.45 and accepted.logo_placement.width < 0.15
    reopened = AuthoredProjects(Workspace(state)).read(cid)["authored"]
    raw = reopened["brief"]["products"][0]["framing"]["en-US"]["1:1"]
    assert raw["focal_x"] == 0.3
    assert raw["logo_placement"] == accepted.logo_placement.model_dump()
    assert raw["text_placements"]["message"]["color"] is None


def test_palette_cannot_silently_recolor_custom_studio_text(case):
    from pydantic import ValidationError

    state, runner, run, first = case
    saved, _ = save(
        state,
        first["campaign_id"],
        lambda a: a["brief"]["brand"].update(palette=["#FFEE00"], text_color="#FFEE00"),
    )
    with pytest.raises(ValidationError, match="text placement color"):
        apply(state, runner, command(state, run, saved["version"]))
    assert state.effective_revision(run) == first["revision_id"]
    # Only deliberately selecting each custom-colored image permits inheritance.
    saved, _ = save(
        state, first["campaign_id"], lambda a: select_default_colors(a, ("1:1", "9:16", "16:9"))
    )
    result = apply(
        state,
        runner,
        command(state, run, saved["version"], [target(r) for r in ("1:1", "9:16", "16:9")]),
    )
    assert result["complete"]


def test_brand_only_acknowledges_same_plan_without_admitting_unrelated_work(case):
    from campaign_studio.motif import campaign_data

    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    result = apply(state, runner, command(state, run, saved["version"]))
    current = AuthoredProjects(Workspace(state)).read(cid)
    planned = Intake(state.storage).prepare(cid, current["version"], Workspace(state))
    assert planned.brief == state.plan(run).brief
    assert state.current_output(cid) == (run, result["revision_id"])
    assert len(Workspace(state).runs(cid)) == 1
    assert (
        campaign_data(Workspace(state), cid)["project"]["authored"]["brief"]["brand"]["name"]
        == "Updated Brand"
    )


def pending_color_reset(state, cid):
    def change(authored):
        authored["form_drafts"]["brand_color_defaults"] = [target(), target("16:9")]
        for ratio in ("1:1", "16:9"):
            placements = authored["brief"]["products"][0]["framing"]["en-US"][ratio][
                "text_placements"
            ]
            for placement in placements.values():
                placement["color"] = None
        authored["brief"]["audience"] = "unfinished audience"

    return save(state, cid, change)


def apply_studio_color(state, runner, run, color, *, left=None):
    from campaign_studio.workspace import TextEdit

    cid = state.run(run)["campaign_id"]
    placement = (
        state.plan(run)
        .brief.products[0]
        .framing["en-US"]["1:1"]
        .text_placements["message"]
        .model_dump(mode="json")
    )
    placement["color"] = color
    if left is not None:
        placement["left"] = left
    edit = TextEdit(
        run_id=run,
        revision_id=state.effective_revision(run),
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        placements={"message": placement},
    )
    brief, plan, _ = Workspace(state).text(cid, edit)
    return runner.revise(run, brief, edit.revision_id, plan.digest, text_edit=edit)


def test_later_studio_color_supersedes_older_saved_brand_reset(case):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    apply(state, runner, command(state, run, saved["version"]))
    pending_color_reset(state, cid)
    # A fresh service instance models a reload with the saved reset still pending.
    assert (
        len(
            AuthoredProjects(Workspace(State(state.storage))).read(cid)["authored"]["form_drafts"][
                "brand_color_defaults"
            ]
        )
        == 2
    )
    apply_studio_color(state, runner, run, "#FFEE00", left=0.08)
    saved, authored = save(state, cid, lambda a: a["brief"]["brand"].update(name="Later Brand"))
    apply(
        state,
        runner,
        command(state, run, saved["version"], authored["form_drafts"]["brand_color_defaults"]),
    )
    frames = state.plan(run).brief.products[0].framing["en-US"]
    assert frames["1:1"].text_placements["message"].color == "#FFEE00"
    assert frames["1:1"].text_placements["message"].left == 0.08
    assert frames["16:9"].text_placements["message"].color is None
    reopened = AuthoredProjects(Workspace(state)).read(cid)["authored"]
    assert reopened["brief"]["audience"] == "unfinished audience"
    assert (
        reopened["brief"]["products"][0]["framing"]["en-US"]["1:1"]["text_placements"]["message"][
            "color"
        ]
        == "#FFEE00"
    )
    assert reopened["form_drafts"]["brand_color_defaults"] == []
    assert state.attempts(run) == []


def test_saved_stale_reset_cannot_erase_later_studio_color(case):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    apply(state, runner, command(state, run, saved["version"]))
    pending_color_reset(state, cid)
    accepted = apply_studio_color(state, runner, run, "#FFEE00")
    # A stale browser form buffer is saved after the Studio acknowledgement.
    # Its reset metadata contradicts the explicit color in the saved draft.
    saved, authored = save(
        state,
        cid,
        lambda a: a["form_drafts"].update(brand_color_defaults=[target(), target("16:9")]),
    )
    placement = authored["brief"]["products"][0]["framing"]["en-US"]["1:1"]["text_placements"][
        "message"
    ]
    assert placement["color"] == "#FFEE00"
    with pytest.raises(ActionError, match="Image colors changed") as refused:
        apply(state, runner, command(state, run, saved["version"], [target()]))
    assert refused.value.code == "authored_conflict"
    assert state.effective_revision(run) == accepted["revision_id"]
    assert Workspace(state).manifest(state.run(run))["outputs"] == accepted["outputs"]
    assert AuthoredProjects(Workspace(state)).read(cid)["authored"] == authored
    assert state.attempts(run) == []


def test_geometry_only_apply_keeps_saved_color_reset_intent(case):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = pending_color_reset(state, cid)
    apply_studio_color(state, runner, run, "#FFFFFF", left=0.08)
    reopened = AuthoredProjects(Workspace(state)).read(cid)
    assert reopened["version"] == saved["version"]
    assert reopened["authored"]["form_drafts"]["brand_color_defaults"] == [target(), target("16:9")]
    apply(state, runner, command(state, run, reopened["version"], [target()]))
    frame = state.plan(run).brief.products[0].framing["en-US"]["1:1"]
    assert frame.text_placements["message"].color is None
    assert frame.text_placements["message"].left == 0.08


def test_new_color_reset_saved_after_studio_apply_remains_deliberate(case):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    apply(state, runner, command(state, run, saved["version"]))
    pending_color_reset(state, cid)
    apply_studio_color(state, runner, run, "#FFEE00")
    saved, _ = pending_color_reset(state, cid)
    apply(state, runner, command(state, run, saved["version"], [target()]))
    assert (
        state.plan(run).brief.products[0].framing["en-US"]["1:1"].text_placements["message"].color
        is None
    )


def test_newer_save_during_studio_apply_retains_its_reset_and_fields(case, monkeypatch):
    import campaign_studio.runner as runner_module

    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    apply(state, runner, command(state, run, saved["version"]))
    pending_color_reset(state, cid)
    render = runner_module.render_revision
    newer = {}

    def concurrent_save(*args, **kwargs):
        newer["saved"], newer["authored"] = pending_color_reset(state, cid)
        return render(*args, **kwargs)

    monkeypatch.setattr(runner_module, "render_revision", concurrent_save)
    apply_studio_color(state, runner, run, "#FFEE00")
    reopened = AuthoredProjects(Workspace(state)).read(cid)
    assert reopened["version"] == newer["saved"]["version"]
    assert reopened["authored"] == newer["authored"]
    assert (
        state.plan(run).brief.products[0].framing["en-US"]["1:1"].text_placements["message"].color
        == "#FFEE00"
    )


def test_lost_color_reconciliation_recovers_and_refuses_stale_brand_target(case, monkeypatch):
    state, runner, run, first = case
    cid = first["campaign_id"]
    saved, _ = save(state, cid, update_brand)
    apply(state, runner, command(state, run, saved["version"]))
    pending_color_reset(state, cid)
    persist = Intake.persist_applied_revision

    def fail_persistence(*args, **kwargs):
        raise OSError("simulated color acknowledgement interruption")

    monkeypatch.setattr(Intake, "persist_applied_revision", fail_persistence)
    with pytest.raises(OSError, match="simulated color"):
        apply_studio_color(state, runner, run, "#FFEE00")
    raw = Intake(state.storage).read(cid)
    revision = state.effective_revision(run)
    assert raw["authored"]["form_drafts"]["brand_color_defaults"] == [target(), target("16:9")]
    reopened = AuthoredProjects(Workspace(State(state.storage))).read(cid)
    assert reopened["authored"]["form_drafts"]["brand_color_defaults"] == [target("16:9")]
    with pytest.raises(ActionError, match="Image colors changed"):
        BrandUpdates(Workspace(state)).prepare(cid, command(state, run, raw["version"], [target()]))
    assert state.effective_revision(run) == revision
    monkeypatch.setattr(Intake, "persist_applied_revision", persist)
    Intake(state.storage).persist_applied_revision(
        cid, Workspace(state), only_superseded_colors=True
    )
    persisted = Intake(state.storage).read(cid)
    assert persisted["version"] != raw["version"]
    assert persisted["authored"]["form_drafts"]["brand_color_defaults"] == [target("16:9")]
    assert state.attempts(run) == []
