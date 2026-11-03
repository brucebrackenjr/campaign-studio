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
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.planning import canonical_hash, make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
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
    assert len(ws.runs(cid)) == 1 and ws.state.attempts(ws.runs(cid)[0]["id"]) == []
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
        database = list(db.iterdump())
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
    assert set(result.json()) == {"version", "saved_at", "authored_hash"}
    assert result.json()["version"] != request["version"]
    reopened = AuthoredProjects(Workspace(State(ws.storage))).read(cid)
    assert reopened["authored"] == authored
    assert reopened["version"] == result.json()["version"]
    assert Intake(ws.storage).read(cid)["value"] == before["value"]
    model = client.get(f"/api/motif/campaigns/{cid}")
    assert model.status_code == 200 and model.json()["project"]["authored"] == authored
    catalog = client.get("/api/motif/workspace").json()["campaigns"]
    assert next(item for item in catalog if item["id"] == cid)["name"] == "Untitled campaign"
    planned = client.post(f"/api/motif/campaigns/{cid}/plan", json=result.json())
    assert planned.status_code == 422 and planned.json()["code"] == "product_limit"
    assert AuthoredProjects(ws).read(cid)["authored"] == authored
    with ws.storage.connect() as db:
        assert list(db.iterdump()) == database


def test_exact_retry_after_commit_loss_and_conflicting_tab_preserve_current_work(
    project, monkeypatch
):
    client, ws, cid = project
    request = action(client, cid)
    other_tab = copy.deepcopy(request)
    request["authored"]["brief"]["audience"] = "First tab"
    write = ws.storage.write

    def lose_response(reference, payload):
        write(reference, payload)
        if reference == ws.storage.project(cid).draft:
            raise OSError("Controlled response loss after durable publication")

    monkeypatch.setattr(ws.storage, "write", lose_response)
    with pytest.raises(OSError):
        AuthoredProjects(ws).save(cid, AuthoredSave.model_validate(request))
    committed = Intake(ws.storage).read(cid)
    monkeypatch.setattr(
        ws.storage, "write", lambda *args: pytest.fail("Exact retry must not write")
    )
    repeated = AuthoredProjects(ws).save(cid, AuthoredSave.model_validate(request))
    assert repeated["version"] == committed["version"]
    assert repeated["saved_at"] == committed["saved_at"]
    other_tab["authored"]["brief"]["audience"] = "Other tab retained locally"
    response = client.post(f"/api/motif/campaigns/{cid}/authored", json=other_tab)
    assert response.status_code == 409 and response.json()["code"] == "authored_conflict"
    assert Intake(ws.storage).read(cid) == committed
    monkeypatch.setattr(ws.storage, "write", write)
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
    monkeypatch.setattr(ws.storage, "write", lambda *args: (_ for _ in ()).throw(OSError("Denied")))
    with pytest.raises(OSError):
        AuthoredProjects(ws).save(cid, AuthoredSave.model_validate(request))
    assert files(ws.storage) == before


def test_current_run_projection_is_read_only_and_raw_invalid_values_do_not_become_file_reads(
    project,
):
    client, ws, cid = project
    before = files(ws.storage)
    first = action(client, cid)
    assert action(client, cid) == first and files(ws.storage) == before
    first["authored"]["brief"]["brand"]["logo_path"] = "/arbitrary/user/typed/hint.png"
    first["authored"]["brief"]["brand"]["text_color"] = "not a color"
    first["authored"]["brief"]["products"] = "not yet a list"
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=first)
    assert saved.status_code == 200, saved.text
    assert action(client, cid)["authored"] == first["authored"]
    catalog = client.get("/api/motif/workspace")
    assert catalog.status_code == 200
    assert next(item for item in catalog.json()["campaigns"] if item["id"] == cid)["products"] == 0
    assert client.post(f"/api/motif/campaigns/{cid}/plan", json=saved.json()).status_code == 422
    assert ws.runs() == []
    stored = json.loads(ws.storage.path(ws.storage.project(cid).draft).read_bytes())
    assert stored["authored"]["brief"]["brand"]["logo_path"] == "/arbitrary/user/typed/hint.png"


def test_applied_copy_reconciles_only_untouched_authored_fields_and_never_plans_old_valid_input(
    tmp_path,
):
    state, runner, run_id, _ = setup_case(tmp_path, count=2, one_market=True)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    cid = first["campaign_id"]
    snapshot = projects.read(cid)
    authored = snapshot["authored"]
    authored["brief"]["audience"] = "Unfinished audience edit"
    authored["brief"]["brand"]["text_color"] = "#12"
    authored["form_drafts"] = unfinished_copy("Pending form stays recoverable")
    request = AuthoredSave(version=snapshot["version"], base=snapshot["base"], authored=authored)
    projects.save(cid, request)
    edit = TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=first["revision_id"],
        market="en-US",
        product_id=ws.state.plan(run_id).brief.products[0].id,
        field_scopes={"message": "all_products", "cta": "all_products"},
        values={"message": "Applied image wording", "cta": "View details"},
    )
    brief, plan, _ = ws.text(cid, edit)
    current = runner.revise(run_id, brief, first["revision_id"], plan.digest)
    # The acknowledged authored base identifies the image inputs for reconciliation.
    reopened = projects.read(cid)
    assert (
        reopened["authored"]["brief"]["markets"]["en-US"]["copy"]["message"]
        == "Applied image wording"
    )
    assert reopened["authored"]["brief"]["audience"] == "Unfinished audience edit"
    assert reopened["authored"]["brief"]["brand"]["text_color"] == "#12"
    assert reopened["authored"]["form_drafts"] == authored["form_drafts"]
    with pytest.raises(ActionError, match="changed in another tab"):
        projects.save(cid, request.model_copy(update={"version": reopened["version"]}))
    saved = projects.save(
        cid,
        AuthoredSave(
            version=reopened["version"], base=reopened["base"], authored=reopened["authored"]
        ),
    )
    before = files(state.storage)
    with pytest.raises(ActionError) as error:
        Intake(state.storage).prepare(cid, saved["version"], ws)
    assert error.value.code == "brief_fields"
    assert state.effective_revision(run_id) == current["revision_id"]
    assert state.attempts(run_id) == [] and len(ws.runs(cid)) == 1
    assert files(state.storage) == before


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
    assert repeated.status_code == 200 and repeated.json()["run"]["id"] == result["run"]["id"]
    assert len(ws.runs(cid)) == 1 and ws.state.attempts(result["run"]["id"]) == []
    assert action(client, cid)["authored"]["form_drafts"] == request["authored"]["form_drafts"]
    run_id = result["run"]["id"]
    ws.state.confirm(run_id, ws.state.plan(run_id).digest)
    from campaign_studio.runner import Runner

    runner = Runner(ws.state)
    first = asyncio.run(runner.execute(run_id))
    body = TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=first["revision_id"],
        market="en-US",
        product_id=ws.state.plan(run_id).brief.products[0].id,
        field_scopes={"message": "all_products", "cta": "all_products"},
        values={
            "message": "Applied after the first authored plan",
            "cta": "See more",
        },
    )
    revised, plan, _ = ws.text(cid, body)
    runner.revise(run_id, revised, first["revision_id"], plan.digest)
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
    entered, release = threading.Event(), threading.Event()
    calls = []

    async def transport(request):
        calls.append(json.loads(request.content))
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
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
        assert entered.wait(2)
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
                    saved = future.result(timeout=2)
                assert saved.status_code == 200, saved.text
                assert not release.is_set()
                assert action(client, campaign)["authored"] == request["authored"]
        finally:
            release.set()
        from test_web_workflow import wait_for_work

        status = wait_for_work(client, cid)
        assert status["completed"] == 12 and status["attempted"] == 1
        assert len(calls) == 1
        assert state.plan(run_id).brief.creative_direction == plan.brief.creative_direction
        assert (
            action(client, cid)["authored"]["brief"]["creative_direction"]
            == "New unfinished direction while work runs"
        )


def test_material_authored_edit_invalidates_confirmed_unstarted_plan_without_dispatch(tmp_path):
    state, runner, run_id, _ = generated_case(tmp_path)
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


def test_rule_publication_preserves_authored_save_during_render(tmp_path, monkeypatch):
    from campaign_studio import rendering
    from campaign_studio.intake import BriefDraft

    state, runner, run_id, _ = setup_case(tmp_path, count=2, one_market=True)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    intake = Intake(ws.storage)
    cid = first["campaign_id"]
    start = projects.read(cid)
    value = state.plan(run_id).brief.model_dump(mode="json")
    value["markets"]["en-US"]["phrase_rules"].append(
        {
            "id": "new-rule",
            "phrase": "not printed in this campaign",
        }
    )
    document = BriefDraft(value=value, version=start["version"], base_revision=first["revision_id"])
    entered, release = threading.Event(), threading.Event()
    render = rendering.render_revision

    def held_render(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return render(*args, **kwargs)

    monkeypatch.setattr(rendering, "render_revision", held_render)
    with ThreadPoolExecutor(max_workers=2) as pool:
        operation = pool.submit(intake.recheck_rules, cid, document, run_id, ws)
        assert entered.wait(2)
        try:
            saved_work = copy.deepcopy(start["authored"])
            saved_work["brief"]["audience"] = "New audience during rule rendering"
            saved_work["form_drafts"] = unfinished_copy("Still unfinished")
            saving = pool.submit(
                projects.save,
                cid,
                AuthoredSave(
                    version=start["version"],
                    base=start["base"],
                    authored=saved_work,
                ),
            )
            ack = saving.result(timeout=2)
            assert ack["version"] and not release.is_set()
        finally:
            release.set()
        result = operation.result(timeout=5)
    reopened = projects.read(cid)
    assert reopened["authored"]["brief"]["audience"] == saved_work["brief"]["audience"]
    assert reopened["authored"]["form_drafts"] == saved_work["form_drafts"]
    assert (
        reopened["authored"]["brief"]["markets"]["en-US"]["phrase_rules"]
        == state.plan(run_id).brief.model_dump(mode="json")["markets"]["en-US"]["phrase_rules"]
    )
    assert {i["id"]: i["sha256"] for i in result["outputs"]} == {
        i["id"]: i["sha256"] for i in first["outputs"]
    }
    assert state.attempts(run_id) == []


def test_explicit_logo_replacement_supersedes_only_its_guarded_brief_choice(tmp_path, monkeypatch):
    from test_logo_editing import apply_logo, command, replacement

    from campaign_studio import runner as runner_module
    from campaign_studio.exporting import package_payloads

    state, runner, run_id, _ = setup_case(tmp_path, count=2, one_market=True)
    asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    cid = state.run(run_id)["campaign_id"]
    choice_a, choice_b, choice_c = [
        replacement(state, cid, size) for size in ((300, 900), (400, 300), (200, 600))
    ]
    request = projects.read(cid)
    authored = request["authored"]
    authored["brief"]["brand"]["logo_path"] = choice_a
    authored["brief"]["brand"]["logo_provenance"]["label"] = "Pending Brief logo"
    authored["brief"]["audience"] = "  Unfinished unrelated work  "
    authored["brief"]["brand"]["text_color"] = "#12"
    authored["form_drafts"] = {
        "fields": {
            "brief/brand/en": {"brand-name": "  Unfinished raw brand  ", "text-color": "#12"},
            "brief/copy/en": {"copy-headline": "Unapplied Brief wording"},
        }
    }
    projects.save(
        cid,
        AuthoredSave.model_validate({key: request[key] for key in ("version", "base", "authored")}),
    )
    # Moving the current logo is not a decision about the separate Brief choice.
    apply_logo(state, runner, command(state, run_id))
    assert projects.read(cid)["authored"]["brief"]["brand"]["logo_path"] == choice_a
    before = projects.read(cid)
    edit = command(state, run_id, replacement=choice_b, draft_version=before["version"])
    result, impact = apply_logo(state, runner, edit)
    saved = projects.read(cid)
    applied_reference = state.plan(run_id).brief.brand.logo_path
    assert saved["authored"]["brief"]["brand"]["logo_path"] == applied_reference
    assert (
        state.storage.path(applied_reference).read_bytes()
        == state.storage.path(choice_b).read_bytes()
    )
    assert saved["authored"]["brief"]["brand"]["logo_provenance"]["label"] == "User-supplied logo"
    assert (
        saved["authored"]["form_drafts"]["fields"]["brief/brand/en"]["brand-name"]
        == "  Unfinished raw brand  "
    )
    assert saved["authored"]["brief"]["audience"] == "  Unfinished unrelated work  "
    assert saved["authored"]["brief"]["brand"]["text_color"] == "#12"
    assert (
        saved["authored"]["form_drafts"]["fields"]["brief/copy/en"]
        == authored["form_drafts"]["fields"]["brief/copy/en"]
    )
    assert len(impact["render_outputs"]) == 6 and result["complete"]
    package = package_payloads(state, result["revision_id"])
    assert json.loads(package["manifest.json"])["logo_provenance"]["label"] == "User-supplied logo"
    assert b"replacement_draft_version" not in package["manifest.json"]

    # A competing authored Save after publication but before reconciliation is
    # later work. The committed replacement receipt cannot erase that choice.
    original_render = runner_module.render_revision

    def publish_then_choose(*args, **kwargs):
        rendered = original_render(*args, **kwargs)
        latest = projects.read(cid)
        latest["authored"]["brief"]["brand"]["logo_path"] = choice_c
        latest["authored"]["brief"]["brand"]["logo_provenance"]["label"] = "Newer deliberate choice"
        projects.save(
            cid,
            AuthoredSave.model_validate(
                {key: latest[key] for key in ("version", "base", "authored")}
            ),
        )
        return rendered

    monkeypatch.setattr(runner_module, "render_revision", publish_then_choose)
    edit = command(state, run_id, replacement=choice_a, draft_version=saved["version"])
    apply_logo(state, runner, edit)
    later = projects.read(cid)["authored"]["brief"]
    assert later["brand"]["logo_path"] == choice_c
    assert later["brand"]["logo_provenance"]["label"] == "Newer deliberate choice"
    assert (
        state.storage.path(state.plan(run_id).brief.brand.logo_path).read_bytes()
        == state.storage.path(choice_a).read_bytes()
    )
    assert state.attempts(run_id) == []


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
        run_id = planned.json()["run"]["id"]
        ws.state.confirm(run_id, ws.state.plan(run_id).digest)
        result = asyncio.run(Runner(ws.state).execute(run_id))
        assert result["complete"] and len(result["outputs"]) == 6
        model = client.get(endpoint).json()
        assert not model["unplanned_draft"]
        request = action(client, cid)
        assert "subject_box" not in request["authored"]["brief"]["products"][0]["source"]
        assert model["output_brief"]["products"][0]["source"]["subject_box"] is None
    request["authored"]["brief"]["audience"] = "  "
    assert client.post(endpoint + "/authored", json=request).status_code == 200
    assert client.get(endpoint).json()["unplanned_draft"]
    assert action(client, cid)["authored"]["brief"]["audience"] == "  "
    assert all(ws.state.attempts(run["id"]) == [] for run in ws.runs(cid))


def test_explicit_replay_replan_reconciles_verified_master_without_hiding_source_changes(project):
    import hashlib

    client, ws, _ = project
    cid = seed_campaign(ws, replay=True)
    endpoint = f"/api/motif/campaigns/{cid}"
    first = ws.run_for(cid)["id"]
    ws.state.confirm(first, ws.state.plan(first).digest)
    assert asyncio.run(Runner(ws.state).execute(first))["complete"]
    request = action(client, cid)
    original_replay = copy.deepcopy(request["authored"]["brief"]["replay"])
    original_master = original_replay["sources"]["product-a"]
    original_bytes = ws.storage.path(original_master["path"]).read_bytes()
    request["authored"]["brief"]["markets"]["en-US"]["copy"]["message"] = "A new hold."
    request["authored"]["brief"]["markets"]["es-MX"]["copy"]["message"] = "Una nueva ruta."
    request["authored"]["form_drafts"] = unfinished_copy("Unapplied words")
    saved = client.post(endpoint + "/authored", json=request)
    assert saved.status_code == 200, saved.text
    planned = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
    assert planned.status_code == 200, planned.text
    run_id = planned.json()["run"]["id"]
    plan = ws.state.plan(run_id)
    replay = action(client, cid)["authored"]["brief"]["replay"]
    master = replay["sources"]["product-a"]
    assert master["path"] == plan.brief.replay.sources["product-a"].path
    assert master["path"] != original_master["path"]
    assert ws.storage.path(master["path"]).read_bytes() == original_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == master["sha256"]
    restored_metadata = copy.deepcopy(replay)
    restored_metadata["sources"]["product-a"]["path"] = original_master["path"]
    assert restored_metadata == original_replay
    ws.state.require_authored_plan(run_id)
    ws.state.confirm(run_id, plan.digest)
    result = asyncio.run(Runner(ws.state).execute(run_id))
    assert result["complete"] and len(result["outputs"]) == 12
    current = client.get(endpoint).json()
    assert not current["unplanned_draft"]
    clean = action(client, cid)
    assert clean["authored"]["form_drafts"] == request["authored"]["form_drafts"]
    for field, different in (
        ("path", original_master["path"]),
        ("sha256", "0" * 64),
        ("model", "deliberately-different-model"),
    ):
        changed = action(client, cid)
        changed["authored"]["brief"]["replay"]["sources"]["product-a"][field] = different
        assert client.post(endpoint + "/authored", json=changed).status_code == 200
        current = client.get(endpoint).json()
        assert current["unplanned_draft"]
        with pytest.raises(StateError, match="authored brief changed"):
            ws.state.require_authored_plan(run_id)
        restored = action(client, cid)
        restored["authored"] = copy.deepcopy(clean["authored"])
        assert client.post(endpoint + "/authored", json=restored).status_code == 200
    assert not client.get(endpoint).json()["unplanned_draft"]
    assert all(ws.state.attempts(run["id"]) == [] for run in ws.runs(cid))


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


def test_reused_sources_and_logo_apply_preserve_authored_plan_readiness(tmp_path):
    from test_logo_editing import apply_logo, command, replacement

    from campaign_studio.motif import campaign_data

    state, runner, run_id, body = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    asyncio.run(runner.execute(run_id, provider))
    ws, intake = Workspace(state), Intake(state.storage)
    projects = AuthoredProjects(ws)
    cid = state.run(run_id)["campaign_id"]

    def save_current(change=None):
        current = projects.read(cid)
        if change:
            change(current["authored"])
        return projects.save(
            cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")})
        )

    save_current(lambda a: a["brief"]["markets"]["en-US"]["copy"].update(message="New words."))
    planned = intake.prepare(cid, projects.read(cid)["version"], ws)
    run_id = ws.run_for(cid)["id"]
    assert planned.brief.replay and not planned.generation_products
    state.confirm(run_id, planned.digest)
    asyncio.run(runner.execute(run_id))
    assert projects.read(cid)["authored"]["brief"]["replay"] is None
    model = campaign_data(ws, cid)
    assert not model["unplanned_draft"]

    artwork = replacement(state, cid, (400, 300))
    apply_logo(
        state,
        runner,
        command(state, run_id, replacement=artwork, draft_version=projects.read(cid)["version"]),
    )
    model = campaign_data(ws, cid)
    assert not model["unplanned_draft"]
    state.require_authored_plan(run_id)
    save_current(lambda a: a["form_drafts"].update(unfinished_copy("Unapplied words")))
    assert not campaign_data(ws, cid)["unplanned_draft"]
    state.require_authored_plan(run_id)

    clean_authored = copy.deepcopy(projects.read(cid)["authored"])
    explicit_replay = copy.deepcopy(model["output_brief"]["replay"])
    explicit_replay["history"] = "A deliberate different replay declaration"
    for change in (
        lambda a: a["brief"]["brand"].update(logo_path=artwork),
        lambda a: a["brief"]["markets"]["en-US"]["copy"].update(message="Different copy"),
        lambda a: a["brief"]["products"][0].update(description="Different product"),
        lambda a: a["brief"].update(replay=explicit_replay),
        lambda a: a["brief"].update(replay={}),
        lambda a: a["brief"].update(replay=[]),
        lambda a: a["brief"].update(replay=False),
        lambda a: a["brief"].update(replay=0),
        lambda a: a["brief"].update(replay=""),
    ):
        save_current(change)
        changed = campaign_data(ws, cid)
        assert changed["unplanned_draft"]
        with pytest.raises(StateError, match="authored brief changed"):
            state.require_authored_plan(run_id)
        save_current(lambda a: a.update(copy.deepcopy(clean_authored)))
        assert not campaign_data(ws, cid)["unplanned_draft"]
        assert projects.read(cid)["authored"]["form_drafts"] == clean_authored["form_drafts"]

    save_current(lambda a: a["brief"].update(audience="  "))
    model = campaign_data(ws, cid)
    assert model["unplanned_draft"]
    assert projects.read(cid)["authored"]["brief"]["audience"] == "  "
    apply_logo(
        state,
        runner,
        command(
            state,
            run_id,
            replacement=replacement(state, cid, (350, 300)),
            draft_version=projects.read(cid)["version"],
        ),
    )
    assert campaign_data(ws, cid)["unplanned_draft"]
    with pytest.raises(StateError, match="authored brief changed"):
        state.require_authored_plan(run_id)
    assert len(state.attempts(state.run(run_id)["id"])) == 0
    assert sum(len(state.attempts(r["id"])) for r in ws.runs(cid)) == 1


def test_unstarted_plan_keeps_accepted_authored_base_and_rejects_foreign_paths(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    cid = state.run(run_id)["campaign_id"]
    read = projects.read(cid)
    projects.save(cid, AuthoredSave(**{k: read[k] for k in ("version", "base", "authored")}))
    # Reproduce the interrupted publication boundary: a new saved plan exists while the
    # last acknowledged authored document still names the prior run and paths.
    stranded = state.create(make_plan(state.plan(run_id).brief, state.storage), 0)
    read = projects.read(cid)
    old = Intake(state.storage).read(cid)
    assert (
        read["authored"]["brief"]["brand"]["logo_path"]
        == old["authored"]["brief"]["brand"]["logo_path"]
    )
    assert ws.run_for(cid)["id"] == stranded
    assert read["base"]["run_id"] == run_id
    before = files(state.storage)
    with state.storage.connect() as db:
        rows = list(db.iterdump())
    request = {k: read[k] for k in ("version", "base", "authored")}
    request["authored"]["brief"]["audience"] = "Still editable after the interrupted publication."
    projects.save(cid, AuthoredSave(**request))
    assert projects.read(cid)["authored"] == request["authored"]
    with state.storage.connect() as db:
        assert list(db.iterdump()) == rows
    assert all(
        state.storage.path(name).read_bytes() == payload
        for name, payload in before.items()
        if name != ws.storage.project(cid).draft
    )
    other = state.plan(run_id).brief.model_dump(mode="json")
    other["campaign_id"] = "b8ca6fb0-e390-5cde-bed0-8f7e28306866"
    from campaign_fixtures import independent_brief

    other_copy = independent_brief(state, Brief.model_validate(other), other["campaign_id"])
    other_run = state.create(make_plan(other_copy, state.storage), 0)
    read = projects.read(cid)
    request = {k: read[k] for k in ("version", "base", "authored")}
    request["authored"]["brief"]["brand"]["logo_path"] = state.plan(other_run).brief.brand.logo_path
    with pytest.raises(ActionError, match="file picker"):
        projects.save(cid, AuthoredSave(**request))


@pytest.mark.parametrize("unstarted_authored_base", [False, True])
def test_applied_wording_reconciles_matching_raw_controls_and_independent_override_fields(
    tmp_path, unstarted_authored_base
):
    state, runner, run_id, _ = setup_case(tmp_path)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    cid = first["campaign_id"]
    saved = projects.read(cid)
    authored = saved["authored"]
    en = authored["brief"]["markets"]["en-US"]["copy"]
    es = authored["brief"]["markets"]["es-MX"]["copy"]
    authored["form_drafts"] = {
        "fields": {
            "brief/copy/en": {"copy-headline": en["message"], "copy-cta": en["cta"] or ""},
            "brief/brand/en": {"copy-headline": "  independent unfinished  ", "text-color": "#12"},
            "brief/copy/es": {"copy-headline": es["message"], "copy-cta": es["cta"] or ""},
        }
    }
    product = authored["brief"]["products"][0]
    product["copy_overrides"] = {"en-US": {"cta": "Independent CTA"}, "es-MX": {"message": None}}
    projects.save(
        cid, AuthoredSave(version=saved["version"], base=saved["base"], authored=authored)
    )
    if unstarted_authored_base:
        pending_run = state.create(make_plan(state.plan(run_id).brief, state.storage), 0)
        reference = ws.storage.project(cid).draft
        document = json.loads(state.storage.path(reference).read_bytes())
        document["authored_base"] = {
            "run_id": pending_run,
            "revision_id": None,
            "metadata_snapshot_id": None,
        }
        state.storage.write(reference, json.dumps(document).encode())
        # Passive reads cannot replace this independently saved copy.
        assert projects.read(cid)["authored"] == authored
    edit = TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=first["revision_id"],
        market="en-US",
        product_id=product["id"],
        field_scopes={"message": "all_products", "cta": "all_products"},
        values={"message": "Applied shared wording", "cta": "Explore details"},
    )
    brief, plan, _ = ws.text(cid, edit)
    result = runner.revise(run_id, brief, first["revision_id"], plan.digest, text_edit=edit)
    read = projects.read(cid)
    fields = read["authored"]["form_drafts"]["fields"]
    assert fields["brief/copy/en"] == {
        "copy-headline": "Applied shared wording",
        "copy-cta": "Explore details",
    }
    assert fields["brief/brand/en"] == authored["form_drafts"]["fields"]["brief/brand/en"]
    assert fields["brief/copy/es"] == authored["form_drafts"]["fields"]["brief/copy/es"]
    second = TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=result["revision_id"],
        market="en-US",
        product_id=product["id"],
        field_scopes={"message": "product"},
        values={"message": "Applied product wording"},
    )
    brief, plan, _ = ws.text(cid, second)
    result = runner.revise(run_id, brief, result["revision_id"], plan.digest, text_edit=second)
    read = projects.read(cid)
    overrides = read["authored"]["brief"]["products"][0]["copy_overrides"]
    assert overrides == {
        "en-US": {"message": "Applied product wording", "cta": "Independent CTA"},
        "es-MX": {"message": None},
    }
    request = AuthoredSave(version=read["version"], base=read["base"], authored=read["authored"])
    ack = projects.save(cid, request)
    assert projects.save(cid, request) == ack, "lost acknowledgement retry is idempotent"
    reopened = AuthoredProjects(Workspace(State(state.storage))).read(cid)
    assert reopened["authored"] == read["authored"]
    assert state.effective_revision(run_id) == result["revision_id"]
    assert state.attempts(run_id) == []


@pytest.mark.parametrize("unstarted_authored_base", [False, True])
def test_current_image_edits_survive_new_plan_without_overwriting_pending_work(
    tmp_path, unstarted_authored_base
):
    from test_workflow import pixel_hashes

    from campaign_studio.brief import Framing, LogoPlacement
    from campaign_studio.motif import campaign_data
    from campaign_studio.workspace import FrameEdit, LogoEdit, TextEdit

    state, runner, original_run, _ = setup_case(tmp_path, one_market=True)
    first = asyncio.run(runner.execute(original_run))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    cid = first["campaign_id"]
    saved = projects.read(cid)
    raw = saved["authored"]
    raw["brief"]["audience"] = "A separate new audience for the next production"
    raw["form_drafts"] = {"fields": {"brief/brand/en": {"brand-name": "  unfinished brand  "}}}
    ack = projects.save(
        cid, AuthoredSave(version=saved["version"], base=saved["base"], authored=raw)
    )
    planned = Intake(state.storage).prepare(cid, ack["version"], ws)
    pending_run = ws.run_for(cid)["id"]
    assert pending_run != original_run
    if unstarted_authored_base:
        # The reported workspace was saved against its newer unstarted plan.
        # Seed that exact initial boundary, then Apply all four editors before
        # any further project Save. A preparatory Save would mask this defect.
        reference = ws.storage.project(cid).draft
        document = json.loads(state.storage.path(reference).read_bytes())
        document["authored_base"] = {
            "run_id": pending_run,
            "revision_id": None,
            "metadata_snapshot_id": None,
        }
        state.storage.write(reference, json.dumps(document).encode())
    if unstarted_authored_base:
        with pytest.raises(StateError, match="fresh production plan"):
            state.require_authored_plan(pending_run)
    else:
        state.require_authored_plan(pending_run)
    baseline = projects.read(cid)
    model = campaign_data(ws, cid)
    assert (
        model["can_edit"]
        and model["can_describe"]
        and all(model["logo_editable_products"].values())
    )
    assert model["output_run_id"] == original_run and model["run"]["id"] == pending_run
    commands = [
        (
            "text",
            "text_edit",
            TextEdit,
            {
                "ratio": "1:1",
                "values": {"message": "Edit the saved photograph"},
                "field_scopes": {"message": "product"},
            },
        ),
        ("frame", "frame_edit", FrameEdit, {"ratio": "1:1", "framing": Framing(focal_x=0.6)}),
        (
            "text",
            "text_edit",
            TextEdit,
            {
                "ratio": "1:1",
                "placements": {"cta": {"left": 0.1, "top": 0.4, "width": 0.7, "font_size": 38}},
            },
        ),
        (
            "logo",
            "logo_edit",
            LogoEdit,
            {"ratio": "1:1", "placement": LogoPlacement(left=0.1, top=0.7, width=0.35)},
        ),
    ]
    revision = first["revision_id"]
    for method, argument, schema, values in commands:
        edit = schema(
            run_id=original_run,
            revision_id=revision,
            product_id="product-1",
            market="en-US",
            **values,
        )
        brief, preview, _ = getattr(ws, method)(cid, edit)
        result = runner.revise(original_run, brief, revision, preview.digest, **{argument: edit})
        revision = result["revision_id"]
        assert state.current_output(cid) == (original_run, revision)
        read = projects.read(cid)
        assert read["authored"]["brief"]["audience"] == baseline["authored"]["brief"]["audience"]
        assert read["authored"]["form_drafts"] == baseline["authored"]["form_drafts"]
        with pytest.raises(StateError, match="fresh production plan"):
            state.confirm(pending_run, planned.digest)
        if not unstarted_authored_base:
            projects.save(
                cid,
                AuthoredSave(version=read["version"], base=read["base"], authored=read["authored"]),
            )
    reopened = AuthoredProjects(Workspace(State(state.storage))).read(cid)
    product = reopened["authored"]["brief"]["products"][0]
    assert product["copy_overrides"]["en-US"]["message"] == "Edit the saved photograph"
    assert product["framing"]["en-US"]["1:1"]["logo_placement"]["left"] == 0.1
    applied_pixels = pixel_hashes(state, result)
    applied_product = state.plan(original_run).brief.products[0]
    assert product["framing"] == applied_product.model_dump(mode="json")["framing"]
    assert state.attempts(original_run) == state.attempts(pending_run) == []
    assert state.run(pending_run)["state"] == "planned"
    assert campaign_data(ws, cid)["unplanned_draft"]

    # A fresh review after the image corrections stays admitted through the
    # browser's numeric JSON roundtrip and an ordinary Save/reopen.
    def browser_numbers(value):
        if isinstance(value, dict):
            return {key: browser_numbers(item) for key, item in value.items()}
        if isinstance(value, list):
            return [browser_numbers(item) for item in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    raw = browser_numbers(reopened["authored"])
    ack = projects.save(
        cid, AuthoredSave(version=reopened["version"], base=reopened["base"], authored=raw)
    )
    Intake(state.storage).prepare(cid, ack["version"], ws)
    refreshed = projects.read(cid)
    assert refreshed["authored"] == browser_numbers(refreshed["authored"])
    assert canonical_hash(refreshed["authored"]) == canonical_hash(
        browser_numbers(refreshed["authored"])
    )
    projects.save(
        cid,
        AuthoredSave(
            version=refreshed["version"],
            base=refreshed["base"],
            authored=browser_numbers(refreshed["authored"]),
        ),
    )
    state.require_authored_plan(ws.run_for(cid)["id"])
    assert not campaign_data(ws, cid)["unplanned_draft"]
    reviewed_run = ws.run_for(cid)["id"]
    reviewed = state.plan(reviewed_run)
    state.confirm(reviewed_run, reviewed.digest)
    composed = asyncio.run(runner.execute(reviewed_run))
    assert pixel_hashes(state, composed) == applied_pixels
    assert state.attempts(reviewed_run) == []
    reopened = projects.read(cid)
    changed = copy.deepcopy(reopened["authored"])
    changed["brief"]["audience"] = "A genuinely different next audience"
    projects.save(
        cid, AuthoredSave(version=reopened["version"], base=reopened["base"], authored=changed)
    )
    with pytest.raises(StateError, match="fresh production plan"):
        state.require_authored_plan(reviewed_run)
    reopened = projects.read(cid)
    with pytest.raises(StateError):
        state.require_current_revision(original_run, first["revision_id"])
    # Removing a product never grants an old output permission to put it back.
    reopened["authored"]["brief"]["products"] = reopened["authored"]["brief"]["products"][1:]
    projects.save(
        cid,
        AuthoredSave(
            version=reopened["version"], base=reopened["base"], authored=reopened["authored"]
        ),
    )
    with pytest.raises(ActionError) as removed:
        ws.text(
            cid,
            TextEdit(
                ratio="1:1",
                run_id=reviewed_run,
                revision_id=composed["revision_id"],
                product_id="product-1",
                market="en-US",
                values={"cta": "Removed"},
                field_scopes={"cta": "product"},
            ),
        )
    assert removed.value.code == "product_removed"


@pytest.mark.parametrize("interruption", ["newer_save", "failed_write"])
def test_explicit_apply_binding_preserves_concurrent_and_failed_work(
    tmp_path, monkeypatch, interruption
):
    from campaign_studio import runner as runner_module

    state, runner, run_id, _ = setup_case(tmp_path, one_market=True)
    first = asyncio.run(runner.execute(run_id))
    ws = Workspace(state)
    projects = AuthoredProjects(ws)
    cid = first["campaign_id"]
    saved = projects.read(cid)
    projects.save(cid, AuthoredSave(**{k: saved[k] for k in ("version", "base", "authored")}))
    pending_run = state.create(make_plan(state.plan(run_id).brief, state.storage), 0)
    reference = ws.storage.project(cid).draft
    document = json.loads(state.storage.path(reference).read_bytes())
    document["authored_base"] = {
        "run_id": pending_run,
        "revision_id": None,
        "metadata_snapshot_id": None,
    }
    state.storage.write(reference, json.dumps(document).encode())
    untouched = projects.read(cid)
    edit = TextEdit(
        ratio="1:1",
        run_id=run_id,
        revision_id=first["revision_id"],
        product_id="product-1",
        market="en-US",
        values={"message": "Applied shared headline", "cta": "Applied call to action"},
        field_scopes={"message": "all_products", "cta": "all_products"},
    )
    brief, preview, _ = ws.text(cid, edit)
    if interruption == "newer_save":
        render = runner_module.render_revision

        def save_then_render(*args, **kwargs):
            current = projects.read(cid)
            raw = current["authored"]
            raw["brief"]["audience"] = "New audience while rendering"
            raw["brief"]["markets"]["en-US"]["copy"]["message"] = "Independent newer headline"
            raw["brief"]["products"][0]["source"] = copy.deepcopy(
                raw["brief"]["products"][1]["source"]
            )
            raw["form_drafts"] = unfinished_copy("  Unfinished newer wording  ")
            projects.save(
                cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")})
            )
            return render(*args, **kwargs)

        monkeypatch.setattr(runner_module, "render_revision", save_then_render)
        result = runner.revise(run_id, brief, first["revision_id"], preview.digest, text_edit=edit)
        reopened = AuthoredProjects(Workspace(State(state.storage))).read(cid)
        raw = reopened["authored"]
        assert raw["brief"]["audience"] == "New audience while rendering"
        assert raw["brief"]["markets"]["en-US"]["copy"] == {
            "message": "Independent newer headline",
            "cta": "Applied call to action",
        }
        assert raw["brief"]["products"][0]["source"] == raw["brief"]["products"][1]["source"]
        assert raw["form_drafts"] == unfinished_copy("  Unfinished newer wording  ")
        assert result["edit_receipt"]["authored_base"]["version"] == untouched["version"]
        assert reopened["version"] != untouched["version"]
    else:
        write = state.storage.write

        def fail_output(reference, data):
            if "/Revisions/" in reference and reference.endswith(".png"):
                raise OSError("Controlled output failure")
            return write(reference, data)

        monkeypatch.setattr(state.storage, "write", fail_output)
        with pytest.raises(StateError) as failure:
            runner.revise(run_id, brief, first["revision_id"], preview.digest, text_edit=edit)
        assert failure.value.code == "apply_not_accepted"
        assert state.current_output(cid) == (run_id, first["revision_id"])
        assert projects.read(cid) == untouched
    assert state.attempts(run_id) == state.attempts(pending_run) == []


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
    request["authored"]["form_drafts"]["product_images"] = {product["id"]: source}
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
    other["authored"]["form_drafts"]["product_images"] = {"other": source}
    refused = client.post(f"/api/motif/campaigns/{foreign}/authored", json=other)
    assert refused.status_code == 422 and refused.json()["code"] == "authored_attachment"
    reopened["authored"]["brief"]["products"] = []
    reopened["authored"]["form_drafts"]["product_images"] = {}
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
    assert source["path"].startswith(ws.storage.project(cid).ref("Runs") + "/")
    request["authored"]["form_drafts"]["product_images"] = {product["id"]: source}
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
    other["authored"]["form_drafts"]["product_images"] = {product["id"]: source}
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
