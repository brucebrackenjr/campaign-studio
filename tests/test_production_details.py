"""Synthetic producer-context checks; no live provider or user-runtime access."""

import copy
import json

import pytest
from campaign_fixtures import authored_edit
from pydantic import ValidationError

from campaign_studio.brief import Brief
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_sample
from campaign_studio.storage import Storage
from campaign_studio.validation import validation_issues
from campaign_studio.workspace import ActionError

pytest_plugins = ["test_web_workflow"]


@pytest.fixture
def sample(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage)
    return storage, brief.model_dump(mode="json")


def rule_plan(storage, value, phrase, *, match="literal", case_sensitive=False):
    value["markets"]["en-US"]["phrase_rules"] = [
        {
            "id": "context-rule",
            "phrase": phrase,
            "match": match,
            "case_sensitive": case_sensitive,
        }
    ]
    return make_plan(Brief.model_validate(value), storage)


def first_rule(plan):
    return next(item for item in plan.findings if item.rule == "context-rule")


@pytest.mark.parametrize(
    "phrase,text,match,sensitive,matched",
    [
        ("café", "A cafe\u0301 visit.", "literal", False, True),
        ("STRASSE", "Die Straße.", "literal", False, True),
        ("STRASSE", "Die Straße.", "literal", True, False),
        ("a+b?", "Choose a+b? today.", "literal", False, True),
        ("a+b?", "Choose aaab today.", "literal", False, False),
        ("cat", "scatter cat2 cat_cat", "whole_word", False, False),
        ("cat", "A cat, here.", "whole_word", False, True),
    ],
)
def test_exact_unicode_literal_and_word_semantics(sample, phrase, text, match, sensitive, matched):
    storage, value = sample
    value["markets"]["en-US"]["copy"] = {"message": text}
    finding = first_rule(rule_plan(storage, value, phrase, match=match, case_sensitive=sensitive))
    assert finding.severity == ("blocking" if matched else "pass")
    context = finding.context.phrase
    assert context.configured_phrase == phrase
    assert context.match == match and context.case_sensitive == sensitive
    assert context.normalization == ("NFC" if sensitive else "NFC_then_casefold")
    assert context.word_boundary == ("Unicode_word_characters" if match == "whole_word" else "none")
    assert [item.value for item in context.matches] == ([text] if matched else [])
    assert all(
        set(item.model_dump()) == {"field", "value", "origin", "path"} for item in context.matches
    )


def test_multiple_visible_fields_name_and_override_origins(sample):
    storage, value = sample
    value["markets"]["en-US"]["copy"] = {
        "message": "Made for every day.",
        "cta": "Choose every day.",
    }
    value["products"][0].update(
        name="Every day kit", copy_overrides={"en-US": {"cta": "Try every day."}}
    )
    plan = rule_plan(storage, value, "every day")
    finding = first_rule(plan)
    matches = {item.field: item for item in finding.context.phrase.matches}
    assert set(matches) == {"message", "cta"}
    assert matches["cta"].value == "Try every day." and matches["cta"].origin == "product_override"
    assert matches["cta"].path == ["products", 0, "copy_overrides", "en-US", "cta"]
    assert matches["message"].origin == "market_shared"
    assert finding.context.product_name == "Every day kit"
    assert finding.context.product_id == value["products"][0]["id"]
    assert finding.context.market == "en-US" and finding.context.ratio == "1:1"
    assert finding.context.fields[1].product_index == 0


def test_nonvisible_inputs_and_no_rules_are_not_scanned(sample):
    storage, value = sample
    value["creative_direction"] = "PRIVATE_SEARCH_TERM"
    value["products"][0].update(name="PRIVATE_SEARCH_TERM", description="PRIVATE_SEARCH_TERM")
    value["products"][0]["copy_overrides"] = {"en-US": {"cta": None}}
    value["markets"]["en-US"]["copy"] = {
        "message": "A useful choice.",
        "cta": "PRIVATE_SEARCH_TERM",
    }
    plan = rule_plan(storage, value, "PRIVATE_SEARCH_TERM")
    finding = first_rule(plan)
    assert finding.severity == "pass" and finding.context.phrase.matches == []
    value["markets"]["en-US"]["phrase_rules"] = None
    plan = make_plan(Brief.model_validate(value), storage)
    assert not any(item.context and item.context.phrase for item in plan.findings)


def test_safe_nested_validation_never_echoes_unknown_keys_or_values(sample):
    _, value = sample
    secret = "PRIVATE_VALIDATION_SENTINEL"
    value["products"][1]["source"]["role"] = secret
    value["brand"]["palette"] = None
    value["markets"]["es-MX"]["copy"][secret] = {"/Users/private/source": secret}
    value["markets"][secret] = {"copy": {"message": secret}}
    with pytest.raises(ValidationError) as failure:
        Brief.model_validate(value)
    issues = validation_issues(failure.value)
    serialized = json.dumps(issues)
    assert secret not in serialized and "/Users/" not in serialized
    assert "input" not in issues[0] and "ctx" not in issues[0]
    assert any(
        item["path"] == ["products", 1, "source"] and item["product_index"] == 1 for item in issues
    )
    assert any(item["path"] == ["brand", "palette"] for item in issues)
    assert any(
        item["path"] == ["markets", "es-MX", "copy"] and item["market"] == "es-MX"
        for item in issues
    )


def test_import_and_form_rejection_preserve_values_and_return_safe_fields(ui):
    client, workspace = ui
    intake = Intake(workspace.storage)
    campaign, created = intake.new()
    original = intake.read(campaign)
    _, brief = write_sample(workspace.storage)
    value = copy.deepcopy(created["value"])
    value["products"] = [brief.products[0].model_dump(mode="json")]
    value["products"][0]["source"]["path"] = ""
    value["audience"] = "Keep this producer-entered audience."
    value["brand"]["palette"] = None
    document = BriefDraft(value=value, version=created["version"])
    before = document.model_dump()
    with pytest.raises(ActionError) as failure:
        intake.save(campaign, document, workspace)
    assert failure.value.field_errors and document.model_dump() == before
    assert intake.read(campaign) == original
    action = authored_edit(client, campaign)
    action["authored"]["brief"] = value
    saved = client.post(f"/api/motif/campaigns/{campaign}/authored", json=action)
    assert saved.status_code == 200, saved.text
    current = intake.read(campaign)
    assert current["authored"]["brief"] == value
    response = client.post(
        f"/api/motif/campaigns/{campaign}/plan", json={"version": saved.json()["version"]}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "brief_fields" and response.json()["message"]
    assert any(item["path"] == ["brand", "palette"] for item in response.json()["field_errors"])
    assert value["audience"] not in response.text
    assert intake.read(campaign) == current
    imported = brief.model_dump(mode="json")
    imported["products"][1]["copy_overrides"] = {"es-MX": {"message": None}}
    imported["creative_direction"] = "PRIVATE_PROMPT_SENTINEL"
    response = client.post("/api/motif/import", json=imported)
    assert response.status_code == 422 and response.json()["code"] == "import_brief"
    assert "PRIVATE_PROMPT_SENTINEL" not in response.text
    assert any(
        item["product_index"] == 1 and item["market"] == "es-MX"
        for item in response.json()["field_errors"]
    )
    assert intake.read(campaign) == current and workspace.runs() == []


def test_request_errors_keep_safe_messages_and_structured_fields(ui):
    client, workspace = ui
    campaign = client.post("/api/motif/campaigns", json={"name": "Safe errors"}).json()[
        "campaign_id"
    ]
    response = client.post(
        f"/api/motif/campaigns/{campaign}/authored", json={"authored": "PRIVATE_BODY_SENTINEL"}
    )
    assert response.status_code == 422 and response.json()["code"] == "validation"
    assert all(isinstance(item, str) for item in response.json()["fields"])
    assert response.json()["field_errors"]
    assert "PRIVATE_BODY_SENTINEL" not in response.text
    assert workspace.runs() == []


def test_incomplete_draft_plan_errors_have_field_context_and_no_run(ui):
    client, workspace = ui
    campaign, created = Intake(workspace.storage).new()
    before = Intake(workspace.storage).read(campaign)
    response = client.post(
        f"/api/motif/campaigns/{campaign}/plan", json={"version": created["version"]}
    )
    assert response.status_code == 422
    paths = [item["path"] for item in response.json()["field_errors"]]
    assert ["products"] in paths
    assert response.json()["code"] == "product_limit"
    assert Intake(workspace.storage).read(campaign) == before and workspace.runs() == []


def test_preflight_findings_map_logo_crop_and_copy_to_known_fields(sample):
    storage, value = sample
    storage.path(value["brand"]["logo_path"]).unlink()
    value["products"][1]["source"]["subject_box"] = {"left": 0, "top": 0, "right": 1, "bottom": 1}
    value["markets"]["en-US"]["copy"]["message"] = "Longword" * 12
    plan = make_plan(Brief.model_validate(value), storage)
    logo = next(item for item in plan.findings if item.item == "logo")
    crop = next(item for item in plan.findings if item.rule == "subject_crop")
    text = next(item for item in plan.findings if item.rule == "text_fit")
    assert logo.context.fields[0].path == ["brand", "logo_path"]
    assert crop.context.product_id == value["products"][1]["id"] and crop.context.ratio
    assert crop.context.fields[0].path == ["products", 1, "source", "subject_box"]
    assert text.context.fields[0].path == ["markets", "en-US", "copy", "message"]
