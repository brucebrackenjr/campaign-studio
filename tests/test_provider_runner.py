import asyncio
import base64
import json
import re
from pathlib import PurePosixPath
from uuid import uuid4

import httpx
import pytest

from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.generation import MODEL
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.intake import Intake
from campaign_studio.motif import attempt_details, campaign_data
from campaign_studio.planning import canonical_hash, make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage, StorageError
from campaign_studio.workspace import Workspace, provider_notice


def generated_case(tmp_path, concept_count=1):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage)
    payload = storage.path(brief.products[0].source.path).read_bytes()
    response = {
        "created": 1,
        "data": [{"b64_json": base64.b64encode(payload).decode()}],
        "usage": {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
    }
    value = brief.model_dump()
    for product in value["products"][:concept_count]:
        product["source"] = {
            "role": "concept",
            "imagery_absent": True,
            "description": "A synthetic geometric subject for mocked transport only.",
            "provenance": {
                "label": "Synthetic mocked source",
                "rights": "Local fixture",
                "synthetic": True,
            },
        }
    state = State(storage)
    run_id = state.create(make_plan(Brief.model_validate(value), storage), concept_count)
    plan = state.plan(run_id)
    prepared = plan.brief.model_dump(mode="json")
    write_draft(
        storage,
        brief.campaign_id,
        {
            "authored": {"brief": prepared},
            "composition_schema": 1,
            "version": uuid4().hex,
            "planned_run": run_id,
            "planned_authored_hash": canonical_hash(prepared),
            "authored_base": {"run_id": run_id, "revision_id": None, "metadata_snapshot_id": None},
        },
    )
    state.confirm(run_id, plan.digest)
    return state, Runner(state), run_id, response


def test_sdk_request_parameters_raw_response_and_zero_automatic_retries(tmp_path):
    state, runner, run_id, body = generated_case(tmp_path)
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        assert state.attempts(run_id)[0]["state"] == "dispatching"
        return httpx.Response(200, json=body, headers={"x-request-id": "synthetic-request"})

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    manifest = asyncio.run(runner.execute(run_id, provider))
    assert manifest["complete"]
    assert len(requests) == 1
    assert {
        name: requests[0][name]
        for name in ("model", "size", "quality", "n", "background", "output_format")
    } == {
        "model": MODEL,
        "size": "1920x1920",
        "quality": "high",
        "n": 1,
        "background": "opaque",
        "output_format": "png",
    }
    attempt = state.attempts(run_id)[0]
    assert attempt["state"] == "decoded"
    assert attempt["request_id"] == "synthetic-request"
    assert json.loads(state.storage.path(attempt["response_ref"]).read_bytes()) == body
    assert state.storage.path(attempt["source_ref"]).is_file()
    assert re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*-[a-f0-9]{32}\.png", PurePosixPath(attempt["source_ref"]).name
    )
    assert "Authorization" not in state.storage.path(attempt["response_ref"]).read_text()
    assert manifest["metrics"]["attempted_requests"] == 1
    assert manifest["metrics"]["usage_cost_estimate"]["actual_charge"] is None


def test_500_response_consumes_one_slot_and_pauses_without_sdk_retry(tmp_path):
    state, runner, run_id, _ = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(
            500, json={"error": {"message": "Synthetic server failure", "type": "server_error"}}
        )

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    result = asyncio.run(runner.execute(run_id, provider))
    assert len(calls) == 1
    assert not result["complete"] and result["metrics"]["sources_ready"] == 1
    assert state.attempts(run_id)[0]["state"] == "failed"
    with pytest.raises(StateError, match="confirmation"):
        asyncio.run(runner.execute(run_id, provider))
    with pytest.raises(StateError, match="another"):
        state.confirm(run_id, state.plan(run_id, current=False).digest, retry_allowance=1)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("code", "notice", "remedy"),
    [
        ("project_spend_limit_exceeded", "billing_limit", "project spending limit"),
        ("organization_spend_limit_exceeded", "billing_limit", "organization spending limit"),
        ("organization_usage_limit_exceeded", "billing_limit", "organization usage limit"),
        ("credit_balance_exhausted", "billing_limit", "credit balance"),
        ("insufficient_quota", "billing_limit", "billing quota"),
        ("rate_limit_error", "rate_limited", "request rate"),
        ("rate_limit_exceeded", "rate_limited", "request rate"),
        ("PRIVATE UNKNOWN PROVIDER DIAGNOSTIC", "provider_failure", "recognized limit reason"),
    ],
)
def test_recorded_limit_reason_has_safe_remedy_without_automatic_retry(
    tmp_path, code, notice, remedy
):
    from campaign_studio.workspace import provider_notice

    state, runner, run_id, _ = generated_case(tmp_path)
    calls = []
    body = {"error": {"code": code, "message": "PRIVATE RESPONSE TEXT"}}

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(429, json=body)

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    result = asyncio.run(runner.execute(run_id, provider))
    attempt = state.attempts(run_id)[0]
    expected_kind = "http_429" if code.startswith("PRIVATE") else code
    assert attempt["state"] == "failed" and attempt["error_kind"] == expected_kind
    assert json.loads(state.storage.path(attempt["response_ref"]).read_bytes()) == body
    restored = State(state.storage)
    message = provider_notice(restored.attempts(run_id))
    assert message.code == notice and remedy in message.message
    assert "PRIVATE" not in message.message and "PRIVATE" not in attempt["error_kind"]
    assert result["metrics"]["attempted_requests"] == 1
    assert result["metrics"]["sources_ready"] == 1
    assert not state.storage.path(attempt["source_ref"]).exists()
    if notice == "billing_limit":
        assert "allowance does not resolve" in message.message
    with pytest.raises(StateError, match="confirmation"):
        asyncio.run(runner.execute(run_id, provider))
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [OSError, StorageError])
def test_http_refusal_stays_known_when_diagnostic_receipt_cannot_be_saved(
    tmp_path, monkeypatch, failure
):
    state, runner, run_id, _ = generated_case(tmp_path)
    write = state.storage.write
    calls = []

    def fail_receipt(reference, payload):
        if reference.endswith("/response.json"):
            raise failure("Synthetic diagnostic write failure")
        return write(reference, payload)

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={"error": {"code": "insufficient_quota"}})

    monkeypatch.setattr(state.storage, "write", fail_receipt)
    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    result = asyncio.run(runner.execute(run_id, provider))
    attempt = state.attempts(run_id)[0]
    assert attempt["state"] == "failed" and attempt["error_kind"] == "insufficient_quota"
    assert not state.storage.path(attempt["response_ref"]).exists()
    assert not state.storage.path(attempt["source_ref"]).exists()
    details = attempt_details(Workspace(state), [attempt])[0]
    assert not details["response_retained"] and not details["response_recoverable"]
    assert provider_notice([attempt]).code == "billing_limit"
    assert result["metrics"]["sources_ready"] == 1
    assert result["metrics"]["attempted_requests"] == 1
    with pytest.raises(StateError, match="confirmation"):
        asyncio.run(runner.execute(run_id, provider))
    assert state.attempts(run_id) == [attempt] and len(calls) == 1


@pytest.mark.parametrize("reported", [True, False])
def test_failed_http_response_preserves_usage_without_treating_it_as_imagery(tmp_path, reported):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(
            503,
            json={
                "error": {"message": "Synthetic provider error"},
                **({"usage": body["usage"]} if reported else {}),
            },
        )

    result = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    attempt = state.attempts(run_id)[0]
    assert result["metrics"]["sources_ready"] == 1
    assert attempt["state"] == "failed" and attempt["error_kind"] == "http_503"
    assert not state.storage.path(attempt["source_ref"]).exists()
    usage = attempt["usage_json"]
    assert (json.loads(usage) if usage else None) == (body["usage"] if reported else None)
    campaign = state.run(run_id)["campaign_id"]
    cost = state.campaign_metrics(campaign)["usage_cost_estimate"]
    assert cost["estimated_usd"] == (0.00108 if reported else None)
    assert cost["unreported_attempts"] == (0 if reported else 1)
    runner.recover_local(run_id)
    assert state.attempts(run_id) == [attempt]
    assert state.campaign_metrics(campaign)["usage_cost_estimate"] == cost
    assert len(calls) == 1


def test_overall_deadline_waits_for_transport_cleanup_and_preserves_unknown(tmp_path, monkeypatch):
    import campaign_studio.planning as planning

    original = planning.request_for
    monkeypatch.setattr(
        planning,
        "request_for",
        lambda *args: original(*args).model_copy(update={"deadline_seconds": 0.02}),
    )
    state, runner, run_id, body = generated_case(tmp_path)
    calls, cleaned = [], []

    async def transport(request):
        calls.append(request.url.path)
        try:
            await asyncio.sleep(1)
            return httpx.Response(200, json=body)
        finally:
            cleaned.append(True)

    result = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    assert len(calls) == 1 and cleaned == [True]
    assert not result["complete"]
    assert state.attempts(run_id)[0]["state"] == "unknown"
    assert result["metrics"]["unknown_requests"] == 1
    assert result["metrics"]["usage_cost_estimate"]["unreported_attempts"] == 1


@pytest.mark.parametrize(
    "behavior", ["silent_headers", "silent_body", "deadline", "disconnect", "connection_failure"]
)
def test_socket_wait_uses_overall_deadline_and_never_retries(tmp_path, monkeypatch, behavior):
    """Real HTTP reads catch inactivity limits that MockTransport cannot exercise."""
    import campaign_studio.planning as planning

    original = planning.request_for
    deadline = 0.08 if behavior == "deadline" else 0.8
    monkeypatch.setattr(
        planning,
        "request_for",
        lambda *args: original(*args).model_copy(update={"deadline_seconds": deadline}),
    )
    state, runner, run_id, body = generated_case(tmp_path)

    async def scenario():
        calls, dispatches, limits = [], [], []
        tasks = set()

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                calls.append(json.loads(await reader.readexactly(length)))
                if behavior == "disconnect":
                    return
                payload = json.dumps(body).encode()
                response_headers = (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"x-request-id: local-socket-proof\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                )
                if behavior == "silent_body":
                    writer.write(response_headers)
                    await writer.drain()
                # A short real socket wait exceeds the scaled legacy read limit.
                await asyncio.sleep(0.15)
                writer.write(payload if behavior == "silent_body" else response_headers + payload)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        if behavior == "connection_failure":
            server.close()
            await server.wait_closed()

        class LocalTransport(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request):
                dispatches.append(request.url.path)
                timeout = request.extensions["timeout"]
                limits.append(timeout.copy())
                # Keep regressions fast: legacy read=60 becomes 0.06 seconds.
                if timeout["read"] is not None:
                    timeout["read"] /= 1000
                request.url = httpx.URL(f"http://127.0.0.1:{port}/v1/images/generations")
                return await super().handle_async_request(request)

        try:
            result = await runner.execute(
                run_id, OpenAIImages("synthetic-test-credential", transport=LocalTransport())
            )
        finally:
            server.close()
            await server.wait_closed()
            pending = list(tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        assert dispatches == ["/v1/images/generations"]
        assert len(calls) == (0 if behavior == "connection_failure" else 1)
        assert limits == [{"connect": 10, "read": None, "write": 30, "pool": 10}]
        return result

    result = asyncio.run(scenario())
    attempt = state.attempts(run_id)[0]
    assert result["metrics"]["attempted_requests"] == 1
    if behavior in {"silent_headers", "silent_body"}:
        assert result["complete"]
        assert attempt["state"] == "decoded"
        assert attempt["request_id"] == "local-socket-proof"
        assert json.loads(attempt["usage_json"]) == body["usage"]
        assert result["metrics"]["usage_cost_estimate"]["unreported_attempts"] == 0
    else:
        assert not result["complete"]
        assert attempt["state"] == "unknown"
        assert attempt["error_kind"] == (
            "overall_deadline" if behavior == "deadline" else "APIConnectionError"
        )
        assert result["metrics"]["unknown_requests"] == 1
        assert result["metrics"]["usage_cost_estimate"]["unreported_attempts"] == 1
        assert not state.storage.path(attempt["response_ref"]).exists()
        with pytest.raises(StateError, match="confirmation"):
            asyncio.run(runner.execute(run_id, OpenAIImages("synthetic-test-credential")))
        assert state.attempts(run_id) == [attempt]


def test_duplicate_start_and_stop_prevent_second_dispatch(tmp_path):
    state, runner, run_id, body = generated_case(tmp_path, concept_count=2)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def transport(request):
            calls.append(request.url.path)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=body)

        provider = OpenAIImages(
            "synthetic-test-credential", transport=httpx.MockTransport(transport)
        )
        active = asyncio.create_task(runner.execute(run_id, provider))
        await entered.wait()
        with pytest.raises(StorageError, match="already active"):
            await runner.execute(run_id, provider)
        Runner(State(state.storage))
        assert state.run(run_id)["state"] == "running"
        state.stop(run_id)
        release.set()
        result = await active
        assert not result["complete"] and len(calls) == 1
        assert state.run(run_id)["state"] == "stopped"

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed_retry", [False, True])
def test_saved_response_survives_interrupted_metadata_and_recovers_without_provider(
    tmp_path, confirmed_retry
):
    state, _, run_id, body = generated_case(tmp_path)
    state.set_status(run_id, "running")
    attempt = state.reserve(run_id, "product-1")
    state.storage.write(attempt["response_ref"], json.dumps(body).encode())
    restarted = Runner(State(state.storage))
    assert state.run(run_id)["state"] == "paused"
    assert state.attempts(run_id)[0]["state"] == "received"
    assert state.revision_count(run_id) == 0
    if confirmed_retry:
        state.confirm(run_id, state.plan(run_id).digest, retry_allowance=2)
        assert state.run(run_id)["state"] == "confirmed"
    result = restarted.recover_local(run_id)
    assert result["complete"] and len(state.attempts(run_id)) == 1
    assert state.attempts(run_id)[0]["state"] == "decoded"


@pytest.mark.parametrize("confirmed", [False, True])
def test_local_recovery_refuses_unstarted_plan_before_recovery_writes(
    tmp_path, monkeypatch, confirmed
):
    state, runner, run_id, _ = generated_case(tmp_path)
    if not confirmed:
        state.set_status(run_id, "planned")
    state.stop(run_id)
    before = state.run(run_id)
    admission = state.require_local_recovery

    def under_runner_lock(identity):
        with pytest.raises(StorageError, match="already active"), state.storage.exclusive_run():
            pytest.fail("Recovery admission must hold the runner lock")
        return admission(identity)

    monkeypatch.setattr(state, "require_local_recovery", under_runner_lock)
    monkeypatch.setattr(
        runner, "_recover_received", lambda *args: pytest.fail("Unstarted recovery reached writes")
    )
    with pytest.raises(StateError, match="not started") as caught:
        runner.recover_local(run_id)
    assert caught.value.code == "recovery_not_started"
    assert state.run(run_id) == before
    assert state.attempts(run_id) == []
    assert state.revision_count(run_id) == 0


def test_interrupted_intent_needs_explicit_retry_and_retains_old_unknown(tmp_path):
    state, _, run_id, body = generated_case(tmp_path)
    state.set_status(run_id, "running")
    state.reserve(run_id, "product-1")
    restarted = Runner(State(state.storage))
    assert state.attempts(run_id)[0]["state"] == "unknown"
    partial = restarted.recover_local(run_id)
    assert partial["metrics"]["sources_ready"] == 1
    state.confirm(run_id, state.plan(run_id, current=False).digest, retry_allowance=2)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body)

    result = asyncio.run(
        restarted.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    assert result["complete"] and len(calls) == 1
    assert [item["state"] for item in state.attempts(run_id)] == ["unknown", "decoded"]
    assert result["metrics"]["attempted_requests"] == 2
    assert result["metrics"]["usage_cost_estimate"]["unreported_attempts"] == 1


def save_current(state, campaign, mutate):
    projects = AuthoredProjects(Workspace(state))
    current = projects.read(campaign)
    mutate(current["authored"]["brief"])
    return projects.save(
        campaign,
        AuthoredSave(
            version=current["version"], base=current["base"], authored=current["authored"]
        ),
    )


def test_local_copy_failure_never_authorizes_a_paid_retry_and_keeps_current_copy(tmp_path):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(True)
        return httpx.Response(200, json=body)

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    first = asyncio.run(runner.execute(run_id, provider))
    campaign = first["campaign_id"]
    save_current(
        state, campaign, lambda b: b["markets"]["es-MX"]["copy"].update(message="Para cada día.")
    )
    blocked = save_current(
        state, campaign, lambda b: b["markets"]["en-US"]["copy"].update(message="TESTBLOCK")
    )
    with pytest.raises(StateError):
        Handoff(state).create_current_zip(campaign, DeliveryAction(version=blocked["version"]))
    assert len(calls) == 1 and state.revision_count(run_id) == 0
    save_current(
        state, campaign, lambda b: b["markets"]["en-US"]["copy"].update(message="Ready for today.")
    )
    # A lost source requires a new, explicitly reviewed request, while current copy survives.
    attempt = state.attempts(run_id)[0]
    state.storage.path(attempt["source_ref"]).unlink()
    state.storage.path(attempt["response_ref"]).unlink()
    ws = Workspace(state)
    version = AuthoredProjects(ws).read(campaign)["version"]
    proposal = Intake(state.storage).prepare(campaign, version, ws)
    next_run = ws.run_for(campaign)["id"]
    assert next_run != run_id and len(calls) == 1
    assert proposal.generation_products == ["product-1"]
    state.confirm(next_run, proposal.digest)
    assert asyncio.run(runner.execute(next_run, provider))["complete"]
    assert len(calls) == 2
    assert (
        ws.current_composition(campaign)["plan"].brief.markets["es-MX"].text.message
        == "Para cada día."
    )
    assert state.revision_count(next_run) == 0


def test_source_production_never_attempts_a_finished_png_write(tmp_path, monkeypatch):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []
    original = state.storage.write_new

    def disallow_finished_image(reference, payload):
        assert "/Outputs/" not in reference
        return original(reference, payload)

    monkeypatch.setattr(state.storage, "write_new", disallow_finished_image)

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body)

    result = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    assert result["complete"] and result["sources_ready"] == 2
    assert result["metrics"]["completed_images"] == 0 and result["outputs"] == []
    assert state.revision_count(run_id) == 0 and len(calls) == 1
    assert not list(state.storage.root.glob("Campaigns/**/Outputs/**/*.png"))


def test_request_model_change_changes_plan_digest_and_transparent_logo_blocks(
    tmp_path, monkeypatch
):
    from PIL import Image

    import campaign_studio.planning as planning

    state, _, run_id, _ = generated_case(tmp_path)
    brief = state.plan(run_id).brief
    before = planning.make_plan(brief, state.storage)
    original = planning.request_for
    monkeypatch.setattr(
        planning,
        "request_for",
        lambda *args: original(*args).model_copy(update={"model": "test-changed-model"}),
    )
    after = planning.make_plan(brief, state.storage)
    assert before.digest != after.digest
    assert before.generation_requests["product-1"].model == MODEL
    with Image.new("RGBA", (20, 20)) as invisible:
        invisible.save(state.storage.path(brief.brand.logo_path))
    assert planning.make_plan(brief, state.storage).blocked


def test_received_response_survives_temporary_source_write_failure(tmp_path, monkeypatch):
    state, runner, run_id, body = generated_case(tmp_path)
    state.set_status(run_id, "running")
    attempt = state.reserve(run_id, "product-1")
    state.storage.write(attempt["response_ref"], json.dumps(body).encode())
    write = state.storage.write

    def fail_source(reference, payload):
        if reference == attempt["source_ref"]:
            raise OSError("Synthetic temporary source write failure")
        return write(reference, payload)

    monkeypatch.setattr(state.storage, "write", fail_source)
    partial = runner.recover_local(run_id)
    assert partial["metrics"]["sources_ready"] == 1
    assert state.attempts(run_id)[0]["state"] == "received"
    assert state.attempts(run_id)[0]["error_kind"] == "saved_response_storage"
    assert attempt_details(Workspace(state), state.attempts(run_id))[0]["response_recoverable"]
    assert json.loads(state.attempts(run_id)[0]["usage_json"]) == body["usage"]
    cost = state.campaign_metrics(state.run(run_id)["campaign_id"])["usage_cost_estimate"]
    assert cost["estimated_usd"] == 0.00108
    assert state.storage.path(attempt["response_ref"]).is_file()
    monkeypatch.setattr(state.storage, "write", write)
    complete = runner.recover_local(run_id)
    assert complete["complete"] and complete["metrics"]["sources_ready"] == 2
    assert len(state.attempts(run_id)) == 1
    assert state.source_is_intact(state.attempts(run_id)[0])
    assert state.campaign_metrics(state.run(run_id)["campaign_id"])["usage_cost_estimate"] == cost


@pytest.mark.parametrize("failure", ["source_write", "image_decode"])
def test_received_usage_survives_image_processing_failure(tmp_path, monkeypatch, failure):
    state, runner, run_id, body = generated_case(tmp_path)
    write = state.storage.write
    calls = []
    if failure == "image_decode":
        body["data"] = [{"b64_json": base64.b64encode(b"not an image").decode()}]
    else:

        def fail_source(reference, payload):
            if reference == state.attempts(run_id)[0]["source_ref"]:
                raise OSError("Synthetic temporary source write failure")
            return write(reference, payload)

        monkeypatch.setattr(state.storage, "write", fail_source)

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body, headers={"x-request-id": "usage-before-image"})

    first = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    attempt = state.attempts(run_id)[0]
    assert first["metrics"]["sources_ready"] == 1 and attempt["state"] == "received"
    assert attempt["request_id"] == "usage-before-image"
    expected_kind = (
        "saved_response_decode" if failure == "image_decode" else "saved_response_storage"
    )
    assert attempt["error_kind"] == expected_kind
    notice = provider_notice([attempt])
    assert notice.code == "local_processing"
    assert ("try local recovery" in notice.message) is (failure == "source_write")
    facts = attempt_details(Workspace(state), [attempt])[0]
    assert facts["response_retained"]
    assert facts["response_recoverable"] is (failure == "source_write")
    model = campaign_data(Workspace(state), state.run(run_id)["campaign_id"])
    assert len(model["local_recovery"]["output_ids"]) == (6 if failure == "source_write" else 0)
    assert model["local_recovery"]["saved_result"] is (failure == "source_write")
    assert first["outputs"] == [] and state.revision_count(run_id) == 0
    assert json.loads(attempt["usage_json"]) == body["usage"]
    campaign = state.run(run_id)["campaign_id"]
    cost = state.campaign_metrics(campaign)["usage_cost_estimate"]
    assert cost["estimated_usd"] == 0.00108
    monkeypatch.setattr(state.storage, "write", write)
    recovered = runner.recover_local(run_id)
    assert recovered["complete"] is (failure == "source_write")
    assert state.campaign_metrics(campaign)["usage_cost_estimate"] == cost
    assert json.loads(state.attempts(run_id)[0]["usage_json"]) == body["usage"]
    assert len(calls) == len(state.attempts(run_id)) == 1


def test_changed_generated_master_requires_recovery_but_preserves_autosaved_copy(tmp_path):
    import hashlib

    from PIL import Image

    state, runner, run_id, body = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body)

    first = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    campaign = first["campaign_id"]
    attempt = state.attempts(run_id)[0]
    original = state.storage.path(attempt["source_ref"]).read_bytes()
    assert attempt["source_sha256"] == hashlib.sha256(original).hexdigest()
    with Image.new("RGB", (1920, 1920), "red") as replacement:
        replacement.save(state.storage.path(attempt["source_ref"]))
    assert not state.source_is_intact(attempt)
    saved = save_current(
        state, campaign, lambda b: b["markets"]["es-MX"]["copy"].update(message="Para cada día.")
    )
    ws = Workspace(state)
    assert "product-1" not in ws.current_composition(campaign)["sources"]
    with pytest.raises(StateError):
        Handoff(state).create_current_zip(campaign, DeliveryAction(version=saved["version"]))
    assert runner.recover_local(run_id)["complete"]
    assert state.storage.path(attempt["source_ref"]).read_bytes() == original
    assert state.source_is_intact(state.attempts(run_id)[0])
    assert (
        ws.current_composition(campaign)["plan"].brief.markets["es-MX"].text.message
        == "Para cada día."
    )
    assert len(calls) == len(state.attempts(run_id)) == 1
    assert state.revision_count(run_id) == 0


@pytest.mark.parametrize("entry", [None, "invalid image entry"])
@pytest.mark.parametrize("reported", [True, False])
def test_malformed_response_entry_remains_a_bounded_local_recovery_failure(
    tmp_path, entry, reported
):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(
            200, json={"data": [entry], **({"usage": body["usage"]} if reported else {})}
        )

    first = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    assert first["metrics"]["sources_ready"] == 1
    assert state.attempts(run_id)[0]["error_kind"] == "saved_response_decode"
    recovered = runner.recover_local(run_id)
    assert recovered["metrics"]["sources_ready"] == 1
    assert state.attempts(run_id)[0]["state"] == "received"
    assert state.attempts(run_id)[0]["error_kind"] == "saved_response_decode"
    assert len(calls) == 1 and len(state.attempts(run_id)) == 1
    usage = state.attempts(run_id)[0]["usage_json"]
    assert (json.loads(usage) if usage else None) == (body["usage"] if reported else None)
    cost = state.campaign_metrics(state.run(run_id)["campaign_id"])["usage_cost_estimate"]
    assert cost["estimated_usd"] == (0.00108 if reported else None)
    assert cost["unreported_attempts"] == (0 if reported else 1)
