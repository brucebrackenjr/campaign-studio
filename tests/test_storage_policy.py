import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.handoff import Handoff, PackageAction
from campaign_studio.intake import Intake
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import CreativeUpdate, RevisionTarget
from campaign_studio.storage import CapacityError


@pytest.mark.parametrize("kind", ["workspace_budget", "disk_space"])
def test_launched_recovery_returns_measured_capacity_without_changing_saved_work(tmp_path, kind):
    from test_web_workflow import wait_for_work

    state, runner, run, brief = setup_case(tmp_path, one_market=True)
    assert asyncio.run(runner.execute(run))["complete"]
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Capacity refusal must not construct a provider"),
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
        with storage.connect() as db:
            rows_before = list(db.iterdump())
        client.headers["x-campaign-token"] = app.state.csrf
        accepted = client.post(f"/api/campaigns/{brief.campaign_id}/recover", json={"run_id": run})
        assert accepted.status_code == 202
        result = wait_for_work(client, brief.campaign_id)
        assert result["notice_code"] == "storage_capacity"
        capacity = result["capacity"]
        assert capacity["kind"] == kind and capacity["projected_bytes"] > 0
        assert capacity["budget_bytes"] == storage.budget_bytes
        assert capacity["reserve_bytes"] == storage.reserve_bytes
        assert capacity["used_bytes"] >= measured["used_bytes"]
        assert capacity["free_bytes"] > 0
        assert result["attempted"] == 0
        assert (
            client.get(f"/api/campaigns/{brief.campaign_id}/status").json()["capacity"] == capacity
        )
        assert {
            p.relative_to(storage.root): p.read_bytes()
            for p in storage.root.rglob("*")
            if p.is_file() and not p.name.startswith("state.sqlite3")
        } == before
        with storage.connect() as db:
            assert list(db.iterdump()) == rows_before


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
    # A private plan can outlive its output directory. It still protects identity.
    orphan = uuid4().hex
    state.storage.write(
        state.storage.project(brief.campaign_id).ref(f"Runs/{run}/Revisions/{orphan}/plan.json"),
        b"preserved",
    )
    first = True

    def colliding_revision():
        nonlocal first
        if first:
            first = False
            return SimpleNamespace(hex=orphan)
        return uuid4()

    monkeypatch.setattr("campaign_studio.storage.uuid4", colliding_revision)
    assert state.reserve_revision(run) != orphan
    assert (
        state.storage.path(
            state.storage.project(brief.campaign_id).ref(f"Runs/{run}/Revisions/{orphan}/plan.json")
        ).read_bytes()
        == b"preserved"
    )
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
    with pytest.raises(CapacityError, match="storage limit"):
        asyncio.run(runner.execute(run, provider))
    attempt = state.attempts(run)[0]
    assert len(requests) == 1 and attempt["state"] == "decoded"
    assert state.storage.path(attempt["response_ref"]).is_file()
    assert state.source_is_intact(attempt)
    assert state.storage.summary()["over_budget"]
    state.storage.budget_bytes = 10_000_000_000
    recovered = runner.recover_local(run)
    assert recovered["complete"] and len(state.attempts(run)) == len(requests) == 1


def test_exact_package_reuse_over_budget_and_changed_snapshot_requires_new_space(tmp_path):
    state, runner, run, _ = setup_case(tmp_path)
    manifest = asyncio.run(runner.execute(run))
    revision = manifest["revision_id"]
    target = RevisionTarget(
        campaign_id="80d58cbe-5e57-5544-9b07-a4f018714dde",
        run_id=run,
        revision_id=revision,
        mode="saved_version",
    )
    handoff = Handoff(state)
    action = PackageAction(snapshot_id=handoff.metadata(target)["snapshot_id"])
    archive = handoff.create_zip(target, action)
    folder = handoff.create_folder(target, action)
    original_zip = handoff.download(target, archive["artifact_id"])
    assert handoff.create_zip(target, action) == archive
    assert handoff.create_folder(target, action) == folder
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM package_exports").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT COUNT(*) FROM package_exports WHERE reference LIKE '%.zip'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM package_exports WHERE reference LIKE '%/index.html'"
            ).fetchone()[0]
            == 1
        )
    state.storage.budget_bytes = 1
    assert handoff.create_zip(target, action) == archive
    assert handoff.create_folder(target, action) == folder
    assert handoff.download(target, archive["artifact_id"]) == original_zip
    state.update_creative(
        revision,
        "product-1/en-US/1x1",
        CreativeUpdate(expected_version=0, description="Retained while over budget."),
    )
    new_action = PackageAction(snapshot_id=handoff.metadata(target)["snapshot_id"])
    with pytest.raises(CapacityError):
        handoff.create_zip(target, new_action)
    with pytest.raises(CapacityError):
        handoff.create_folder(target, new_action)
    state.storage.budget_bytes = 10_000_000_000
    new_folder = handoff.create_folder(target, new_action)
    assert new_folder != folder and handoff.create_folder(target, new_action) == new_folder
    (Path(new_folder["location"]) / "index.html").write_text("altered report")
    repaired = handoff.create_folder(target, new_action)
    assert repaired != new_folder and handoff.create_folder(target, new_action) == repaired
    assert handoff.download(target, archive["artifact_id"]) == original_zip


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


def test_package_identity_exhaustion_is_bounded_and_preserves_existing_rows(tmp_path, monkeypatch):
    from campaign_studio.state import StateError

    state, _, run, _ = setup_case(tmp_path)
    target = RevisionTarget(
        campaign_id="80d58cbe-5e57-5544-9b07-a4f018714dde",
        run_id=run,
        revision_id="a" * 32,
        mode="saved_version",
    )
    calls = []
    monkeypatch.setattr(
        state, "identity_exists", lambda table, identity: calls.append(identity) or True
    )
    with pytest.raises(StateError, match="identity could not be reserved"):
        state.register_package(target, "exports/example.zip", "0" * 64, "1" * 64)
    assert len(calls) == 100
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM package_exports").fetchone()[0] == 0


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
    assert result["metrics"]["completed_images"] == 6
