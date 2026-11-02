"""Field-scoped local correction and authoritative independent text geometry."""

import asyncio
import io

import pytest
from PIL import Image, ImageChops
from pydantic import ValidationError
from test_workflow import setup_case

from campaign_studio.brief import FORMATS, Brief, Copy, Framing, TextPlacement
from campaign_studio.imaging import (
    ImageProblem,
    compose,
    save_srgb,
    text_block_overlay,
    text_blocks,
    text_layout,
    wording_overlay,
)
from campaign_studio.planning import Output, make_plan
from campaign_studio.previews import DraftPreviews
from campaign_studio.rendering import inspect_png
from campaign_studio.state import StateError
from campaign_studio.workspace import ActionError, TextEdit, Workspace


def edit(state, run_id, **changes):
    return TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=state.effective_revision(run_id),
        market="en-US",
        product_id="product-1",
        **changes,
    )


def test_mixed_field_scopes_preserve_other_values_and_exact_preview_pixels(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    cid = first["campaign_id"]
    # Establish independent product CTA values using the same existing service.
    for product, cta in [("product-1", "First CTA"), ("product-2", "Second CTA")]:
        command = edit(state, run_id, values={"cta": cta}, field_scopes={"cta": "product"})
        command.product_id = product
        brief, plan, _ = ws.text(cid, command)
        first = runner.revise(run_id, brief, command.revision_id, plan.digest)
    old_hashes = {o["id"]: o["sha256"] for o in first["outputs"]}
    command = edit(
        state,
        run_id,
        values={"message": "One shared headline", "cta": "Selected CTA"},
        field_scopes={"message": "all_products", "cta": "product"},
    )
    brief, plan, impact = ws.text(cid, command)
    assert len(impact["render_outputs"]) == 6
    assert impact["generation_products"] == []
    assert brief.effective_copy(brief.products[0], "en-US").cta == "Selected CTA"
    assert brief.effective_copy(brief.products[1], "en-US").cta == "Second CTA"
    assert all(
        brief.effective_copy(p, "en-US").message == "One shared headline" for p in brief.products
    )
    previews = DraftPreviews(state)
    previews.render(cid, command, plan, impact, text_preview=True)
    images = next(iter(previews.entries.values()))["images"]
    saved = runner.revise(run_id, brief, command.revision_id, plan.digest)
    prefix = state.revision(saved["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    for output in saved["outputs"]:
        if output["market"] == "es-MX":
            assert output["sha256"] == old_hashes[output["id"]]
        elif output["id"] in images:
            with (
                Image.open(io.BytesIO(images[output["id"]])) as preview,
                Image.open(state.storage.path(f"{prefix}/{output['file']}")) as actual,
            ):
                assert (
                    ImageChops.difference(preview.convert("RGB"), actual.convert("RGB")).getbbox()
                    is None
                )
    assert state.attempts(run_id) == []


def test_checked_current_value_propagates_without_fake_typing_and_then_is_noop(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    initial = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    cid = initial["campaign_id"]
    command = edit(
        state,
        run_id,
        values={"message": "Visible product wording"},
        field_scopes={"message": "product"},
    )
    brief, plan, _ = ws.text(cid, command)
    runner.revise(run_id, brief, command.revision_id, plan.digest)
    command = edit(
        state,
        run_id,
        values={"message": "Visible product wording"},
        field_scopes={"message": "all_products"},
    )
    brief, plan, impact = ws.text(cid, command)
    assert all("product-2/" in output for output in impact["render_outputs"])
    assert len(impact["render_outputs"]) == 3
    assert all(
        "message" not in p.copy_overrides.get("en-US", {}).model_dump()
        if "en-US" in p.copy_overrides
        else True
        for p in brief.products
    )
    runner.revise(run_id, brief, command.revision_id, plan.digest)
    command.revision_id = state.effective_revision(run_id)
    with pytest.raises(ActionError, match="No changes"):
        ws.text(cid, command)
    assert state.attempts(run_id) == []


def test_all_products_missing_input_blocks_whole_apply_but_selected_edit_remains_safe(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    old = state.plan(run_id)
    state.storage.path(old.assets["products/product-2"].reference).unlink()
    command = edit(
        state, run_id, values={"cta": "Changed CTA"}, field_scopes={"cta": "all_products"}
    )
    with pytest.raises((ActionError, StateError)):
        ws.text(first["campaign_id"], command)
    assert state.effective_revision(run_id) == first["revision_id"]
    command.field_scopes = {"cta": "product"}
    _, _, impact = ws.text(first["campaign_id"], command)
    assert len(impact["render_outputs"]) == 3
    assert all(o.startswith("product-1/en-US/") for o in impact["render_outputs"])
    assert state.attempts(run_id) == []


def test_initial_all_products_equal_overrides_are_not_normalized_by_apply(tmp_path):
    state, runner, _, original = setup_case(tmp_path)
    value = original.model_dump()
    headline = value["markets"]["en-US"]["copy"]["message"]
    for product in value["products"]:
        product["copy_overrides"]["en-US"] = {"message": headline}
    plan = make_plan(Brief.model_validate(value), state.storage)
    run_id = state.create(plan, 0)
    state.confirm(run_id, state.plan(run_id).digest)
    saved = asyncio.run(runner.execute(run_id))
    before = state.plan(run_id).brief.model_dump()
    command = edit(
        state, run_id, values={"message": headline}, field_scopes={"message": "all_products"}
    )
    with pytest.raises(ActionError, match="No changes"):
        Workspace(state).text(saved["campaign_id"], command)
    assert state.plan(run_id).brief.model_dump() == before
    assert state.effective_revision(run_id) == saved["revision_id"]
    assert len(Workspace(state).revisions(run_id)) == 1
    assert state.attempts(run_id) == []


@pytest.mark.parametrize("ratio", list(FORMATS))
def test_text_position_uses_actual_glyphs_not_invisible_column_and_stays_exact(ratio):
    copy = Copy(message="Go", cta="Explore")
    framing = Framing(
        text_placements={
            "message": TextPlacement(left=0.8, top=0.6, width=0.88, font_size=60),
        }
    )
    blocks = text_blocks(copy, ratio, framing=framing)
    message = next(b for b in blocks if b["id"] == "message")
    assert message["bounds"][0] >= round(FORMATS[ratio][0] * 0.8)
    assert message["bounds"][2] <= FORMATS[ratio][0]
    assert message["placement"]["left"] + message["placement"]["width"] > 1
    assert next(b for b in blocks if b["id"] == "cta")["lines"] == [
        line for line in text_layout(copy, ratio) if line.text == "Explore"
    ]
    with (
        Image.new("RGB", (1920, 1920), "#123456") as scene,
        Image.new("RGBA", (200, 80), "red") as logo,
    ):
        result = compose(scene, logo, copy, ratio, "#FFFFFF", framing=framing)
        assert result.size == FORMATS[ratio]
        result.close()
    framing.text_placements["message"].left = 0.99
    with pytest.raises(ImageProblem, match="Headline extends beyond"):
        text_layout(copy, ratio, framing=framing)
    assert (
        text_blocks(copy, ratio, framing=framing, validate=False)[0]["bounds"][2]
        > FORMATS[ratio][0]
    )


@pytest.mark.parametrize("ratio", list(FORMATS))
def test_packaged_families_and_overlapping_palette_colors_use_final_visible_pixels(tmp_path, ratio):
    text = Copy(message="Para cada día.", cta="Explora")
    framing = Framing(
        text_placements={
            "message": TextPlacement(
                left=0.1,
                top=0.2,
                width=0.8,
                font_size=90,
                font_id="barlow_condensed",
                color="#FFDD00",
            ),
            "cta": TextPlacement(left=0.12, top=0.22, width=0.8, font_size=60, color="#FFFFFF"),
        }
    )
    width, height = FORMATS[ratio]
    output = Output(
        id="p/es-MX/" + ratio.replace(":", "x"),
        product_id="p",
        market="es-MX",
        ratio=ratio,
        width=width,
        height=height,
        copy=text,
        framing=framing,
    )
    with (
        Image.new("RGB", (1920, 1920), "#123456") as scene,
        Image.new("RGBA", (200, 80), "red") as logo,
    ):
        result = compose(scene, logo, text, ratio, "#FFFFFF", framing=framing)
        target = tmp_path / "result.png"
        save_srgb(result, target)
        assert all(f.severity == "pass" for f in inspect_png(target, output, logo, "#FFFFFF"))
        overlay = wording_overlay(text, ratio, "#FFFFFF", framing=framing)
        bounds = overlay.getbbox()
        x, y = next(
            (x, y)
            for y in range(bounds[1], bounds[3])
            for x in range(bounds[0], bounds[2])
            if overlay.getpixel((x, y))[3] == 255
        )
        result.putpixel((x, y), (0, 0, 0))
        save_srgb(result, target)
        assert (
            next(
                f
                for f in inspect_png(target, output, logo, "#FFFFFF")
                if f.rule == "controlled_text_color"
            ).severity
            == "blocking"
        )
        result.close()
        overlay.close()


def test_text_style_validation_and_selected_size_are_authoritative(tmp_path):
    _, _, _, brief = setup_case(tmp_path)
    value = brief.model_dump(mode="json")
    placement = dict(
        left=0.1, top=0.5, width=0.8, font_size=30, font_id="barlow_condensed", color="#123456"
    )
    value["products"][0]["framing"] = {
        "en-US": {"1:1": {"text_placements": {"message": placement}}}
    }
    with pytest.raises(ValidationError, match="controlled palette"):
        Brief.model_validate(value)
    value["brand"]["palette"].append("#123456")
    assert (
        Brief.model_validate(value)
        .products[0]
        .framing["en-US"]["1:1"]
        .text_placements["message"]
        .font_id
        == "barlow_condensed"
    )
    placement["font_id"] = "system-serif"
    with pytest.raises(ValidationError):
        Brief.model_validate(value)
    # The arranged size can fit a word that the unused default size cannot.
    text = Copy(message="Longword" * 12)
    framing = Framing(
        text_placements={"message": TextPlacement(left=0.1, top=0.2, width=0.8, font_size=12)}
    )
    assert text_layout(text, "1:1", framing=framing)
    with pytest.raises(ImageProblem):
        text_layout(text, "1:1")


def test_text_http_preview_apply_and_blocked_geometry_preserve_other_outputs(tmp_path):
    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    campaign = first["campaign_id"]
    app = create_app(
        Settings(data_dir=state.storage.root),
        provider_factory=lambda: pytest.fail("Text edits must not construct a provider"),
        key_available=lambda: False,
    )
    path = f"/api/campaigns/{campaign}/text"
    body = {
        "run_id": run_id,
        "revision_id": first["revision_id"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
        "placements": {},
    }
    hashes = {o["id"]: o["sha256"] for o in first["outputs"]}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        initial = client.post(path + "/preview", json=body)
        assert initial.status_code == 200, initial.text
        initial = initial.json()
        assert not initial["blocked"] and len(initial["images"]) == 1
        layers = initial["images"][0]["layers"]
        assert set(layers["text_blocks"]) == {"message", "cta"}
        for url in [
            layers["base_url"],
            *(block["url"] for block in layers["text_blocks"].values()),
        ]:
            assert client.get(url).status_code == 200
        body["placements"] = {
            "message": dict(left=0.1, top=0.4, width=0.8, font_size=42, font_id="barlow_condensed"),
            "cta": dict(left=0.11, top=0.41, width=0.8, font_size=38),
        }
        proposed = client.post(path + "/preview", json=body)
        assert proposed.status_code == 200, proposed.text
        preview = proposed.json()
        assert preview["impact"]["render_outputs"] == ["product-1/en-US/1x1"]
        image = client.get(preview["images"][0]["url"]).content
        layers = preview["images"][0]["layers"]
        with Image.open(io.BytesIO(client.get(layers["base_url"]).content)) as base:
            reconstructed = base.convert("RGBA")
        for block in layers["text_blocks"].values():
            with Image.open(io.BytesIO(client.get(block["url"]).content)) as layer:
                reconstructed.alpha_composite(layer.convert("RGBA"))
        with Image.open(io.BytesIO(image)) as final:
            assert (
                ImageChops.difference(reconstructed.convert("RGB"), final.convert("RGB")).getbbox()
                is None
            )
        reconstructed.close()
        applied = client.post(path + "/apply", json=body | {"preview_digest": preview["digest"]})
        assert applied.status_code == 200, applied.text
        current = client.get(f"/api/motif/campaigns/{campaign}").json()
        assert current["manifest"]["edit_receipt"] == {
            "kind": "text",
            "base_revision": body["revision_id"],
            "preview_digest": preview["digest"],
        }
        for output in current["manifest"]["outputs"]:
            if output["id"] == "product-1/en-US/1x1":
                with (
                    Image.open(io.BytesIO(image)) as wanted,
                    Image.open(io.BytesIO(client.get(output["url"]).content)) as saved,
                ):
                    assert (
                        ImageChops.difference(wanted.convert("RGB"), saved.convert("RGB")).getbbox()
                        is None
                    )
            else:
                assert output["sha256"] == hashes[output["id"]]
        # Retrying the old explicit Apply cannot create a duplicate image version.
        assert (
            client.post(
                path + "/apply", json=body | {"preview_digest": preview["digest"]}
            ).status_code
            == 409
        )
        body["revision_id"] = current["manifest"]["revision_id"]
        body["placements"]["message"]["left"] = 0.99
        blocked = client.post(path + "/preview", json=body).json()
        assert blocked["blocked"] and not blocked["images"]
        assert blocked["geometry"]["text_blocks"]["message"]["bounds"][2] > 1080
        refused = client.post(path + "/apply", json=body | {"preview_digest": blocked["digest"]})
        assert refused.status_code == 422
        assert state.effective_revision(run_id) == body["revision_id"]
        assert state.revision_count(run_id) == 2
        assert state.attempts(run_id) == []


def test_text_reset_removes_persisted_overrides_without_changing_other_composition(tmp_path):
    from campaign_studio.workspace import TextEdit

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    cid = first["campaign_id"]
    command = TextEdit(
        run_id=run_id,
        revision_id=first["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        placements={"message": None, "cta": None},
    )
    with pytest.raises(ActionError, match="No changes"):
        ws.text(cid, command)
    initial = state.plan(run_id)
    command.placements = {"message": TextPlacement(left=0.1, top=0.4, width=0.8, font_size=40)}
    brief, plan, _ = ws.text(cid, command)
    custom = runner.revise(run_id, brief, command.revision_id, plan.digest, text_edit=command)
    command.revision_id = custom["revision_id"]
    command.placements = {"message": None, "cta": None}
    brief, plan, impact = ws.text(cid, command)
    assert impact["render_outputs"] == [command.output_id]
    before = next(o for o in initial.outputs if o.id == command.output_id)
    after = next(o for o in plan.outputs if o.id == command.output_id)
    assert after.framing == before.framing and after.text == before.text
    reset = runner.revise(run_id, brief, command.revision_id, plan.digest, text_edit=command)
    assert {o["id"]: o["pixel_sha256"] for o in reset["outputs"]} == {
        o["id"]: o["pixel_sha256"] for o in first["outputs"]
    }
    command.revision_id = reset["revision_id"]
    with pytest.raises(ActionError, match="No changes"):
        ws.text(cid, command)
    assert state.revision_count(run_id) == 3 and state.attempts(run_id) == []


def test_font_size_is_canvas_limited_with_only_renderer_representation_ceiling(monkeypatch):
    from campaign_studio.validation import validation_issues

    placement = TextPlacement(left=0, top=0, width=1, font_size=2400)
    framing = Framing(text_placements={"message": placement})
    block = text_blocks(Copy(message="i"), "9:16", framing=framing)[0]
    assert block["bounds"][2] < 1080 and block["bounds"][3] < 1920
    monkeypatch.setattr(
        "campaign_studio.imaging.ImageDraw.Draw",
        lambda *a: pytest.fail("Out-of-canvas text must not rasterize"),
    )
    placement.font_size = 10000
    with pytest.raises(ImageProblem):
        wording_overlay(Copy(message="i"), "9:16", "#FFFFFF", framing=framing)
    with pytest.raises(ValidationError) as invalid:
        from campaign_studio.workspace import TextEdit

        TextEdit(
            run_id="a" * 32,
            revision_id="b" * 32,
            product_id="p",
            market="en-US",
            ratio="1:1",
            placements={"message": dict(left=0, top=0, width=1, font_size=65536)},
        )
    issue = validation_issues(invalid.value)[0]
    assert issue["label"] == "Text size" and issue["code"] == "text_size"


def test_each_text_reference_respects_later_blocks_and_translucent_layer_edges(tmp_path):
    text = Copy(message="Go", cta="Go")
    placement = dict(left=0.1, top=0.2, width=0.8, font_size=180)
    framing = Framing(
        text_placements={
            "message": TextPlacement(**placement, color="#FFDD00"),
            "cta": TextPlacement(**placement, color="#FFFFFF"),
        }
    )
    output = Output(
        id="p/en-US/1x1",
        product_id="p",
        market="en-US",
        ratio="1:1",
        width=1080,
        height=1080,
        copy=text,
        framing=framing,
    )
    with (
        Image.new("RGB", (1400, 1400), "#123456") as scene,
        Image.new("RGBA", (200, 80), "red") as logo,
    ):
        result = compose(scene, logo, text, "1:1", "#FFFFFF", framing=framing)
        target = tmp_path / "overlap.png"
        save_srgb(result, target)
        checks = {
            f.context.text_block: f
            for f in inspect_png(target, output, logo, "#FFFFFF")
            if f.rule == "controlled_text_color"
        }
        assert "message" not in checks
        assert checks["cta"].severity == "pass"
        blocks = text_blocks(text, "1:1", framing=framing)
        with text_block_overlay(blocks[0]["lines"], (1080, 1080), "#FFFFFF") as layer:
            assert any(layer.getchannel("A").histogram()[1:255])
        result.close()


def test_combined_text_applies_scoped_words_and_one_format_geometry_atomically(tmp_path):
    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    campaign = first["campaign_id"]
    before = state.plan(run_id)
    hashes = {o["id"]: o["pixel_sha256"] for o in first["outputs"]}
    app = create_app(
        Settings(data_dir=state.storage.root),
        provider_factory=lambda: pytest.fail("Combined text must remain local"),
        key_available=lambda: False,
    )
    path = f"/api/campaigns/{campaign}/text"
    body = {
        "run_id": run_id,
        "revision_id": first["revision_id"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
        "values": {"message": "Fresh shared words", "cta": "Try this one"},
        "field_scopes": {"message": "all_products", "cta": "product"},
        "placements": {
            "message": {
                "left": 0.1,
                "top": 0.35,
                "width": 0.8,
                "font_size": 48,
                "font_id": "barlow_condensed",
            }
        },
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(path + "/preview", json=body)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert not preview["blocked"] and len(preview["impact"]["render_outputs"]) == 6
        image = client.get(preview["images"][0]["url"]).content
        response = client.post(path + "/apply", json=body | {"preview_digest": preview["digest"]})
        assert response.status_code == 200, response.text
        current = client.get(f"/api/motif/campaigns/{campaign}").json()
        assert state.revision_count(run_id) == 2
        after = state.plan(run_id)
        for original, changed in zip(before.outputs, after.outputs, strict=True):
            if changed.id == "product-1/en-US/1x1":
                assert changed.framing.text_placements["message"].font_size == 48
            else:
                assert changed.framing == original.framing
            if changed.market == "en-US":
                assert changed.text.message == "Fresh shared words"
                assert changed.text.cta == (
                    "Try this one" if changed.product_id == "product-1" else original.text.cta
                )
        for output in current["manifest"]["outputs"]:
            if output["market"] == "es-MX":
                assert output["pixel_sha256"] == hashes[output["id"]]
            if output["id"] == "product-1/en-US/1x1":
                with (
                    Image.open(io.BytesIO(image)) as wanted,
                    Image.open(io.BytesIO(client.get(output["url"]).content)) as saved,
                ):
                    assert (
                        ImageChops.difference(wanted.convert("RGB"), saved.convert("RGB")).getbbox()
                        is None
                    )
        assert state.attempts(run_id) == []


def test_combined_text_adds_and_clears_optional_cta_with_candidate_geometry(tmp_path):
    from campaign_studio.workspace import TextEdit

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    command = TextEdit(
        run_id=run_id,
        revision_id=first["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        values={"cta": None},
        field_scopes={"cta": "product"},
    )
    brief, plan, _ = ws.text(first["campaign_id"], command)
    cleared = runner.revise(run_id, brief, command.revision_id, plan.digest, text_edit=command)
    command.revision_id = cleared["revision_id"]
    command.values = {"cta": "New optional CTA"}
    command.placements = {"cta": TextPlacement(left=0.15, top=0.7, width=0.7, font_size=36)}
    brief, plan, impact = ws.text(first["campaign_id"], command)
    assert len(impact["render_outputs"]) == 3
    added = runner.revise(run_id, brief, command.revision_id, plan.digest, text_edit=command)
    output = next(o for o in state.plan(run_id).outputs if o.id == command.output_id)
    accepted = output.framing.text_placements["cta"]
    assert output.text.cta == "New optional CTA" and accepted.top == 0.7
    command.revision_id = added["revision_id"]
    command.values = {"cta": None}
    # The pending gesture is obsolete when its optional wording is cleared.
    command.placements = {"cta": TextPlacement(left=0.99, top=0.99, width=1, font_size=100)}
    brief, plan, _ = ws.text(first["campaign_id"], command)
    output = next(o for o in plan.outputs if o.id == command.output_id)
    assert output.text.cta is None and output.framing.text_placements["cta"] == accepted
    assert not [f for f in plan.findings if f.severity == "blocking"]
    assert state.attempts(run_id) == []


def test_combined_text_checks_sources_for_all_wording_targets(tmp_path):
    from campaign_studio.workspace import TextEdit

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    state.storage.path(state.plan(run_id).assets["products/product-2"].reference).unlink()
    command = TextEdit(
        run_id=run_id,
        revision_id=first["revision_id"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        values={"message": "Shared change"},
        field_scopes={"message": "all_products"},
    )
    with pytest.raises((ActionError, StateError)):
        Workspace(state).text(first["campaign_id"], command)
    assert state.effective_revision(run_id) == first["revision_id"]
    command.field_scopes = {"message": "product"}
    _, _, impact = Workspace(state).text(first["campaign_id"], command)
    assert len(impact["render_outputs"]) == 3


def test_combined_text_refuses_another_affected_format_that_cannot_fit(tmp_path):
    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    state, runner, _, original = setup_case(tmp_path)
    value = original.model_dump()
    value["markets"]["en-US"]["copy"]["message"] = "Go"
    value["products"][1]["framing"] = {
        "en-US": {
            "16:9": {
                "text_placements": {
                    "message": {"left": 0.1, "top": 0.1, "width": 0.8, "font_size": 500}
                }
            }
        }
    }
    plan = make_plan(Brief.model_validate(value), state.storage)
    run_id = state.create(plan, 0)
    state.confirm(run_id, state.plan(run_id).digest)
    first = asyncio.run(runner.execute(run_id))
    assert first["complete"]
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Text remains local"),
    )
    body = {
        "run_id": run_id,
        "revision_id": first["revision_id"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
        "values": {"message": "Fresh shared words for everyone"},
        "field_scopes": {"message": "all_products"},
        "placements": {},
    }
    path = f"/api/campaigns/{first['campaign_id']}/text"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(path + "/preview", json=body)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["blocked"] and not preview["images"]
        assert any(f["item"] == "product-2/en-US/16x9" for f in preview["findings"])
        response = client.post(path + "/apply", json=body | {"preview_digest": preview["digest"]})
        assert response.status_code == 422
        assert state.effective_revision(run_id) == first["revision_id"]
        assert state.revision_count(run_id) == 1 and state.attempts(run_id) == []


def test_combined_text_corrects_rule_withholding_without_recovering_other_omissions(tmp_path):
    from test_rule_decisions import prepare, recheck

    with prepare(tmp_path) as (app, client, run_id, first, url):
        response, _ = recheck(client, url, "LOCAL CONTROL")
        assert response.status_code == 200, response.text
        shown = client.get(url).json()
        assert len(shown["rule_withheld_outputs"]) == 3
        saved = {o["id"]: o["sha256"] for o in shown["manifest"]["outputs"] if o["file"]}
        path = f"/api/campaigns/{first['campaign_id']}/text"
        body = {
            "run_id": run_id,
            "revision_id": shown["manifest"]["revision_id"],
            "product_id": "product-1",
            "market": "en-US",
            "ratio": "1:1",
            "values": {},
            "field_scopes": {},
            "placements": {},
        }
        # The actual offending wording remains inspectable, but cannot be applied.
        initial = client.post(path + "/preview", json=body)
        assert initial.status_code == 200, initial.text
        assert initial.json()["blocked"] and initial.json()["images"] == []
        body.update(
            values={"message": "Corrected local words"}, field_scopes={"message": "product"}
        )
        preview = client.post(path + "/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert not preview.json()["blocked"]
        applied = client.post(
            path + "/apply", json=body | {"preview_digest": preview.json()["digest"]}
        )
        assert applied.status_code == 200, applied.text
        current = client.get(url).json()
        assert current["manifest"]["complete"] and current["wording_issue"] is None
        assert all(
            o["sha256"] == saved[o["id"]]
            for o in current["manifest"]["outputs"]
            if o["id"] in saved
        )
        assert app.state.workspace.state.attempts(run_id) == []

    # A genuine first-production omission remains unavailable to the Text editor.
    from campaign_studio.workspace import TextEdit

    with prepare(tmp_path / "failed", initial_partial=True) as (app, client, run_id, first, url):
        command = TextEdit(
            run_id=run_id,
            revision_id=first["revision_id"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            values={"message": "Corrected local words"},
            field_scopes={"message": "product"},
        )
        with pytest.raises(ActionError, match="available saved image"):
            app.state.workspace.text(first["campaign_id"], command)
        assert app.state.workspace.state.effective_revision(run_id) == first["revision_id"]


def test_text_findings_cover_selected_or_changed_outputs_while_export_blocks_campaign(tmp_path):
    import copy

    from test_rule_decisions import package, prepare

    with prepare(tmp_path) as (app, client, run_id, first, url):
        current = client.get(url).json()
        value = copy.deepcopy(current["value"])
        other_phrase = value["markets"]["en-US"]["copy"]["message"].split()[0]
        assert other_phrase.casefold() not in "local control"
        value["markets"]["en-US"]["phrase_rules"] = [
            {"id": "selected-rule", "phrase": "LOCAL CONTROL"},
            {"id": "other-rule", "phrase": other_phrase},
        ]
        checked = client.post(
            url + "/recheck",
            json={
                "value": value,
                "version": current["version"],
                "base_revision": current["base_revision"],
                "run_id": current["run"]["id"],
            },
        )
        assert checked.status_code == 200, checked.text
        shown = client.get(url).json()
        assert len(shown["rule_withheld_outputs"]) == 6
        path = f"/api/campaigns/{first['campaign_id']}/text"
        body = {
            "run_id": run_id,
            "revision_id": shown["manifest"]["revision_id"],
            "product_id": "product-1",
            "market": "en-US",
            "ratio": "1:1",
            "values": {},
            "field_scopes": {},
            "placements": {},
        }
        initial = client.post(path + "/preview", json=body)
        assert initial.status_code == 200, initial.text
        assert initial.json()["blocked"]
        assert {f["item"] for f in initial.json()["findings"]} == {"product-1/en-US/1x1"}
        shared = client.post(
            path + "/preview",
            json=body
            | {
                "values": {"message": "LOCAL CONTROL"},
                "field_scopes": {"message": "all_products"},
            },
        )
        assert shared.status_code == 200, shared.text
        assert {f["item"].split("/")[0] for f in shared.json()["findings"]} == {
            "product-1",
            "product-2",
        }
        body.update(
            values={"message": "Corrected local words"}, field_scopes={"message": "product"}
        )
        preview = client.post(path + "/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert not preview.json()["blocked"] and preview.json()["findings"] == []
        applied = client.post(
            path + "/apply", json=body | {"preview_digest": preview.json()["digest"]}
        )
        assert applied.status_code == 200, applied.text
        current = client.get(url).json()
        assert len(current["rule_withheld_outputs"]) == 3
        assert package(client, first["campaign_id"], run_id, current).status_code == 409
        assert app.state.workspace.state.attempts(run_id) == []
        for retired in ("preview", "apply"):
            assert (
                client.post(
                    f"/api/campaigns/{first['campaign_id']}/{retired}", json=body
                ).status_code
                == 404
            )


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("w" * 58, "An unbroken copy segment is too wide"),
        ("Line\n" * 12, "Supplied copy exceeds the current layout"),
    ],
)
def test_default_text_measurement_preserves_invalid_geometry_without_admitting_it(message, error):
    wording = Copy(message=message)
    with pytest.raises(ImageProblem, match=error):
        text_blocks(wording, "1:1")
    measured = text_blocks(wording, "1:1", validate=False)
    assert measured[0]["text"] == message
    assert measured[0]["bounds"][2] > 1080 or measured[0]["bounds"][3] > 1080 * 0.36
    assert text_blocks(Copy(message="Stay."), "1:1") == text_blocks(
        Copy(message="Stay."), "1:1", validate=False
    )


def test_reset_default_text_http_reports_fit_findings_and_preserves_saved_images(tmp_path):
    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    campaign = first["campaign_id"]
    prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    saved = {
        output["file"]: state.storage.path(f"{prefix}/{output['file']}").read_bytes()
        for output in first["outputs"]
    }
    app = create_app(
        Settings(data_dir=state.storage.root),
        provider_factory=lambda: pytest.fail("Text measurement cannot request a provider"),
        key_available=lambda: False,
    )
    path = f"/api/campaigns/{campaign}/text"
    body = {
        "run_id": run_id,
        "revision_id": first["revision_id"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
        "placements": {"message": None, "cta": None},
        "field_scopes": {"message": "product"},
    }
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        for message, evidence in [
            ("w" * 58, "An unbroken copy segment is too wide for the output."),
            (
                "Line\n" * 12,
                "Supplied copy exceeds the current layout; shorten it or revise the layout.",
            ),
        ]:
            body["values"] = {"message": message}
            response = client.post(path + "/preview", json=body)
            assert response.status_code == 200, response.text
            preview = response.json()
            assert preview["blocked"] and not preview["images"]
            assert preview["geometry"]["text_blocks"]["message"]["bounds"]
            assert any(
                finding["rule"] == "text_fit" and finding["evidence"] == evidence
                for finding in preview["findings"]
            )
            assert (
                client.post(
                    path + "/apply", json=body | {"preview_digest": preview["digest"]}
                ).status_code
                == 422
            )
        body["values"] = {"message": "Stay."}
        response = client.post(path + "/preview", json=body)
        assert response.status_code == 200, response.text
        assert not response.json()["blocked"] and response.json()["images"]
    assert state.effective_revision(run_id) == first["revision_id"]
    assert state.revision_count(run_id) == 1
    assert state.attempts(run_id) == []
    assert all(
        state.storage.path(f"{prefix}/{name}").read_bytes() == data for name, data in saved.items()
    )


@pytest.mark.parametrize("wording", [False, True])
def test_apply_reuses_only_verified_unchanged_pixel_checks(tmp_path, monkeypatch, wording):
    import campaign_studio.rendering as rendering

    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    command = edit(
        state,
        run_id,
        **(
            {
                "values": {"message": "Fresh shared wording"},
                "field_scopes": {"message": "all_products"},
            }
            if wording
            else {
                "placements": {"message": TextPlacement(left=0.1, top=0.1, width=0.7, font_size=48)}
            }
        ),
    )
    brief, plan, impact = Workspace(state).text(first["campaign_id"], command)
    inspected = []
    original = rendering.inspect_png

    def inspect(path, output, *args):
        inspected.append(output.id)
        return original(path, output, *args)

    monkeypatch.setattr(rendering, "inspect_png", inspect)
    saved = runner.revise(run_id, brief, command.revision_id, plan.digest, text_edit=command)
    assert saved["complete"]
    assert set(inspected) == set(impact["render_outputs"])
    assert len(inspected) == (6 if wording else 1)
    old = {o["id"]: o for o in first["outputs"]}
    proof_rules = {"image_integrity", "logo_pixels", "controlled_text_color"}
    for output in saved["outputs"]:
        if output["id"] not in inspected:
            assert output["sha256"] == old[output["id"]]["sha256"]
            assert output["pixel_sha256"] == old[output["id"]]["pixel_sha256"]
            before = [
                f
                for f in first["findings"]
                if f["item"] == output["id"] and f["rule"] in proof_rules
            ]
            after = [
                f
                for f in saved["findings"]
                if f["item"] == output["id"] and f["rule"] in proof_rules
            ]
            assert before == after
    assert state.attempts(run_id) == []


def test_apply_refuses_corrupted_copy_of_unchanged_output(tmp_path, monkeypatch):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    command = edit(
        state,
        run_id,
        placements={"message": TextPlacement(left=0.1, top=0.1, width=0.7, font_size=48)},
    )
    brief, plan, _ = Workspace(state).text(first["campaign_id"], command)
    original = state.storage.write
    corrupted = []

    def write(reference, payload):
        original(reference, payload)
        if reference.endswith("images/product-2/1x1/en-US.png"):
            state.storage.path(reference).write_bytes(b"corrupted copied output")
            corrupted.append(reference)

    monkeypatch.setattr(state.storage, "write", write)
    with pytest.raises(StateError, match="Could not apply"):
        runner.revise(run_id, brief, command.revision_id, plan.digest, text_edit=command)
    assert corrupted and state.effective_revision(run_id) == first["revision_id"]
    prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    import hashlib

    assert all(
        hashlib.sha256(state.storage.path(f"{prefix}/{o['file']}").read_bytes()).hexdigest()
        == o["sha256"]
        for o in first["outputs"]
    )
