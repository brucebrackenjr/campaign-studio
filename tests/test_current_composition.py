"""Current editing metadata persists independently of previews and final delivery."""

import asyncio
import copy
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_provider_runner import generated_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Framing
from campaign_studio.campaign_drafts import read, write
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.motif import campaign_data
from campaign_studio.provider import OpenAIImages
from campaign_studio.sample import write_sample
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.workspace import ActionError, FrameEdit, TextEdit, Workspace


@pytest.fixture
def current(tmp_path):
    storage = Storage(tmp_path / "workspace")
    _, brief = write_sample(storage, 2, True)
    state = State(storage)
    write(
        storage,
        brief.campaign_id,
        {
            "value": brief.model_dump(mode="json"),
            "authored": {"brief": brief.model_dump(mode="json")},
            "composition_schema": 1,
            "version": uuid4().hex,
        },
    )
    return Workspace(state), brief.campaign_id


def save_campaign(ws, cid, mutate):
    project = AuthoredProjects(ws)
    current = project.read(cid)
    value = copy.deepcopy(current["authored"])
    mutate(value["brief"])
    return project.save(
        cid, AuthoredSave(version=current["version"], base=current["base"], authored=value)
    )


def test_supplied_sources_open_studio_without_production_or_final_png(current):
    ws, cid = current
    data = campaign_data(ws, cid)
    assert data["can_edit"] and data["manifest"]["complete"]
    assert data["output_run_id"] is None
    assert data["manifest"]["preview_only"]
    assert Intake(ws.storage).prepare(cid, data["version"], ws).generation_products == []
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs/**/*.png"))


def test_campaign_copy_brand_and_rules_are_current_without_an_apply(current):
    ws, cid = current
    save_campaign(
        ws, cid, lambda b: b["markets"]["en-US"]["copy"].update(message="Current headline")
    )
    first = campaign_data(ws, cid)
    assert {o["copy"]["message"] for o in first["manifest"]["outputs"]} == {"Current headline"}
    save_campaign(ws, cid, lambda b: b["brand"].update(text_color=b["brand"]["palette"][0]))
    save_campaign(
        ws,
        cid,
        lambda b: b["markets"]["en-US"].update(
            phrase_rules=[
                {
                    "id": "rule-test",
                    "phrase": "Current",
                    "match": "literal",
                    "case_sensitive": False,
                }
            ]
        ),
    )
    assert not campaign_data(ws, cid)["manifest"]["complete"]
    save_campaign(ws, cid, lambda b: b["markets"]["en-US"].update(phrase_rules=[]))
    assert campaign_data(ws, cid)["manifest"]["complete"]
    restarted = Workspace(State(ws.storage))
    assert (
        campaign_data(restarted, cid)["output_brief"]["markets"]["en-US"]["copy"]["message"]
        == "Current headline"
    )


def test_150_studio_saves_do_not_render_or_accumulate(current, monkeypatch):
    ws, cid = current
    before = {
        str(p.relative_to(ws.storage.root)) for p in ws.storage.root.rglob("*") if p.is_file()
    }
    monkeypatch.setattr(
        "campaign_studio.compositions.make_plan",
        lambda *a, **k: pytest.fail("Metadata save must not make a render plan"),
    )
    for index in range(150):
        doc = read(ws.storage, cid)
        edit = FrameEdit(
            revision_id=doc["version"],
            product_id=doc["authored"]["brief"]["products"][0]["id"],
            market="en-US",
            ratio="1:1",
            framing=Framing(zoom=1 + index / 200),
        )
        version = ws.save_composition(cid, edit)
        assert ws.save_composition(cid, edit) == version
    after = {str(p.relative_to(ws.storage.root)) for p in ws.storage.root.rglob("*") if p.is_file()}
    assert before == after
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        stored = json.loads(db.execute("SELECT document_json FROM campaign_drafts").fetchone()[0])
        assert "value" not in stored
        assert "plan" not in stored
        assert (
            stored["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.745
        )


def test_stale_tab_and_failed_database_save_preserve_current_metadata(current, monkeypatch):
    ws, cid = current
    doc = read(ws.storage, cid)
    edit = FrameEdit(
        revision_id=doc["version"],
        product_id=doc["authored"]["brief"]["products"][0]["id"],
        market="en-US",
        ratio="1:1",
        framing=Framing(zoom=1.2),
    )
    ws.save_composition(cid, edit)
    with pytest.raises(ActionError, match="another tab"):
        ws.save_composition(cid, edit.model_copy(update={"framing": Framing(zoom=1.3)}))
    current = read(ws.storage, cid)
    monkeypatch.setattr(
        "campaign_studio.compositions.write_draft",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(OSError):
        ws.save_composition(cid, edit.model_copy(update={"revision_id": current["version"]}))
    assert read(ws.storage, cid) == current


def test_incomplete_studio_text_is_saved_and_can_be_corrected(current):
    ws, cid = current
    doc = read(ws.storage, cid)
    edit = TextEdit(
        revision_id=doc["version"],
        product_id=doc["authored"]["brief"]["products"][0]["id"],
        market="en-US",
        ratio="1:1",
        values={"message": ""},
        field_scopes={"message": "product"},
    )
    version = ws.save_composition(cid, edit)
    assert campaign_data(ws, cid)["composition_issues"]
    ws.save_composition(
        cid,
        edit.model_copy(update={"revision_id": version, "values": {"message": "Corrected text"}}),
    )
    assert campaign_data(ws, cid)["manifest"]["complete"]


def test_generated_source_production_stops_before_final_pngs(tmp_path):
    state, runner, run_id, response = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    provider = OpenAIImages("synthetic-test-key", transport=httpx.MockTransport(transport))
    result = asyncio.run(runner.execute(run_id, provider))
    assert result["complete"] and len(calls) == 1
    ws = Workspace(state)
    cid = state.run(run_id)["campaign_id"]
    assert campaign_data(ws, cid)["manifest"]["complete"]
    assert not list(state.storage.root.glob("Campaigns/**/Outputs/**/*.png"))
    save_campaign(ws, cid, lambda b: b["markets"]["en-US"]["copy"].update(message="Local edit"))
    assert not ws.current_composition(cid)["plan"].generation_products
    assert len(calls) == 1
    save_campaign(
        ws,
        cid,
        lambda b: b.update(
            creative_direction="A different scene, requiring a new confirmed source request."
        ),
    )
    assert ws.current_composition(cid)["plan"].generation_products
    assert len(calls) == 1


def test_current_api_preview_save_and_export(current, monkeypatch):
    ws, cid = current
    app = create_app(Settings(data_dir=ws.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        data = client.get(f"/api/motif/campaigns/{cid}").json()
        assert data["can_edit"]
        headers = {"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"}
        product = data["output_brief"]["products"][0]["id"]
        body = {
            "revision_id": data["version"],
            "product_id": product,
            "market": "en-US",
            "ratio": "1:1",
            "framing": {"zoom": 1.1},
        }
        with monkeypatch.context() as patch:
            patch.setattr(
                "campaign_studio.compositions.make_plan",
                lambda *a, **k: pytest.fail(
                    "A Studio save and acknowledgement must not inspect sources "
                    "or build a render plan"
                ),
            )
            saved = client.post(f"/api/campaigns/{cid}/framing/save", json=body, headers=headers)
        assert saved.status_code == 200, saved.text
        current = saved.json()
        slot = current["composition_slots"][0]
        preview = client.get(
            f"/api/campaigns/{cid}/composition/{current['version']}/images/{slot['id']}"
        )
        assert preview.status_code == 200, preview.text
        assert preview.content.startswith(b"\x89PNG")
        download = client.post(
            f"/api/campaigns/{cid}/delivery/zip",
            json={"version": current["version"]},
            headers=headers,
        )
        assert download.status_code == 200, download.text
        assert download.content.startswith(b"PK")
        assert not list(ws.storage.root.glob("Campaigns/**/Outputs/**/*.png"))


def test_description_persists_through_editing_and_lost_acknowledgement(current):
    from campaign_studio.compositions import save_metadata
    from campaign_studio.review import CreativeUpdate

    ws, cid = current
    first = campaign_data(ws, cid)
    creative = first["manifest"]["outputs"][0]["id"]
    action = CreativeUpdate(expected_version=0, description="A carefully described product.")
    saved = save_metadata(ws, cid, first["version"], creative, action)
    retried = save_metadata(ws, cid, first["version"], creative, action)
    assert retried["version"] == saved["version"]
    save_campaign(ws, cid, lambda b: b["markets"]["en-US"]["copy"].update(message="New headline"))
    save_campaign(ws, cid, lambda b: b["brand"].update(text_color=b["brand"]["palette"][0]))
    data = campaign_data(ws, cid)
    ws.save_composition(
        cid,
        FrameEdit(
            revision_id=data["version"],
            product_id=creative.split("/")[0],
            market="en-US",
            ratio="1:1",
            framing=Framing(zoom=1.1),
        ),
    )
    description = campaign_data(ws, cid)["metadata"]["creatives"][creative]
    assert description["description"] == action.description and description["reset_reason"] is None
    save_campaign(ws, cid, lambda b: b["products"][0]["source"].update(path=""))
    document = read(ws.storage, cid)
    assert document["creative_metadata"][creative]["description"] == action.description
    assert document["creative_metadata"][creative]["reset_reason"] == "source_changed"


def test_nonimagery_campaign_changes_do_not_request_a_new_generated_source(tmp_path):
    state, runner, run, response = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    )
    result = asyncio.run(runner.execute(run, provider))
    cid = result["campaign_id"]
    ws = Workspace(state)
    save_campaign(ws, cid, lambda b: b["products"][0].update(description="A catalog description."))
    save_campaign(
        ws, cid, lambda b: b["products"][0]["source"]["provenance"].update(rights="Known rights.")
    )
    snapshot = ws.current_composition(cid)
    assert not snapshot["plan"].generation_products
    assert snapshot["plan"].cost_estimate["estimated_usd"] == 0
    assert len(state.attempts(run)) == 1


@pytest.mark.parametrize("market", ["en-US", "es-MX"])
@pytest.mark.parametrize("shared", [False, True])
def test_blank_headline_preserves_editable_sources_but_blocks_every_delivery(
    current, market, shared
):
    ws, cid = current
    save_campaign(
        ws, cid, lambda b: b["markets"].update({"es-MX": copy.deepcopy(b["markets"]["en-US"])})
    )
    original = campaign_data(ws, cid)
    pid = original["output_brief"]["products"][0]["id"]

    def blank(brief):
        if shared:
            brief["markets"][market]["copy"]["message"] = ""
        else:
            brief["products"][0].setdefault("copy_overrides", {}).setdefault(market, {})[
                "message"
            ] = ""

    save_campaign(ws, cid, blank)
    data = campaign_data(ws, cid)
    assert data["composition_issues"]
    assert not data["manifest"]["complete"]
    assert data["editable_products"] == original["editable_products"]
    assert set(data["source_urls"]) == set(original["source_urls"])
    assert len(data["manifest"]["outputs"]) == len(original["manifest"]["outputs"])
    for output in data["manifest"]["outputs"]:
        before = next(o for o in original["manifest"]["outputs"] if o["id"] == output["id"])
        if output["market"] == market and (shared or output["product_id"] == pid):
            assert output["copy"]["message"] == ""
        else:
            assert output["copy"] == before["copy"]
    app = create_app(Settings(data_dir=ws.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"}
        version = data["version"]
        # Incomplete wording remains visible and editable in both markets.
        for language in ["en-US", "es-MX"]:
            response = client.get(
                f"/api/campaigns/{cid}/composition/{version}/images/{pid}/{language}/1x1"
            )
            assert response.status_code == 200, response.text
        for partial in [False, True]:
            for endpoint in ["zip", "report-preview"]:
                response = client.post(
                    f"/api/campaigns/{cid}/delivery/{endpoint}",
                    json={"version": version, "allow_partial": partial},
                    headers=headers,
                )
                assert response.status_code == 422, response.text
        for language in ["en-US", "es-MX"]:
            response = client.get(
                f"/api/campaigns/{cid}/composition/{version}/download/{pid}/{language}/1x1"
            )
            assert response.status_code == 422, response.text
    assert campaign_data(ws, cid)["editable_products"][pid]
    # Correction retains the same source ownership and retires the incomplete issue.
    if shared:
        save_campaign(
            ws, cid, lambda b: b["markets"][market]["copy"].update(message="Corrected headline")
        )
    else:
        ws.save_composition(
            cid,
            TextEdit(
                revision_id=data["version"],
                product_id=pid,
                market=market,
                ratio="1:1",
                values={"message": "Corrected headline"},
                field_scopes={"message": "product"},
            ),
        )
    corrected = campaign_data(ws, cid)
    assert not corrected["composition_issues"]
    assert corrected["manifest"]["complete"]
    assert set(corrected["source_urls"]) == set(original["source_urls"])


@pytest.mark.parametrize("market", ["en-US", "es-MX"])
@pytest.mark.parametrize("shared", [False, True])
def test_blank_headline_projection_isolates_other_incomplete_product(current, market, shared):
    ws, cid = current
    save_campaign(
        ws, cid, lambda b: b["markets"].update({"es-MX": copy.deepcopy(b["markets"]["en-US"])})
    )
    original = campaign_data(ws, cid)
    retained = original["output_brief"]["products"][0]["id"]
    excluded = original["output_brief"]["products"][1]["id"]

    def incomplete(brief):
        brief["products"][1]["name"] = ""
        if shared:
            brief["markets"][market]["copy"]["message"] = ""
        else:
            brief["products"][0].setdefault("copy_overrides", {}).setdefault(market, {})[
                "message"
            ] = ""

    save_campaign(ws, cid, incomplete)
    data = campaign_data(ws, cid)
    assert data["editable_products"][retained]
    assert not data["editable_products"][excluded]
    assert len(data["manifest"]["outputs"]) == 6
    assert not data["manifest"]["complete"]
    assert len(data["composition_issues"]) == 2
    for output in data["manifest"]["outputs"]:
        assert output["product_id"] == retained
        if output["market"] == market:
            assert output["copy"]["message"] == ""
    # The projection has no authority to bypass the strict current document.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ws.current_composition(cid)
    assert read(ws.storage, cid)["authored"]["brief"]["products"][1]["name"] == ""
    save_campaign(ws, cid, lambda b: b["products"][1].update(name="Restored product"))
    recovered = campaign_data(ws, cid)
    assert len(recovered["manifest"]["outputs"]) == 12
    assert recovered["editable_products"][excluded]
    assert len(recovered["composition_issues"]) == 1


@pytest.mark.parametrize("market", ["en-US", "es-MX"])
@pytest.mark.parametrize("scope", ["product", "all_products"])
def test_text_post_preview_displays_empty_headline_without_saving_or_admitting_delivery(
    current, market, scope
):
    from pydantic import ValidationError

    ws, cid = current
    save_campaign(
        ws, cid, lambda b: b["markets"].update({"es-MX": copy.deepcopy(b["markets"]["en-US"])})
    )
    original = read(ws.storage, cid)
    pid = original["authored"]["brief"]["products"][0]["id"]
    body = {
        "revision_id": original["version"],
        "product_id": pid,
        "market": market,
        "ratio": "1:1",
        "values": {"message": ""},
        "field_scopes": {"message": scope},
    }
    app = create_app(Settings(data_dir=ws.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"}
        response = client.post(f"/api/campaigns/{cid}/text/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        proposed = response.json()
        assert proposed["composition_issues"]
        assert not proposed["blocked"]
        assert "message" not in proposed["geometry"]["text_blocks"]
        assert "cta" in proposed["geometry"]["text_blocks"]
        assert proposed["images"]
        selected = next(
            image for image in proposed["images"] if image["id"] == f"{pid}/{market}/1x1"
        )
        assert "message" not in selected["layers"]["text_blocks"]
        assert "cta" in selected["layers"]["text_blocks"]
        assert client.get(selected["url"]).status_code == 200
        assert read(ws.storage, cid) == original, (
            "a preview must not save source or copy instructions"
        )
        with pytest.raises(ValidationError):
            ws.text(cid, TextEdit(**body))
        saved = client.post(f"/api/campaigns/{cid}/text/save", json=body, headers=headers)
        assert saved.status_code == 200, saved.text
        version = saved.json()["version"]
        after = client.post(
            f"/api/campaigns/{cid}/text/preview",
            json={**body, "revision_id": version},
            headers=headers,
        )
        assert after.status_code == 200, after.text
        assert "message" not in after.json()["geometry"]["text_blocks"]
        other = "es-MX" if market == "en-US" else "en-US"
        current_data = campaign_data(ws, cid)
        assert all(
            output["copy"]["message"]
            for output in current_data["manifest"]["outputs"]
            if output["market"] == other
        )
        for partial in [False, True]:
            refused = client.post(
                f"/api/campaigns/{cid}/delivery/zip",
                json={"version": version, "allow_partial": partial},
                headers=headers,
            )
            assert refused.status_code == 422, refused.text


@pytest.mark.parametrize(
    ("path", "invalid"),
    [
        ([], None),
        ([], "unfinished brief"),
        ([], []),
        ([], {}),
        (["markets"], None),
        (["markets"], "unfinished markets"),
        (["markets"], []),
        (["markets"], {}),
        (["markets", "en-US"], None),
        (["markets", "en-US"], "unfinished market"),
        (["markets", "en-US"], []),
        (["markets", "en-US"], {}),
        (["markets", "en-US", "copy"], None),
        (["markets", "en-US", "copy"], "unfinished copy"),
        (["markets", "en-US", "copy"], []),
        (["markets", "en-US", "copy"], {}),
        (["products"], None),
        (["products"], "not yet a list"),
        (["products"], {}),
        (["products"], []),
        (["products", 0], None),
        (["products", 0], "unfinished product"),
        (["products", 0], []),
        (["products", 0], {}),
        (["products", 0, "copy_overrides"], None),
        (["products", 0, "copy_overrides"], "unfinished overrides"),
        (["products", 0, "copy_overrides"], []),
        (["products", 0, "copy_overrides", "en-US"], None),
        (["products", 0, "copy_overrides", "en-US"], "unfinished override"),
        (["products", 0, "copy_overrides", "en-US"], []),
        (["products", 0, "copy_overrides", "en-US"], {"message": None}),
    ],
)
def test_malformed_preview_containers_remain_validation_errors_without_image_reads(
    current, monkeypatch, path, invalid
):
    from pydantic import ValidationError

    from campaign_studio.compositions import preview_brief

    ws, cid = current
    document = read(ws.storage, cid)
    value = copy.deepcopy(document["authored"]["brief"])
    # One product makes malformed product containers unprojectable while still
    # exercising the supported empty-headline projection alongside each shape.
    value["products"] = value["products"][:1]
    value["markets"]["en-US"]["copy"]["message"] = ""
    value["products"][0]["copy_overrides"] = {"en-US": {"message": ""}}
    if path:
        target = value
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = copy.deepcopy(invalid)
    else:
        value = copy.deepcopy(invalid)
    original = copy.deepcopy(value)
    monkeypatch.setattr(
        "campaign_studio.compositions.load_image",
        lambda *args, **kwargs: pytest.fail("Malformed preview containers must not read images."),
    )
    monkeypatch.setattr(
        "campaign_studio.imaging.load_image",
        lambda *args, **kwargs: pytest.fail("Malformed preview containers must not read images."),
    )
    with pytest.raises(ValidationError):
        preview_brief(value)
    assert value == original
    assert read(ws.storage, cid) == document


@pytest.mark.parametrize("field", ["message", "cta"])
@pytest.mark.parametrize("scope", ["product", "all_products"])
@pytest.mark.parametrize("other_blank", [False, True])
def test_preview_refuses_over_limit_target_without_isolating_it(current, field, scope, other_blank):
    from campaign_studio.text_limits import LIMITS

    ws, cid = current
    if other_blank:
        save_campaign(
            ws,
            cid,
            lambda b: (
                b["products"][1].setdefault("copy_overrides", {}).update({"en-US": {"message": ""}})
            ),
        )
    original = read(ws.storage, cid)
    limit = LIMITS["headline" if field == "message" else "cta"]
    body = {
        "revision_id": original["version"],
        "product_id": original["authored"]["brief"]["products"][0]["id"],
        "market": "en-US",
        "ratio": "1:1",
        "values": {field: "X" * (limit + 1)},
        "field_scopes": {field: scope},
    }
    app = create_app(Settings(data_dir=ws.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"}
        endpoint = f"/api/campaigns/{cid}/text"
        refused = client.post(endpoint + "/preview", json=body, headers=headers)
        assert refused.status_code == 422, refused.text
        assert any(
            issue["code"] == "text_too_long"
            and issue["path"][-1] == field
            and str(limit) in issue["message"]
            for issue in refused.json()["field_errors"]
        )
        assert read(ws.storage, cid) == original
        corrected = client.post(
            endpoint + "/preview",
            json={**body, "values": {field: "Corrected wording"}},
            headers=headers,
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["images"]
        assert corrected.json()["geometry"]["text_blocks"][field]
        assert read(ws.storage, cid) == original
