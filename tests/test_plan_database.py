import asyncio
import json

import pytest
from test_composition_migration import legacy as legacy
from test_workflow import setup_case

from campaign_studio.brief import Brief, Framing
from campaign_studio.campaign_drafts import read
from campaign_studio.cleanup import Cleanup
from campaign_studio.plan_storage import COMPLETED_FORMAT, STORAGE_FORMAT, completed_run, load_plan
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import WORKSPACE_SCHEMA, Storage
from campaign_studio.workspace import FrameEdit, Workspace


def assert_normalized(state, run_id):
    with state.storage.connect() as db:
        records = db.execute(
            "SELECT plan_json FROM runs WHERE id=? UNION ALL "
            "SELECT plan_json FROM revisions WHERE run_id=?",
            (run_id, run_id),
        ).fetchall()
        for (payload,) in records:
            saved = json.loads(payload)
            if saved["storage_format"] == COMPLETED_FORMAT:
                assert completed_run(payload).campaign_id == state.run(run_id)["campaign_id"]
                assert "brief" not in saved
                continue
            assert saved["storage_format"] == STORAGE_FORMAT
            assert "outputs" not in saved
            assert load_plan(payload).outputs
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_run_and_applied_plan_are_only_in_sqlite(tmp_path):
    state, runner, run_id, brief = setup_case(tmp_path, 2, True)
    project = state.storage.project(brief.campaign_id)
    asyncio.run(runner.execute(run_id))
    edit = FrameEdit(
        run_id=run_id,
        revision_id=read(state.storage, brief.campaign_id)["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        framing=Framing(zoom=1.23),
    )
    Workspace(state).save_composition(brief.campaign_id, edit)
    reopened = State(state.storage)
    assert_normalized(reopened, run_id)
    with pytest.raises(StateError, match="production is complete"):
        reopened.plan(run_id, current=False)
    assert (
        Workspace(reopened).current_composition(brief.campaign_id)["plan"].outputs[0].framing.zoom
        == 1.23
    )
    assert not state.storage.path(project.ref("Outputs/plan.json")).exists()
    assert not state.storage.path(project.ref("Outputs/manifest.json")).exists()
    assert not list(state.storage.path(project.run(run_id)).rglob("*plan.json"))


@pytest.fixture
def rc2_plan_case(legacy):
    state, run_id = legacy.state, legacy.run
    old_file = state.run(run_id)["plan_ref"]
    legacy.project.write(old_file, legacy.plan.model_dump_json(indent=2).encode())
    original_import = legacy.project.ref("Brief/Imports/original.json")
    legacy.project.write(original_import, b'{"original": "Keep exact submitted input"}')
    notes = state.storage.path(legacy.project.run(run_id)) / "producer notes.json"
    notes.write_text('{"keep": true}')
    with state.storage.connect() as db:
        db.execute("UPDATE runs SET plan_json=NULL WHERE id=?", (run_id,))
        db.execute(
            "UPDATE revisions SET plan_json=? WHERE id=?",
            (legacy.plan.model_dump_json(), legacy.revision),
        )
        db.execute("PRAGMA user_version=13")
    legacy.old_file = old_file
    legacy.original_import = original_import
    legacy.notes = notes
    return legacy


def test_rc2_metadata_migration_keeps_sources_and_user_files_and_retires_working_images(
    rc2_plan_case,
):
    case = rc2_plan_case
    reopened = State(case.storage)
    assert_normalized(reopened, case.run)
    with pytest.raises(StateError, match="production is complete"):
        reopened.plan(case.run, current=False)
    current = Workspace(reopened).current_composition(case.cid)
    assert current["plan"].brief.products[0].framing["en-US"]["1:1"].zoom == 1.23
    assert current["plan"].brief.markets["en-US"].text.message == "New Campaign headline"
    assert not case.storage.path(case.old_file).exists()
    assert (
        case.storage.path(case.original_import).read_bytes()
        == b'{"original": "Keep exact submitted input"}'
    )
    assert case.notes.read_text() == '{"keep": true}'
    assert not any(case.storage.path(ref).exists() for ref in case.files)
    with case.storage.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (WORKSPACE_SCHEMA,)
        assert not db.execute(
            "SELECT 1 FROM file_owners WHERE reference=?", (case.old_file,)
        ).fetchone()
        assert not db.execute("SELECT 1 FROM workspace_migrations").fetchone()
    assert Workspace(State(case.storage)).current_composition(case.cid)["plan"] == current["plan"]


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_plan_migration_resumes_without_losing_its_only_copy(rc2_plan_case, monkeypatch, stage):
    case = rc2_plan_case

    def interrupt(*args, **kwargs):
        raise SystemExit("controlled interruption")

    with monkeypatch.context() as patch:
        patch.setattr(
            Cleanup, "record_intent" if stage == "before_commit" else "_resume", interrupt
        )
        with pytest.raises(SystemExit, match="controlled interruption"):
            State(case.storage)
    assert case.storage.path(case.old_file).is_file()
    with case.storage.connect() as db:
        payload = db.execute("SELECT plan_json FROM runs WHERE id=?", (case.run,)).fetchone()[0]
        assert (payload is None) == (stage == "before_commit")
    reopened = State(case.storage)
    current = Workspace(reopened).current_composition(case.cid)
    assert current["plan"].brief.products[0].framing["en-US"]["1:1"].zoom == 1.23
    assert not case.storage.path(case.old_file).exists()
    assert_normalized(reopened, case.run)


def test_damaged_original_plan_is_not_deleted_or_replaced(rc2_plan_case):
    case = rc2_plan_case
    case.storage.path(case.old_file).write_bytes(b"damaged plan")
    State(case.storage)
    assert case.storage.path(case.old_file).read_bytes() == b"damaged plan"
    with case.storage.connect() as db:
        assert db.execute("SELECT plan_json FROM runs WHERE id=?", (case.run,)).fetchone() == (
            None,
        )
        assert db.execute("SELECT name FROM workspace_migrations").fetchall() == [
            (f"campaign_outputs:{case.cid}",)
        ]
    assert all(case.storage.path(ref).exists() for ref in case.files)


def test_paid_request_snapshot_and_uncertain_attempt_survive_without_plan_files(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, supplied = write_sample(storage, 2, True)
    value = supplied.model_dump(mode="json")
    value["products"][1]["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "A small brass lantern in an autumn scene.",
        "provenance": value["copy_provenance"],
    }
    state = State(storage)
    run_id = state.create(make_plan(Brief.model_validate(value), storage), 1)
    confirmed = state.plan(run_id)
    state.confirm(run_id, confirmed.digest)
    with storage.connect() as db:
        db.execute("UPDATE runs SET state='running' WHERE id=?", (run_id,))
    attempt = state.reserve(run_id, "product-2")
    before = state.campaign_metrics(supplied.campaign_id)
    reopened = State(storage)
    reopened.reconcile_interrupted()
    assert reopened.plan(run_id, current=False).generation_requests == confirmed.generation_requests
    assert reopened.plan(run_id, current=False).cost_estimate == confirmed.cost_estimate
    assert reopened.run(run_id)["allowance"] == 1
    assert reopened.attempts(run_id)[0]["id"] == attempt["id"]
    assert reopened.attempts(run_id)[0]["state"] == "unknown"
    assert (
        reopened.campaign_metrics(supplied.campaign_id)["usage_cost_estimate"]
        == before["usage_cost_estimate"]
    )
    assert len(reopened.attempts(run_id)) == 1
    assert_normalized(reopened, run_id)
