"""Opening saved Logo controls avoids work without weakening edit admission."""

import asyncio
import io

import pytest
from PIL import Image
from test_logo_editing import replacement
from test_workflow import setup_case

import campaign_studio.previews as previews_module
import campaign_studio.workspace as workspace_module
from campaign_studio.imaging import save_srgb
from campaign_studio.planning import compare_plans, make_local_plan
from campaign_studio.previews import DraftPreviews
from campaign_studio.state import StateError
from campaign_studio.workspace import ActionError, LogoEdit, Workspace


@pytest.fixture
def saved(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path, 2, True)
    manifest = asyncio.run(runner.execute(run_id))
    edit = LogoEdit(
        run_id=run_id,
        revision_id=manifest["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
    )
    return state, runner, manifest["campaign_id"], edit


def pixels(payload):
    with Image.open(io.BytesIO(payload)) as image:
        assert image.info["icc_profile"]
        return image.mode, image.size, image.tobytes()


def test_unchanged_open_matches_replanned_pixels_geometry_and_contract(saved, monkeypatch):
    state, _, cid, initial = saved
    old = state.plan(initial.run_id)
    replanned = make_local_plan(old, old.brief, state.storage)
    assert replanned.model_dump() == old.model_dump()
    files = {p: p.read_bytes() for p in state.storage.root.rglob("*.png")}
    original_encode = previews_module.save_srgb

    def default_encoding(image, destination, **kwargs):
        save_srgb(image, destination)

    def forbid_replanning(*args, **kwargs):
        pytest.fail("Opening an unchanged logo must reuse its accepted plan")

    monkeypatch.setattr(workspace_module, "make_local_plan", forbid_replanning)
    for ratio in ["1:1", "9:16", "16:9"]:
        edit = initial.model_copy(update={"ratio": ratio})
        _, plan, impact = Workspace(state).logo(cid, edit, allow_unchanged=True)
        assert plan.digest == replanned.digest
        expected = compare_plans(old, replanned).model_dump()
        assert all(impact[key] == value for key, value in expected.items())
        before = DraftPreviews(state)
        monkeypatch.setattr(previews_module, "save_srgb", default_encoding)
        original = before.render(cid, edit, replanned, impact, logo_preview=True)
        after = DraftPreviews(state)
        monkeypatch.setattr(previews_module, "save_srgb", original_encode)
        actual = after.render(cid, edit, plan, impact, logo_preview=True)
        original_layers = original[0]["layers"]
        actual_layers = actual[0]["layers"]
        assert {k: v for k, v in original_layers.items() if not k.endswith("_url")} == {
            k: v for k, v in actual_layers.items() if not k.endswith("_url")
        }
        original_images = next(iter(before.entries.values()))["images"]
        actual_images = next(iter(after.entries.values()))["images"]
        assert original_images.keys() == actual_images.keys()
        assert len(actual_images) == 4
        for key in original_images:
            assert pixels(original_images[key]) == pixels(actual_images[key])
    assert all(path.read_bytes() == content for path, content in files.items())
    assert state.effective_revision(initial.run_id) == initial.revision_id
    assert state.attempts(initial.run_id) == []


@pytest.mark.parametrize("kind", ["reset", "placement", "replacement", "apply"])
def test_only_initial_open_can_skip_replanning(saved, monkeypatch, kind):
    state, _, cid, edit = saved
    values = edit.model_dump(exclude_unset=True)
    if kind == "reset":
        values["placement"] = None
    elif kind == "placement":
        values["placement"] = {"left": 0.1, "top": 0.1, "width": 0.2}
    elif kind == "replacement":
        values["replacement"] = replacement(state, cid)
    calls = []

    def record(previous, brief, storage):
        calls.append(True)
        return make_local_plan(previous, brief, storage)

    monkeypatch.setattr(workspace_module, "make_local_plan", record)
    edit = LogoEdit.model_validate(values)
    if kind == "apply":
        with pytest.raises(ActionError) as error:
            Workspace(state).logo(cid, edit)
        assert error.value.code == "no_changes"
    else:
        Workspace(state).logo(cid, edit, allow_unchanged=True)
    assert calls == [True]


@pytest.mark.parametrize(
    "damage", ["missing_logo", "changed_logo", "missing_source", "changed_source"]
)
def test_unchanged_open_still_checks_current_input_bytes(saved, damage):
    state, _, cid, edit = saved
    plan = state.plan(edit.run_id)
    logo = damage.endswith("logo")
    asset = plan.assets["logo" if logo else "products/product-1"]
    path = state.storage.path(asset.reference)
    if damage.startswith("missing"):
        path.unlink()
    else:
        path.write_bytes(b"Not the accepted input bytes")
    if logo:
        _, proposed, impact = Workspace(state).logo(cid, edit, allow_unchanged=True)
        assert not impact["logo_available"]
        items = DraftPreviews(state).render(cid, edit, proposed, impact, logo_preview=True)
        assert items[0]["layers"]["logo_url"] is None
    else:
        with pytest.raises(StateError, match="missing or changed"):
            Workspace(state).logo(cid, edit, allow_unchanged=True)


def test_unchanged_open_cannot_reuse_another_tabs_superseded_revision(saved):
    state, runner, cid, edit = saved
    moved = LogoEdit.model_validate(
        {
            **edit.model_dump(exclude_unset=True),
            "placement": {"left": 0.1, "top": 0.1, "width": 0.2},
        }
    )
    brief, plan, _ = Workspace(state).logo(cid, moved)
    runner.revise(edit.run_id, brief, edit.revision_id, plan.digest, logo_edit=moved)
    with pytest.raises(ActionError) as error:
        Workspace(state).logo(cid, edit, allow_unchanged=True)
    assert error.value.code == "logo_stale_revision"


def test_only_logo_previews_use_fast_compression(saved, monkeypatch):
    from campaign_studio.workspace import FrameEdit

    state, _, cid, logo = saved
    calls = []

    def record(image, path, *, compress_level=None):
        calls.append(compress_level)
        save_srgb(image, path, compress_level=compress_level)

    monkeypatch.setattr(previews_module, "save_srgb", record)
    _, plan, impact = Workspace(state).logo(cid, logo, allow_unchanged=True)
    DraftPreviews(state).render(cid, logo, plan, impact, logo_preview=True)
    assert calls == [3, 3, 3, 3]
    calls.clear()
    framing = FrameEdit(
        run_id=logo.run_id,
        revision_id=logo.revision_id,
        product_id=logo.product_id,
        market=logo.market,
        ratio=logo.ratio,
        framing=plan.outputs[0].framing,
    )
    DraftPreviews(state).render(cid, framing, plan, impact, framing_preview=True)
    assert calls == [None, None]
