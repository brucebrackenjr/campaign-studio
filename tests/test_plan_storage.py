import builtins
import copy
import json

import pytest

from campaign_studio import planning
from campaign_studio.brief import FORMATS, Brief
from campaign_studio.generation import GenerationRequest
from campaign_studio.plan_storage import STORAGE_FORMAT, dump_plan, load_plan
from campaign_studio.planning import Asset, Finding, Plan, expand_outputs


@pytest.fixture
def brief():
    provenance = {"label": "Synthetic test input", "rights": "Test fixture", "synthetic": True}
    return Brief.model_validate(
        {
            "campaign_id": "80d58cbe-5e57-5544-9b07-a4f018714dde",
            "name": "Storage fixture",
            "audience": "People testing local image editing.",
            "creative_direction": "A quiet setting with space around the product.",
            "brand": {
                "id": "test-brand",
                "name": "Test brand",
                "palette": ["#FFFFFF", "#123456"],
                "text_color": "#FFFFFF",
            },
            "copy_provenance": provenance,
            "markets": {
                "en-US": {"copy": {"message": "English message", "cta": "Learn more"}},
                "es-MX": {"copy": {"message": "Mensaje", "cta": "Más información"}},
            },
            "products": [
                {
                    "id": "product-one",
                    "name": "First product",
                    "description": "A supplied product scene.",
                    "source": {
                        "role": "finished_scene",
                        "path": "Campaigns/test/Assets/source.png",
                        "provenance": provenance,
                        "subject_box": {"left": 0.4, "top": 0.4, "right": 0.6, "bottom": 0.6},
                    },
                    "copy_overrides": {"es-MX": {"message": "Otro mensaje", "cta": None}},
                    "framing": {
                        "en-US": {
                            "1:1": {
                                "zoom": 1.599,
                                "focal_x": 0.43,
                                "focal_y": 0.62,
                                "copy_placement": "bottom",
                                "inherit_subject": False,
                                "subject_box": None,
                                "logo_placement": {"left": 0.11, "top": 0.13, "width": 0.25},
                                "text_placements": {
                                    "message": {
                                        "left": 0.15,
                                        "top": 0.23,
                                        "width": 0.68,
                                        "font_size": 76,
                                        "font_id": "barlow_condensed",
                                        "color": "#123456",
                                    },
                                    "cta": {
                                        "left": 0.2,
                                        "top": 0.7,
                                        "width": 0.5,
                                        "font_size": 37,
                                        "font_id": "noto_sans",
                                        "color": None,
                                    },
                                },
                            },
                            "16:9": {
                                "subject_box": {
                                    "left": 0.3,
                                    "top": 0.35,
                                    "right": 0.7,
                                    "bottom": 0.65,
                                }
                            },
                        }
                    },
                },
                {
                    "id": "product-two",
                    "name": "Second product",
                    "description": "A concept requiring generation.",
                    "source": {
                        "role": "concept",
                        "imagery_absent": True,
                        "description": "A brass lantern on an autumn porch.",
                        "provenance": provenance,
                    },
                },
            ],
        }
    )


@pytest.fixture
def plan(brief):
    return Plan(
        digest="the-original-confirmed-digest",
        brief=brief,
        assets={
            "products/product-one": Asset(
                reference="Campaigns/test/Assets/source.png",
                sha256="1" * 64,
                width=1920,
                height=1920,
            )
        },
        outputs=expand_outputs(brief),
        generation_requests={
            "product-two": GenerationRequest(
                method_version=1,
                model="retained-model",
                prompt="The exact already-confirmed request.",
                deadline_seconds=75,
            )
        },
        transmitted_data={"product-two": "The confirmed scene description."},
        findings=[
            Finding(
                item="product-one/en-US/1x1",
                rule="retained-check",
                severity="warning",
                evidence="Retained validation result.",
            )
        ],
        cost_status="unknown",
        cost_estimate={"amount": 0.1, "currency": "USD", "source": "stored estimate"},
    )


def test_saved_plan_has_one_settings_location_and_preserves_every_runtime_field(plan):
    serialized = dump_plan(plan)
    saved = json.loads(serialized)
    assert saved["storage_format"] == STORAGE_FORMAT
    assert "outputs" not in saved
    assert saved["brief"] == plan.brief.model_dump(mode="json")
    assert serialized.count('"logo_placement"') == 2  # Two explicit format entries only.
    loaded = load_plan(serialized)
    assert loaded.model_dump(mode="json") == plan.model_dump(mode="json")
    assert loaded.digest == plan.digest
    assert loaded.generation_requests == plan.generation_requests
    assert loaded.assets == plan.assets
    assert loaded.cost_estimate == plan.cost_estimate
    assert loaded.findings == plan.findings
    assert dump_plan(loaded) == serialized


@pytest.mark.parametrize("markets", [("en-US",), ("es-MX",), ("es-MX", "en-US")])
def test_output_identity_formats_and_copy_follow_selected_markets(brief, markets):
    value = brief.model_dump(mode="json")
    value["markets"] = {market: value["markets"][market] for market in markets}
    if "es-MX" not in markets:
        value["products"][0]["copy_overrides"] = {}
    selected = Brief.model_validate(value)
    outputs = expand_outputs(selected)
    assert len(outputs) == len(selected.products) * len(markets) * len(FORMATS)
    for product in selected.products:
        for market in markets:
            for ratio, dimensions in FORMATS.items():
                output = next(
                    o for o in outputs if o.id == f"{product.id}/{market}/{ratio.replace(':', 'x')}"
                )
                assert (output.product_id, output.market, output.ratio) == (
                    product.id,
                    market,
                    ratio,
                )
                assert (output.width, output.height) == dimensions
                assert output.text == selected.effective_copy(product, market)
    if "es-MX" in markets:
        spanish = next(o for o in outputs if o.id == "product-one/es-MX/1x1")
        assert spanish.text.message == "Otro mensaje"
        assert spanish.text.cta is None


def test_source_safety_inheritance_explicit_clear_and_custom_box_survive(plan):
    before = plan.brief.model_dump(mode="json")
    outputs = {o.id: o for o in load_plan(dump_plan(plan)).outputs}
    square = outputs["product-one/en-US/1x1"]
    portrait = outputs["product-one/en-US/9x16"]
    landscape = outputs["product-one/en-US/16x9"]
    assert square.framing.subject_box is None
    assert not square.framing.inherit_subject
    assert portrait.framing.subject_box == plan.brief.products[0].source.subject_box
    assert (
        landscape.framing.subject_box == plan.brief.products[0].framing["en-US"]["16:9"].subject_box
    )
    assert landscape.framing.subject_box != portrait.framing.subject_box
    assert outputs["product-two/en-US/9x16"].framing.subject_box is None
    square.framing.zoom = 1.2
    portrait.framing.subject_box.left = 0.1
    assert plan.brief.model_dump(mode="json") == before


def test_load_never_runs_preflight_reads_assets_or_rebuilds_requests(plan, monkeypatch):
    modern = dump_plan(plan)
    legacy = plan.model_dump_json()

    def forbidden(*args, **kwargs):
        raise AssertionError("Loading stored settings must not perform I/O or new planning.")

    for name in ("make_plan", "load_image", "require_font", "request_for"):
        monkeypatch.setattr(planning, name, forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert load_plan(modern) == plan
    assert load_plan(legacy) == plan


def test_legacy_plan_migrates_without_changing_inputs_or_digest(plan):
    legacy = plan.model_dump(mode="json")
    before = copy.deepcopy(legacy)
    migrated = dump_plan(load_plan(legacy))
    assert load_plan(migrated) == plan
    assert dump_plan(load_plan(migrated)) == migrated
    assert legacy == before


@pytest.mark.parametrize("field", ["zoom", "copy", "identity", "missing", "duplicate"])
def test_inconsistent_legacy_outputs_are_not_silently_discarded(plan, field):
    legacy = plan.model_dump(mode="json")
    if field == "zoom":
        legacy["outputs"][0]["framing"]["zoom"] = 1.8
    elif field == "copy":
        legacy["outputs"][0]["copy"]["message"] = "Unrepresented saved wording"
    elif field == "identity":
        legacy["outputs"][0]["id"] = "another-product/en-US/1x1"
    elif field == "missing":
        legacy["outputs"].pop()
    else:
        legacy["outputs"].append(copy.deepcopy(legacy["outputs"][0]))
    with pytest.raises(ValueError, match="outputs do not match"):
        load_plan(legacy)


def test_dump_rejects_runtime_settings_not_represented_in_brief(plan):
    plan.outputs[0].framing.zoom = 1.8
    with pytest.raises(ValueError, match="outputs do not match"):
        dump_plan(plan)


@pytest.mark.parametrize("payload", ["[]", '{"storage_format":"future"}'])
def test_invalid_storage_envelopes_are_rejected(payload):
    with pytest.raises(ValueError):
        load_plan(payload)


def test_normalized_storage_rejects_a_second_output_settings_document(plan):
    value = json.loads(dump_plan(plan))
    value["outputs"] = plan.model_dump(mode="json")["outputs"]
    with pytest.raises(ValueError, match="cannot also contain"):
        load_plan(value)
