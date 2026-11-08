"""An unfinished product must not hide or replace its unaffected neighbours."""

import asyncio
import copy
from uuid import uuid4

import pytest
from campaign_fixtures import authored_edit
from pydantic import ValidationError

from campaign_studio.bundled import install_campaigns
from campaign_studio.campaign_drafts import read, write
from campaign_studio.compositions import preview_brief
from campaign_studio.sample import write_sample

pytest_plugins = ["test_web_workflow"]


def seed_three(workspace):
    _, brief = write_sample(workspace.storage, 3)
    value = brief.model_dump(mode="json")
    write(
        workspace.storage,
        brief.campaign_id,
        {
            "value": value,
            "authored": {"brief": value},
            "composition_schema": 1,
            "version": uuid4().hex,
        },
    )
    return brief.campaign_id


@pytest.mark.parametrize("unfinished_index", [0, 2])
def test_unfinished_photo_preserves_other_previews_edits_and_strict_export(ui, unfinished_index):
    client, workspace = ui
    campaign = seed_three(workspace)
    document = authored_edit(client, campaign)
    products = document["authored"]["brief"]["products"]
    incomplete = products[unfinished_index]
    source = copy.deepcopy(incomplete["source"])
    incomplete["source"] = {
        "role": "finished_scene",
        "provenance": source["provenance"],
        "path": "",
        # This reproduces an earlier concept-to-photo toggle with leftover fields.
        "imagery_absent": True,
        "description": "A newly selected photo will be supplied.",
    }
    incomplete_before = copy.deepcopy(incomplete)
    response = client.post(f"/api/motif/campaigns/{campaign}/authored", json=document)
    assert response.status_code == 200, response.text
    data = client.get(f"/api/motif/campaigns/{campaign}").json()
    assert data["can_edit"] and not data["manifest"]["complete"]
    assert data["unplanned_draft"]
    assert data["plan"]["outputs"] == 18
    assert data["manifest"]["metrics"]["completed_images"] == 12
    assert data["manifest"]["metrics"]["expected_images"] == 18
    assert all(issue["product_index"] == unfinished_index for issue in data["composition_issues"])
    assert not data["editable_products"][incomplete["id"]]
    remaining = [product for product in products if product["id"] != incomplete["id"]]
    assert all(data["editable_products"][product["id"]] for product in remaining)
    assert data["plan"]["generation_products"] == []
    before_files = {
        str(path): path.read_bytes()
        for path in workspace.storage.root.rglob("*")
        if path.is_file() and not path.name.startswith("state.sqlite3")
    }
    before_document = read(workspace.storage, campaign)
    for product in remaining:
        output = next(
            item for item in data["manifest"]["outputs"] if item["product_id"] == product["id"]
        )
        image = client.get(output["url"])
        assert image.status_code == 200, image.text
        assert image.content.startswith(b"\x89PNG")
        assert client.get(data["source_urls"][product["id"]]).status_code == 200
    assert read(workspace.storage, campaign) == before_document

    selected = remaining[0]["id"]
    edit = {
        "revision_id": data["version"],
        "product_id": selected,
        "market": "en-US",
        "ratio": "1:1",
        "framing": {"zoom": 1.1},
    }
    preview = client.post(f"/api/campaigns/{campaign}/framing/preview", json=edit)
    assert preview.status_code == 200, preview.text
    assert not preview.json()["blocked"] and preview.json()["images"]
    saved = client.post(f"/api/campaigns/{campaign}/framing/save", json=edit)
    assert saved.status_code == 200, saved.text
    assert len(saved.json()["composition_slots"]) == 12
    assert saved.json()["composition_issues"]
    value = read(workspace.storage, campaign)["authored"]["brief"]
    assert len(value["products"]) == 3 and value["products"][unfinished_index] == incomplete_before
    selected_value = next(product for product in value["products"] if product["id"] == selected)
    assert selected_value["framing"]["en-US"]["1:1"]["zoom"] == 1.1

    wording = {
        "revision_id": saved.json()["version"],
        "product_id": selected,
        "market": "en-US",
        "ratio": "1:1",
        "values": {"message": "A shared new headline."},
        "field_scopes": {"message": "all_products"},
    }
    preview = client.post(f"/api/campaigns/{campaign}/text/preview", json=wording)
    assert preview.status_code == 200, preview.text
    assert not preview.json()["blocked"] and preview.json()["images"]
    saved = client.post(f"/api/campaigns/{campaign}/text/save", json=wording)
    assert saved.status_code == 200, saved.text
    value = read(workspace.storage, campaign)["authored"]["brief"]
    assert len(value["products"]) == 3
    assert value["products"][unfinished_index]["source"] == incomplete_before["source"]
    assert value["markets"]["en-US"]["copy"]["message"] == "A shared new headline."
    assert all(
        slot["copy"]["message"] == "A shared new headline."
        for slot in saved.json()["composition_slots"]
        if slot["market"] == "en-US"
    )
    for partial in (False, True):
        denied = client.post(
            f"/api/campaigns/{campaign}/delivery/zip",
            json={"version": saved.json()["version"], "allow_partial": partial},
        )
        assert denied.status_code == 422, denied.text
    denied_plan = client.post(
        f"/api/motif/campaigns/{campaign}/plan", json={"version": saved.json()["version"]}
    )
    assert denied_plan.status_code == 422, denied_plan.text
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert before_files == {
        str(path): path.read_bytes()
        for path in workspace.storage.root.rglob("*")
        if path.is_file() and not path.name.startswith("state.sqlite3")
    }
    assert not list(workspace.storage.root.glob("Campaigns/**/Outputs"))
    assert not list(workspace.storage.root.glob("Campaigns/**/Exports"))


def test_preview_projection_preserves_original_issue_addresses_and_rejects_global_errors(ui):
    client, workspace = ui
    campaign = seed_three(workspace)
    document = authored_edit(client, campaign)
    value = document["authored"]["brief"]
    value["products"][0]["source"]["path"] = ""
    value["products"][2]["source"]["subject_box"] = {
        "left": 0.05,
        "top": 0.05,
        "right": 0.95,
        "bottom": 0.95,
    }
    response = client.post(f"/api/motif/campaigns/{campaign}/authored", json=document)
    assert response.status_code == 200, response.text
    data = client.get(f"/api/motif/campaigns/{campaign}").json()
    crop = next(f for f in data["plan"]["findings"] if f["rule"] == "subject_crop")
    assert crop["context"]["product_id"] == value["products"][2]["id"]
    assert all(
        f["path"][1] == 2 and f["product_index"] == 2 and f["label"].startswith("Product 3")
        for f in crop["context"]["fields"]
    )
    value["audience"] = ""
    with pytest.raises(ValidationError):
        preview_brief(value)
    value["audience"] = "A valid audience."
    value["products"][1]["source"]["path"] = ""
    value["products"][2]["source"]["path"] = ""
    with pytest.raises(ValidationError):
        preview_brief(value)


def test_bundled_winter_source_choice_keeps_two_existing_products_visible(ui):
    client, workspace = ui
    receipt = asyncio.run(install_campaigns(workspace))
    campaign = next(
        entry["id"] for entry in receipt["campaigns"] if entry["slug"] == "bridle-and-bough"
    )
    document = authored_edit(client, campaign)
    products = document["authored"]["brief"]["products"]
    missing = next(product for product in products if product["source"]["role"] == "concept")
    missing["source"] = {
        "role": "finished_scene",
        "path": "",
        "provenance": missing["source"]["provenance"],
    }
    saved = client.post(f"/api/motif/campaigns/{campaign}/authored", json=document)
    assert saved.status_code == 200, saved.text
    data = client.get(f"/api/motif/campaigns/{campaign}").json()
    assert data["unplanned_draft"] and data["retry_plan"] is None
    assert data["plan"]["generation_products"] == []
    assert data["manifest"]["metrics"]["completed_images"] == 12
    assert data["manifest"]["metrics"]["expected_images"] == 18
    assert not data["manifest"]["complete"] and data["composition_issues"]
    for product in products:
        if product["id"] == missing["id"]:
            assert not data["editable_products"][product["id"]]
        else:
            assert data["editable_products"][product["id"]]
            assert client.get(data["product_images"][product["id"]]["url"]).status_code == 200
    assert len(read(workspace.storage, campaign)["authored"]["brief"]["products"]) == 3
