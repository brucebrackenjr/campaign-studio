"""Completed production retains request evidence, not an obsolete editing snapshot."""

import asyncio
import copy
import json

import httpx
import pytest
from test_composition_migration import legacy as legacy
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio import compositions as compositions_module
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Framing
from campaign_studio.campaign_drafts import read, write
from campaign_studio.costing import estimate_recorded_usage
from campaign_studio.plan_storage import COMPLETED_FORMAT, completed_run, load_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.runner import Runner
from campaign_studio.state import State, StateError
from campaign_studio.workspace import FrameEdit, Workspace


def saved_document(state, campaign):
    with state.storage.connect() as db:
        return db.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign,)
        ).fetchone()[0]


def seed_preproduction_draft(state, run, *, pending=False):
    plan = state.plan(run, current=False)
    value = plan.brief.model_dump(mode="json")
    authored = {"brief": copy.deepcopy(value), "form_drafts": {"fields": {"brand": "#12"}}}
    if pending:
        authored["brief"]["audience"] = "  Unfinished audience for a later production  "
        authored["brief"]["markets"]["en-US"]["copy"]["message"] = "  My unsaved next headline  "
    document = {
        "value": value,
        "authored": authored,
        "version": "preproduction-draft",
        "composition_schema": 1,
        "planned_run": run,
        "authored_base": {"run_id": run, "revision_id": None, "metadata_snapshot_id": None},
        "base_run": run,
        "base_revision": None,
    }
    write(state.storage, plan.brief.campaign_id, document)
    return plan, authored


def test_supplied_completion_keeps_only_marker_and_current_edit_settings(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    assert asyncio.run(runner.execute(run))["complete"]
    payload = state.run(run)["plan_json"]
    assert json.loads(payload) == {
        "storage_format": COMPLETED_FORMAT,
        "campaign_id": brief.campaign_id,
        "generation": {},
    }
    assert state.source_generation(run) == {}
    assert (
        Workspace(state).current_composition(brief.campaign_id)["plan"].brief.campaign_id
        == brief.campaign_id
    )
    with pytest.raises(StateError, match="production is complete"):
        state.plan(run, current=False)
    with pytest.raises(ValueError, match="production is complete"):
        load_plan(payload)
    with pytest.raises(ValueError, match="production is complete"):
        state.read_plan(brief.campaign_id, state.run(run)["plan_ref"])
    assert asyncio.run(runner.execute(run))["complete"]
    assert state.attempts(run) == []


def test_completed_generated_run_retains_exact_request_and_usage_after_restart(tmp_path):
    state, runner, run, body = generated_case(tmp_path)
    original = state.plan(run, current=False)
    with state.storage.connect() as db:
        before_count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    provider = OpenAIImages(
        "synthetic", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    manifest = asyncio.run(runner.execute(run, provider))
    assert manifest["complete"] and before_count == 0
    expected = state.campaign_metrics(original.brief.campaign_id)
    attempts = state.attempts(run)
    reopened = State(state.storage)
    receipt = completed_run(reopened.run(run)["plan_json"])
    assert set(receipt.generation) == {attempt["product_id"] for attempt in attempts}
    for product_id, item in receipt.generation.items():
        assert item.request == original.generation_requests[product_id]
        assert set(item.inputs.model_dump()) == {
            "audience",
            "creative_direction",
            "role",
            "description",
        }
    assert reopened.attempts(run) == attempts
    assert reopened.campaign_metrics(original.brief.campaign_id) == expected
    cost = estimate_recorded_usage(attempts, original.generation_requests)
    assert expected["usage_cost_estimate"]["estimated_usd"] == cost["estimated_usd"]
    payload = json.loads(reopened.run(run)["plan_json"])
    assert "brief" not in payload and "outputs" not in payload


def test_partial_retry_keeps_confirmed_plan_until_complete_then_releases_pin(tmp_path):
    state, runner, run, body = generated_case(tmp_path)
    original = state.plan(run, current=False)
    failure = OpenAIImages(
        "synthetic", transport=httpx.MockTransport(lambda _: httpx.Response(500, json={}))
    )
    partial = asyncio.run(runner.execute(run, failure))
    assert not partial["complete"]
    assert completed_run(state.run(run)["plan_json"]) is None
    assert state.plan(run, current=False) == original
    state.confirm(run, state.plan(run).digest, retry_allowance=2)
    pinned = state.run(run)["confirmed_revision"]
    assert pinned is None
    confirmed = state.plan(run, current=False)
    assert confirmed.generation_requests == original.generation_requests
    success = OpenAIImages(
        "synthetic", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    accepted = asyncio.run(runner.execute(run, success))
    assert accepted["complete"]
    assert state.run(run)["confirmed_revision"] is None
    assert completed_run(state.run(run)["plan_json"]) is not None
    assert len(state.attempts(run)) == 2
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (0,)


def test_completion_and_draft_rebase_roll_back_together_then_recover(tmp_path, monkeypatch):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    original, _ = seed_preproduction_draft(state, run)
    before = saved_document(state, brief.campaign_id)

    def fail_after_rebase(*args):
        raise OSError("controlled completion failure")

    with monkeypatch.context() as patch:
        patch.setattr(compositions_module, "dump_completed", fail_after_rebase)
        with pytest.raises(OSError, match="controlled completion failure"):
            asyncio.run(runner.execute(run))
    assert state.plan(run, current=False) == original
    assert saved_document(state, brief.campaign_id) == before
    assert state.current_output(brief.campaign_id) is None
    accepted = Runner(State(state.storage)).recover_local(run)
    assert accepted["complete"]
    document = read(state.storage, brief.campaign_id)
    assert document["authored_base"]["revision_id"] is None
    assert completed_run(state.run(run)["plan_json"]) is not None


def test_schema14_upgrade_preserves_current_fields_and_retires_obsolete_images(legacy):
    from campaign_studio.storage import WORKSPACE_SCHEMA

    with legacy.storage.connect() as db:
        db.execute("PRAGMA user_version=14")
    reopened = State(legacy.storage)
    current = read(legacy.storage, legacy.cid)
    assert current["composition_schema"] == 1
    assert (
        current["authored"]["brief"]["markets"]["en-US"]["copy"]["message"]
        == "New Campaign headline"
    )
    assert current["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.23
    assert current["authored_base"]["revision_id"] is None
    assert not any(legacy.storage.path(ref).exists() for ref in legacy.files)
    payload = reopened.run(legacy.run)["plan_json"]
    assert completed_run(payload) is not None
    assert State(legacy.storage).run(legacy.run)["plan_json"] == payload
    with legacy.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone() == (WORKSPACE_SCHEMA,)


def test_150_saves_keep_one_current_document_and_unchanged_production_receipt(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    projects = AuthoredProjects(Workspace(state))
    document = projects.read(brief.campaign_id)
    projects.save(
        brief.campaign_id,
        AuthoredSave(**{key: document[key] for key in ("version", "base", "authored")}),
    )
    payload = state.run(run)["plan_json"]
    workspace = Workspace(state)
    for index in range(150):
        edit = FrameEdit(
            run_id=run,
            revision_id=read(state.storage, brief.campaign_id)["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            framing=Framing(zoom=1.1 + index / 1000),
        )
        workspace.save_composition(brief.campaign_id, edit)
        with state.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
            assert db.execute("SELECT COUNT(*) FROM revisions").fetchone() == (0,)
            assert db.execute("SELECT COUNT(*) FROM campaign_drafts").fetchone() == (1,)
            assert db.execute("SELECT COUNT(*) FROM package_exports").fetchone() == (0,)
            assert db.execute("SELECT draft_comparisons FROM runs").fetchone() == (None,)
    assert state.run(run)["plan_json"] == payload
    outputs = state.storage.path(state.storage.project(brief.campaign_id).ref("Outputs"))
    assert not outputs.exists()
    assert not list(outputs.rglob("*.json"))
    assert state.attempts(run) == []


def test_completed_source_receipt_does_not_replace_newer_campaign_instructions(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    original = state.plan(run, current=False)
    asyncio.run(runner.execute(run))
    workspace = Workspace(state)
    projects = AuthoredProjects(workspace)
    current = projects.read(brief.campaign_id)
    current["authored"]["brief"]["markets"]["en-US"]["copy"]["message"] = "The newer headline"
    projects.save(
        brief.campaign_id,
        AuthoredSave(**{key: current[key] for key in ("version", "base", "authored")}),
    )
    before = read(state.storage, brief.campaign_id)
    receipt = state.run(run)["plan_json"]
    assert asyncio.run(Runner(State(state.storage)).execute(run))["complete"]
    after = read(state.storage, brief.campaign_id)
    assert after["authored"] == before["authored"]
    assert state.run(run)["plan_json"] == receipt
    assert original.brief.markets["en-US"].text.message != "The newer headline"
    assert (
        workspace.current_composition(brief.campaign_id)["plan"].outputs[0].text.message
        == "The newer headline"
    )
