import asyncio
import hashlib
import json
import shutil
from html.parser import HTMLParser

import pytest
from PIL import Image, ImageDraw

from campaign_studio.brief import Brief
from campaign_studio.exporting import export_package
from campaign_studio.imaging import load_image, logo_placement, save_srgb
from campaign_studio.planning import compare_plans, make_plan
from campaign_studio.rendering import inspect_png
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage


def setup_case(tmp_path, count=2, one_market=False):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage, count, one_market)
    state = State(storage)
    run_id = state.create(make_plan(brief, storage), 0)
    state.confirm(run_id, state.plan(run_id).digest)
    return state, Runner(state), run_id, brief


def pixel_hashes(state, manifest):
    prefix = state.revision(manifest["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    values = []
    for output in manifest["outputs"]:
        with Image.open(state.storage.path(f"{prefix}/{output['file']}")) as image:
            values.append(hashlib.sha256(image.convert("RGB").tobytes()).hexdigest())
    return values


def edit_copy(state, run_id, message):
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["es-MX"]["copy"]["message"] = message
    return Brief.model_validate(value)


def revise(state, runner, run_id, brief):
    current = state.run(run_id)["current_revision"]
    preview = make_plan(brief, state.storage)
    return runner.revise(run_id, brief, current, preview.digest)


def test_complete_local_run_is_idempotent_and_reopens_without_originals(tmp_path):
    state, runner, run_id, original = setup_case(tmp_path)
    for product in original.products:
        state.storage.path(product.source.path).unlink()
    first = asyncio.run(runner.execute(run_id))
    assert first["complete"] and first["metrics"]["completed_images"] == 12
    assert first["evidence"] == "synthetic"
    assert state.attempts(run_id) == []
    assert asyncio.run(runner.execute(run_id))["revision_id"] == first["revision_id"]
    previous_pixels = pixel_hashes(state, first)
    reopened = Runner(State(state.storage)).recover_local(run_id)
    assert reopened["complete"]
    # Recovery renders again; ICC creation metadata may differ, but pixels must not.
    assert pixel_hashes(state, reopened) == previous_pixels


def test_three_product_us_only_campaign_produces_nine_real_pngs(tmp_path):
    _, runner, run_id, _ = setup_case(tmp_path, 3, True)
    result = asyncio.run(runner.execute(run_id))
    assert result["complete"]
    assert len(result["outputs"]) == 9
    assert result["metrics"]["attempted_requests"] == 0


def test_shared_six_override_three_and_rule_only_revision(tmp_path, monkeypatch):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    corrected = revise(state, runner, run_id, edit_copy(state, run_id, "Para cada día."))
    assert corrected["complete"] and len(corrected["outputs"]) == 12
    changed = [
        new["id"]
        for old, new in zip(first["outputs"], corrected["outputs"], strict=True)
        if old["sha256"] != new["sha256"]
    ]
    assert len(changed) == 6 and all("es-MX" in item for item in changed)
    value = state.plan(run_id).brief.model_dump()
    value["products"][0]["copy_overrides"] = {"es-MX": {"message": "Sólo para hoy."}}
    third = revise(state, runner, run_id, Brief.model_validate(value))
    assert (
        sum(
            a["sha256"] != b["sha256"]
            for a, b in zip(corrected["outputs"], third["outputs"], strict=True)
        )
        == 3
    )
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["es-MX"]["phrase_rules"] = [{"id": "absent", "phrase": "Never printed"}]
    monkeypatch.setattr(
        "campaign_studio.rendering.compose",
        lambda *args: pytest.fail("Rule-only edit rendered imagery"),
    )
    fourth = revise(state, runner, run_id, Brief.model_validate(value))
    assert fourth["complete"]
    assert all(
        item["severity"] == "pass" for item in fourth["findings"] if item["rule"] == "absent"
    )
    assert state.attempts(run_id) == []
    # The saved override round trip must retain omitted-field inheritance.
    assert state.plan(run_id).brief.products[0].copy_overrides["es-MX"].model_fields_set == {
        "message"
    }


def test_blocking_edit_preserves_current_and_exports_explicit_partial(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["en-US"]["copy"]["message"] = "TESTBLOCK"
    blocked = make_plan(Brief.model_validate(value), state.storage)
    with pytest.raises(StateError, match="Correct the campaign details"):
        state.create(blocked, 0)
    partial = revise(state, runner, run_id, blocked.brief)
    assert not partial["complete"]
    assert partial["metrics"]["completed_images"] == 6
    assert state.run(run_id)["current_revision"] == first["revision_id"]
    assert all(item["file"] is None for item in partial["outputs"] if item["market"] == "en-US")
    with pytest.raises(StateError, match="explicitly"):
        export_package(state, partial["revision_id"], "exports/not-allowed")
    index = export_package(state, partial["revision_id"], "exports/partial", allow_partial=True)
    assert "Partial" in state.storage.path(index).read_text()


def test_moved_complete_package_has_only_local_files_and_escaped_content(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    result = asyncio.run(runner.execute(run_id))
    export_package(state, result["revision_id"], "exports/complete")
    moved = tmp_path / "moved"
    shutil.copytree(state.storage.path("exports/complete"), moved)

    class Links(HTMLParser):
        references = []

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name in {"src", "href"}:
                    self.references.append(value)

    parser = Links()
    parser.feed((moved / "index.html").read_text())
    assert len(list(moved.rglob("*.png"))) == 12
    assert all(
        f'id="{item[1:]}"' in (moved / "index.html").read_text()
        if item.startswith("#")
        else ":" not in item and (moved / item).is_file()
        for item in parser.references
    )
    manifest = (moved / "manifest.json").read_text()
    assert "creative_direction" not in manifest and "response.json" not in manifest
    assert "private/" not in manifest and "api_key" not in manifest
    assert json.loads(manifest)["complete"] is True


def test_exported_pixel_checks_detect_removed_logo_and_wrong_text_color(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    result = asyncio.run(runner.execute(run_id))
    plan = state.plan(run_id)
    revision = state.revision(result["revision_id"])
    path = state.storage.path(
        revision["manifest_ref"].rsplit("/", 1)[0] + "/" + result["outputs"][0]["file"]
    )
    logo, _ = load_image(state.storage.path(plan.assets["logo"].reference))
    with Image.open(path) as image:
        damaged = image.convert("RGB")
    placed, position = logo_placement(logo, damaged.size)
    ImageDraw.Draw(damaged).rectangle(
        (*position, position[0] + placed.width, position[1] + placed.height), fill="black"
    )
    save_srgb(damaged, path)
    findings = inspect_png(path, plan.outputs[0], logo, "#FF0000")
    assert {item.rule for item in findings if item.severity == "blocking"} == {
        "logo_pixels",
        "controlled_text_color",
    }
    with pytest.raises(StateError, match="saved output changed"):
        export_package(state, result["revision_id"], "exports/damaged")


def test_stale_edit_preview_is_rejected_without_changing_revision(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    result = asyncio.run(runner.execute(run_id))
    brief = edit_copy(state, run_id, "Para cada día.")
    impact = compare_plans(state.plan(run_id), make_plan(brief, state.storage))
    assert len(impact.render_outputs) == 6
    with pytest.raises(StateError, match="preview changed"):
        runner.revise(run_id, brief, result["revision_id"], "outdated")
    assert state.run(run_id)["current_revision"] == result["revision_id"]


def test_local_recovery_rebuilds_pixels_then_cleans_superseded_output_files(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    revision = state.revision(first["revision_id"])
    prefix = revision["manifest_ref"].rsplit("/", 1)[0]
    damaged = state.storage.path(f"{prefix}/{first['outputs'][0]['file']}")
    expected_pixels = pixel_hashes(state, first)
    old_manifest = state.storage.path(revision["manifest_ref"]).read_bytes()
    damaged.write_bytes(b"synthetic damaged PNG")
    recovered = runner.recover_local(run_id)
    assert recovered["complete"] and recovered["revision_id"] != first["revision_id"]
    assert pixel_hashes(state, recovered) == expected_pixels
    assert not damaged.exists()
    assert state.storage.path(revision["manifest_ref"]).read_bytes() == old_manifest
    assert state.revision(first["revision_id"])["images_retired"]
    assert state.run(run_id)["current_revision"] == recovered["revision_id"]
    assert state.attempts(run_id) == []


def test_contact_shadow_uses_visible_base_and_keeps_product_pixels():
    from campaign_studio.imaging import product_shadow

    with (
        Image.new("RGBA", (100, 120)) as product,
        Image.new("RGBA", (400, 400), "#C0C0C0") as scene,
    ):
        ImageDraw.Draw(product).rectangle((45, 20, 79, 99), fill=(170, 80, 30, 255))
        product.putpixel((0, 110), (170, 80, 30, 1))
        original = product.tobytes()
        product_shadow(scene, product, (120, 140))
        assert scene.getpixel((182, 240))[0] < 192
        assert scene.getpixel((122, 250))[0] == 192
        assert scene.getpixel((182, 280))[0] == 192
        scene.alpha_composite(product, (120, 140))
        assert scene.getpixel((180, 230)) == (170, 80, 30, 255)
        assert product.tobytes() == original
