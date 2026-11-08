import asyncio
import copy
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient
from PIL import Image
from test_provider_runner import generated_case

from campaign_studio import authored as authored_module
from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.campaign_drafts import read as read_draft
from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.planning import canonical_hash
from campaign_studio.provider import OpenAIImages
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage
from campaign_studio.validation import field_issue
from campaign_studio.workspace import ActionError, TextEdit, Workspace


@pytest.fixture
def project(tmp_path):
    settings = Settings(data_dir=tmp_path / "runtime")
    app = create_app(
        settings,
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Saving authored work must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        cid = client.post("/api/motif/campaigns", json={"name": "Authored work"}).json()[
            "campaign_id"
        ]
        yield client, app.state.workspace, cid


def action(client, cid):
    value = client.get(f"/api/motif/campaigns/{cid}/authored").json()
    return {key: value[key] for key in ("version", "base", "authored")}


def files(storage):
    return {
        p.relative_to(storage.root).as_posix(): p.read_bytes()
        for p in storage.root.rglob("*")
        if p.is_file() and not p.name.startswith("state.sqlite3")
    }


def unfinished_copy(message):
    return {"fields": {"brief/copy/en": {"copy-headline": message}}}


def seed_current_plan(state, run):
    from uuid import uuid4

    if read_draft(state.storage, state.run(run)["campaign_id"]):
        return
    value = state.plan(run, current=False).brief.model_dump(mode="json")
    write_draft(
        state.storage,
        value["campaign_id"],
        {
            "value": value,
            "authored": {"brief": value},
            "composition_schema": 1,
            "version": uuid4().hex,
            "planned_run": run,
            "planned_authored_hash": canonical_hash(value),
            "authored_base": {"run_id": run, "revision_id": None, "metadata_snapshot_id": None},
        },
    )


@pytest.mark.parametrize("market", ["en-US", "es-MX"])
def test_form_prohibited_rule_saves_blocks_and_can_be_corrected(project, market):
    from uuid import uuid4

    client, ws, cid = project
    _, sample = write_sample(ws.storage)
    request = action(client, cid)
    value = sample.model_dump(mode="json")
    value["campaign_id"] = cid
    value["brand"]["id"] = request["authored"]["brief"]["brand"]["id"]
    for asset in [value["brand"], *(p["source"] for p in value["products"])]:
        key = "logo_path" if "logo_path" in asset else "path"
        upload = client.post(
            "/api/uploads",
            content=ws.storage.path(asset[key]).read_bytes(),
            headers={"Content-Type": "image/png", "X-Campaign-Id": cid},
        )
        assert upload.status_code == 200, upload.text
        asset[key] = upload.json()["reference"]
    for language in value["markets"].values():
        language["phrase_rules"] = None
    identifier = f"rule-{uuid4().hex}"
    rule = {
        "id": identifier,
        "phrase": value["markets"][market]["copy"]["message"],
        "match": "literal",
        "case_sensitive": False,
    }
    value["markets"][market]["phrase_rules"] = [rule]
    request["authored"]["brief"] = value
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert saved.status_code == 200, saved.text
    refused = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert refused.status_code == 422, refused.text
    matches = [
        item for item in refused.json()["proposal"]["findings"] if item["rule"] == identifier
    ]
    assert len(matches) == 6 and all(item["severity"] == "blocking" for item in matches)
    assert ws.runs(cid) == []
    request = action(client, cid)
    assert request["authored"]["brief"]["markets"][market]["phrase_rules"][0]["id"] == identifier
    request["authored"]["brief"]["markets"][market]["phrase_rules"][0]["phrase"] = "Absent phrase"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    planned = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert planned.status_code == 200, planned.text
    assert ws.runs(cid) == []
    request = action(client, cid)
    request["authored"]["brief"]["markets"][market]["phrase_rules"][0]["phrase"] = ""
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    invalid = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert invalid.status_code == 422
    assert invalid.json()["field_errors"][0]["label"].startswith(
        "Prohibited word 1 – Word or phrase – "
    )


def test_phrase_rule_validation_context_keeps_ids_internal_and_safe_indexes():
    assert (
        field_issue(["markets", "en-US", "phrase_rules", 0, "id"], "invalid", "Check")["label"]
        == "Prohibited word 1 – English (United States)"
    )
    assert (
        field_issue(["markets", "es-MX", "phrase_rules", 1, "phrase"], "invalid", "Check")["label"]
        == "Prohibited word 2 – Word or phrase – Spanish (Mexico)"
    )
    assert (
        field_issue(["markets", "en-US", "phrase_rules"], "invalid", "Check")["label"]
        == "Prohibited words – English (United States)"
    )
    assert (
        field_issue(["markets", "es-MX", "phrase_rules", "PRIVATE"], "invalid", "Check")["label"]
        == "Prohibited words – Spanish (Mexico)"
    )


def test_incomplete_invalid_authored_state_reopens_exactly_without_production(project):
    client, ws, cid = project
    request = action(client, cid)
    before = Intake(ws.storage).read(cid)
    with ws.storage.connect() as db:
        database = [
            line for line in db.iterdump() if not line.startswith('INSERT INTO "campaign_drafts"')
        ]
    authored = request["authored"]
    authored["brief"].update(name="", audience="  unfinished\n", creative_direction="/not/a/file")
    authored["brief"]["brand"].update(text_color="#12", palette="still typing")
    authored["brief"]["markets"]["en-US"]["copy"]["message"] = ""
    authored["form_drafts"] = {
        "fields": {"rule/en/-1": {"mw-rule-phrase": ""}},
        "hint": {"path": "private/uploads/user-entered-hint.png"},
    }
    authored["review_drafts"] = {"a/en-US/1x1": {"description": "Unfinished description\n"}}
    authored["feedback"] = {"warning": "Required wording is blank", "errors": ["Earlier failure"]}
    authored["ui_context"] = {"page": "campaign", "tab": "brand", "language": "es"}
    result = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert result.status_code == 200, result.text
    assert {"version", "saved_at", "authored_hash"} <= set(result.json())
    assert result.json()["version"] != request["version"]
    reopened = AuthoredProjects(Workspace(State(ws.storage))).read(cid)
    assert reopened["authored"] == authored
    assert reopened["version"] == result.json()["version"]
    projected = Intake(ws.storage).read(cid)["value"]
    assert projected["brand"]["logo_path"] == "" and projected["products"] == []
    assert projected["brand"]["id"] == before["value"]["brand"]["id"]
    model = client.get(f"/api/motif/campaigns/{cid}")
    assert model.status_code == 200 and model.json()["project"]["authored"] == authored
    catalog = client.get("/api/motif/workspace").json()["campaigns"]
    assert next(item for item in catalog if item["id"] == cid)["name"] == "Untitled campaign"
    planned = client.post(f"/api/motif/campaigns/{cid}/plan", json=result.json())
    assert planned.status_code == 422 and planned.json()["code"] == "product_limit"
    assert AuthoredProjects(ws).read(cid)["authored"] == authored
    with ws.storage.connect() as db:
        assert [
            line for line in db.iterdump() if not line.startswith('INSERT INTO "campaign_drafts"')
        ] == database


def test_exact_retry_after_commit_loss_and_conflicting_tab_preserve_current_work(
    project, monkeypatch
):
    client, ws, cid = project
    request = action(client, cid)
    other_tab = copy.deepcopy(request)
    request["authored"]["brief"]["audience"] = "First tab"
    write = authored_module.write_draft
    save = AuthoredProjects._save

    def lose_response(*args, **kwargs):
        save(*args, **kwargs)
        raise OSError("Controlled response loss after durable publication")

    monkeypatch.setattr(AuthoredProjects, "_save", lose_response)
    with pytest.raises(OSError):
        AuthoredProjects(ws).save(cid, AuthoredSave.model_validate(request))
    committed = Intake(ws.storage).read(cid)
    monkeypatch.setattr(AuthoredProjects, "_save", save)
    monkeypatch.setattr(
        authored_module,
        "write_draft",
        lambda *args, **kwargs: pytest.fail("Exact retry must not write"),
    )
    repeated = AuthoredProjects(ws).save(cid, AuthoredSave.model_validate(request))
    assert repeated["version"] == committed["version"]
    assert repeated["saved_at"] == committed["saved_at"]
    other_tab["authored"]["brief"]["audience"] = "Other tab retained locally"
    response = client.post(f"/api/motif/campaigns/{cid}/authored", json=other_tab)
    assert response.status_code == 409 and response.json()["code"] == "authored_conflict"
    assert Intake(ws.storage).read(cid) == committed
    monkeypatch.setattr(authored_module, "write_draft", write)
    reviewed = action(client, cid)
    reviewed["authored"] = other_tab["authored"]
    replacement = client.post(f"/api/motif/campaigns/{cid}/authored", json=reviewed)
    assert replacement.status_code == 200
    assert action(client, cid)["authored"]["brief"]["audience"] == "Other tab retained locally"
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=request).status_code == 409


def test_owned_attachment_guard_and_failed_save_leave_previous_document_intact(
    project, monkeypatch
):
    client, ws, cid = project
    other = client.post("/api/motif/campaigns", json={"name": "Other"}).json()["campaign_id"]
    data = io.BytesIO()
    Image.new("RGBA", (20, 10), "red").save(data, "PNG")
    upload = client.post(
        "/api/uploads",
        content=data.getvalue(),
        headers={"Content-Type": "image/png", "X-Campaign-Id": other},
    )
    assert upload.status_code == 200
    request = action(client, cid)
    request["authored"]["form_drafts"] = {
        "product_images": {"retained": {"path": upload.json()["reference"]}}
    }
    before = files(ws.storage)
    rejected = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert rejected.status_code == 422 and rejected.json()["code"] == "authored_attachment"
    assert files(ws.storage) == before
    request["authored"]["form_drafts"] = {}
    request["authored"]["brief"]["audience"] = "Not yet committed"
    unchanged = read_draft(ws.storage, cid)
    monkeypatch.setattr(
        authored_module,
        "write_draft",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Denied")),
    )
    with pytest.raises(OSError):
        AuthoredProjects(ws).save(cid, AuthoredSave.model_validate(request))
    assert read_draft(ws.storage, cid) == unchanged
    assert files(ws.storage) == before


def test_current_run_projection_is_read_only_and_raw_invalid_values_do_not_become_file_reads(
    project,
    monkeypatch,
):
    client, ws, cid = project
    before = files(ws.storage)
    first = action(client, cid)
    assert action(client, cid) == first and files(ws.storage) == before
    monkeypatch.setattr(
        "campaign_studio.compositions.load_image",
        lambda *args: pytest.fail("Incomplete form text must not cause an image read."),
    )
    first["authored"]["brief"]["brand"]["text_color"] = "not a color"
    first["authored"]["brief"]["products"] = "not yet a list"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=first)
    assert saved.status_code == 200, saved.text
    assert action(client, cid)["authored"] == first["authored"]
    catalog = client.get("/api/motif/workspace")
    assert catalog.status_code == 200
    assert next(item for item in catalog.json()["campaigns"] if item["id"] == cid)["products"] == 0
    planned = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert planned.status_code == 422 and planned.json()["code"] == "product_limit"
    assert ws.runs() == []
    with ws.storage.connect() as db:
        stored = json.loads(
            db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (cid,)
            ).fetchone()[0]
        )
    assert "value" not in stored
    assert not ws.storage.path(ws.storage.project(cid).draft).exists()
    assert stored["authored"] == first["authored"]


def test_arbitrary_logo_reference_is_rejected_without_reading_or_saving_it(project, monkeypatch):
    client, ws, cid = project
    before = action(client, cid)
    document = read_draft(ws.storage, cid)
    before_files = files(ws.storage)
    request = copy.deepcopy(before)
    reference = "/arbitrary/user/typed/hint.png"
    request["authored"]["brief"]["brand"]["logo_path"] = reference
    original_path = ws.storage.path

    def reject_arbitrary_read(value):
        assert value != reference, "Reject an unowned reference before resolving its file path."
        return original_path(value)

    monkeypatch.setattr(ws.storage, "path", reject_arbitrary_read)
    monkeypatch.setattr(
        "campaign_studio.compositions.load_image",
        lambda *args: pytest.fail("An unowned logo must not be opened."),
    )
    response = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert response.status_code == 422
    assert response.json()["code"] == "authored_attachment"
    assert action(client, cid) == before and read_draft(ws.storage, cid) == document
    assert files(ws.storage) == before_files and ws.runs() == []


def test_studio_wording_preserves_incomplete_campaign_fields_without_using_old_valid_input(
    tmp_path,
):
    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    projects = AuthoredProjects(ws)
    current = projects.read(cid)
    authored = current["authored"]
    authored["brief"]["audience"] = "Unfinished audience edit"
    authored["brief"]["brand"]["text_color"] = "#12"
    authored["form_drafts"] = unfinished_copy("Pending form stays recoverable")
    projects.save(cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}))
    before = files(ws.storage)
    version = ws.save_composition(
        cid,
        TextEdit(
            revision_id=projects.read(cid)["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            values={"message": "Current image wording", "cta": "View details"},
            field_scopes={"message": "all_products", "cta": "all_products"},
        ),
    )
    reopened = projects.read(cid)
    assert (
        reopened["authored"]["brief"]["markets"]["en-US"]["copy"]["message"]
        == "Current image wording"
    )
    assert reopened["authored"]["brief"]["audience"] == "Unfinished audience edit"
    assert reopened["authored"]["brief"]["brand"]["text_color"] == "#12"
    assert reopened["authored"]["form_drafts"] == authored["form_drafts"]
    with pytest.raises(ActionError) as error:
        Intake(ws.storage).prepare(cid, version, ws)
    assert error.value.code == "brief_fields"
    assert files(ws.storage) == before and ws.runs(cid) == []


def test_current_valid_authored_brief_plans_once_with_stable_pending_target_identity(project):
    client, ws, cid = project
    _, fixture = write_sample(ws.storage)
    request = action(client, cid)
    brief = fixture.model_dump(mode="json")
    brief["campaign_id"] = cid
    brief["products"] = brief["products"][:1]
    brief["products"][0]["id"] = "new-2ebc85e2-7df7-45b3-ac32-57db5372ce07"
    brief["markets"].pop("es-MX")
    for item, key in ((brief["brand"], "logo_path"), (brief["products"][0]["source"], "path")):
        response = client.post(
            "/api/uploads",
            content=ws.storage.path(item[key]).read_bytes(),
            headers={"Content-Type": "image/png", "X-Campaign-Id": cid},
        )
        assert response.status_code == 200
        item[key] = response.json()["reference"]
    request["authored"]["brief"] = brief
    request["authored"]["form_drafts"] = unfinished_copy("Unapplied Brief wording")
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert saved.status_code == 200, saved.text
    planned = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert planned.status_code == 200, planned.text
    result = planned.json()
    assert result["plan"]["outputs"] == 3 and result["plan"]["generation_products"] == []
    assert result["plan_brief"]["products"][0]["id"] == brief["products"][0]["id"]
    assert (
        result["plan_brief"]["markets"]["en-US"]["copy"]["message"] != "Unapplied pending wording"
    )
    repeated = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert repeated.status_code == 200 and repeated.json()["run"] is None
    assert ws.runs(cid) == []
    body = TextEdit(
        revision_id=action(client, cid)["version"],
        ratio="1:1",
        market="en-US",
        product_id=brief["products"][0]["id"],
        field_scopes={"message": "all_products", "cta": "all_products"},
        values={"message": "Saved after the first authored preparation", "cta": "See more"},
    )
    ws.save_composition(cid, body)
    reopened = action(client, cid)["authored"]
    assert reopened["brief"]["markets"]["en-US"]["copy"]["message"] == body.values["message"]
    assert reopened["form_drafts"] == request["authored"]["form_drafts"]


@pytest.mark.parametrize("with_supplied_scene", [False, True])
def test_authored_concept_plan_and_replan_keep_only_real_attachment_paths(
    project, with_supplied_scene
):
    client, ws, cid = project
    _, fixture = write_sample(ws.storage)
    request = action(client, cid)
    brief = fixture.model_dump(mode="json")
    brief["campaign_id"] = cid
    brief["products"] = brief["products"][: 2 if with_supplied_scene else 1]
    concept = brief["products"][0]
    concept["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "A fictional brush roll; no product photograph was supplied.",
        "provenance": {
            "label": "Producer description",
            "rights": "Invented demonstration product",
            "synthetic": True,
        },
    }
    references = [(brief["brand"], "logo_path")]
    if with_supplied_scene:
        references.append((brief["products"][1]["source"], "path"))
    uploaded = {}
    for item, key in references:
        data = ws.storage.path(item[key]).read_bytes()
        result = client.post(
            "/api/uploads",
            content=data,
            headers={"Content-Type": "image/png", "X-Campaign-Id": cid},
        )
        assert result.status_code == 200, result.text
        item[key] = result.json()["reference"]
        uploaded[item[key]] = data
    request["authored"]["brief"] = brief
    pending = unfinished_copy("Unapplied Brief wording")
    request["authored"]["form_drafts"] = pending
    expected_outputs = len(brief["products"]) * len(brief["markets"]) * 3
    previous_run = None
    for audience in ("First authored plan", "Changed audience for the next plan"):
        request["authored"]["brief"]["audience"] = audience
        invalid = copy.deepcopy(request)
        invalid["authored"]["brief"]["markets"]["en-US"]["copy"]["message"] = ""
        saved_invalid = client.post(f"/api/motif/campaigns/{cid}/authored", json=invalid)
        assert saved_invalid.status_code == 200, saved_invalid.text
        before = Intake(ws.storage).read(cid)
        runs = ws.runs(cid)
        refused = client.post(
            f"/api/motif/campaigns/{cid}/plan",
            json={"version": saved_invalid.json()["version"]},
        )
        assert refused.status_code == 422, refused.text
        assert Intake(ws.storage).read(cid) == before
        assert ws.runs(cid) == runs
        request["version"] = saved_invalid.json()["version"]
        saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
        assert saved.status_code == 200, saved.text
        body = {"version": saved.json()["version"]}
        planned = client.post(f"/api/motif/campaigns/{cid}/plan", json=body)
        assert planned.status_code == 200, planned.text
        model = planned.json()
        run_id = model["run"]["id"]
        assert run_id != previous_run
        assert model["plan"]["outputs"] == expected_outputs
        assert model["plan"]["generation_products"] == [concept["id"]]
        assert model["plan_brief"]["audience"] == audience
        assert model["plan_brief"]["products"][0]["source"] == concept["source"]
        reopened = action(client, cid)
        document = Intake(ws.storage).read(cid)
        assert document["planned_run"] == run_id
        assert document["authored_base"] == reopened["base"]
        assert reopened["base"] == {
            "run_id": run_id,
            "revision_id": None,
            "metadata_snapshot_id": None,
        }
        assert reopened["authored"]["brief"]["products"][0]["source"] == concept["source"]
        assert reopened["authored"]["form_drafts"] == pending
        if with_supplied_scene:
            source = reopened["authored"]["brief"]["products"][1]["source"]
            assert source["path"].startswith(ws.storage.project(cid).run(run_id) + "/Inputs/")
            assert source == model["plan_brief"]["products"][1]["source"]
        repeated = client.post(f"/api/motif/campaigns/{cid}/plan", json=body)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["run"]["id"] == run_id
        assert ws.state.attempts(run_id) == []
        previous_run, request = run_id, reopened
    request["authored"]["form_drafts"]["fields"]["brief/copy/en"]["copy-headline"] = (
        "Still editable"
    )
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert saved.status_code == 200, saved.text
    assert action(client, cid)["authored"] == request["authored"]
    assert len(ws.runs(cid)) == 2
    assert all(ws.storage.path(path).read_bytes() == data for path, data in uploaded.items())


def test_authored_save_during_provider_wait_preserves_new_work_and_pinned_execution(tmp_path):
    state, _, run_id, response = generated_case(tmp_path)
    seed_current_plan(state, run_id)
    entered, release = threading.Event(), threading.Event()
    calls = []

    async def transport(request):
        calls.append(json.loads(request.content))
        entered.set()
        assert await asyncio.to_thread(release.wait, 60), "Test did not release the provider"
        return httpx.Response(200, json=response)

    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: True,
        provider_factory=lambda: OpenAIImages(
            "synthetic-test-credential", transport=httpx.MockTransport(transport)
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        other = client.post("/api/motif/campaigns", json={"name": "Other campaign"}).json()[
            "campaign_id"
        ]
        cid = state.run(run_id)["campaign_id"]
        plan = state.plan(run_id)
        started = client.post(
            f"/api/campaigns/{cid}/start",
            json={"run_id": run_id, "digest": plan.digest, "allowance": 1},
        )
        assert started.status_code == 202, started.text
        assert entered.wait(30), f"Mock provider was never reached: {state.run(run_id)['state']}"
        try:
            for campaign in (cid, other):
                request = action(client, campaign)
                request["authored"]["brief"]["creative_direction"] = (
                    "New unfinished direction while work runs"
                )
                request["authored"]["form_drafts"] = unfinished_copy("")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        client.post, f"/api/motif/campaigns/{campaign}/authored", json=request
                    )
                    saved = future.result(timeout=10)
                assert saved.status_code == 200, saved.text
                assert not release.is_set()
                assert action(client, campaign)["authored"] == request["authored"]
        finally:
            release.set()
        import time

        for _ in range(300):
            if app.state.operations[run_id].done():
                break
            time.sleep(0.01)
        assert app.state.operations[run_id].done()
        assert state.campaign_metrics(cid)["attempted_requests"] == 1
        assert len(calls) == 1
        assert (
            next(iter(state.source_generation(run_id).values()))["inputs"]["creative_direction"]
            == plan.brief.creative_direction
        )
        reopened = action(client, cid)
        assert (
            reopened["authored"]["brief"]["creative_direction"]
            == "New unfinished direction while work runs"
        )
        assert reopened["authored"]["form_drafts"] == unfinished_copy("")
        assert reopened["base"]["run_id"] == run_id
        assert reopened["base"]["revision_id"] == state.effective_revision(run_id)
        with state.storage.connect() as db:
            record = json.loads(
                db.execute(
                    "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (cid,)
                ).fetchone()[0]
            )
            completed = json.loads(
                db.execute("SELECT plan_json FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            )
        assert record["authored_base"] == reopened["base"]
        assert "value" not in record
        assert completed["storage_format"] == "completed-run-v1"
        assert "brief" not in completed
        with pytest.raises(StateError, match="complete"):
            state.plan(run_id, current=False)


def test_material_authored_edit_invalidates_confirmed_unstarted_plan_without_dispatch(tmp_path):
    state, runner, run_id, _ = generated_case(tmp_path)
    seed_current_plan(state, run_id)
    ws = Workspace(state)
    cid = state.run(run_id)["campaign_id"]
    snapshot = AuthoredProjects(ws).read(cid)
    snapshot["authored"]["brief"]["creative_direction"] = "A changed production request"
    AuthoredProjects(ws).save(
        cid,
        AuthoredSave.model_validate(
            {key: snapshot[key] for key in ("version", "base", "authored")}
        ),
    )
    with pytest.raises(StateError, match="fresh production plan"):
        state.confirm(run_id, state.plan(run_id).digest)

    class NoDispatch:
        async def generate(self, *args):
            pytest.fail("Changed authored brief must not dispatch a confirmed older request")

    with pytest.raises(StateError, match="fresh production plan"):
        asyncio.run(runner.execute(run_id, NoDispatch()))
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: True,
        provider_factory=lambda: pytest.fail("Must refuse before provider construction"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(
            f"/api/campaigns/{cid}/start",
            json={"run_id": run_id, "digest": state.plan(run_id).digest, "allowance": 1},
        )
        assert response.status_code == 409 and response.json()["code"] == "stale_brief"
    assert state.attempts(run_id) == [] and state.revision_count(run_id) == 0


def test_authored_save_invalidates_deletion_inventory_and_pending_deletion_blocks_recreation(
    project,
):
    client, ws, cid = project
    cleanup = Cleanup(ws.state)
    inventory = cleanup.preview_campaign(cid)
    request = action(client, cid)
    request["authored"]["brief"]["audience"] = "Changed after inventory"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert saved.status_code == 200
    before = files(ws.storage)
    with pytest.raises(StateError, match="changed"):
        cleanup.delete_campaign(cid, inventory["token"], defer_cleanup=True)
    assert files(ws.storage) == before
    fresh = cleanup.preview_campaign(cid)
    cleanup.delete_campaign(cid, fresh["token"], defer_cleanup=True)
    request["version"] = saved.json()["version"]
    refused = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert refused.status_code == 409
    assert files(ws.storage) == before


def test_export_snapshot_keeps_its_rules_while_newer_campaign_save_survives(tmp_path, monkeypatch):
    from campaign_studio import delivery

    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    snapshot = ws.current_composition(cid)
    snapshot["plan"].outputs = snapshot["plan"].outputs[:1]
    render = delivery.render_png
    saved = []

    def save_during_render(*args, **kwargs):
        current = AuthoredProjects(ws).read(cid)
        current["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = [
            {"id": "later-rule", "phrase": "Synthetic", "match": "literal", "case_sensitive": False}
        ]
        current["authored"]["brief"]["audience"] = "Saved during export"
        saved.append(
            AuthoredProjects(ws).save(
                cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")})
            )
        )
        return render(*args, **kwargs)

    monkeypatch.setattr(delivery, "render_png", save_during_render)
    with delivery.prepare_current_zip(ws.state, snapshot) as package:
        assert package.stream.read(2) == b"PK"
    current = AuthoredProjects(ws).read(cid)
    assert saved and current["version"] == saved[-1]["version"]
    assert current["authored"]["brief"]["audience"] == "Saved during export"
    assert current["authored"]["brief"]["markets"]["en-US"]["phrase_rules"][0]["id"] == "later-rule"
    assert snapshot["version"] != current["version"]


def test_logo_replacement_saves_only_its_owned_choice_and_rejects_a_stale_save(tmp_path):
    from test_logo_editing import replacement

    from campaign_studio.workspace import LogoEdit

    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    projects = AuthoredProjects(ws)
    current = projects.read(cid)
    current["authored"]["brief"]["audience"] = "Keep this audience"
    projects.save(cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}))
    new_logo = replacement(ws.state, cid)
    edit = LogoEdit(
        revision_id=projects.read(cid)["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        replacement=new_logo,
    )
    ws.save_composition(cid, edit)
    saved = projects.read(cid)
    assert saved["authored"]["brief"]["brand"]["logo_path"] == new_logo
    assert saved["authored"]["brief"]["audience"] == "Keep this audience"
    changed = copy.deepcopy(saved)
    changed["authored"]["brief"]["name"] = "A later change"
    projects.save(cid, AuthoredSave(**{k: changed[k] for k in ("version", "base", "authored")}))
    with pytest.raises(ActionError, match="another tab"):
        ws.save_composition(cid, edit)
    assert projects.read(cid)["authored"]["brief"]["name"] == "A later change"


def test_form_optional_defaults_do_not_claim_stale_images_and_invalid_work_is_retained(project):
    client, ws, cid = project
    _, sample = write_sample(ws.storage)
    request = action(client, cid)
    brief = sample.model_dump(mode="json")
    brief["campaign_id"] = cid
    brief["brand"]["id"] = request["authored"]["brief"]["brand"]["id"]
    brief["products"] = brief["products"][:1]
    brief["products"][0]["source"].pop("subject_box")  # Ordinary Add product form shape.
    for owner, key in [(brief["brand"], "logo_path"), (brief["products"][0]["source"], "path")]:
        uploaded = client.post(
            "/api/uploads",
            content=ws.storage.path(owner[key]).read_bytes(),
            headers={"Content-Type": "image/png", "X-Campaign-Id": cid},
        )
        assert uploaded.status_code == 200
        owner[key] = uploaded.json()["reference"]
    request["authored"]["brief"] = brief
    endpoint = f"/api/motif/campaigns/{cid}"
    for audience in ("First production", "Changed production input"):
        request["authored"]["brief"]["audience"] = audience
        saved = client.post(endpoint + "/authored", json=request)
        assert saved.status_code == 200
        planned = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
        assert planned.status_code == 200, planned.text
        assert planned.json()["run"] is None
        assert len(planned.json()["manifest"]["outputs"]) == 6
        model = client.get(endpoint).json()
        assert not model["unplanned_draft"]
        request = action(client, cid)
        assert "subject_box" not in request["authored"]["brief"]["products"][0]["source"]
        assert model["output_brief"]["products"][0]["source"].get("subject_box") is None
    request["authored"]["brief"]["audience"] = "  "
    assert client.post(endpoint + "/authored", json=request).status_code == 200
    assert client.get(endpoint).json()["composition_issues"]
    assert action(client, cid)["authored"]["brief"]["audience"] == "  "
    assert all(ws.state.attempts(run["id"]) == [] for run in ws.runs(cid))


def test_replay_preparation_reuses_exact_master_and_rejects_changed_checksums(project):
    import hashlib

    client, ws, _ = project
    cid = seed_campaign(ws, replay=True)
    endpoint = f"/api/motif/campaigns/{cid}"
    current = action(client, cid)
    original_replay = copy.deepcopy(current["authored"]["brief"]["replay"])
    master = original_replay["sources"]["product-a"]
    original_bytes = ws.storage.path(master["path"]).read_bytes()
    assert hashlib.sha256(original_bytes).hexdigest() == master["sha256"]
    current["authored"]["brief"]["markets"]["en-US"]["copy"]["message"] = "A new hold."
    current["authored"]["form_drafts"] = unfinished_copy("Words in an unrelated form")
    saved = client.post(endpoint + "/authored", json=current)
    assert saved.status_code == 200, saved.text
    prepared = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["run"] is None
    assert not prepared.json()["plan"]["generation_products"]
    assert action(client, cid)["authored"]["brief"]["replay"] == original_replay
    assert ws.storage.path(master["path"]).read_bytes() == original_bytes
    assert ws.runs(cid) == []
    changed = action(client, cid)
    changed["authored"]["brief"]["replay"]["sources"]["product-a"]["sha256"] = "0" * 64
    saved = client.post(endpoint + "/authored", json=changed)
    assert saved.status_code == 200
    refused = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
    assert refused.status_code == 422, refused.text
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 0


def test_changed_generation_inputs_prepare_without_assuming_a_retained_replay_master(project):
    client, ws, _ = project
    cid = seed_campaign(ws, replay=True)
    endpoint = f"/api/motif/campaigns/{cid}"
    request = action(client, cid)
    original_replay = copy.deepcopy(request["authored"]["brief"]["replay"])
    request["authored"]["brief"]["audience"] = "A deliberately different photography audience"
    saved = client.post(endpoint + "/authored", json=request)
    assert saved.status_code == 200
    planned = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
    assert planned.status_code == 200, planned.text
    run_id = planned.json()["run"]["id"]
    plan = ws.state.plan(run_id)
    assert plan.brief.replay is None
    assert plan.generation_products == ["product-a"]
    assert action(client, cid)["authored"]["brief"]["replay"] == original_replay
    ws.state.require_authored_plan(run_id)
    assert ws.state.run(run_id)["state"] == "planned"
    assert all(ws.state.attempts(run["id"]) == [] for run in ws.runs(cid))


def test_reused_sources_and_logo_autosave_need_no_new_production_request(tmp_path):
    from test_logo_editing import replacement

    from campaign_studio.workspace import LogoEdit

    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    before = ws.current_composition(cid)
    current = AuthoredProjects(ws).read(cid)
    version = ws.save_composition(
        cid,
        LogoEdit(
            revision_id=current["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            replacement=replacement(ws.state, cid),
        ),
    )
    prepared = Intake(ws.storage).prepare(cid, version, ws)
    assert not prepared.generation_products
    after = ws.current_composition(cid)
    assert after["sources"] == before["sources"]
    assert ws.runs(cid) == []


def test_unstarted_production_plan_cannot_replace_current_fields_or_admit_foreign_paths(tmp_path):
    state, _, run, _ = generated_case(tmp_path)
    plan = state.plan(run, current=False)
    ws = Workspace(state)
    cid = plan.brief.campaign_id
    seed_current_plan(state, run)
    current = AuthoredProjects(ws).read(cid)
    current["authored"]["brief"]["name"] = "Current producer name"
    saved = AuthoredProjects(ws).save(
        cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")})
    )
    assert AuthoredProjects(ws).read(cid)["authored"]["brief"]["name"] == "Current producer name"
    assert state.plan(run, current=False).brief.name == plan.brief.name
    foreign, _ = Intake(ws.storage).new(name="Other campaign")
    reference = ws.storage.project(foreign).ref("Assets/foreign.png")
    ws.storage.project(foreign).write(reference, b"not-authorized")
    current = AuthoredProjects(ws).read(cid)
    current["authored"]["form_drafts"] = {"product_images": {"other": {"path": reference}}}
    with pytest.raises(ActionError, match="file picker"):
        AuthoredProjects(ws).save(
            cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")})
        )
    assert AuthoredProjects(ws).read(cid)["version"] == saved["version"]


@pytest.mark.parametrize("product_scope", [False, True])
def test_wording_autosave_preserves_other_fields_and_explicit_scope(tmp_path, product_scope):
    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    projects = AuthoredProjects(ws)
    current = projects.read(cid)
    current["authored"]["brief"]["products"][1]["copy_overrides"] = {
        "en-US": {"message": "Intentional other headline", "cta": "Intentional other CTA"}
    }
    current["authored"]["form_drafts"] = unfinished_copy("Text still in an unrelated form")
    projects.save(cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}))
    version = ws.save_composition(
        cid,
        TextEdit(
            revision_id=projects.read(cid)["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            values={"message": "Changed wording"},
            field_scopes={"message": "product" if product_scope else "all_products"},
        ),
    )
    plan = ws.current_composition(cid, version)["plan"]
    assert plan.outputs[0].text.message == "Changed wording"
    other = next(o for o in plan.outputs if o.product_id == "product-2" and o.market == "en-US")
    assert other.text.message == (
        "Intentional other headline" if product_scope else "Changed wording"
    )
    assert other.text.cta == "Intentional other CTA"
    assert projects.read(cid)["authored"]["form_drafts"] == current["authored"]["form_drafts"]


@pytest.mark.parametrize("incomplete_campaign", [False, True])
def test_current_image_edits_survive_prepare_without_overwriting_campaign_fields(
    tmp_path, incomplete_campaign
):
    from campaign_studio.brief import Framing
    from campaign_studio.workspace import FrameEdit

    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    projects = AuthoredProjects(ws)
    current = projects.read(cid)
    current["authored"]["brief"]["audience"] = "" if incomplete_campaign else "A different audience"
    projects.save(cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}))
    version = ws.save_composition(
        cid,
        FrameEdit(
            revision_id=projects.read(cid)["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            framing=Framing(zoom=1.17),
        ),
    )
    if incomplete_campaign:
        with pytest.raises(ActionError):
            Intake(ws.storage).prepare(cid, version, ws)
    else:
        assert not Intake(ws.storage).prepare(cid, version, ws).generation_products
    saved = AuthoredProjects(Workspace(State(ws.storage))).read(cid)
    assert saved["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.17
    assert saved["authored"]["brief"]["audience"] == current["authored"]["brief"]["audience"]
    assert ws.runs(cid) == []


@pytest.mark.parametrize("interruption", ["newer_save", "failed_write"])
def test_studio_autosave_preserves_concurrent_or_failed_work(tmp_path, monkeypatch, interruption):
    from campaign_studio import compositions

    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws)
    projects = AuthoredProjects(ws)
    current = projects.read(cid)
    edit = TextEdit(
        revision_id=current["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        values={"message": "Studio text"},
        field_scopes={"message": "product"},
    )
    if interruption == "newer_save":
        current["authored"]["brief"]["name"] = "Saved elsewhere"
        projects.save(cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}))
        before = read_draft(ws.storage, cid)
        with pytest.raises(ActionError, match="another tab"):
            ws.save_composition(cid, edit)
    else:
        before = read_draft(ws.storage, cid)
        monkeypatch.setattr(
            compositions, "write_draft", lambda *a, **k: (_ for _ in ()).throw(OSError("Full disk"))
        )
        with pytest.raises(OSError, match="Full disk"):
            ws.save_composition(cid, edit)
    assert read_draft(ws.storage, cid) == before
    assert ws.runs(cid) == []


def test_retained_product_photo_is_owned_saveable_and_separate_from_concept_input(project):
    client, ws, cid = project
    request = action(client, cid)
    with io.BytesIO() as image:
        Image.new("RGB", (320, 240), "#345678").save(image, format="PNG")
        upload = client.post(
            "/api/uploads",
            content=image.getvalue(),
            headers={"Content-Type": "image/png", "X-Campaign-Id": cid},
        ).json()
    source = {
        "role": "finished_scene",
        "path": upload["reference"],
        "provenance": {"label": "My photo", "rights": "Owned", "synthetic": False},
    }
    product = {
        "id": "product-own",
        "name": "My product",
        "description": "The product description",
        "source": source,
        "framing": {},
        "copy_overrides": {},
    }
    request["authored"]["brief"]["products"] = [product]
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=request).status_code == 200
    request = action(client, cid)
    request["authored"].setdefault("form_drafts", {})["product_images"] = {product["id"]: source}
    request["authored"]["brief"]["products"][0]["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "Invent a different product",
        "provenance": source["provenance"],
    }
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=request).status_code == 200
    model = client.get(f"/api/motif/campaigns/{cid}").json()
    retained = model["product_images"][product["id"]]
    assert retained["source"]["path"] == source["path"]
    assert "path" not in model["project"]["authored"]["brief"]["products"][0]["source"]
    assert client.get(retained["url"]).status_code == 200
    request = action(client, cid)
    request["authored"]["brief"]["products"][0]["source"] = retained["source"]
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=request).status_code == 200
    reopened = action(client, cid)
    assert reopened["authored"]["brief"]["products"][0]["source"]["path"] == source["path"]
    foreign = client.post("/api/motif/campaigns", json={"name": "Other campaign"}).json()[
        "campaign_id"
    ]
    other = action(client, foreign)
    other["authored"].setdefault("form_drafts", {})["product_images"] = {"other": source}
    refused = client.post(f"/api/motif/campaigns/{foreign}/authored", json=other)
    assert refused.status_code == 422 and refused.json()["code"] == "authored_attachment"
    reopened["authored"]["brief"]["products"] = []
    reopened["authored"].setdefault("form_drafts", {})["product_images"] = {}
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=reopened).status_code == 200
    assert client.get(retained["url"]).status_code == 404
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 0


def test_retained_snapshot_photo_can_be_replanned_after_concept_plan(project):
    client, ws, _ = project
    cid = seed_campaign(ws)
    endpoint = f"/api/motif/campaigns/{cid}"
    request = action(client, cid)
    product = request["authored"]["brief"]["products"][0]
    source = copy.deepcopy(product["source"])
    assert source["path"].startswith(ws.storage.project(cid).ref("Assets") + "/")
    request["authored"].setdefault("form_drafts", {})["product_images"] = {product["id"]: source}
    product["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "A newly described fictional product",
        "provenance": source["provenance"],
    }
    saved = client.post(endpoint + "/authored", json=request)
    assert saved.status_code == 200, saved.text
    concept = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
    assert concept.status_code == 200, concept.text
    assert product["id"] in concept.json()["plan"]["generation_products"]
    request = action(client, cid)
    request["authored"]["brief"]["products"][0]["source"] = source
    saved = client.post(endpoint + "/authored", json=request)
    assert saved.status_code == 200, saved.text
    restored = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["plan"]["generation_products"] == []
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 0

    foreign = client.post("/api/motif/campaigns", json={"name": "Other campaign"}).json()[
        "campaign_id"
    ]
    other = action(client, foreign)
    other["authored"].setdefault("form_drafts", {})["product_images"] = {product["id"]: source}
    refused = client.post(f"/api/motif/campaigns/{foreign}/authored", json=other)
    assert refused.status_code == 422 and refused.json()["code"] == "authored_attachment"

    # A retained record is not proof that its file is still readable.
    request = action(client, cid)
    original = ws.storage.path(source["path"]).read_bytes()
    for payload in (None, b"not a readable image"):
        path = ws.storage.path(source["path"])
        if payload is None:
            path.unlink()
        else:
            path.write_bytes(payload)
        request = action(client, cid)
        request["authored"]["brief"]["products"][0]["source"] = source
        request["authored"]["brief"]["audience"] += " Changed audience."
        saved = client.post(endpoint + "/authored", json=request)
        assert saved.status_code == 200, saved.text
        refused = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
        assert refused.status_code == 422, refused.text
        assert ws.state.campaign_metrics(cid)["attempted_requests"] == 0
        path.write_bytes(original)


@pytest.mark.parametrize("field", ["product", "logo", "form", "replay"])
@pytest.mark.parametrize(
    "path_kind",
    [
        "posix",
        "drive",
        "backslash-drive",
        "unc",
        "slash-unc",
        "uri",
        "url",
        "traversal",
        "owned-traversal",
        "relative",
        "foreign",
        "unowned",
        "missing",
        "integer",
        "list",
        "object",
        "boolean",
        "zero",
        "empty-list",
        "empty-object",
        "false",
    ],
)
def test_new_attachment_paths_are_rejected_before_document_or_file_mutation(
    project, field, path_kind
):
    from uuid import uuid4

    from test_product_upload_lifecycle import request_for

    client, ws, cid = project
    added = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=request_for(client, cid))
    assert added.status_code == 200, added.text
    baseline = action(client, cid)
    pid = baseline["authored"]["brief"]["products"][0]["id"]
    original = baseline["authored"]["brief"]["products"][0]["source"]["path"]
    bad = {
        "posix": "/nonexistent-motif-boundary.png",
        "drive": "C:/Users/example/photo.png",
        "backslash-drive": r"C:\Users\example\photo.png",
        "unc": r"\\server\share\photo.png",
        "slash-unc": "//server/share/photo.png",
        "uri": "file:///tmp/photo.png",
        "url": "https://example.invalid/photo.png",
        "traversal": "../outside.png",
        "relative": "unattached-photo.png",
        "integer": 42,
        "list": ["photo.png"],
        "object": {"path": "photo.png"},
        "boolean": True,
        "zero": 0,
        "empty-list": [],
        "empty-object": {},
        "false": False,
    }.get(path_kind)
    if path_kind == "owned-traversal":
        bad = f"Campaigns/{ws.storage.project(cid).folder}/Assets/Products/../../../../outside.png"
    if path_kind == "unowned":
        bad = ws.storage.project(cid).ref("Assets/Products/unregistered.png")
        ws.storage.write_new(bad, ws.storage.path(original).read_bytes())
    if path_kind == "foreign":
        other, _ = Intake(ws.storage).new()
        bad = ws.storage.project(other).ref("Assets/Products/foreign.png")
        ws.storage.project(other).write(bad, ws.storage.path(original).read_bytes())
    if path_kind == "missing":
        bad = ws.storage.project(cid).ref(f"Runs/{uuid4().hex}/Inputs/missing.png")
        ws.storage.project(cid).claim(bad)
    request = copy.deepcopy(baseline)
    if field == "product":
        request["authored"]["brief"]["products"][0]["source"]["path"] = bad
    elif field == "logo":
        request["authored"]["brief"]["brand"]["logo_path"] = bad
    elif field == "form":
        request["authored"]["form_drafts"] = {"product_images": {pid: {"path": bad}}}
    else:
        request["authored"]["brief"]["replay"] = {"sources": {pid: {"path": bad}}}
    before_files = files(ws.storage)
    with ws.storage.connect() as db:
        before = {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("campaign_drafts", "file_owners", "uploads", "cleanup_intents")
        }
    rejected = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "authored_attachment"
    assert files(ws.storage) == before_files
    assert action(client, cid) == baseline
    with ws.storage.connect() as db:
        assert {
            table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in before
        } == before
        assert db.execute("SELECT count(*) FROM cleanup_intents").fetchone() == (0,)
    assert client.get(f"/api/motif/campaigns/{cid}").status_code == 200
    for page in ("campaign", "production", "studio", "handoff"):
        assert client.get(f"/campaigns/{cid}/{page}").status_code == 200
    # A rejected selection must not strand ordinary subsequent work.
    baseline["authored"]["brief"]["name"] = "Still editable"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=baseline)
    assert saved.status_code == 200, saved.text
    assert ws.storage.path(original).read_bytes() == before_files[original]


def test_imported_display_hints_and_empty_unfinished_paths_still_save(project):
    client, ws, _ = project
    _, sample = write_sample(ws.storage)
    value = sample.model_dump(mode="json")
    value["brand"]["logo_path"] = "attachments/Brand logo.png"
    value["products"][0]["source"]["path"] = "photos/Lantern image.png"
    imported = client.post("/api/motif/import", json=value)
    assert imported.status_code == 200, imported.text
    cid = imported.json()["campaign_id"]
    current = action(client, cid)
    document = read_draft(ws.storage, cid)
    assert document["attachment_hints"]["logo"] == "Brand logo.png"
    pid = current["authored"]["brief"]["products"][0]["id"]
    assert document["attachment_hints"][pid] == "Lantern image.png"
    assert current["authored"]["brief"]["products"][0]["source"]["path"] == ""
    current["authored"]["form_drafts"] = {"product_images": {pid: {"path": ""}}}
    current["authored"]["brief"]["audience"] = "Unfinished campaign stays editable"
    response = client.post(f"/api/motif/campaigns/{cid}/authored", json=current)
    assert response.status_code == 200, response.text
    assert read_draft(ws.storage, cid)["attachment_hints"] == document["attachment_hints"]
    assert client.get(f"/api/motif/campaigns/{cid}").status_code == 200


@pytest.mark.parametrize("original_survives", [True, False])
def test_explicit_owned_reattachment_can_repair_a_previously_poisoned_product(
    project, original_survives
):
    from test_product_upload_lifecycle import photo, request_for

    client, ws, cid = project
    added = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=request_for(client, cid))
    assert added.status_code == 200
    original = action(client, cid)["authored"]["brief"]["products"][0]["source"]["path"]
    document = read_draft(ws.storage, cid)
    document["authored"]["brief"]["products"][0]["source"]["path"] = "/old-invalid-selection.png"
    write_draft(ws.storage, cid, document)
    replacement = original
    if not original_survives:
        # The preceding faulty release could also retire the old upload after committing
        # the bad path. Recovery then needs an explicit fresh attachment, not invented bytes.
        ws.storage.unlink(ws.storage.path(original))
        with ws.storage.connect() as db:
            db.execute("DELETE FROM file_owners WHERE reference=?", (original,))
        replacement = Intake(ws.storage).upload(photo(), "image/png", cid, "product", "Repair")[
            "reference"
        ]
    assert client.get(f"/api/motif/campaigns/{cid}").status_code == 409
    request = action(client, cid)  # The raw authored endpoint remains readable.
    request["authored"]["brief"]["products"][0]["source"]["path"] = replacement
    response = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert response.status_code == 200, response.text
    assert client.get(f"/api/motif/campaigns/{cid}").status_code == 200
    assert ws.storage.path(replacement).is_file()
    assert action(client, cid)["authored"]["brief"]["products"][0]["source"]["path"] == replacement


@pytest.mark.parametrize("field", ["product", "logo", "form", "replay"])
@pytest.mark.parametrize("empty", [None, ""])
def test_empty_attachment_fields_remain_editable(project, field, empty):
    from test_product_upload_lifecycle import request_for

    client, ws, cid = project
    added = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=request_for(client, cid))
    assert added.status_code == 200
    request = action(client, cid)
    pid = request["authored"]["brief"]["products"][0]["id"]
    if field == "product":
        request["authored"]["brief"]["products"][0]["source"]["path"] = empty
    elif field == "logo":
        request["authored"]["brief"]["brand"]["logo_path"] = empty
    elif field == "form":
        request["authored"]["form_drafts"] = {"product_images": {pid: {"path": empty}}}
    else:
        request["authored"]["brief"]["replay"] = {"sources": {pid: {"path": empty}}}
    result = client.post(f"/api/motif/campaigns/{cid}/authored", json=request)
    assert result.status_code == 200, result.text
    assert client.get(f"/api/motif/campaigns/{cid}").status_code == 200
