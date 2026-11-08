"""Field-scoped local correction and authoritative independent text geometry."""

import io

import pytest
from PIL import Image
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
from campaign_studio.planning import Output
from campaign_studio.rendering import inspect_png
from campaign_studio.state import StateError
from campaign_studio.workspace import ActionError, TextEdit, Workspace


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


# Autosave replaces accepted image revisions. The following cases exercise the
# equivalent editing, scoping, integrity, and final-delivery boundaries directly.
@pytest.fixture
def current_editor(tmp_path):
    state, _, run, brief = setup_case(tmp_path)
    return state, Workspace(state), brief.campaign_id, run


def current_edit(state, cid, **changes):
    from campaign_studio.campaign_drafts import read

    return TextEdit(
        revision_id=read(state.storage, cid)["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        **changes,
    )


def test_scoped_words_and_selected_geometry_save_atomically_and_export_exactly(current_editor):
    import hashlib

    from test_workflow import delivery

    from campaign_studio.compositions import preview_image

    state, ws, cid, run = current_editor
    for product, cta in [("product-1", "First CTA"), ("product-2", "Second CTA")]:
        edit = current_edit(state, cid, values={"cta": cta}, field_scopes={"cta": "product"})
        edit.product_id = product
        ws.save_composition(cid, edit)
    before = ws.current_composition(cid)
    command = current_edit(
        state,
        cid,
        values={"message": "One shared headline", "cta": "Selected CTA"},
        field_scopes={"message": "all_products", "cta": "product"},
        placements={
            "message": TextPlacement(left=0.1, top=0.12, width=0.7, font_size=48, color="#FFFFFF")
        },
    )
    version = ws.save_composition(cid, command)
    assert ws.save_composition(cid, command) == version
    current = ws.current_composition(cid)
    for output in current["plan"].outputs:
        if output.market == "en-US":
            assert output.text.message == "One shared headline"
            assert output.text.cta == (
                "Selected CTA" if output.product_id == "product-1" else "Second CTA"
            )
        else:
            assert output.text == next(o.text for o in before["plan"].outputs if o.id == output.id)
        if output.id == "product-1/en-US/1x1":
            assert output.framing.text_placements["message"].font_size == 48
        else:
            assert output.framing == next(
                o.framing for o in before["plan"].outputs if o.id == output.id
            )
    preview = preview_image(ws, cid, version, "product-1", "en-US", "1:1")
    with Image.open(io.BytesIO(preview)) as image:
        expected = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
    manifest, pixels, _, _ = delivery(state, cid)
    assert pixels["product-1/en-US/1x1"] == expected
    assert manifest["complete"] and len(pixels) == 12
    assert state.attempts(run) == [] and state.revision_count(run) == 0
    assert not list(state.storage.root.glob("Campaigns/*/Outputs"))


def test_propagating_existing_words_changes_other_products_and_keeps_independent_cta(
    current_editor,
):
    state, ws, cid, _ = current_editor
    ws.save_composition(
        cid,
        current_edit(
            state,
            cid,
            values={"message": "Visible product wording"},
            field_scopes={"message": "product"},
        ),
    )
    ws.save_composition(
        cid,
        current_edit(
            state,
            cid,
            values={"message": "Visible product wording"},
            field_scopes={"message": "all_products"},
        ),
    )
    brief = ws.current_composition(cid)["plan"].brief
    assert all(
        brief.effective_copy(p, "en-US").message == "Visible product wording"
        for p in brief.products
    )
    assert all(
        brief.effective_copy(p, "es-MX").message != "Visible product wording"
        for p in brief.products
    )


@pytest.mark.parametrize("cta", ["Optional action", ""])
def test_optional_cta_autosaves_with_its_placement_and_survives_restart(current_editor, cta):
    from test_workflow import delivery

    from campaign_studio.state import State

    state, ws, cid, _ = current_editor
    command = current_edit(
        state,
        cid,
        values={"cta": cta},
        field_scopes={"cta": "product"},
        placements={"cta": TextPlacement(left=0.12, top=0.32, width=0.7, font_size=25)},
    )
    ws.save_composition(cid, command)
    reopened = Workspace(State(state.storage)).current_composition(cid)
    selected = next(o for o in reopened["plan"].outputs if o.id == "product-1/en-US/1x1")
    assert selected.text.cta == (cta or None)
    manifest, _, _, _ = delivery(state, cid)
    assert next(o for o in manifest["outputs"] if o["id"] == selected.id)["copy"]["cta"] == (
        cta or None
    )


def test_missing_other_source_does_not_block_instruction_save_but_blocks_complete_export(
    current_editor,
):
    from campaign_studio.handoff import DeliveryAction, Handoff

    state, ws, cid, _ = current_editor
    old = ws.current_composition(cid)
    source = state.storage.path(old["sources"]["product-2"].reference)
    payload = source.read_bytes()
    source.unlink()
    version = ws.save_composition(
        cid,
        current_edit(
            state,
            cid,
            values={"message": "Saved with missing source"},
            field_scopes={"message": "all_products"},
        ),
    )
    with pytest.raises((ActionError, StateError, ValueError)):
        Handoff(state).create_current_zip(cid, DeliveryAction(version=version))
    source.write_bytes(payload)
    assert all(
        o.text.message == "Saved with missing source"
        for o in ws.current_composition(cid)["plan"].outputs
        if o.market == "en-US"
    )


def test_invalid_text_geometry_is_saved_for_repair_and_never_silently_exported(current_editor):
    from campaign_studio.handoff import DeliveryAction, Handoff

    state, ws, cid, _ = current_editor
    command = current_edit(
        state,
        cid,
        placements={"message": TextPlacement(left=0.9, top=0.95, width=0.1, font_size=120)},
    )
    version = ws.save_composition(cid, command)
    assert ws.current_composition(cid)["version"] == version
    with pytest.raises((ActionError, StateError)):
        Handoff(state).create_current_zip(cid, DeliveryAction(version=version))
    command = current_edit(
        state,
        cid,
        placements={"message": TextPlacement(left=0.1, top=0.1, width=0.8, font_size=40)},
    )
    version = ws.save_composition(cid, command)
    with Handoff(state).create_current_zip(cid, DeliveryAction(version=version)) as download:
        assert download.size > 1000


def test_reset_text_preserves_crop_and_logo_in_same_format(current_editor):
    from campaign_studio.workspace import FrameEdit

    state, ws, cid, _ = current_editor
    current = ws.current_composition(cid)
    framing = Framing(zoom=1.2, focal_x=0.6, focal_y=0.4)
    ws.save_composition(
        cid,
        FrameEdit(
            revision_id=current["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            framing=framing,
        ),
    )
    ws.save_composition(
        cid,
        current_edit(
            state,
            cid,
            placements={"message": TextPlacement(left=0.1, top=0.1, width=0.6, font_size=42)},
        ),
    )
    ws.save_composition(cid, current_edit(state, cid, placements={"message": None}))
    output = ws.current_composition(cid)["plan"].outputs[0]
    assert output.framing.zoom == 1.2 and output.framing.focal_x == 0.6
    assert "message" not in output.framing.text_placements


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_source_integrity_is_checked_at_delivery_after_unrelated_metadata_save(
    current_editor, damage
):
    from campaign_studio.handoff import DeliveryAction, Handoff

    state, ws, cid, _ = current_editor
    source = state.storage.path(ws.current_composition(cid)["sources"]["product-2"].reference)
    version = ws.save_composition(
        cid,
        current_edit(
            state,
            cid,
            placements={"message": TextPlacement(left=0.1, top=0.1, width=0.7, font_size=48)},
        ),
    )
    if damage == "missing":
        source.unlink()
    else:
        source.write_bytes(b"not the verified source image")
    with pytest.raises((ActionError, StateError, ValueError)):
        Handoff(state).create_current_zip(cid, DeliveryAction(version=version))
    assert not state.__dict__.get("delivery_jobs")
