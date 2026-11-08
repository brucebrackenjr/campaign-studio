import copy
import hashlib
import io

import pytest
from campaign_fixtures import authored_edit, seed_campaign
from PIL import Image
from test_web_workflow import page_data, sample_plan

from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.sample import write_sample
from campaign_studio.workspace import ActionError

pytest_plugins = ["test_web_workflow"]


def upload(client, storage, reference, campaign):
    response = client.post(
        "/api/uploads",
        content=storage.path(reference).read_bytes(),
        headers={"content-type": "image/png", "X-Campaign-Id": campaign},
    )
    assert response.status_code == 200, response.text
    assert client.get(response.json()["url"]).status_code == 200
    return response.json()["reference"]


@pytest.mark.parametrize("count", [1, 5])
def test_form_intake_owns_ids_assets_and_plan_boundary(ui, count):
    client, workspace = ui
    intake = Intake(workspace.storage)
    campaign, created = intake.new()
    _, fixture = write_sample(workspace.storage)
    imported = fixture.model_dump(mode="json")
    imported["campaign_id"] = campaign
    imported["name"] = "Campaign from forms"
    imported["brand"]["logo_path"] = upload(
        client, workspace.storage, fixture.brand.logo_path, campaign
    )
    uploaded = upload(client, workspace.storage, fixture.products[0].source.path, campaign)
    imported["products"] = [copy.deepcopy(imported["products"][0]) for _ in range(count)]
    for product in imported["products"]:
        product["id"] = "external-id"
        product["source"]["path"] = uploaded
    imported["markets"].pop("es-MX")
    document = intake.save(
        campaign, BriefDraft(value=imported, version=created["version"]), workspace
    )
    products = document["value"]["products"]
    assert len({p["id"] for p in products}) == count
    assert all(p["id"] != "external-id" for p in products)
    planned = client.post(
        f"/api/motif/campaigns/{campaign}/plan", json={"version": document["version"]}
    )
    assert planned.status_code == 200, planned.text
    assert workspace.runs(campaign) == []  # Supplied photos do not need a paid-source run.
    current = client.get(f"/api/motif/campaigns/{campaign}").json()
    assert current["manifest"]["metrics"]["completed_images"] == count * 3
    edit = {
        "ratio": "1:1",
        "revision_id": current["version"],
        "market": "en-US",
        "product_id": products[0]["id"],
        "field_scopes": {"message": "all_products"},
        "values": {"message": "One campaign for every product."},
    }
    preview = client.post(f"/api/campaigns/{campaign}/text/preview", json=edit)
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["images"]) == 1
    saved = client.post(f"/api/campaigns/{campaign}/text/save", json=edit)
    assert saved.status_code == 200, saved.text
    reopened = client.get(f"/api/motif/campaigns/{campaign}").json()
    assert {o["copy"]["message"] for o in reopened["manifest"]["outputs"]} == {
        "One campaign for every product."
    }
    assert len(reopened["manifest"]["outputs"]) == count * 3
    assert not list(workspace.storage.root.glob("Campaigns/**/Outputs"))


def test_upload_and_draft_failures_are_safe_and_keep_draft(ui):
    client, workspace = ui
    intake = Intake(workspace.storage)
    campaign, created = intake.new()
    value = created["value"]
    assert (
        client.post(
            "/api/uploads",
            content=b"not an image",
            headers={"content-type": "image/png", "X-Campaign-Id": campaign},
        ).status_code
        == 422
    )
    files_before = {
        str(path): path.read_bytes()
        for path in workspace.storage.root.rglob("*")
        if path.is_file() and path.name != "state.sqlite3"
    }
    with workspace.storage.connect() as db:
        database_before = list(db.iterdump())
    unsupported = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(unsupported, format="GIF")
    rejected = client.post(
        "/api/uploads",
        content=unsupported.getvalue(),
        headers={"content-type": "image/png", "X-Campaign-Id": campaign},
    )
    assert rejected.status_code == 422
    assert rejected.json()["input_image"]["kind"] == "unsupported_type"
    assert rejected.json()["input_image"]["detected_type"] == "GIF"
    assert files_before == {
        str(path): path.read_bytes()
        for path in workspace.storage.root.rglob("*")
        if path.is_file() and path.name != "state.sqlite3"
    }
    with workspace.storage.connect() as db:
        assert list(db.iterdump()) == database_before
    assert (
        client.post(
            "/api/uploads",
            content=b"x" * (20 * 1024 * 1024 + 1),
            headers={"content-type": "image/png", "X-Campaign-Id": campaign},
        ).status_code
        == 413
    )
    value["brand"]["logo_path"] = "../../.env"
    with pytest.raises(ActionError) as refused:
        intake.save(campaign, BriefDraft(value=value, version=created["version"]), workspace)
    assert refused.value.status == 422
    assert (
        client.post(
            f"/api/motif/campaigns/{campaign}/plan", json={"version": created["version"]}
        ).status_code
        == 422
    )
    assert workspace.runs() == []
    assert (
        page_data(client, client.get(f"/campaigns/{campaign}/campaign"))["intake"]["brand"][
            "logo_path"
        ]
        == ""
    )
    value["brand"]["logo_path"] = ""
    value["name"] = "Edited name"
    saved = intake.save(campaign, BriefDraft(value=value, version=created["version"]), workspace)
    with pytest.raises(ActionError) as stale:
        intake.save(campaign, BriefDraft(value=value, version=created["version"]), workspace)
    assert stale.value.status == 409
    value["products"] = [{}] * 11
    with pytest.raises(ActionError) as oversized:
        intake.save(campaign, BriefDraft(value=value, version=saved["version"]), workspace)
    assert oversized.value.status == 422


def test_actual_png_preview_is_ephemeral_bound_and_matches_saved_compositor(ui):
    client, workspace = ui
    data = sample_plan(client)
    cid = data["campaignId"]
    before = authored_edit(client, cid)
    edit = {
        "ratio": "1:1",
        "revision_id": data["version"],
        "market": "es-MX",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products", "cta": "all_products"},
        "values": data["brief"]["markets"]["es-MX"]["copy"] | {"message": "Para cada día."},
    }
    preview = client.post(f"/api/campaigns/{cid}/text/preview", json=edit)
    assert preview.status_code == 200, preview.text
    images = preview.json()["images"]
    assert len(images) == 1 and not preview.json()["blocked"]
    proposed = client.get(images[0]["url"]).content
    assert authored_edit(client, cid) == before and workspace.runs(cid) == []
    assert (
        client.get(
            images[0]["url"].replace(cid, "014c1210-29a9-50d2-823a-7a936aecd62b")
        ).status_code
        == 404
    )
    saved = client.post(f"/api/campaigns/{cid}/text/save", json=edit)
    assert saved.status_code == 200, saved.text
    version = saved.json()["composition_version"]
    actual = client.get(f"/api/campaigns/{cid}/composition/{version}/download/product-1/es-MX/1x1")
    assert actual.status_code == 200, actual.text
    with (
        Image.open(io.BytesIO(proposed)) as preview_image,
        Image.open(io.BytesIO(actual.content)) as final,
    ):
        assert preview_image.convert("RGBA").tobytes() == final.convert("RGBA").tobytes()
    assert (
        client.get(images[0]["url"]).status_code == 200
    )  # Issued immutable Studio preview finishes decoding.
    assert not list(workspace.storage.root.glob("Campaigns/**/Outputs"))


def test_campaign_autosave_updates_current_images_and_previous_download_remains_independent(ui):
    import json
    from zipfile import ZipFile

    client, workspace = ui
    data = sample_plan(client)
    cid = data["campaignId"]
    first = client.post(f"/api/campaigns/{cid}/delivery/zip", json={"version": data["version"]})
    assert first.status_code == 200, first.text
    original = first.content
    original_hash = hashlib.sha256(original).hexdigest()
    action = authored_edit(client, cid)
    action["authored"]["brief"]["name"] = "Renamed campaign"
    action["authored"]["brief"]["markets"]["en-US"]["copy"]["message"] = "Current campaign wording"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
    assert saved.status_code == 200, saved.text
    current = client.get(f"/api/motif/campaigns/{cid}").json()
    assert current["version"] != data["version"]
    assert {
        o["copy"]["message"] for o in current["manifest"]["outputs"] if o["market"] == "en-US"
    } == {"Current campaign wording"}
    later = client.post(f"/api/campaigns/{cid}/delivery/zip", json={"version": current["version"]})
    assert later.status_code == 200, later.text
    with ZipFile(io.BytesIO(original)) as a, ZipFile(io.BytesIO(later.content)) as b:
        old = json.loads(a.read(next(n for n in a.namelist() if n.endswith("/manifest.json"))))
        new = json.loads(b.read(next(n for n in b.namelist() if n.endswith("/manifest.json"))))
        assert old["campaign_name"] != new["campaign_name"] == "Renamed campaign"
        assert a.testzip() is None and b.testzip() is None
    assert hashlib.sha256(original).hexdigest() == original_hash
    assert not list(workspace.storage.root.rglob("*.zip"))
    assert workspace.runs(cid) == []


def test_catalog_lists_registered_cli_campaigns_without_special_fixture_identity(ui):
    client, workspace = ui
    sample_plan(client)
    assert len(workspace.catalog()) == 1
    assert len(workspace.runs()) == 0
    seeded = seed_campaign(client.app.state.workspace, replay=True)
    entries = workspace.catalog()
    assert len(entries) == 2
    assert (
        next(item for item in entries if item["id"] == seeded)["name"]
        == "HOLD – Make the next move"
    )


def test_import_validates_before_returning_and_never_opens_imported_paths(ui):
    client, workspace = ui
    created = client.post("/api/motif/campaigns", json={"name": "Existing work"}).json()
    campaign = created["campaign_id"]
    before = client.get(f"/api/motif/campaigns/{campaign}").json()["value"]
    _, source = write_sample(workspace.storage)
    catalog_before = workspace.catalog()
    value = source.model_dump(mode="json")
    value["brand"]["palette"] = None
    rejected = client.post("/api/motif/import", json=value)
    assert rejected.status_code == 422
    assert "current draft is unchanged" in rejected.json()["message"]
    assert client.get(f"/api/motif/campaigns/{campaign}").json()["value"] == before
    assert workspace.catalog() == catalog_before
    value = source.model_dump(mode="json")
    value["brand"]["logo_path"] = "/outside/never-open-this.png"
    value["products"][0]["source"]["path"] = "https://example.invalid/image.png"
    imported = client.post("/api/motif/import", json=value)
    assert imported.status_code == 200
    imported_id = imported.json()["campaign_id"]
    assert imported_id not in {campaign, value["campaign_id"]}
    document = client.get(f"/api/motif/campaigns/{imported_id}").json()
    assert document["value"]["brand"]["logo_path"] == ""
    assert all(p["source"]["path"] == "" for p in document["value"]["products"])
    assert client.get(f"/api/motif/campaigns/{campaign}").json()["value"] == before


def test_new_edit_copy_is_retained_when_reopening_and_replanning_brief(ui):
    client, workspace = ui
    data = sample_plan(client)
    cid = data["campaignId"]
    action = authored_edit(client, cid)
    action["authored"]["brief"]["name"] = "A saved brief draft"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
    assert saved.status_code == 200, saved.text
    action["version"] = saved.json()["version"]
    edit = {
        "ratio": "1:1",
        "revision_id": saved.json()["version"],
        "market": "es-MX",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products"},
        "values": {"message": "Conserva esta frase."},
    }
    result = client.post(f"/api/campaigns/{cid}/text/save", json=edit)
    assert result.status_code == 200, result.text
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=action).status_code == 409
    current = authored_edit(client, cid)
    assert current["authored"]["brief"]["name"] == "A saved brief draft"
    assert (
        current["authored"]["brief"]["markets"]["es-MX"]["copy"]["message"]
        == "Conserva esta frase."
    )
    current["authored"]["brief"]["name"] = "Name only edit"
    renamed = client.post(f"/api/motif/campaigns/{cid}/authored", json=current)
    assert renamed.status_code == 200
    planned = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": renamed.json()["version"]}
    )
    assert planned.status_code == 200, planned.text
    assert (
        workspace.current_composition(cid)["plan"].brief.markets["es-MX"].text.message
        == "Conserva esta frase."
    )


@pytest.mark.parametrize("change", ["name", "logo", "text_color", "rule"])
def test_local_brief_changes_adopt_verified_mocked_master_but_scene_change_does_not(
    tmp_path, change
):
    import asyncio

    import httpx
    from test_provider_runner import generated_case

    from campaign_studio.intake import BriefDraft, Intake
    from campaign_studio.provider import OpenAIImages
    from campaign_studio.workspace import Workspace

    state, runner, run_id, body = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    asyncio.run(runner.execute(run_id, provider))
    workspace = Workspace(state)
    intake = Intake(state.storage)
    value = state.plan(run_id).brief.model_dump(mode="json")
    value["name"] = "Name only edit"
    if change == "logo":
        with io.BytesIO() as buffer:
            logo = Image.new("RGBA", (180, 70), (0, 0, 0, 0))
            from PIL import ImageDraw

            ImageDraw.Draw(logo).rectangle((10, 10, 170, 60), fill="white")
            logo.save(buffer, format="PNG")
            value["brand"]["logo_path"] = intake.upload(
                buffer.getvalue(), "image/png", value["campaign_id"]
            )["reference"]
    elif change == "text_color":
        value["brand"]["text_color"] = "#202A36"
        value["brand"]["palette"].append("#202A36")
    elif change == "rule":
        value["markets"]["en-US"]["phrase_rules"] = [
            {
                "id": "",
                "phrase": "test phrase",
            }
        ]
    document = intake.save(
        value["campaign_id"],
        BriefDraft(value=value, version=intake.read(value["campaign_id"])["version"]),
        workspace,
    )
    plan = intake.prepare(value["campaign_id"], document["version"], workspace)
    assert plan.generation_products == []
    new_run = workspace.run_for(value["campaign_id"])
    assert new_run["id"] == run_id and len(state.attempts(run_id)) == 1
    assert plan.assets["masters/product-1"]
    next_value = intake.read(value["campaign_id"])["value"]
    next_value["creative_direction"] = "A changed scene that requires a new request."
    document = intake.save(
        value["campaign_id"],
        BriefDraft(value=next_value, version=document["version"], base_revision=None),
        workspace,
    )
    changed = intake.prepare(value["campaign_id"], document["version"], workspace)
    assert len(changed.generation_products) == 1
    assert len(state.attempts(run_id)) == 1  # No second paid request was dispatched.


def test_concurrent_same_version_brief_saves_cannot_both_win(ui):
    from concurrent.futures import ThreadPoolExecutor

    client, workspace = ui
    campaign, created = Intake(workspace.storage).new()

    def attempt(name):
        value = copy.deepcopy(created["value"])
        value["name"] = name
        try:
            return Intake(workspace.storage).save(
                campaign, BriefDraft(value=value, version=created["version"]), workspace
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, ["First writer", "Second writer"]))
    assert sum(result is not None for result in results) == 1
    actual = Intake(workspace.storage).read(campaign)
    assert actual == next(result for result in results if result is not None)


def test_preview_expiry_and_source_integrity_never_change_saved_state(ui):
    client, workspace = ui
    data = sample_plan(client)
    cid = data["campaignId"]
    before = authored_edit(client, cid)
    edit = {
        "ratio": "1:1",
        "revision_id": data["version"],
        "market": "en-US",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products"},
        "values": {"message": "A local preview"},
    }
    response = client.post(f"/api/campaigns/{cid}/text/preview", json=edit)
    assert response.status_code == 200, response.text
    previews = client.app.state.previews
    previews.lifetime = 0
    assert client.get(response.json()["images"][0]["url"]).status_code == 404
    assert previews.entries == {}
    asset = workspace.current_composition(cid)["plan"].assets["products/product-1"]
    workspace.storage.path(asset.reference).write_bytes(b"changed source")
    rejected = client.post(f"/api/campaigns/{cid}/text/preview", json=edit)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["blocked"] and rejected.json()["images"] == []
    assert authored_edit(client, cid) == before and workspace.runs(cid) == []


def test_oversized_pixel_header_is_rejected_before_decode(ui):
    import struct
    import zlib

    client, workspace = ui
    campaign = client.post("/api/motif/campaigns", json={"name": "Oversized image"}).json()[
        "campaign_id"
    ]
    with io.BytesIO() as buffer:
        Image.new("RGB", (1, 1)).save(buffer, format="PNG")
        content = bytearray(buffer.getvalue())
    content[16:24] = struct.pack(">II", 100000, 100000)
    content[29:33] = struct.pack(">I", zlib.crc32(content[12:29]) & 0xFFFFFFFF)
    response = client.post(
        "/api/uploads",
        content=bytes(content),
        headers={"content-type": "image/png", "X-Campaign-Id": campaign},
    )
    assert response.status_code == 422
    assert workspace.runs() == []


def test_import_names_unsupported_keys_without_echoing_values(ui):
    client, workspace = ui
    _, brief = write_sample(workspace.storage)
    value = brief.model_dump(mode="json")
    value["unrecognized_setting"] = {"private": "SECRET_VALUE_NOT_FOR_ERROR"}
    value["products"][0]["source"]["unknown_image_option"] = "ANOTHER_PRIVATE_VALUE"
    response = client.post("/api/motif/import", json=value)
    assert response.status_code == 422
    issues = response.json()["field_errors"]
    root = next(item for item in issues if "unrecognized_setting" in item["message"])
    nested = next(item for item in issues if "unknown_image_option" in item["message"])
    assert root["path"] == [] and root["code"] == "unsupported_field"
    assert nested["path"] == ["products", 0, "source"]
    assert nested["product_index"] == 0 and "Product 1" in nested["label"]
    assert "SECRET_VALUE_NOT_FOR_ERROR" not in response.text
    assert "ANOTHER_PRIVATE_VALUE" not in response.text
    assert workspace.runs() == []
