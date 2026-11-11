"""Mocked regeneration retains accepted pixels and accounts each deliberate request."""

import asyncio
import base64
import copy
import io
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import read, write
from campaign_studio.handoff import Handoff
from campaign_studio.motif import campaign_data
from campaign_studio.planning import canonical_hash, make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.regeneration import (
    RegenerationConfirm,
    RegenerationReview,
    existing,
    prepare,
    review,
)
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage
from campaign_studio.workspace import ActionError, Workspace


def image_response(color):
    stream = io.BytesIO()
    Image.new("RGB", (1920, 1920), color).save(stream, format="PNG")
    return {
        "data": [{"b64_json": base64.b64encode(stream.getvalue()).decode()}],
        "usage": {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
    }


@pytest.fixture
def generated(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage, 3)
    value = brief.model_dump(mode="json")
    for product in value["products"][:2]:
        product["source"] = {
            "role": "concept",
            "imagery_absent": True,
            "description": "A synthetic subject for mocked transport only.",
            "provenance": {
                "label": "Synthetic fixture",
                "rights": "Local fixture",
                "synthetic": True,
            },
        }
    state = State(storage)
    old_run = state.create(make_plan(Brief.model_validate(value), storage), 2)
    frozen = state.plan(old_run, current=False)
    authored = {"brief": frozen.brief.model_dump(mode="json")}
    write(
        storage,
        brief.campaign_id,
        {
            "authored": authored,
            "version": uuid4().hex,
            "composition_schema": 1,
            "planned_run": old_run,
            "planned_authored_hash": canonical_hash(authored["brief"]),
            "authored_base": {"run_id": old_run, "revision_id": None, "metadata_snapshot_id": None},
        },
    )
    state.confirm(old_run, frozen.digest)
    runner = Runner(state)
    body = image_response("#665588")
    provider = OpenAIImages(
        "synthetic", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    )
    asyncio.run(runner.execute(old_run, provider))
    ws = Workspace(state)
    cid = state.run(old_run)["campaign_id"]
    return ws, runner, cid


def confirmation(ws, cid, product="product-1"):
    data = campaign_data(ws, cid)
    proposed = review(ws, cid, RegenerationReview(version=data["version"], product_id=product))
    return proposed, RegenerationConfirm(
        **{key: proposed[key] for key in ("version", "product_id", "operation_id", "digest")}
    )


def test_review_is_read_only_and_only_app_generated_sources_are_eligible(generated):
    ws, _, cid = generated
    before = read(ws.storage, cid)
    proposed, action = confirmation(ws, cid)
    assert read(ws.storage, cid) == before
    assert proposed["product_id"] == "product-1"
    assert proposed["cost_estimate"]["request_count"] == 1
    assert len(ws.state.attempts(before["source_assets"]["product-1"]["run_id"])) == 2
    assert set(campaign_data(ws, cid)["regeneratable_products"]) == {"product-1", "product-2"}
    with pytest.raises(ActionError, match="supplied imagery"):
        review(ws, cid, RegenerationReview(version=action.version, product_id="product-3"))


def test_success_targets_one_product_and_old_confirmation_cannot_dispatch_again(generated):
    ws, runner, cid = generated
    before = read(ws.storage, cid)
    proposed, action = confirmation(ws, cid)
    run = prepare(ws, cid, action)
    frozen = ws.state.plan(run["id"], current=False)
    assert frozen.generation_products == ["product-1"]
    assert frozen.generation_requests["product-1"].model_dump() == proposed["request"]
    assert read(ws.storage, cid)["version"] == before["version"]
    assert read(ws.storage, cid)["source_assets"] == before["source_assets"]
    assert prepare(ws, cid, action)["id"] == run["id"]
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=image_response("#557799"))

    ws.state.confirm(run["id"], frozen.digest)
    result = asyncio.run(
        runner.execute(
            run["id"], OpenAIImages("synthetic", transport=httpx.MockTransport(transport))
        )
    )
    assert result["replacement_outcome"] == "replaced"
    after = read(ws.storage, cid)
    assert after["authored"] == before["authored"]
    assert after["version"] != before["version"]
    assert (
        after["source_assets"]["product-1"]["asset"]["sha256"]
        != before["source_assets"]["product-1"]["asset"]["sha256"]
    )
    for pid, record in before["source_assets"].items():
        assert ws.storage.path(record["asset"]["reference"]).is_file()
        if pid != "product-1":
            assert after["source_assets"][pid] == record
    assert existing(ws, cid, action)["id"] == run["id"]
    assert prepare(ws, cid, action)["id"] == run["id"]
    assert len(calls) == 1
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 3
    assert campaign_data(ws, cid)["manifest"]["complete"]


@pytest.mark.parametrize("outcome", ["failed", "timeout", "stop", "conflict"])
def test_failure_stop_or_changed_document_keeps_accepted_source_and_export(generated, outcome):
    ws, runner, cid = generated
    before = read(ws.storage, cid)
    _, action = confirmation(ws, cid)
    run = prepare(ws, cid, action)
    plan = ws.state.plan(run["id"], current=False)
    ws.state.confirm(run["id"], plan.digest)
    calls = []

    async def transport(request):
        calls.append(request)
        if outcome == "stop":
            ws.state.stop(run["id"])
        elif outcome == "conflict":
            project = AuthoredProjects(ws)
            current = project.read(cid)
            changed = copy.deepcopy(current["authored"])
            changed["brief"]["name"] = "Changed during generation"
            project.save(
                cid,
                AuthoredSave(version=current["version"], base=current["base"], authored=changed),
            )
        if outcome == "failed":
            return httpx.Response(400, json={"error": {"message": "Simulated failure"}})
        if outcome == "timeout":
            raise httpx.ReadTimeout("Simulated timeout")
        return httpx.Response(200, json=image_response("#779955"))

    result = asyncio.run(
        runner.execute(
            run["id"], OpenAIImages("synthetic", transport=httpx.MockTransport(transport))
        )
    )
    after = read(ws.storage, cid)
    assert after["source_assets"] == before["source_assets"]
    assert len(calls) == 1
    assert result["replacement_outcome"] == {"stop": "stopped", "conflict": "conflict"}.get(
        outcome, "failed"
    )
    if outcome != "conflict":
        assert after["version"] == before["version"]
    else:
        assert after["authored"]["brief"]["name"] == "Changed during generation"
        assert ws.state.attempts(run["id"])[0]["state"] == "decoded"
    assert prepare(ws, cid, action)["id"] == run["id"]
    assert len(calls) == 1
    data = campaign_data(ws, cid)
    assert data["manifest"]["complete"]
    download = Handoff(ws.state).current_png(cid, "product-1/en-US/1x1", data["version"])
    assert download.stream.read(8).startswith(b"\x89PNG")
    download.close()


def test_stale_review_or_tampered_source_refuses_before_new_request(generated):
    ws, _, cid = generated
    _, action = confirmation(ws, cid)
    project = AuthoredProjects(ws)
    current = project.read(cid)
    changed = copy.deepcopy(current["authored"])
    changed["brief"]["name"] = "Changed before confirm"
    project.save(
        cid, AuthoredSave(version=current["version"], base=current["base"], authored=changed)
    )
    with pytest.raises(ActionError, match="changed"):
        prepare(ws, cid, action)
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 2
    _, fresh = confirmation(ws, cid)
    document = read(ws.storage, cid)
    ws.storage.path(document["source_assets"]["product-1"]["asset"]["reference"]).write_bytes(
        b"changed"
    )
    with pytest.raises(ActionError):
        prepare(ws, cid, fresh)
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 2


@pytest.mark.parametrize("stop", [False, True])
def test_http_confirmation_is_idempotent_and_export_survives_provider_wait(generated, stop):
    import threading
    import time

    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    ws, _, cid = generated
    before = read(ws.storage, cid)
    release = threading.Event()
    dispatched = threading.Event()
    calls = []

    async def transport(request):
        calls.append(request)
        dispatched.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, json=image_response("#887755"))

    app = create_app(
        Settings(data_dir=ws.storage.root),
        key_available=lambda: True,
        provider_factory=lambda: OpenAIImages(
            "synthetic", transport=httpx.MockTransport(transport)
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"})
        proposed = client.post(
            f"/api/campaigns/{cid}/regeneration/preview",
            json={"version": before["version"], "product_id": "product-1"},
        )
        assert proposed.status_code == 200, proposed.text
        confirmation = {
            key: proposed.json()[key] for key in ("version", "product_id", "operation_id", "digest")
        }
        assert read(ws.storage, cid) == before
        try:
            started = client.post(f"/api/campaigns/{cid}/regeneration/start", json=confirmation)
            assert started.status_code == 202, started.text
            run_id = started.json()["run_id"]
            assert dispatched.wait(5)
            duplicate = client.post(f"/api/campaigns/{cid}/regeneration/start", json=confirmation)
            assert duplicate.status_code == 202, duplicate.text
            assert len(calls) == 1
            assert read(ws.storage, cid)["version"] == before["version"]
            png = client.get(
                f"/api/campaigns/{cid}/composition/{before['version']}/download/product-1/en-US/1x1"
            )
            assert png.status_code == 200, png.text
            archive = client.post(
                f"/api/campaigns/{cid}/delivery/zip", json={"version": before["version"]}
            )
            assert archive.status_code == 200, archive.text
            if stop:
                assert client.get(f"/api/campaigns/{cid}/status").json()["stoppable"]
                assert (
                    client.post(f"/api/campaigns/{cid}/stop", json={"run_id": run_id}).status_code
                    == 200
                )
        finally:
            release.set()
        for _ in range(200):
            status = client.get(f"/api/campaigns/{cid}/status").json()
            if not status["active"]:
                break
            time.sleep(0.01)
        assert not status["active"]
        assert len(calls) == 1
        duplicate = client.post(f"/api/campaigns/{cid}/regeneration/start", json=confirmation)
        assert duplicate.status_code == 202
        assert len(calls) == 1
        after = read(ws.storage, cid)
        assert after["regeneration_receipt"]["outcome"] == ("stopped" if stop else "replaced")
        if stop:
            assert after["source_assets"] == before["source_assets"]
            assert after["version"] == before["version"]
            app.state.runner.recover_local(run_id)
            assert read(ws.storage, cid)["source_assets"] == before["source_assets"]
        assert ws.state.campaign_metrics(cid)["attempted_requests"] == 3


def test_old_confirmation_stays_idempotent_after_a_later_replacement(generated):
    ws, runner, cid = generated
    _, first = confirmation(ws, cid)
    for action, color in [(first, "#786655"), (None, "#116688")]:
        if action is None:
            _, action = confirmation(ws, cid)
        run = prepare(ws, cid, action)
        ws.state.confirm(run["id"], ws.state.plan(run["id"], current=False).digest)
        provider = OpenAIImages(
            "synthetic",
            transport=httpx.MockTransport(
                lambda _, color=color: httpx.Response(200, json=image_response(color))
            ),
        )
        asyncio.run(runner.execute(run["id"], provider))
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 4
    old = prepare(ws, cid, first)
    assert old["state"] == "complete"
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 4


@pytest.mark.parametrize("change", ["edit", "source"])
def test_changed_after_confirmation_is_refused_before_dispatch(generated, change):
    ws, runner, cid = generated
    _, action = confirmation(ws, cid)
    run = prepare(ws, cid, action)
    ws.state.confirm(run["id"], ws.state.plan(run["id"], current=False).digest)
    if change == "edit":
        project = AuthoredProjects(ws)
        current = project.read(cid)
        changed = copy.deepcopy(current["authored"])
        changed["brief"]["audience"] = "A concurrent accepted edit"
        project.save(
            cid, AuthoredSave(version=current["version"], base=current["base"], authored=changed)
        )
    else:
        ws.storage.path(
            read(ws.storage, cid)["source_assets"]["product-1"]["asset"]["reference"]
        ).write_bytes(b"changed source")
    calls = []
    provider = OpenAIImages(
        "synthetic", transport=httpx.MockTransport(lambda request: calls.append(request))
    )
    with pytest.raises((ActionError, StateError)):
        asyncio.run(runner.execute(run["id"], provider))
    assert calls == []
    assert ws.state.attempts(run["id"]) == []
    assert read(ws.storage, cid)["regeneration_receipt"]["outcome"] == "not_started"
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 2


def test_prepared_product_regenerates_background_without_altering_uploaded_product(generated):
    from campaign_studio.intake import Intake

    ws, runner, cid = generated
    stream = io.BytesIO()
    with Image.new("RGBA", (200, 200), (0, 0, 0, 0)) as cutout:
        cutout.paste((150, 70, 40, 255), (50, 50, 150, 150))
        cutout.save(stream, format="PNG")
    uploaded = Intake(ws.storage).upload(
        stream.getvalue(), "image/png", campaign_id=cid, source_role="prepared_product"
    )
    reference = uploaded["reference"]
    projects = AuthoredProjects(ws)
    current = projects.read(cid)
    value = copy.deepcopy(current["authored"])
    value["brief"]["products"][0]["source"] = {
        "role": "prepared_product",
        "path": reference,
        "opaque_nonreflective_product_confirmed": True,
        "provenance": {
            "label": "Supplied test object",
            "rights": "Local fixture",
            "synthetic": True,
        },
    }
    saved = projects.save(
        cid, AuthoredSave(version=current["version"], base=current["base"], authored=value)
    )
    plan = Intake(ws.storage).prepare(cid, saved["version"], ws)
    run_id = read(ws.storage, cid)["planned_run"]
    ws.state.confirm(run_id, plan.digest)

    def provider(color):
        return OpenAIImages(
            "synthetic",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=image_response(color))
            ),
        )

    asyncio.run(runner.execute(run_id, provider("#778899")))
    before = read(ws.storage, cid)
    uploaded = before["authored"]["brief"]["products"][0]["source"]
    original_pixels = ws.storage.path(uploaded["path"]).read_bytes()
    proposed, action = confirmation(ws, cid)
    assert proposed["background_only"]
    assert "An empty campaign environment only" in proposed["request"]["prompt"]
    run = prepare(ws, cid, action)
    ws.state.confirm(run["id"], ws.state.plan(run["id"], current=False).digest)
    asyncio.run(runner.execute(run["id"], provider("#998877")))
    after = read(ws.storage, cid)
    assert after["authored"] == before["authored"]
    assert ws.storage.path(uploaded["path"]).read_bytes() == original_pixels
    assert (
        after["source_assets"]["product-1"]["asset"]["sha256"]
        != before["source_assets"]["product-1"]["asset"]["sha256"]
    )
    assert after["source_assets"]["product-3"] == before["source_assets"]["product-3"]


def test_simultaneous_http_confirmations_dispatch_at_most_once(generated):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    ws, _, cid = generated
    release = threading.Event()
    barrier = threading.Barrier(2)
    calls = []

    async def transport(request):
        calls.append(request)
        while not release.is_set():
            await asyncio.sleep(0.01)
        return httpx.Response(200, json=image_response("#665577"))

    app = create_app(
        Settings(data_dir=ws.storage.root),
        key_available=lambda: True,
        provider_factory=lambda: OpenAIImages(
            "synthetic", transport=httpx.MockTransport(transport)
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"})
        proposed = client.post(
            f"/api/campaigns/{cid}/regeneration/preview",
            json={"version": read(ws.storage, cid)["version"], "product_id": "product-1"},
        ).json()
        action = {key: proposed[key] for key in ("version", "product_id", "operation_id", "digest")}

        def confirm():
            barrier.wait()
            return client.post(f"/api/campaigns/{cid}/regeneration/start", json=action)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: confirm(), range(2)))
            assert any(response.status_code == 202 for response in results)
            assert all(response.status_code in {202, 409} for response in results)
            for _ in range(100):
                if calls:
                    break
                time.sleep(0.01)
            assert len(calls) == 1
            with ws.storage.connect() as db:
                assert (
                    db.execute("SELECT count(*) FROM runs WHERE campaign_id=?", (cid,)).fetchone()[
                        0
                    ]
                    == 2
                )
        finally:
            release.set()
        for _ in range(200):
            if not client.get(f"/api/campaigns/{cid}/status").json()["active"]:
                break
            time.sleep(0.01)
        assert len(calls) == 1


@pytest.mark.parametrize("outcome", ["stop", "failure", "conflict"])
def test_generic_start_retry_and_recovery_cannot_bypass_replacement_guards(generated, outcome):
    import time

    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    ws, runner, cid = generated
    _, action = confirmation(ws, cid)
    run = prepare(ws, cid, action)
    plan = ws.state.plan(run["id"], current=False)
    ws.state.confirm(run["id"], plan.digest)
    before = read(ws.storage, cid)["source_assets"]

    async def transport(_):
        if outcome == "stop":
            ws.state.stop(run["id"])
        elif outcome == "conflict":
            projects = AuthoredProjects(ws)
            current = projects.read(cid)
            changed = copy.deepcopy(current["authored"])
            changed["brief"]["name"] = "Concurrent name edit"
            projects.save(
                cid,
                AuthoredSave(version=current["version"], base=current["base"], authored=changed),
            )
        return (
            httpx.Response(400, json={"error": {"message": "Fixture failure"}})
            if outcome == "failure"
            else httpx.Response(200, json=image_response("#334455"))
        )

    asyncio.run(
        runner.execute(
            run["id"], OpenAIImages("synthetic", transport=httpx.MockTransport(transport))
        )
    )
    attempts = ws.state.campaign_metrics(cid)["attempted_requests"]
    app = create_app(
        Settings(data_dir=ws.storage.root),
        key_available=lambda: True,
        provider_factory=lambda: pytest.fail("Generic recovery must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"})
        for retry in [False, True]:
            refused = client.post(
                f"/api/campaigns/{cid}/start",
                json={
                    "run_id": run["id"],
                    "digest": plan.digest,
                    "allowance": 2 if retry else 1,
                    "retry": retry,
                },
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "regeneration_review"
        recovered = client.post(f"/api/campaigns/{cid}/recover", json={"run_id": run["id"]})
        assert recovered.status_code == 202, recovered.text
        for _ in range(200):
            if not client.get(f"/api/campaigns/{cid}/status").json()["active"]:
                break
            time.sleep(0.01)
        assert read(ws.storage, cid)["source_assets"] == before
        assert ws.state.campaign_metrics(cid)["attempted_requests"] == attempts


@pytest.mark.parametrize("publication_wins", [False, True])
def test_stop_and_source_publication_have_one_atomic_http_order(
    generated, monkeypatch, publication_wins
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    import campaign_studio.regeneration as regeneration
    from campaign_studio.app import create_app
    from campaign_studio.config import Settings

    ws, _, cid = generated
    before = read(ws.storage, cid)
    reached, release, stop_entered = threading.Event(), threading.Event(), threading.Event()
    original_check, original_write = regeneration.require_current, regeneration.write
    original_stop = State.stop
    action = {}

    def check_before_publication(state, campaign, intent):
        document = original_check(state, campaign, intent)
        run = existing(ws, cid, RegenerationConfirm(**action))
        if (
            not publication_wins
            and run
            and state.attempts(run["id"])
            and state.attempts(run["id"])[0]["state"] == "decoded"
        ):
            reached.set()
            assert release.wait(5)
        return document

    def write_during_publication(storage, campaign, document, *, db=None):
        if (
            publication_wins
            and document.get("regeneration_receipt", {}).get("outcome") == "replaced"
        ):
            reached.set()
            assert release.wait(5)
        return original_write(storage, campaign, document, db=db)

    def stop_after_publication_started(state, run_id):
        stop_entered.set()
        return original_stop(state, run_id)

    monkeypatch.setattr(regeneration, "require_current", check_before_publication)
    monkeypatch.setattr(regeneration, "write", write_during_publication)
    monkeypatch.setattr(State, "stop", stop_after_publication_started)
    app = create_app(
        Settings(data_dir=ws.storage.root),
        key_available=lambda: True,
        provider_factory=lambda: OpenAIImages(
            "synthetic",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=image_response("#123456"))
            ),
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"})
        proposed = client.post(
            f"/api/campaigns/{cid}/regeneration/preview",
            json={"version": before["version"], "product_id": "product-1"},
        ).json()
        action = {key: proposed[key] for key in ("version", "product_id", "operation_id", "digest")}
        started = client.post(f"/api/campaigns/{cid}/regeneration/start", json=action)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        try:
            assert reached.wait(5)
            if publication_wins:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending_stop = pool.submit(
                        client.post, f"/api/campaigns/{cid}/stop", json={"run_id": run_id}
                    )
                    assert stop_entered.wait(5)
                    release.set()
                    stopped = pending_stop.result(timeout=5)
                assert stopped.status_code == 409, stopped.text
                assert stopped.json()["code"] == "not_running"
            else:
                stopped = client.post(f"/api/campaigns/{cid}/stop", json={"run_id": run_id})
                assert stopped.status_code == 200, stopped.text
        finally:
            release.set()
        for _ in range(200):
            if not client.get(f"/api/campaigns/{cid}/status").json()["active"]:
                break
            time.sleep(0.01)
        after = read(ws.storage, cid)
        assert after["regeneration_receipt"]["outcome"] == (
            "replaced" if publication_wins else "stopped"
        )
        assert bool(ws.state.run(run_id)["stop_requested"]) is not publication_wins
        assert (after["source_assets"] == before["source_assets"]) is not publication_wins
        assert ws.state.campaign_metrics(cid)["attempted_requests"] == 3
