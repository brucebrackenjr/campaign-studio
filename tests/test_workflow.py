import asyncio
import hashlib
import io
import json
from html.parser import HTMLParser
from uuid import uuid4
from zipfile import ZipFile

import pytest
from PIL import Image, ImageDraw

from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.imaging import load_image, logo_placement, save_srgb
from campaign_studio.planning import canonical_hash, make_plan
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
    prepared = state.plan(run_id).brief.model_dump(mode="json")
    write_draft(
        storage,
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
    state.confirm(run_id, state.plan(run_id).digest)
    return state, Runner(state), run_id, brief


def edit_copy(state, run_id, message):
    from campaign_studio.workspace import Workspace

    value = (
        Workspace(state)
        .current_composition(state.run(run_id)["campaign_id"])["plan"]
        .brief.model_dump()
    )
    value["markets"]["es-MX"]["copy"]["message"] = message
    return Brief.model_validate(value)


def revise(state, runner, run_id, brief):
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.workspace import Workspace

    workspace = Workspace(state)
    projects = AuthoredProjects(workspace)
    current = projects.read(brief.campaign_id)
    projects.save(
        brief.campaign_id,
        AuthoredSave(
            version=current["version"],
            base=current["base"],
            authored=current["authored"] | {"brief": brief.model_dump(mode="json")},
        ),
    )
    return workspace.current_composition(brief.campaign_id)


def delivery(state, cid):
    from campaign_studio.handoff import DeliveryAction, Handoff
    from campaign_studio.workspace import Workspace

    workspace = Workspace(state)
    current = workspace.current_composition(cid)
    with (
        Handoff(state).create_current_zip(
            cid, DeliveryAction(version=current["version"])
        ) as result,
        ZipFile(result.stream) as archive,
    ):
        files = {name: archive.read(name) for name in archive.namelist()}
        manifest_name = next(name for name in files if name.endswith("/manifest.json"))
        manifest = json.loads(files[manifest_name])
        prefix = manifest_name.removesuffix("manifest.json")
        pixels = {}
        for output in manifest["outputs"]:
            if output["file"]:
                with Image.open(io.BytesIO(files[prefix + output["file"]])) as image:
                    pixels[output["id"]] = hashlib.sha256(
                        image.convert("RGB").tobytes()
                    ).hexdigest()
        return manifest, pixels, files, prefix


def test_complete_local_sources_are_idempotent_and_export_after_restart_without_originals(tmp_path):
    state, runner, run_id, original = setup_case(tmp_path)
    for product in original.products:
        state.storage.path(product.source.path).unlink()
    first = asyncio.run(runner.execute(run_id))
    assert first["complete"] and first["metrics"]["completed_images"] == 0
    assert first["sources_ready"] == 2 and state.attempts(run_id) == []
    assert asyncio.run(runner.execute(run_id))["complete"]
    assert state.run(run_id)["current_revision"] is None
    before = delivery(state, original.campaign_id)
    reopened = State(state.storage)
    after = delivery(reopened, original.campaign_id)
    assert len(before[1]) == 12 and before[1] == after[1]
    assert not list(state.storage.root.glob("Campaigns/*/Outputs"))


def test_three_product_us_only_campaign_exports_nine_real_pngs(tmp_path):
    state, runner, run_id, brief = setup_case(tmp_path, 3, True)
    result = asyncio.run(runner.execute(run_id))
    assert result["complete"] and result["outputs"] == []
    manifest, pixels, _, _ = delivery(state, brief.campaign_id)
    assert len(pixels) == 9 and state.attempts(run_id) == []


def test_shared_six_override_three_and_rule_only_metadata_save(tmp_path, monkeypatch):
    state, runner, run_id, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    first = delivery(state, brief.campaign_id)[1]
    revise(state, runner, run_id, edit_copy(state, run_id, "Para cada día."))
    second = delivery(state, brief.campaign_id)[1]
    changed = [key for key in first if first[key] != second[key]]
    assert len(changed) == 6 and all("es-MX" in key for key in changed)
    value = edit_copy(state, run_id, "Para cada día.").model_dump()
    value["products"][0]["copy_overrides"] = {"es-MX": {"message": "Sólo para hoy."}}
    revise(state, runner, run_id, Brief.model_validate(value))
    third = delivery(state, brief.campaign_id)[1]
    assert sum(second[key] != third[key] for key in third) == 3
    value["markets"]["es-MX"]["phrase_rules"] = [{"id": "absent", "phrase": "Never printed"}]
    monkeypatch.setattr(
        "campaign_studio.rendering.compose", lambda *args: pytest.fail("Autosave rendered imagery")
    )
    fourth = revise(state, runner, run_id, Brief.model_validate(value))
    assert all(f.severity == "pass" for f in fourth["plan"].findings if f.rule == "absent")
    assert state.attempts(run_id) == []
    assert fourth["plan"].brief.products[0].copy_overrides["es-MX"].model_fields_set == {"message"}
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0


def test_blocking_copy_is_saved_but_cannot_be_exported_as_partial(tmp_path):
    from campaign_studio.handoff import DeliveryAction, Handoff

    state, runner, run_id, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["en-US"]["copy"]["message"] = "TESTBLOCK"
    current = revise(state, runner, run_id, Brief.model_validate(value))
    assert current["plan"].blocked
    for partial in (False, True):
        with pytest.raises(StateError, match="prohibited|wording"):
            Handoff(state).create_current_zip(
                brief.campaign_id, DeliveryAction(version=current["version"], allow_partial=partial)
            )
    value["markets"]["en-US"]["copy"]["message"] = "Corrected wording"
    revise(state, runner, run_id, Brief.model_validate(value))
    assert len(delivery(state, brief.campaign_id)[1]) == 12
    assert not state.attempts(run_id)


def test_moved_complete_package_has_only_local_files_and_escaped_content(tmp_path):
    state, runner, run_id, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    manifest, pixels, files, prefix = delivery(state, brief.campaign_id)
    moved = tmp_path / "moved-delivery"
    for name, payload in files.items():
        target = moved / name.removeprefix(prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    class Links(HTMLParser):
        references = []

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name in {"src", "href"}:
                    self.references.append(value)

    parser = Links()
    parser.feed((moved / "index.html").read_text())
    assert len(pixels) == 12
    assert all(
        f'id="{item[1:]}"' in (moved / "index.html").read_text()
        if item.startswith("#")
        else ":" not in item and (moved / item).is_file()
        for item in parser.references
    )
    serialized = json.dumps(manifest)
    assert all(
        secret not in serialized
        for secret in ("creative_direction", "response.json", "private/", "api_key")
    )
    assert manifest["complete"] is True


def test_exported_pixel_checks_detect_removed_logo_and_wrong_text_color(tmp_path):
    state, runner, run_id, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    manifest, _, files, prefix = delivery(state, brief.campaign_id)
    plan = state.plan(run_id)
    path = tmp_path / "damaged-export.png"
    path.write_bytes(files[prefix + manifest["outputs"][0]["file"]])
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
    assert delivery(state, brief.campaign_id)[0]["complete"]


def test_stale_tab_save_is_rejected_without_changing_current_instructions(tmp_path):
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.workspace import Workspace

    state, runner, run_id, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    projects = AuthoredProjects(Workspace(state))
    old = projects.read(brief.campaign_id)
    current = revise(state, runner, run_id, edit_copy(state, run_id, "Para cada día."))
    with pytest.raises(ValueError, match="changed|saved"):
        projects.save(
            brief.campaign_id,
            AuthoredSave(version=old["version"], base=old["base"], authored=old["authored"]),
        )
    assert Workspace(state).current_composition(brief.campaign_id)["version"] == current["version"]


def test_repeated_exports_recreate_finished_pixels_without_retained_output_history(tmp_path):
    state, runner, run_id, brief = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    first = delivery(state, brief.campaign_id)
    second = delivery(state, brief.campaign_id)
    assert first[1] == second[1]
    assert not list(state.storage.root.glob("Campaigns/*/Outputs"))
    assert not list(state.storage.root.glob("Campaigns/*/Exports"))
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM package_exports").fetchone()[0] == 0
    assert not state.attempts(run_id)


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
