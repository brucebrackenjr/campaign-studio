import copy

import pytest
from PIL import Image
from pydantic import ValidationError

from campaign_studio.brief import Brief
from campaign_studio.imaging import load_image
from campaign_studio.planning import compare_plans, make_plan
from campaign_studio.sample import write_sample
from campaign_studio.storage import Storage


@pytest.fixture
def sample(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage)
    return storage, brief


def changed(brief, edit):
    value = brief.model_dump(exclude_unset=True)
    edit(value)
    return Brief.model_validate(value)


def test_matrix_is_data_driven_and_plan_is_deterministic(sample):
    storage, brief = sample
    plan = make_plan(brief, storage)
    assert not plan.blocked
    assert len(plan.outputs) == 12
    assert plan.generation_products == []
    assert make_plan(brief, storage).digest == plan.digest
    _, alternative = write_sample(storage, 3, one_market=True)
    assert len(make_plan(alternative, storage).outputs) == 9


def test_shared_override_and_rule_only_impacts(sample):
    storage, brief = sample
    original = make_plan(brief, storage)
    shared = changed(
        brief, lambda value: value["markets"]["es-MX"]["copy"].update(message="Para cada día.")
    )
    impact = compare_plans(original, make_plan(shared, storage))
    assert len(impact.render_outputs) == 6
    assert impact.generation_products == []
    overridden = changed(
        brief,
        lambda value: value["products"][0].update(
            copy_overrides={"es-MX": {"message": "Sólo para hoy."}}
        ),
    )
    assert len(compare_plans(original, make_plan(overridden, storage)).render_outputs) == 3
    ruled = changed(brief, lambda value: value["markets"]["es-MX"].update(phrase_rules=None))
    impact = compare_plans(original, make_plan(ruled, storage))
    assert impact.render_outputs == []
    assert len(impact.recheck_outputs) == 6


def test_null_optional_override_clears_and_omission_inherits(sample):
    _, brief = sample
    override = changed(
        brief, lambda value: value["products"][0].update(copy_overrides={"es-MX": {"cta": None}})
    )
    resolved = override.effective_copy(override.products[0], "es-MX")
    assert resolved.message == "Siempre contigo."
    assert resolved.cta is None


@pytest.mark.parametrize("problem", ["duplicate", "unsupported_market", "message_null", "audience"])
def test_invalid_briefs_do_not_reach_planning(sample, problem):
    _, brief = sample
    value = brief.model_dump(exclude_unset=True)
    if problem == "duplicate":
        value["products"][1]["id"] = value["products"][0]["id"]
    elif problem == "unsupported_market":
        value["markets"]["de-DE"] = copy.deepcopy(value["markets"]["en-US"])
    elif problem == "message_null":
        value["products"][0]["copy_overrides"] = {"es-MX": {"message": None}}
    else:
        value["audience"] = "   "
    with pytest.raises(ValidationError):
        Brief.model_validate(value)


def test_optional_phrase_rules_may_be_omitted_null_or_empty(sample):
    storage, brief = sample
    for rules in ("omitted", None, []):
        value = brief.model_dump(mode="json")
        if rules == "omitted":
            value["markets"]["en-US"].pop("phrase_rules")
        else:
            value["markets"]["en-US"]["phrase_rules"] = rules
        parsed = Brief.model_validate(value)
        assert not parsed.markets["en-US"].phrase_rules
        assert not make_plan(parsed, storage).blocked


def test_corrupt_named_image_blocks_and_never_becomes_generation(sample):
    storage, brief = sample
    storage.path(brief.products[0].source.path).write_bytes(b"broken PNG")
    plan = make_plan(brief, storage)
    assert plan.blocked
    assert plan.generation_products == []
    assert any(item.item == "products/product-1" and item.rule == "asset" for item in plan.findings)


def test_phrase_rules_scan_visible_copy_and_preserve_spanish(sample):
    storage, brief = sample
    value = brief.model_dump(exclude_unset=True)
    value["creative_direction"] = "Private directions contain TESTBLOCK."
    value["markets"]["es-MX"]["copy"]["message"] = "¡Cada día!\n¿Por qué no? Niño."
    valid = Brief.model_validate(value)
    assert not make_plan(valid, storage).blocked
    assert (
        valid.effective_copy(valid.products[0], "es-MX").message
        == value["markets"]["es-MX"]["copy"]["message"]
    )
    value["markets"]["en-US"]["copy"]["message"] = "TESTBLOCK"
    assert make_plan(Brief.model_validate(value), storage).blocked


def test_known_crop_and_copy_defects_block_before_dispatch(sample):
    storage, brief = sample
    wide_subject = changed(
        brief,
        lambda value: value["products"][0]["source"].update(
            subject_box={"left": 0.05, "top": 0.05, "right": 0.95, "bottom": 0.95}
        ),
    )
    assert any(
        item.rule == "subject_crop" and item.severity == "blocking"
        for item in make_plan(wide_subject, storage).findings
    )
    long_copy = changed(
        brief, lambda value: value["markets"]["en-US"]["copy"].update(message="Long " * 300)
    )
    assert any(item.rule == "text_fit" for item in make_plan(long_copy, storage).findings)
    missing_glyph = changed(
        brief, lambda value: value["markets"]["en-US"]["copy"].update(message="Missing 漢 glyph")
    )
    assert any(item.rule == "text_fit" for item in make_plan(missing_glyph, storage).findings)


def test_orientation_applied_and_corrupt_profile_rejected(tmp_path):
    image = Image.new("RGB", (30, 60), "red")
    exif = Image.Exif()
    exif[274] = 6
    original = tmp_path / "rotated.jpg"
    image.save(original, exif=exif)
    oriented, kind = load_image(original)
    assert oriented.size == (60, 30)
    assert kind == "JPEG"
    with Image.open(original) as unchanged:
        assert unchanged.size == (30, 60)
    bad = tmp_path / "bad-profile.png"
    image.save(bad, icc_profile=b"not an ICC profile")
    with pytest.raises(ValueError, match="color profile"):
        load_image(bad)


def test_prepared_boundary_ignores_one_alpha_unit_but_blocks_visible_clipping(sample):
    storage, brief = sample
    reference = "prepared-edge.png"
    with Image.new("RGBA", (100, 100)) as image:
        image.paste((150, 70, 40, 255), (25, 25, 75, 75))
        image.putpixel((0, 50), (150, 70, 40, 1))
        image.save(storage.path(reference))
    value = brief.model_dump()
    value["products"][0]["source"] = {
        "role": "prepared_product",
        "path": reference,
        "opaque_nonreflective_product_confirmed": True,
        "provenance": {
            "label": "Synthetic alpha boundary",
            "rights": "Local fixture",
            "synthetic": True,
        },
    }
    prepared = Brief.model_validate(value)
    original = storage.path(reference).read_bytes()
    plan = make_plan(prepared, storage)
    assert not plan.blocked
    assert not any(item.rule == "alpha_residue" for item in plan.findings)
    assert storage.path(reference).read_bytes() == original
    for opacity in (2, 128, 255):
        with Image.open(storage.path(reference)) as source:
            image = source.copy()
        image.putpixel((0, 50), (150, 70, 40, opacity))
        image.save(storage.path(reference))
        assert any(
            item.rule == "prepared_product" and item.severity == "blocking"
            for item in make_plan(prepared, storage).findings
        )


def test_background_request_excludes_product_description_and_concept_keeps_its_subject(sample):
    from campaign_studio.generation import request_for

    _, brief = sample
    value = brief.model_dump()
    value["products"][0]["description"] = "Unique product-specific detail that must stay local."
    value["products"][0]["source"] = {
        "role": "prepared_product",
        "path": "local.png",
        "opaque_nonreflective_product_confirmed": True,
        "provenance": {"label": "Test fixture", "rights": "Local fixture", "synthetic": True},
    }
    prepared = Brief.model_validate(value)
    background = request_for(prepared, prepared.products[0])
    assert background.method_version == 2
    assert prepared.products[0].description not in background.prompt
    assert "Subject:" not in background.prompt
    assert "empty campaign environment" in background.prompt
    value["products"][0]["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "A unique described concept subject.",
        "provenance": {"label": "Test fixture", "rights": "Local fixture", "synthetic": True},
    }
    concept = Brief.model_validate(value)
    assert (
        concept.products[0].source.description in request_for(concept, concept.products[0]).prompt
    )
