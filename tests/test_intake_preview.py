import copy
import hashlib
import io
from pathlib import Path

import pytest
from campaign_fixtures import authored_edit, seed_campaign
from PIL import Image
from test_web_workflow import page_data, sample_plan, start, wait_for_work

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
    assert workspace.runs() == []
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
    assert document["value"]["campaign_id"] == campaign
    assert len({p["id"] for p in document["value"]["products"]}) == count
    assert all(p["id"] != "external-id" for p in document["value"]["products"])
    assert workspace.runs() == []
    assert page_data(client, client.get(f"/campaigns/{campaign}/campaign"))["editingBrief"]
    planned = client.post(
        f"/api/motif/campaigns/{campaign}/plan", json={"version": document["version"]}
    )
    assert planned.status_code == 200, planned.text
    data = page_data(client, client.get(f"/campaigns/{campaign}/production"))
    assert len(data["brief"]["products"]) == count
    assert data["allowance"] == 0
    assert len(workspace.state.plan(data["runId"]).outputs) == count * 3
    assert workspace.state.attempts(data["runId"]) == []
    assert len(workspace.runs()) == 1
    start(client, data)
    completed = wait_for_work(client, campaign)
    assert completed["completed"] == count * 3 and completed["attempted"] == 0
    if count == 5:
        current = page_data(client, client.get(f"/campaigns/{campaign}/studio"))
        selected = [p["id"] for p in current["brief"]["products"][-2:]]
        edit = {
            "ratio": "1:1",
            "run_id": current["runId"],
            "revision_id": current["currentRevision"],
            "market": "en-US",
            "product_id": current["brief"]["products"][0]["id"],
            "field_scopes": {"message": "all_products", "cta": "all_products"},
            "values": current["brief"]["markets"]["en-US"]["copy"]
            | {"message": "One campaign for every product."},
        }
        preview = client.post(f"/api/campaigns/{campaign}/text/preview", json=edit)
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["impact"]["render_outputs"]) == 15
        assert len(preview.json()["images"]) == 1
        assert {item["id"].split("/")[0] for item in preview.json()["images"]} == {
            edit["product_id"]
        }
        saved = client.post(
            f"/api/campaigns/{campaign}/text/apply",
            json=edit | {"preview_digest": preview.json()["digest"]},
        )
        assert saved.status_code == 200 and saved.json()["complete"]
        assert (
            client.get(f"/api/motif/campaigns/{campaign}").json()["manifest"]["metrics"][
                "completed_images"
            ]
            == 15
        )
        assert workspace.state.attempts(current["runId"]) == []

        response = client.get(f"/campaigns/{campaign}/production?products=5")
        reopened = page_data(client, response)
        assert len(reopened["brief"]["products"]) == 5
        assert [p["id"] for p in reopened["brief"]["products"][-2:]] == selected
        assert len(reopened["manifest"]["outputs"]) == 15


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
    start(client, data)
    wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    before = page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio"))
    run = workspace.run_for("80d58cbe-5e57-5544-9b07-a4f018714dde")
    old_plan = workspace.state.plan(run["id"]).model_dump_json()
    revisions = workspace.revisions(run["id"])
    edit = {
        "ratio": "1:1",
        "run_id": run["id"],
        "revision_id": run["current_revision"],
        "market": "es-MX",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products", "cta": "all_products"},
        "values": before["brief"]["markets"]["es-MX"]["copy"] | {"message": "Para cada día."},
    }
    response = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/preview", json=edit
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert len(preview["images"]) == 1
    assert not preview["blocked"]
    rendered = {item["id"]: client.get(item["url"]).content for item in preview["images"]}
    for content in rendered.values():
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            assert image.format == "PNG"
    assert workspace.revisions(run["id"]) == revisions
    assert workspace.state.plan(run["id"]).model_dump_json() == old_plan
    assert workspace.state.attempts(run["id"]) == []
    assert (
        client.get(
            preview["images"][0]["url"].replace(
                "80d58cbe-5e57-5544-9b07-a4f018714dde", "014c1210-29a9-50d2-823a-7a936aecd62b"
            )
        ).status_code
        == 404
    )
    saved = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/apply",
        json=edit | {"preview_digest": preview["digest"]},
    ).json()
    assert saved["complete"]
    for output in client.get(f"/api/motif/campaigns/{before['campaignId']}").json()["manifest"][
        "outputs"
    ]:
        if output["id"] in rendered:
            with (
                Image.open(io.BytesIO(client.get(output["url"]).content)) as saved_image,
                Image.open(io.BytesIO(rendered[output["id"]])) as draft_image,
            ):
                assert saved_image.size == draft_image.size
                assert (
                    saved_image.convert("RGBA").tobytes() == draft_image.convert("RGBA").tobytes()
                )
    assert client.get(preview["images"][0]["url"]).status_code == 404
    assert workspace.state.attempts(run["id"]) == []


def test_brief_draft_does_not_replace_existing_images_and_previous_export_stays_available(ui):
    client, workspace = ui
    data = sample_plan(client)
    start(client, data)
    wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    before = page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio"))
    value = copy.deepcopy(before["brief"])
    value["name"] = "Renamed campaign"
    action = authored_edit(client, value["campaign_id"])
    action["authored"]["brief"] = value
    draft = client.post(
        "/api/motif/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/authored", json=action
    )
    assert draft.status_code == 200, draft.text
    assert (
        workspace.run_for("80d58cbe-5e57-5544-9b07-a4f018714dde")["current_revision"]
        == before["currentRevision"]
    )
    plan = client.post(
        "/api/motif/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/plan",
        json={"version": draft.json()["version"]},
    )
    assert plan.status_code == 200, plan.text
    previous = client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/handoff")
    page = page_data(client, previous)
    assert page["manifest"]["revision_id"] == before["currentRevision"]
    assert all(client.get(item["url"]).status_code == 200 for item in page["manifest"]["outputs"])
    exported = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export",
        json={
            "run_id": page["runId"],
            "revision_id": page["currentRevision"],
            "allow_partial": False,
        },
    )
    assert exported.status_code == 200, exported.text
    assert exported.json()["included"] == 12
    package = exported.json()
    package_root = Path(package["location"])
    package_hashes = {
        str(path.relative_to(package_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package_root.rglob("*")
        if path.is_file()
    }
    overview = client.get(package["url"])
    assert overview.status_code == 200
    assert len(workspace.runs("80d58cbe-5e57-5544-9b07-a4f018714dde")) == 2
    current_plan = page_data(
        client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/production")
    )
    start(client, current_plan)
    complete = wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    assert complete["completed"] == 12 and complete["elapsed_seconds"] > 0
    current = page_data(
        client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio")
    )
    assert current["currentRevision"] != before["currentRevision"]
    assert all(
        client.get(item["url"]).status_code == 200 for item in current["manifest"]["outputs"]
    )
    for item in before["manifest"]["outputs"]:
        response = client.get(item["url"])
        assert response.status_code == 410
        assert "earlier exported package" in response.text
        assert "Open a retained version" not in response.text
        assert (
            'href="/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio">Open current images</a>'
            in response.text
        )
    unknown = client.get("/campaigns/not-a-campaign/images/unknown/product/en-US/1x1")
    assert unknown.status_code == 404
    assert 'href="/">Back to workbench</a>' in unknown.text
    assert client.get(package["url"]).content == overview.content
    assert package_hashes == {
        str(path.relative_to(package_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in package_root.rglob("*")
        if path.is_file()
    }
    assert "English (United States)" in overview.text and "Instagram post" in overview.text
    assert "<h3>product-1" not in overview.text


def test_catalog_lists_registered_cli_campaigns_without_special_fixture_identity(ui):
    client, workspace = ui
    sample_plan(client)
    assert len(workspace.catalog()) == 1
    assert len(workspace.runs()) == 1
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
    campaign = data["campaignId"]
    start(client, data)
    wait_for_work(client, campaign)
    first = page_data(client, client.get(f"/campaigns/{campaign}/studio"))
    action = authored_edit(client, campaign)
    action["authored"]["brief"]["name"] = "A saved brief draft"
    document = client.post(f"/api/motif/campaigns/{campaign}/authored", json=action)
    assert document.status_code == 200, document.text
    action["version"] = document.json()["version"]
    edit = {
        "ratio": "1:1",
        "run_id": first["runId"],
        "revision_id": first["currentRevision"],
        "market": "es-MX",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products", "cta": "all_products"},
        "values": first["brief"]["markets"]["es-MX"]["copy"] | {"message": "Conserva esta frase."},
    }
    preview = client.post(f"/api/campaigns/{campaign}/text/preview", json=edit).json()
    saved = client.post(
        f"/api/campaigns/{campaign}/text/apply",
        json=edit | {"preview_digest": preview["digest"]},
    )
    assert saved.status_code == 200, saved.text
    stale = client.post(f"/api/motif/campaigns/{campaign}/authored", json=action)
    assert stale.status_code == 409
    reopened = page_data(client, client.get(f"/campaigns/{campaign}/campaign"))
    assert reopened["intake"]["name"] == "A saved brief draft"
    assert reopened["intake"]["markets"]["es-MX"]["copy"]["message"] == "Conserva esta frase."
    action = authored_edit(client, campaign)
    assert action["base"]["revision_id"] == saved.json()["revision_id"]
    action["authored"]["brief"]["name"] = "Name only edit"
    document = client.post(f"/api/motif/campaigns/{campaign}/authored", json=action)
    assert document.status_code == 200, document.text
    planned = client.post(
        f"/api/motif/campaigns/{campaign}/plan", json={"version": document.json()["version"]}
    )
    assert planned.status_code == 200, planned.text
    current = workspace.state.plan(workspace.run_for(campaign)["id"])
    assert current.brief.markets["es-MX"].text.message == "Conserva esta frase."


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
    manifest = asyncio.run(runner.execute(run_id, provider))
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
            value["brand"]["logo_path"] = intake.upload(buffer.getvalue(), "image/png")["reference"]
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
        BriefDraft(value=value, base_revision=manifest["revision_id"]),
        workspace,
    )
    plan = intake.prepare(value["campaign_id"], document["version"], workspace)
    assert plan.generation_products == []
    new_run = workspace.run_for(value["campaign_id"])
    assert new_run["allowance"] == 0 and state.attempts(new_run["id"]) == []
    assert plan.brief.replay.sources
    next_value = intake.read(value["campaign_id"])["value"]
    next_value["creative_direction"] = "A changed scene that requires a new request."
    document = intake.save(
        value["campaign_id"],
        BriefDraft(value=next_value, version=document["version"], base_revision=None),
        workspace,
    )
    changed = intake.prepare(value["campaign_id"], document["version"], workspace)
    assert len(changed.generation_products) == 1
    assert state.attempts(workspace.run_for(value["campaign_id"])["id"]) == []


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
    start(client, data)
    wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    page = page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio"))
    edit = {
        "ratio": "1:1",
        "run_id": page["runId"],
        "revision_id": page["currentRevision"],
        "market": "en-US",
        "product_id": "product-1",
        "field_scopes": {"message": "all_products", "cta": "all_products"},
        "values": page["brief"]["markets"]["en-US"]["copy"] | {"message": "A local preview"},
    }
    response = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/preview", json=edit
    )
    assert response.status_code == 200
    previews = client.app.state.previews
    previews.lifetime = 0
    assert client.get(response.json()["images"][0]["url"]).status_code == 404
    assert previews.entries == {}
    asset = workspace.state.plan(page["runId"]).assets["products/product-1"]
    workspace.storage.path(asset.reference).write_bytes(b"changed source")
    rejected = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/text/preview", json=edit
    )
    assert rejected.status_code == 409
    assert workspace.state.run(page["runId"])["current_revision"] == page["currentRevision"]
    assert len(workspace.revisions(page["runId"])) == 1
    assert workspace.state.attempts(page["runId"]) == []


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
