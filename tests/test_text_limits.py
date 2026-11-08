"""Purpose bounds preserve exact drafts, and agree across schema, actions, and UI."""
# ruff: noqa: F811

import asyncio
import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_authored import action, project  # noqa: F401
from test_web_workflow import page_data, sample_plan, start, ui, wait_for_work  # noqa: F401

from campaign_studio.brief import Brief
from campaign_studio.bundled import install_campaigns
from campaign_studio.intake import Intake
from campaign_studio.review import CreativeUpdate
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.text_limits import LIMITS, authored_length_issues, browser_contract
from campaign_studio.validation import validation_issues
from campaign_studio.workspace import Workspace

RESOURCES = Path(__file__).resolve().parents[1] / "src/campaign_studio/resources/campaigns"
FIELDS = [
    ("campaign_name", ["name"]),
    ("brand_name", ["brand", "name"]),
    ("product_name", ["products", 0, "name"]),
    ("headline", ["markets", "en-US", "copy", "message"]),
    ("cta", ["markets", "es-MX", "copy", "cta"]),
    ("audience", ["audience"]),
    ("direction", ["creative_direction"]),
    ("description", ["products", 0, "description"]),
    ("description", ["products", 0, "source", "description"]),
    ("origin_label", ["products", 0, "source", "provenance", "label"]),
    ("rights", ["brand", "logo_provenance", "rights"]),
    ("phrase", ["markets", "en-US", "phrase_rules", 0, "phrase"]),
    ("file_reference", ["brand", "logo_path"]),
    ("file_reference", ["replay", "sources", "product-1", "path"]),
    ("replay_model", ["replay", "sources", "product-1", "model"]),
    ("replay_history", ["replay", "history"]),
    ("headline", ["products", 0, "copy_overrides", "en-US", "message"]),
    ("cta", ["products", 0, "copy_overrides", "en-US", "cta"]),
]


def raw_brief():
    raw = json.loads((RESOURCES / "autumn-winds/brief.json").read_text())
    origin = raw["products"][0]["source"]["provenance"]
    raw["products"][0]["id"] = "product-1"
    raw["products"][0]["source"] = {
        "role": "concept",
        "description": "Concept",
        "imagery_absent": True,
        "provenance": origin,
    }
    raw["products"][0]["copy_overrides"] = {"en-US": {"message": "Headline", "cta": "Explore"}}
    raw["markets"]["en-US"]["phrase_rules"] = [{"id": "rule-1", "phrase": "Absent phrase"}]
    raw["replay"] = {
        "partial": True,
        "historical_requests": 1,
        "history": "Historical external source",
        "sources": {
            "product-1": {
                "path": "source.png",
                "sha256": "a" * 64,
                "model": "image-model",
                "generated_on": "2026-10-30",
                "provenance": origin,
            }
        },
    }
    return raw


def set_field(raw, path, value):
    parent = raw
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


@pytest.mark.parametrize("purpose,path", FIELDS)
@pytest.mark.parametrize("character", ["W", "é", "🦊"])
def test_exact_limits_and_safe_specific_feedback(purpose, path, character):
    raw = raw_brief()
    limit = LIMITS[purpose]
    set_field(raw, path, character * limit)
    valid = Brief.model_validate(raw).model_dump(mode="json")
    value = valid
    for key in path:
        value = value[key]
    assert value == character * limit  # No trimming or truncation.
    assert authored_length_issues({"brief": raw}) == []
    set_field(raw, path, character * (limit + 1))
    with pytest.raises(ValidationError) as invalid:
        Brief.model_validate(raw)
    issues = validation_issues(invalid.value, imported_fields=True)
    issue = next(i for i in issues if i["code"] == "text_too_long")
    assert issue["path"] == path
    assert f"{limit:,}" in issue["message"] and issue["label"] != "Brief field"
    assert character * (limit + 1) not in json.dumps(issues)
    draft_issues = authored_length_issues({"brief": raw})
    assert (
        draft_issues[0]["path"] == path and f"{limit + 1:,} entered" in draft_issues[0]["message"]
    )


def test_combining_unicode_is_not_normalized_or_counted_differently():
    raw = raw_brief()
    raw["name"] = "e\u0301" * 12 + "x"
    assert len(raw["name"]) == LIMITS["campaign_name"]
    assert Brief.model_validate(raw).name == raw["name"]
    raw["name"] += "x"
    with pytest.raises(ValidationError):
        Brief.model_validate(raw)


def test_schema_and_rendered_browser_contract_share_limits(ui):
    client, _ = ui
    schema = Brief.model_json_schema()
    assert schema["properties"]["name"]["maxLength"] == LIMITS["campaign_name"]
    assert schema["$defs"]["Brand"]["properties"]["name"]["maxLength"] == LIMITS["brand_name"]
    assert schema["$defs"]["Copy"]["properties"]["message"]["maxLength"] == LIMITS["headline"]
    html = client.get("/").text
    encoded = re.search(
        r'<script id="motif-text-limits" type="application/json">(.*?)</script>', html
    )[1]
    assert json.loads(encoded) == browser_contract()
    assert json.loads(Path("docs/brief.schema.json").read_text()) == schema


def test_scratch_limits_and_authored_correction_reopen_exactly(project):
    client, ws, cid = project
    for field, purpose in [
        ("name", "campaign_name"),
        ("brand", "brand_name"),
        ("audience", "audience"),
    ]:
        limit = LIMITS[purpose]
        rejected = client.post(
            "/api/motif/campaigns", json={"name": "New campaign", field: "🦊" * (limit + 1)}
        )
        assert rejected.status_code == 422
        assert str(limit) in rejected.json()["field_errors"][0]["message"]
    created = client.post(
        "/api/motif/campaigns",
        json={
            "name": "🦊" * LIMITS["campaign_name"],
            "brand": "é" * LIMITS["brand_name"],
            "audience": "W" * 500,
        },
    )
    assert created.status_code == 200, created.text
    request = action(client, cid)
    original = copy.deepcopy(request)
    request["authored"]["brief"]["name"] = "🦊" * (LIMITS["campaign_name"] + 1)
    rejected = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert rejected.status_code == 422
    assert "Campaign name" in rejected.json()["message"]
    assert str(LIMITS["campaign_name"]) in rejected.json()["message"]
    assert action(client, cid) == original
    assert request["authored"]["brief"]["name"] == "🦊" * (LIMITS["campaign_name"] + 1)
    request["authored"]["brief"]["name"] = "🦊" * LIMITS["campaign_name"]
    request["authored"]["brief"]["brand"]["name"] = "é" * LIMITS["brand_name"]
    request["authored"]["brief"]["audience"] = (
        ""  # Still an unfinished draft, not a complete Brief.
    )
    request["authored"]["brief"]["products"] = []
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert saved.status_code == 200, saved.text
    reopened = action(client, cid)
    assert reopened["authored"] == request["authored"]
    assert ws.runs(cid) == []
    assert Intake(ws.storage).read(cid)["authored"] == request["authored"]


@pytest.mark.parametrize("container", ["form_drafts", "review_drafts"])
def test_pending_wording_remains_in_tab_when_rejected_and_can_save_correction(project, container):
    client, _, cid = project
    request = action(client, cid)
    form = {"copy-headline": "🦊" * 121, "image-description": ""}
    if container == "form_drafts":
        request["authored"][container] = {"fields": {"brief/copy/en": form}}
    else:
        request["authored"][container] = {"product-1/en-US/1x1": {"values": form}}
    rejected = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert rejected.status_code == 422 and "Headline" in rejected.json()["message"]
    assert "120" in rejected.json()["message"]
    assert form["copy-headline"] == "🦊" * 121
    form["copy-headline"] = "🦊" * 120
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert saved.status_code == 200, saved.text
    assert action(client, cid)["authored"] == request["authored"]


@pytest.mark.parametrize("purpose,path", FIELDS)
def test_import_reports_each_limit_before_creating_a_campaign(ui, purpose, path):
    client, ws = ui
    raw = raw_brief()
    limit = LIMITS[purpose]
    set_field(raw, path, "X" * (limit + 1))
    before = ws.catalog()
    rejected = client.post("/api/motif/import", json=raw)
    assert rejected.status_code == 422, rejected.text
    issue = next(i for i in rejected.json()["field_errors"] if i["code"] == "text_too_long")
    assert issue["path"] == path and f"{limit:,}" in issue["message"]
    assert ws.catalog() == before


def test_studio_text_refuses_over_limit_then_applies_corrected_copy(ui):
    client, ws = ui
    data = sample_plan(client)
    ready = page_data(client, client.get(f"/campaigns/{data['campaignId']}/studio"))
    edit = {
        "run_id": ready["runId"],
        "revision_id": ready["currentRevision"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
        "values": {"message": "X" * 121},
        "field_scopes": {"message": "product"},
    }
    endpoint = f"/api/campaigns/{data['campaignId']}/text"
    rejected = client.post(endpoint + "/preview", json=edit)
    assert rejected.status_code == 422, rejected.text
    assert "120" in rejected.json()["field_errors"][0]["message"]
    rejected = client.post(endpoint + "/save", json=edit)
    assert rejected.status_code == 422
    assert (
        client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["version"]
        == ready["currentRevision"]
    )
    edit["values"] = {"message": "A fresh headline."}
    preview = client.post(endpoint + "/preview", json=edit)
    assert preview.status_code == 200, preview.text
    applied = client.post(
        endpoint + "/save", json=edit | {"preview_digest": preview.json()["digest"]}
    )
    assert applied.status_code == 200, applied.text
    assert (
        ws.current_composition(data["campaignId"])["plan"]
        .brief.products[0]
        .copy_overrides["en-US"]
        .message
        == "A fresh headline."
    )
    assert ws.runs(data["campaignId"]) == []


def test_image_description_action_uses_description_limit():
    base = {"expected_version": 0}
    assert CreativeUpdate(**base, description="é" * 1000).description == "é" * 1000
    with pytest.raises(ValidationError) as rejected:
        CreativeUpdate(**base, description="é" * 1001)
    assert "1,000" in validation_issues(rejected.value)[0]["message"]


def test_fresh_renamed_examples_install_all_prepared_bytes_without_rendering(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fresh setup must copy prepared images without rendering or requesting images")

    monkeypatch.setattr("campaign_studio.imaging.compose", forbidden)
    monkeypatch.setattr("campaign_studio.runner.Runner.execute", forbidden)
    ws = Workspace(State(Storage(tmp_path / "Workspace")))
    receipt = asyncio.run(install_campaigns(ws))
    assert receipt["state"] == "complete"
    from campaign_studio.campaign_drafts import read

    names = [read(ws.storage, c["id"])["authored"]["brief"]["name"] for c in receipt["campaigns"]]
    assert set(names) == {"Autumn at Home", "Winter at the Ranch"}
    autumn = next(c["id"] for c in receipt["campaigns"] if c["slug"] == "autumn-winds")
    plan = ws.current_composition(autumn)["plan"]
    assert not plan.blocked and len(plan.outputs) == 30 and not plan.generation_products
    original = json.loads((RESOURCES / "autumn-winds/brief.json").read_text())
    expected = {
        hashlib.sha256((RESOURCES / "autumn-winds" / p["source"]["path"]).read_bytes()).hexdigest()
        for p in original["products"]
    }
    actual = {a.sha256 for key, a in plan.assets.items() if key.startswith("products/")}
    assert actual == expected
    assert ws.runs(autumn) == []
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))
