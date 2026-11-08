"""Failed preparation remains owned, including abrupt exits before run publication."""

import hashlib
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from campaign_fixtures import independent_brief, run_folder, seed_campaign
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.planning import Plan, make_plan
from campaign_studio.runner import Runner
from campaign_studio.sample import write_replay, write_sample
from campaign_studio.state import State
from campaign_studio.storage import Storage, StorageError
from campaign_studio.workspace import Workspace


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {"state.sqlite3", "runner.lock"}
        and path != root / "private" / "usage.lock"
    }


def prepared(root):
    storage = Storage(root)
    _, brief = write_sample(storage, 2, True)
    state = State(storage)
    return state, make_plan(brief, storage)


def test_failed_run_insert_keeps_preparation_owned_and_preserves_shared_survivor(tmp_path):
    state, plan = prepared(tmp_path / "runtime")
    old_run = state.create(plan, 0)
    old_files = hashes(state.storage.path(run_folder(state, old_run)))
    survivor = state.create(
        make_plan(
            independent_brief(state, plan.brief, "3bfc470d-bd98-5680-9d69-4c5197769b07"),
            state.storage,
        ),
        0,
    )
    survivor_files = hashes(state.storage.path(run_folder(state, survivor)))
    # Unregistered pre-existing content is outside any automatic cleanup claim.
    state.storage.write("private/runs/unowned-old/keep.txt", b"unowned original")
    with state.storage.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_run BEFORE INSERT ON runs "
            "BEGIN SELECT RAISE(FAIL,'controlled publication failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="controlled publication failure"):
        state.create(plan, 0)
    assert hashes(state.storage.path(run_folder(state, old_run))) == old_files
    assert hashes(state.storage.path(run_folder(state, survivor))) == survivor_files
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_run")
        owned = [
            row[0]
            for row in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=?",
                (plan.brief.campaign_id,),
            )
        ]
    failed_root = next(
        reference
        for reference in owned
        if "/Runs/" in reference
        and reference.count("/") == 3
        and reference != run_folder(state, old_run)
    )
    shared = next(state.storage.path(failed_root + "/Inputs/products").iterdir())
    shared_bytes = shared.read_bytes()
    intake = Intake(state.storage)
    other, document = intake.new()
    document["value"]["products"] = [plan.brief.products[0].model_dump(mode="json")]
    document["value"]["products"][0]["source"]["path"] = shared.relative_to(
        state.storage.root
    ).as_posix()
    copy_ref = intake.upload(shared_bytes, None, other)["reference"]
    document["value"]["products"][0]["source"]["path"] = copy_ref
    intake.write(other, document["value"], document["version"])
    # Reopening does not sweep failed preparations or make them executable.
    reopened = State(state.storage)
    assert Cleanup(reopened).resume_all() == []
    cleanup = Cleanup(reopened)
    preview = cleanup.preview_campaign(plan.brief.campaign_id)
    assert preview["unmanaged_file_count"] == 0
    assert cleanup.delete_campaign(plan.brief.campaign_id, preview["token"])["state"] == "complete"
    assert not shared.exists() and state.storage.path(copy_ref).read_bytes() == shared_bytes
    assert hashes(state.storage.path(run_folder(state, survivor))) == survivor_files
    assert (
        state.storage.path("private/runs/unowned-old/keep.txt").read_bytes() == b"unowned original"
    )
    for reference in owned:
        files = list(state.storage.path(reference).rglob("*"))
        assert all(not p.is_file() or p == shared for p in files)
    # The surviving reference is recorded and reclaimed by its own later confirmation.
    other_preview = cleanup.preview_campaign(other)
    assert cleanup.delete_campaign(other, other_preview["token"])["state"] == "complete"
    assert not shared.exists()
    with state.storage.connect() as db:
        assert db.execute("SELECT id FROM runs").fetchall() == [(survivor,)]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_owner_registration_failure_writes_no_preparation_content(tmp_path):
    state, plan = prepared(tmp_path / "runtime")
    before = hashes(state.storage.root)
    with state.storage.connect() as db:
        owners = db.execute("SELECT * FROM file_owners").fetchall()
        db.execute(
            "CREATE TRIGGER fail_owner BEFORE INSERT ON file_owners "
            "WHEN NEW.reference LIKE '%/Runs/%' "
            "BEGIN SELECT RAISE(FAIL,'controlled owner failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="controlled owner failure"):
        state.create(plan, 0)
    assert hashes(state.storage.root) == before
    assert not list(
        state.storage.path(state.storage.project(plan.brief.campaign_id).ref("Runs")).glob("*")
    )
    with state.storage.connect() as db:
        assert db.execute("SELECT * FROM file_owners").fetchall() == owners
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("checkpoint", ["temporary-input", "first-input", "uncommitted-plan"])
def test_abrupt_preparation_exit_is_owned_until_confirmed_cleanup(tmp_path, checkpoint):
    state, plan = prepared(tmp_path / "runtime")
    project_folder = state.storage.project(plan.brief.campaign_id).folder
    original_inputs = hashes(
        state.storage.path("Campaigns").joinpath(project_folder, "Assets", "Samples")
    )
    plan_file = tmp_path / "prepared-plan.json"
    plan_file.write_text(plan.model_dump_json(), encoding="utf-8")
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.run(
        [sys.executable, __file__, str(state.storage.root), str(plan_file), checkpoint],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 73, process.stdout + process.stderr
    reopened = State(state.storage)
    Runner(reopened)
    cleanup = Cleanup(reopened)
    assert cleanup.resume_all() == []
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        owned = [
            row[0]
            for row in db.execute(
                "SELECT reference FROM file_owners WHERE reference LIKE '%/Runs/%'"
            )
        ]
    assert owned
    assert all(ref.startswith(f"Campaigns/{project_folder}/Runs/") for ref in owned)
    contents = [state.storage.path(ref) for ref in owned if state.storage.path(ref).is_file()]
    assert contents and sum(p.stat().st_size for p in contents) > 0
    assert (
        hashes(state.storage.path("Campaigns").joinpath(project_folder, "Assets", "Samples"))
        == original_inputs
    )
    if checkpoint == "temporary-input":
        assert any(p.suffix == ".tmp" for p in contents)
    if checkpoint == "uncommitted-plan":
        assert len(contents) == len(plan.assets)
        assert not any(p.name == "plan.json" for p in contents)
    preview = cleanup.preview_campaign(plan.brief.campaign_id)
    assert preview["run_count"] == 0
    assert cleanup.delete_campaign(plan.brief.campaign_id, preview["token"])["state"] == "complete"
    assert all(not state.storage.path(ref).exists() for ref in owned)
    assert not hashes(
        state.storage.path("Campaigns").joinpath(project_folder, "Assets", "Samples")
    )  # These, too, are owned campaign sources.
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM file_owners").fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_workbench_opens_one_current_composition_without_revision_history(tmp_path):
    from campaign_studio.brief import Framing
    from campaign_studio.campaign_drafts import read
    from campaign_studio.workspace import FrameEdit

    app = create_app(Settings(data_dir=tmp_path / "runtime"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        cid = seed_campaign(ws, replay=True)
        product_id = read(ws.storage, cid)["authored"]["brief"]["products"][0]["id"]
        for zoom in (1.1, 1.2, 1.3):
            ws.save_composition(
                cid,
                FrameEdit(
                    revision_id=read(ws.storage, cid)["version"],
                    product_id=product_id,
                    market="en-US",
                    ratio="1:1",
                    framing=Framing(zoom=zoom),
                ),
            )
            catalog = client.get("/api/motif/workspace").json()["campaigns"]
            row = next(c for c in catalog if c["id"] == cid)
            detail = client.get(f"/api/motif/campaigns/{cid}").json()
            assert row["available_images"] == len(detail["manifest"]["outputs"]) == 12
            assert detail["output_brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == zoom
        with ws.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM campaign_drafts").fetchone()[0] == 1


@pytest.mark.parametrize("replay,namespace", [(True, "Replays"), (False, "Samples")])
def test_repeated_fixture_delete_reclaims_runtime_copies_only(tmp_path, replay, namespace):
    state = State(Storage(tmp_path / "runtime"))
    workspace = Workspace(state)
    cleanup = Cleanup(state)
    resources = Path(__file__).resolve().parents[1] / "src/campaign_studio/resources/replay"
    package_before = hashes(resources)
    state.storage.write(f"{namespace}/unowned-old/keep.png", b"unowned legacy content")
    for _ in range(2):
        campaign = seed_campaign(workspace, replay=replay)
        with state.storage.connect() as db:
            prefixes = [
                row[0]
                for row in db.execute(
                    "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign,)
                )
            ]
        assert any(f"/Assets/{namespace}/" in prefix for prefix in prefixes)
        preview = cleanup.preview_campaign(campaign)
        assert (
            preview["source_file_count"] == 3
        )  # Two source images and a logo, without run copies.
        assert cleanup.delete_campaign(campaign, preview["token"])["state"] == "complete"
        assert all(not state.storage.path(prefix).exists() for prefix in prefixes)
        assert state.storage.path(f"{namespace}/unowned-old/keep.png").read_bytes() == (
            b"unowned legacy content"
        )
        assert hashes(resources) == package_before
        with state.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM file_owners").fetchone()[0] == 0


def test_shared_original_replay_copy_survives_until_last_campaign_deletion(tmp_path):
    state = State(Storage(tmp_path / "runtime"))
    workspace = Workspace(state)
    campaign = seed_campaign(workspace, replay=True)
    intake = Intake(state.storage)
    other, document = intake.new()
    copied = next(
        state.storage.path(state.storage.project(campaign).ref("Assets/Replays")).rglob(
            "saved-concept.png"
        )
    )
    payload = copied.read_bytes()
    plan = workspace.plan(campaign)
    document["value"]["products"] = [plan.brief.products[0].model_dump(mode="json")]
    document["value"]["products"][0]["source"]["path"] = copied.relative_to(
        state.storage.root
    ).as_posix()
    with pytest.raises(StorageError):
        intake.write(other, document["value"], document["version"])
    independent = intake.upload(payload, None, other)["reference"]
    document["value"]["products"][0]["source"]["path"] = independent
    intake.write(other, document["value"], document["version"])
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(campaign)
    assert preview["unmanaged_file_count"] == 0
    assert cleanup.delete_campaign(campaign, preview["token"])["state"] == "complete"
    assert not copied.exists() and state.storage.path(independent).read_bytes() == payload
    preview = cleanup.preview_campaign(other)
    assert cleanup.delete_campaign(other, preview["token"])["state"] == "complete"
    assert not state.storage.path(independent).exists()


@pytest.mark.parametrize("copy_sources", [write_replay, write_sample])
def test_source_namespace_registration_failure_precedes_creative_writes(tmp_path, copy_sources):
    state = State(Storage(tmp_path / "runtime"))
    before = hashes(state.storage.root)
    with state.storage.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_owner BEFORE INSERT ON file_owners "
            "BEGIN SELECT RAISE(FAIL,'controlled source ownership failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="controlled source ownership failure"):
        copy_sources(state.storage)
    assert hashes(state.storage.root) == before


def interrupt_preparation(root, plan_file, checkpoint):
    state = State(Storage(Path(root)))
    plan = Plan.model_validate_json(Path(plan_file).read_bytes())
    original_write = state.storage.write
    from campaign_studio import storage as storage_module

    original_publish = storage_module.publish_file

    def publish(source, destination, *, replace):
        if checkpoint == "temporary-input" and "Inputs" in destination.parts:
            os._exit(73)  # The real write has flushed its temporary bytes; no finally runs.
        return original_publish(source, destination, replace=replace)

    def write(reference, data):
        original_write(reference, data)
        if checkpoint == "first-input" and "/Inputs/" in reference:
            os._exit(73)

    original_connect = state.storage.connect

    class InterruptedConnection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, parameters=()):
            cursor = self.db.execute(sql, parameters)
            if checkpoint == "uncommitted-plan" and sql.startswith("INSERT INTO runs "):
                os._exit(73)  # The real SQLite insertion must roll back on restart.
            return cursor

    @contextmanager
    def connect():
        with original_connect() as db:
            yield InterruptedConnection(db)

    state.storage.connect = connect
    storage_module.publish_file = publish
    state.storage.write = write
    state.create(plan, 0)
    raise AssertionError("The real process never reached the intended interruption")


if __name__ == "__main__":
    interrupt_preparation(*sys.argv[1:])
