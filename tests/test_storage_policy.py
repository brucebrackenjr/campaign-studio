import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.handoff import Handoff
from campaign_studio.intake import Intake
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import CreativeUpdate
from campaign_studio.storage import CapacityError


@pytest.mark.parametrize("kind", ["workspace_budget", "disk_space"])
def test_completed_source_recovery_does_not_allocate_finished_images_when_capacity_is_low(
    tmp_path, kind
):
    from test_web_workflow import wait_for_work

    from campaign_studio.workspace import Workspace

    state, runner, run, brief = setup_case(tmp_path, one_market=True)
    assert asyncio.run(runner.execute(run))["complete"]
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Completed recovery must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        storage = app.state.storage
        measured = storage.summary()
        if kind == "workspace_budget":
            storage.budget_bytes = measured["used_bytes"]
        else:
            storage.reserve_bytes = measured["free_bytes"] + 1
        before = {
            p.relative_to(storage.root): p.read_bytes()
            for p in storage.root.rglob("*")
            if p.is_file() and not p.name.startswith("state.sqlite3")
        }
        current = Workspace(state).current_composition(brief.campaign_id)
        client.headers["x-campaign-token"] = app.state.csrf
        accepted = client.post(f"/api/campaigns/{brief.campaign_id}/recover", json={"run_id": run})
        assert accepted.status_code == 202, accepted.text
        result = wait_for_work(client, brief.campaign_id)
        assert not result["active"]
        assert result.get("notice_code") != "storage_capacity"
        assert state.attempts(run) == []
        assert (
            Workspace(state).current_composition(brief.campaign_id)["version"] == current["version"]
        )
        assert {
            p.relative_to(storage.root): p.read_bytes()
            for p in storage.root.rglob("*")
            if p.is_file() and not p.name.startswith("state.sqlite3")
        } == before
        assert not list(storage.root.glob("Campaigns/*/Outputs"))


def test_namespace_and_upload_collisions_never_replace_old_records(tmp_path, monkeypatch):
    state, _, run, brief = setup_case(tmp_path)
    saved = {
        p.relative_to(state.storage.root).as_posix(): p.read_bytes()
        for p in state.storage.path(state.storage.project(brief.campaign_id).run(run)).rglob("*")
        if p.is_file()
    }
    calls = 0

    def colliding_run():
        nonlocal calls
        calls += 1
        return SimpleNamespace(hex=run) if calls == 1 else uuid4()

    monkeypatch.setattr("campaign_studio.storage.uuid4", colliding_run)
    second = state.create(make_plan(brief, state.storage), 0)
    assert second != run
    for reference, data in saved.items():
        assert state.storage.path(reference).read_bytes() == data
    assert state.run(run)["state"] == "confirmed"
    # An interrupted output write protects its identity before any revision row exists.
    orphan = uuid4().hex
    relative = state.write_output(run, orphan, state.plan(run).outputs[0], b"preserved")
    first = True

    def colliding_revision():
        nonlocal first
        if first:
            first = False
            return SimpleNamespace(hex=orphan)
        return uuid4()

    monkeypatch.setattr("campaign_studio.storage.uuid4", colliding_revision)
    assert state.reserve_revision(run) != orphan
    output = state.storage.project(brief.campaign_id).ref(f"Outputs/{relative}")
    assert state.storage.path(output).read_bytes() == b"preserved"
    intake = Intake(state.storage)
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    upload = intake.upload(payload, "image/png", brief.campaign_id)
    token = Path(upload["reference"]).stem
    choices = iter([SimpleNamespace(hex=token), uuid4()])
    monkeypatch.setattr("campaign_studio.intake.uuid4", lambda: next(choices))
    another = intake.upload(payload, "image/png", brief.campaign_id)
    assert upload["reference"] != another["reference"]
    assert state.storage.path(upload["reference"]).read_bytes() == payload


def test_new_ids_are_full_and_do_not_overwrite_existing_draft_files(tmp_path, monkeypatch):
    state, _, _, _ = setup_case(tmp_path)
    intake = Intake(state.storage)
    blocked = uuid4()
    state.register_campaign(str(blocked), "Reserved")
    reference = state.storage.project(str(blocked)).draft
    state.storage.write(reference, b"Existing draft stays exact")
    first = True

    def choose():
        nonlocal first
        if first:
            first = False
            return blocked
        return uuid4()

    monkeypatch.setattr("campaign_studio.intake.uuid4", choose)
    cid, document = intake.new()
    assert cid != str(blocked) and len(cid) == 36
    assert state.storage.path(reference).read_bytes() == b"Existing draft stays exact"
    assert document["value"]["products"] == []
    assert set(document["value"]["markets"]) == {"en-US", "es-MX"}
    assert len(document["value"]["brand"]["id"].removeprefix("brand-")) == 32


def test_over_budget_blocks_before_provider_factory_and_attempt_but_not_controls(tmp_path):
    state, _, run, _ = generated_case(tmp_path)
    budget = state.storage.summary()["used_bytes"] + 1
    app = create_app(
        Settings(data_dir=state.storage.root, storage_budget_bytes=budget),
        provider_factory=lambda: pytest.fail("provider constructed"),
        key_available=lambda: True,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        plan = state.plan(run)
        response = client.post(
            f"/api/campaigns/{plan.brief.campaign_id}/start",
            json={
                "run_id": run,
                "digest": plan.digest,
                "allowance": 1,
            },
        )
        assert response.status_code == 409 and response.json()["code"] == "storage_capacity"
        assert response.json()["capacity"]["kind"] == "workspace_budget"
        assert response.json()["capacity"]["budget_bytes"] == budget
        assert "CAMPAIGN_STUDIO" not in response.json()["message"]
        assert state.attempts(run) == []
        assert client.get("/health").status_code == 200
        state.stop(run)
        assert state.run(run)["stop_requested"] == 1


def test_free_disk_guard_and_returned_result_preservation(tmp_path, monkeypatch):
    state, runner, run, response = generated_case(tmp_path)
    normal_summary = state.storage.summary
    monkeypatch.setattr(state.storage, "summary", lambda: {**normal_summary(), "free_bytes": 1})
    with pytest.raises(CapacityError, match="not enough free space"):
        asyncio.run(
            runner.execute(run, SimpleNamespace(generate=lambda *a: pytest.fail("dispatch")))
        )
    assert state.attempts(run) == []
    monkeypatch.setattr(state.storage, "summary", normal_summary)
    requests = []

    def transport(request):
        requests.append(request)
        # Simulate other storage growth or a response larger than the admission estimate.
        # The result must still be preserved; there is no blanket low-level write guard.
        state.storage.budget_bytes = state.storage.summary()["used_bytes"]
        return httpx.Response(200, json=response)

    provider = OpenAIImages("synthetic-only", transport=httpx.MockTransport(transport))
    result = asyncio.run(runner.execute(run, provider))
    assert result["complete"] and result["outputs"] == []
    attempt = state.attempts(run)[0]
    assert len(requests) == 1 and attempt["state"] == "decoded"
    assert state.storage.path(attempt["response_ref"]).is_file()
    assert state.source_is_intact(attempt)
    assert state.storage.summary()["over_budget"]
    state.storage.budget_bytes = 10_000_000_000
    recovered = runner.recover_local(run)
    assert recovered["complete"] and len(state.attempts(run)) == len(requests) == 1


def test_downloads_and_report_previews_work_over_budget_without_writing_to_workspace(tmp_path):
    from campaign_studio.campaign_drafts import read
    from campaign_studio.handoff import DeliveryAction
    from campaign_studio.workspace import Workspace

    state, runner, run, brief = setup_case(tmp_path)
    assert asyncio.run(runner.execute(run))["complete"]
    workspace = Workspace(state)
    handoff = Handoff(state)

    def read_download():
        version = read(state.storage, brief.campaign_id)["version"]
        with (
            handoff.create_current_zip(
                brief.campaign_id, DeliveryAction(version=version)
            ) as download,
            ZipFile(download.stream) as archive,
        ):
            name = next(n for n in archive.namelist() if n.endswith("/manifest.json"))
            return json.loads(archive.read(name))

    original = read_download()
    state.storage.budget_bytes = 1
    assert {o["id"]: o["pixel_sha256"] for o in read_download()["outputs"]} == {
        o["id"]: o["pixel_sha256"] for o in original["outputs"]
    }
    version = read(state.storage, brief.campaign_id)["version"]
    description = CreativeUpdate(expected_version=0, description="Retained while over budget.")
    from campaign_studio.compositions import save_metadata

    save_metadata(workspace, brief.campaign_id, version, "product-1/en-US/1x1", description)
    updated = read_download()
    assert "Retained while over budget." in json.dumps(updated["outputs"])
    current = workspace.current_composition(brief.campaign_id)
    report = handoff.create_current_preview(
        brief.campaign_id, DeliveryAction(version=current["version"])
    )
    assert report["url"].startswith("/reports/current/")
    project = state.storage.project(brief.campaign_id)
    assert not state.storage.path(project.ref("Exports")).exists()
    assert not state.storage.path(project.ref("Outputs")).exists()
    with state.storage.connect() as db:
        assert not db.execute("SELECT 1 FROM package_exports").fetchone()
        assert not db.execute(
            "SELECT 1 FROM file_owners WHERE reference LIKE '%/Exports/%'"
        ).fetchone()


def test_inventory_permission_failure_refuses_growth_without_partial_usage(tmp_path, monkeypatch):
    import os

    from campaign_studio.storage import StorageError

    state, _, _, _ = setup_case(tmp_path)
    original = os.scandir

    def denied(path):
        if Path(path) == state.storage.path("private"):
            raise PermissionError("PRIVATE_INVENTORY_CANARY")
        return original(path)

    monkeypatch.setattr("campaign_studio.storage.os.scandir", denied)
    with pytest.raises(StorageError, match="measured completely") as error:
        state.storage.require_capacity(1)
    assert "PRIVATE_INVENTORY_CANARY" not in str(error.value)
    monkeypatch.setattr("campaign_studio.storage.os.scandir", original)
    original_stat = Path.lstat

    def denied_file(path):
        if path == state.storage.root / "private/state.sqlite3":
            raise PermissionError("PRIVATE_FILE_CANARY")
        return original_stat(path)

    monkeypatch.setattr(Path, "lstat", denied_file)
    with pytest.raises(StorageError, match="measured completely") as error:
        state.storage.require_capacity(1)
    assert "PRIVATE_FILE_CANARY" not in str(error.value)


def test_capacity_scans_do_not_block_async_start_status_or_stop(tmp_path, monkeypatch):
    import threading

    from campaign_studio.capacity import production_bytes

    state, runner, run, _ = generated_case(tmp_path)
    plan = state.plan(run)
    entered = threading.Event()
    release = threading.Event()
    original = state.storage.require_capacity

    def delayed(estimate):
        entered.set()
        assert release.wait(3), "event loop did not release the inventory worker"
        return original(estimate)

    monkeypatch.setattr(state.storage, "require_capacity", delayed)

    async def runner_probe():
        task = asyncio.create_task(
            runner.execute(
                run, SimpleNamespace(generate=lambda *a: pytest.fail("dispatch after Stop"))
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # This turn must run while inventory is still blocked in its worker.
            await asyncio.sleep(0)
            state.stop(run)
        finally:
            release.set()
        result = await task
        assert not result["complete"] and state.attempts(run) == []

    asyncio.run(runner_probe())

    # Exercise the independent HTTP admission boundary with a concurrent request.
    second = state.create(plan, 1)
    from campaign_studio.campaign_drafts import read, write
    from campaign_studio.planning import canonical_hash

    current = read(state.storage, plan.brief.campaign_id)
    current.update(
        planned_run=second, planned_authored_hash=canonical_hash(current["authored"]["brief"])
    )
    current["authored_base"] = {"run_id": second, "revision_id": None, "metadata_snapshot_id": None}
    write(state.storage, plan.brief.campaign_id, current)
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("no live construction"),
    )

    async def http_probe():
        async with app.router.lifespan_context(app):
            entered.clear()
            release.clear()
            monkeypatch.setattr(app.state.storage, "require_capacity", delayed)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                task = asyncio.create_task(
                    client.post(
                        f"/api/campaigns/{plan.brief.campaign_id}/start",
                        json={
                            "run_id": second,
                            "digest": state.plan(second).digest,
                            "allowance": 1,
                        },
                        headers={"X-Campaign-Token": app.state.csrf},
                    )
                )
                try:
                    assert await asyncio.to_thread(entered.wait, 2)
                    health = await asyncio.wait_for(client.get("/health"), 0.5)
                    assert health.status_code == 200
                finally:
                    release.set()
                response = await task
                assert response.status_code == 409 and response.json()["code"] == "missing_key"

    asyncio.run(http_probe())
    assert state.attempts(second) == []
    assert production_bytes(plan, 1) > 64_000_000


def test_stop_during_per_attempt_admission_finishes_as_stopped(tmp_path, monkeypatch):
    import threading

    state, runner, run, _ = generated_case(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = state.storage.require_capacity
    admissions = 0

    def gated(estimate):
        nonlocal admissions
        admissions += 1
        if admissions == 2:  # Runner admission passed; the paid attempt is still unreserved.
            entered.set()
            assert release.wait(3)
        return original(estimate)

    monkeypatch.setattr(state.storage, "require_capacity", gated)

    async def exercise():
        task = asyncio.create_task(
            runner.execute(
                run, SimpleNamespace(generate=lambda *a: pytest.fail("dispatch after Stop"))
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            state.stop(run)
        finally:
            release.set()
        return await task

    result = asyncio.run(exercise())
    assert not result["complete"]
    assert state.run(run)["state"] == "stopped"
    assert state.attempts(run) == []
    assert result["metrics"]["completed_images"] == 0
    assert result["sources_ready"] == 1
    assert not list(state.storage.root.glob("Campaigns/*/Outputs"))


def test_edit_capacity_tracks_other_process_writes_without_rescanning(tmp_path, monkeypatch):
    import subprocess
    import sys

    from campaign_studio.storage import Storage

    storage = Storage(tmp_path / "runtime")
    storage.refresh_edit_capacity()
    storage.write("private/asset.bin", b"first")
    writer = """
import sys
from pathlib import Path
from campaign_studio.storage import Storage

storage = Storage(Path(sys.argv[1]))
storage.write("private/asset.bin", bytes(27))
storage.write_new("private/another.bin", bytes(19))
storage.unlink(storage.path("private/asset.bin"))
"""
    subprocess.run([sys.executable, "-c", writer, str(storage.root)], check=True)
    measured = storage.summary()
    monkeypatch.setattr(storage, "summary", lambda: pytest.fail("An edit walked the workspace"))
    current = storage.require_edit_capacity(1)
    assert (current["used_bytes"], current["file_count"]) == (
        measured["used_bytes"],
        measured["file_count"],
    )
    storage.budget_bytes = current["used_bytes"]
    with pytest.raises(CapacityError) as error:
        storage.require_edit_capacity(1)
    assert error.value.details["kind"] == "workspace_budget"
