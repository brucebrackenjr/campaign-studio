"""Real local logo revisions, owned replacement assets, and preserved partial omissions."""

import asyncio
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops
from test_partial_editing import omitted, partial  # noqa: F401
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.brief import Framing, LogoPlacement
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.exporting import package_payloads
from campaign_studio.imaging import load_image, logo_placement, placed_logo_bounds, save_srgb
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.previews import DraftPreviews
from campaign_studio.review import CreativeUpdate
from campaign_studio.state import State, StateError
from campaign_studio.storage import CapacityError
from campaign_studio.workspace import ActionError, FrameEdit, LogoEdit, Workspace


def command(state, run_id, **changes):
    return LogoEdit(
        run_id=run_id,
        revision_id=state.effective_revision(run_id),
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        placement=LogoPlacement(left=0.04, top=0.03, width=0.8),
        **changes,
    )


def replacement(state, cid, size=(300, 900)):
    with Image.new("RGBA", size, "#167c52") as image, io.BytesIO() as buffer:
        save_srgb(image, buffer)
        return Intake(state.storage).upload(buffer.getvalue(), "image/png", cid)["reference"]


def apply_logo(state, runner, edit):
    brief, plan, impact = Workspace(state).logo(state.run(edit.run_id)["campaign_id"], edit)
    result = runner.revise(edit.run_id, brief, edit.revision_id, plan.digest, logo_edit=edit)
    return result, impact


@pytest.mark.parametrize("explicit", [False, True])
def test_identical_replacement_preserves_default_pixels_but_applies_a_real_selected_move(
    tmp_path, explicit
):
    state, runner, run_id, _ = setup_case(tmp_path, one_market=True)
    first = asyncio.run(runner.execute(run_id))
    if explicit:
        first, _ = apply_logo(state, runner, command(state, run_id))
    cid = first["campaign_id"]
    old = state.plan(run_id)
    output = next(o for o in old.outputs if o.id == "product-1/en-US/1x1")
    placement = output.framing.logo_placement
    if placement is None:
        logo, _ = load_image(state.storage.path(old.assets["logo"].reference))
        rendered, position = logo_placement(logo, (1080, 1080))
        placement = LogoPlacement(
            left=position[0] / 1080, top=position[1] / 1080, width=rendered.width / 1080
        )
        rendered.close()
        logo.close()
    uploaded = Intake(state.storage).upload(
        state.storage.path(old.assets["logo"].reference).read_bytes(), "image/png", cid
    )["reference"]
    edit = LogoEdit(
        run_id=run_id,
        revision_id=first["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        placement=placement,
        replacement=uploaded,
    )
    ws = Workspace(state)
    rows = len(ws.revisions(run_id))
    original_files = {p: p.read_bytes() for p in state.storage.path("campaigns").rglob("*.png")}
    _, preview, impact = ws.logo(cid, edit, allow_unchanged=True)
    assert not impact["render_outputs"]
    assert preview.brief == old.brief
    with pytest.raises(ActionError) as refused:
        apply_logo(state, runner, edit)
    assert refused.value.code == "no_changes"
    assert state.effective_revision(run_id) == first["revision_id"]
    assert len(ws.revisions(run_id)) == rows
    assert all(p.read_bytes() == content for p, content in original_files.items())

    moved = edit.model_copy(
        update={"placement": placement.model_copy(update={"left": placement.left - 1 / 1080})}
    )
    result, impact = apply_logo(state, runner, moved)
    assert impact["render_outputs"] == [edit.output_id]
    changed = [
        o["id"]
        for o in result["outputs"]
        if o["sha256"] != next(p["sha256"] for p in first["outputs"] if p["id"] == o["id"])
    ]
    assert changed == [edit.output_id]
    assert state.plan(run_id).brief.brand.logo_path == old.brief.brand.logo_path
    assert state.attempts(run_id) == []


def test_selected_logo_preview_layers_equal_saved_pixels_and_other_outputs_unchanged(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    edit = command(state, run_id)
    cid = first["campaign_id"]
    for identifier in (edit.output_id, "product-2/en-US/1x1"):
        state.update_creative(
            first["revision_id"],
            identifier,
            CreativeUpdate(expected_version=0, description="Supplied description."),
        )
    _, plan, impact = Workspace(state).logo(cid, edit)
    assert impact["render_outputs"] == [edit.output_id]
    previews = DraftPreviews(state)
    rendered = previews.render(cid, edit, plan, impact, logo_preview=True)
    assert len(rendered) == 1
    token = rendered[0]["url"].split("/")[-4]

    def get(suffix=""):
        return previews.image(cid, token, edit.product_id, edit.market, "1x1" + suffix)

    preview_pixels = get()
    with (
        Image.open(io.BytesIO(get("-base"))) as base,
        Image.open(io.BytesIO(get("-copy"))) as copy,
        Image.open(io.BytesIO(get("-logo"))) as logo,
    ):
        assert copy.mode == "RGBA" and logo.size == (200, 80)
        # The clean base cannot contain the old fixed logo or typography.
        assert ImageChops.difference(
            base.convert("RGB"), Image.open(io.BytesIO(get())).convert("RGB")
        ).getbbox()
        left, top, right, bottom = rendered[0]["layers"]["bounds"]
        composed = base.convert("RGBA")
        artwork = logo.resize((right - left, bottom - top), Image.Resampling.LANCZOS)
        composed.alpha_composite(artwork, (left, top))
        composed.alpha_composite(copy)
        with Image.open(io.BytesIO(preview_pixels)) as final:
            assert composed.convert("RGB").tobytes() == final.tobytes()
        artwork.close()
        composed.close()
    before_revision = state.effective_revision(run_id)
    second, _ = apply_logo(state, runner, edit)
    assert second["complete"] and second["revision_id"] != before_revision
    assert state.attempts(run_id) == []
    metadata = state.metadata_snapshot(second["revision_id"])["creatives"]
    assert metadata[edit.output_id]["description"] is None
    assert metadata["product-2/en-US/1x1"]["description"] == "Supplied description."
    assert metadata["product-2/en-US/1x1"]["description"] == "Supplied description."
    for old, new in zip(first["outputs"], second["outputs"], strict=True):
        assert (old["sha256"] != new["sha256"]) == (new["id"] == edit.output_id)
    output = next(o for o in second["outputs"] if o["id"] == edit.output_id)
    record = state.revision(second["revision_id"])
    path = state.storage.path(record["manifest_ref"]).parent / output["file"]
    # Cache identity intentionally expires after publication; retain pre-save bytes above.
    assert previews.image(cid, token, edit.product_id, edit.market, "1x1") is None
    assert not any(
        f["severity"] == "blocking" for f in second["findings"] if f["item"] == edit.output_id
    )
    with Image.open(path) as saved, Image.open(io.BytesIO(preview_pixels)) as preview:
        assert saved.tobytes() == preview.tobytes()
    with pytest.raises(ActionError) as conflict:
        apply_logo(state, runner, edit)
    assert conflict.value.code == "logo_stale_revision"


def test_replacement_fits_available_set_preserves_dirty_brief_and_owned_references(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    intake = Intake(state.storage)
    value = state.plan(run_id).brief.model_dump(mode="json")
    document = intake.write(cid, value, None)
    value["audience"] = "A separately saved audience edit."
    saved_draft = intake.save(
        cid,
        BriefDraft(value=value, version=document["version"], base_revision=first["revision_id"]),
        Workspace(state),
    )
    old_logo = state.plan(run_id).brief.brand.logo_path
    edit = command(
        state, run_id, replacement=replacement(state, cid), draft_version=saved_draft["version"]
    )
    _, proposed, proposed_impact = Workspace(state).logo(cid, edit)
    old_plan = state.plan(run_id)
    old_artwork, _ = load_image(state.storage.path(old_plan.assets["logo"].reference))
    try:
        for output in proposed.outputs:
            if output.id == edit.output_id:
                continue
            size = (output.width, output.height)
            original, position = logo_placement(old_artwork, size)
            left, top, right, bottom = placed_logo_bounds(
                (300, 900), size, output.framing.logo_placement
            )
            assert (left, top) == position
            assert right - left <= original.width and right <= size[0] and bottom <= size[1]
            assert output.id in {f["id"] for f in proposed_impact["fitted_outputs"]}
            original.close()
    finally:
        old_artwork.close()
    previews = DraftPreviews(state)
    image = previews.render(cid, edit, proposed, proposed_impact, logo_preview=True)[0]
    token = image["url"].split("/")[-4]
    preview_pixels = previews.image(cid, token, edit.product_id, edit.market, "1x1")
    before_files = {
        str(path.relative_to(state.storage.root))
        for path in state.storage.root.rglob("*")
        if path.is_file()
    }
    budget = state.storage.budget_bytes
    state.storage.budget_bytes = 1
    with pytest.raises(CapacityError):
        apply_logo(state, runner, edit)
    assert state.effective_revision(run_id) == first["revision_id"]
    assert before_files == {
        str(path.relative_to(state.storage.root))
        for path in state.storage.root.rglob("*")
        if path.is_file()
    }
    state.storage.budget_bytes = budget
    second, impact = apply_logo(state, runner, edit)
    assert len(impact["render_outputs"]) == 12 and impact["fitted_outputs"]
    assert second["complete"] and state.attempts(run_id) == []
    output = next(o for o in second["outputs"] if o["id"] == edit.output_id)
    record = state.revision(second["revision_id"])
    with (
        Image.open(io.BytesIO(preview_pixels)) as preview,
        Image.open(state.storage.path(record["manifest_ref"]).parent / output["file"]) as saved,
    ):
        assert preview.tobytes() == saved.tobytes()
    assert all(
        a["sha256"] != b["sha256"] for a, b in zip(first["outputs"], second["outputs"], strict=True)
    )
    updated = intake.read(cid)
    assert updated["value"]["audience"] == value["audience"]
    assert (
        updated["value"]["brand"]["logo_path"]
        == state.plan(run_id).brief.brand.logo_path
        != old_logo
    )
    assert state.storage.path(old_logo).is_file()
    assert (
        State(state.storage).plan(run_id).brief.brand.logo_path
        == updated["value"]["brand"]["logo_path"]
    )
    # A replacement cannot read an absolute path or another campaign's owned upload.
    bad = command(state, run_id, replacement="/tmp/logo.png", draft_version=updated["version"])
    with pytest.raises(ActionError, match="file picker"):
        Workspace(state).logo(cid, bad)
    state.register_campaign("014c1210-29a9-50d2-823a-7a936aecd62b")
    with state.storage.connect() as db:
        db.execute(
            "UPDATE file_owners SET campaign_id=? WHERE reference=?",
            ("014c1210-29a9-50d2-823a-7a936aecd62b", edit.replacement),
        )
    bad.replacement = edit.replacement
    with pytest.raises(ActionError, match="file picker"):
        Workspace(state).logo(cid, bad)


def test_partial_replacement_preserves_omissions_and_requires_every_available_input(partial):  # noqa: F811
    state, runner, run_id, first, _, calls = partial
    cid = first["campaign_id"]
    edit = command(state, run_id, replacement=replacement(state, cid))
    edit.product_id = "product-2"
    second, impact = apply_logo(state, runner, edit)
    assert len(impact["render_outputs"]) == 6 and omitted(second) == omitted(first)
    assert not second["complete"]
    assert state.run(run_id)["current_revision"] == second["revision_id"]
    assert State(state.storage).effective_revision(run_id) == second["revision_id"]
    assert len(calls) == len(state.attempts(run_id)) == 1
    edit.revision_id = second["revision_id"]
    plan = state.plan(run_id)
    state.storage.path(plan.assets["products/product-2"].reference).write_bytes(b"damaged")
    with pytest.raises(StateError, match="missing or changed"):
        Workspace(state).logo(cid, edit)


def test_logo_http_uses_existing_guards_and_current_preview_digest(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Provider constructed"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        payload = command(state, run_id).model_dump(mode="json")
        url = f"/api/campaigns/{cid}/logo"
        assert client.post(url + "/preview", json=payload).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.post(
                url + "/preview", json=payload, headers={"Origin": "https://other.invalid"}
            ).status_code
            == 403
        )
        result = client.post(url + "/preview", json=payload)
        assert result.status_code == 200, result.text
        preview = result.json()
        assert not preview["blocked"] and len(preview["images"]) == 1
        pixels = client.get(preview["images"][0]["url"]).content
        assert client.post(url + "/apply", json=payload).status_code == 409
        payload["preview_digest"] = preview["digest"]
        result = client.post(url + "/apply", json=payload)
        assert result.status_code == 200, result.text
        record = state.revision(result.json()["revision_id"])
        manifest = json.loads(state.storage.path(record["manifest_ref"]).read_bytes())
        output = next(o for o in manifest["outputs"] if o["id"] == "product-1/en-US/1x1")
        with (
            Image.open(io.BytesIO(pixels)) as draft,
            Image.open(state.storage.path(record["manifest_ref"]).parent / output["file"]) as saved,
        ):
            assert draft.tobytes() == saved.tobytes()
        assert state.attempts(run_id) == []


def test_unrelated_missing_source_does_not_disable_selected_logo_but_blocks_replacement(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    uploaded = replacement(state, cid)
    plan = state.plan(run_id)
    state.storage.path(plan.assets["products/product-2"].reference).unlink()
    edit = command(state, run_id)
    second, _ = apply_logo(state, runner, edit)
    assert second["complete"]
    assert all(
        a["sha256"] == b["sha256"]
        for a, b in zip(first["outputs"], second["outputs"], strict=True)
        if a["product_id"] == "product-2"
    )
    replace = command(state, run_id, replacement=uploaded)
    with pytest.raises(StateError, match="missing or changed for"):
        Workspace(state).logo(cid, replace)
    assert state.effective_revision(run_id) == second["revision_id"]
    # Existing image framing controls must not clear a separately saved logo placement.
    frame = FrameEdit(
        run_id=run_id,
        revision_id=second["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        framing=Framing(zoom=1.1),
    )
    _, framed, _ = Workspace(state).frame(cid, frame)
    assert (
        next(o for o in framed.outputs if o.id == edit.output_id).framing.logo_placement
        == edit.placement
    )
    assert state.attempts(run_id) == []


def test_missing_old_logo_has_clean_replacement_context_without_general_editing(tmp_path):
    from campaign_studio.motif import campaign_data

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    state.storage.path(state.plan(run_id).assets["logo"].reference).unlink()
    data = campaign_data(Workspace(state), cid)
    assert not data["can_edit"] and data["logo_editable_products"]["product-1"]
    edit = LogoEdit(
        run_id=run_id,
        revision_id=first["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
    )
    _, plan, impact = Workspace(state).logo(cid, edit, allow_unchanged=True)
    assert not impact["logo_available"]
    images = DraftPreviews(state).render(cid, edit, plan, impact, logo_preview=True)
    assert len(images) == 1 and images[0]["layers"]["base_url"]
    edit.replacement = replacement(state, cid)
    second, _ = apply_logo(state, runner, edit)
    assert second["complete"] and state.attempts(run_id) == []


@pytest.mark.parametrize("replace", [False, True])
def test_committed_logo_receipt_reconciles_response_loss_without_resending(tmp_path, replace):
    from campaign_studio.motif import campaign_data

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    edit = command(state, run_id, **({"replacement": replacement(state, cid)} if replace else {}))
    _, preview, _ = Workspace(state).logo(cid, edit)
    second, _ = apply_logo(state, runner, edit)
    # A new service instance sees only the published receipt, even if the caller
    # never received the apply response. It does not infer success from pixels.
    current = campaign_data(Workspace(State(state.storage)), cid)
    assert current["manifest"]["edit_receipt"] == {
        "kind": "logo",
        "base_revision": first["revision_id"],
        "preview_digest": preview.digest,
        **({"replacement_draft_version": edit.draft_version} if replace else {}),
    }
    assert current["manifest"]["revision_id"] == second["revision_id"]
    exported = package_payloads(state, second["revision_id"])
    assert "edit_receipt" not in json.loads(exported["manifest.json"])
    assert current["manifest"]["edit_receipt"]["preview_digest"] == preview.digest
    with pytest.raises(ActionError) as conflict:
        apply_logo(state, runner, edit)
    assert conflict.value.code == "logo_stale_revision"
    assert state.revision_count(run_id) == 2
    assert [r["id"] for r in Workspace(state).revisions(run_id)] == [second["revision_id"]]
    newer = command(state, run_id)
    newer.placement = LogoPlacement(left=0.1, top=0.2, width=0.2)
    third, _ = apply_logo(state, runner, newer)
    assert third["edit_receipt"]["base_revision"] == second["revision_id"]
    assert third["edit_receipt"]["preview_digest"] != preview.digest
    assert state.attempts(run_id) == []


def test_damaged_saved_png_needs_explicit_recovery_not_implicit_logo_repair(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    previous = state.revision(first["revision_id"])
    output = next(o for o in first["outputs"] if o["id"] == "product-2/es-MX/9x16")
    damaged = state.storage.path(previous["manifest_ref"]).parent / output["file"]
    damaged.write_bytes(b"Controlled damaged saved PNG")
    source = state.storage.path(state.plan(run_id).assets["products/product-2"].reference)
    source_bytes = source.read_bytes()
    for replacement_path in [None, replacement(state, cid)]:
        edit = command(state, run_id, replacement=replacement_path)
        with pytest.raises(ActionError, match="es-MX · 9:16.*local recovery") as error:
            Workspace(state).logo(cid, edit, allow_unchanged=True)
        assert error.value.code == "saved_images_unavailable"
    assert damaged.read_bytes() == b"Controlled damaged saved PNG"
    assert source.read_bytes() == source_bytes
    assert state.effective_revision(run_id) == first["revision_id"]
    assert len(Workspace(state).revisions(run_id)) == 1
    assert state.attempts(run_id) == []


def test_logo_publication_succeeds_when_only_image_cleanup_remains_pending(tmp_path, monkeypatch):
    state, runner, run_id, _ = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    prefix = state.storage.path(state.revision(first["revision_id"])["manifest_ref"]).parent
    original_unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path.is_relative_to(prefix / "images"):
            raise PermissionError("Controlled old-image cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    edit = command(state, run_id)
    result, _ = apply_logo(state, runner, edit)
    assert result["complete"] and result["edit_receipt"]["base_revision"] == edit.revision_id
    assert state.effective_revision(run_id) == result["revision_id"]
    assert Cleanup(state).status(cid)["pending"]
    next_edit = command(state, run_id)
    next_edit.placement = LogoPlacement(left=0.3, top=0.2, width=0.2)
    with pytest.raises(StateError, match="pending image cleanup"):
        apply_logo(state, runner, next_edit)
    assert state.effective_revision(run_id) == result["revision_id"]
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert Cleanup(state).resume_all()[0]["state"] == "complete"
    assert state.attempts(run_id) == []


def test_selected_replacement_fit_is_verified_counted_once_and_bound_to_preview(tmp_path):
    from pydantic import ValidationError

    from campaign_studio.imaging import fit_logo_placement

    state, runner, run_id, _ = setup_case(tmp_path, one_market=True)
    first = asyncio.run(runner.execute(run_id))
    cid = first["campaign_id"]
    artwork = replacement(state, cid, (120, 1440))
    before = LogoPlacement(left=0.6, top=0.6, width=0.3)
    fitted = fit_logo_placement((120, 1440), (1080, 1080), before)
    edit = command(state, run_id, replacement=artwork, pre_fit_placement=before).model_copy(
        update={"placement": fitted}
    )
    workspace = Workspace(state)
    brief, plan, impact = workspace.logo(cid, edit)
    selected = [o for o in impact["fitted_outputs"] if o["id"] == edit.output_id]
    assert len(selected) == 1
    assert impact["fitted_count"] == len({o["id"] for o in impact["fitted_outputs"]})
    assert selected[0]["bounds"] == list(placed_logo_bounds((120, 1440), (1080, 1080), fitted))
    assert selected[0]["requested"] == before.model_dump()
    # Omitted metadata is compatible and never guesses a fit from intentional shrinking.
    manual = edit.model_copy(update={"pre_fit_placement": None})
    _, manual_plan, manual_impact = workspace.logo(cid, manual)
    assert manual_plan.digest == plan.digest  # Disclosure does not change image composition.
    assert edit.output_id not in {o["id"] for o in manual_impact["fitted_outputs"]}
    wrong = edit.model_copy(update={"placement": fitted.model_copy(update={"left": 0.5})})
    with pytest.raises(ActionError) as refused:
        workspace.logo(cid, wrong)
    assert refused.value.code == "logo_fit"
    for values in ({"left": float("nan")}, {"width": float("inf")}, {"top": -1}):
        with pytest.raises(ValidationError):
            LogoEdit.model_validate(
                {**edit.model_dump(), "pre_fit_placement": {**before.model_dump(), **values}}
            )
    # Apply verifies the same metadata again; a mismatched fit never publishes.
    with pytest.raises(ActionError):
        runner.revise(run_id, brief, edit.revision_id, plan.digest, logo_edit=wrong)
    assert state.effective_revision(run_id) == first["revision_id"]
    result, after_impact = apply_logo(state, runner, edit)
    assert after_impact["fitted_outputs"] == impact["fitted_outputs"]
    assert result["revision_id"] != first["revision_id"]
    assert state.attempts(run_id) == []
