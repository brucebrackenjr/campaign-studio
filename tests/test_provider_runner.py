import asyncio
import base64
import json

import httpx
import pytest

from campaign_studio.brief import Brief
from campaign_studio.generation import MODEL
from campaign_studio.motif import attempt_details, campaign_data
from campaign_studio.planning import make_plan
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
    state.confirm(run_id, state.plan(run_id).digest)
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
    assert "Authorization" not in state.storage.path(attempt["response_ref"]).read_text()
    assert manifest["metrics"]["attempted_requests"] == 1
    assert manifest["metrics"]["cost_status"] == "unknown"


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
    assert not result["complete"] and result["metrics"]["completed_images"] == 6
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
    assert result["metrics"]["completed_images"] == 6
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
    assert result["metrics"]["completed_images"] == 6
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
    assert result["metrics"]["completed_images"] == 6
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
    assert result["metrics"]["cost_status"] == "unknown"


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
    assert partial["metrics"]["completed_images"] == 6
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
    assert result["metrics"]["cost_status"] == "unknown"


def test_retry_rejects_local_copy_failure_and_preserves_confirmed_current_copy(tmp_path):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(True)
        return httpx.Response(200, json=body)

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    original_digest = state.plan(run_id).digest
    first = asyncio.run(runner.execute(run_id, provider))
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["es-MX"]["copy"]["message"] = "Para cada día."
    edited = make_plan(Brief.model_validate(value), state.storage)
    corrected = runner.revise(run_id, edited.brief, first["revision_id"], edited.digest)
    value["markets"]["en-US"]["copy"]["message"] = "TESTBLOCK"
    blocked = make_plan(Brief.model_validate(value), state.storage)
    partial = runner.revise(run_id, blocked.brief, corrected["revision_id"], blocked.digest)
    assert not partial["complete"]
    with pytest.raises(StateError, match="No imagery is pending"):
        state.confirm(run_id, edited.digest, retry_allowance=2)
    recovered = runner.recover_local(run_id)
    assert recovered["complete"] and len(calls) == 1
    assert state.plan(run_id).brief.markets["es-MX"].text.message == "Para cada día."
    assert state.revision(partial["revision_id"])["complete"] == 0

    # A deliberately confirmed retry for genuinely missing imagery also binds
    # the current corrected plan, even though the original brief differs.
    attempt = state.attempts(run_id)[0]
    state.storage.path(attempt["source_ref"]).unlink()
    state.storage.path(attempt["response_ref"]).unlink()
    state.set_status(run_id, "partial")
    with pytest.raises(StateError, match="stale"):
        state.confirm(run_id, original_digest, retry_allowance=2)
    state.confirm(run_id, edited.digest, retry_allowance=2)
    assert state.plan(run_id, current=False).digest == edited.digest
    retried = asyncio.run(runner.execute(run_id, provider))
    assert retried["complete"] and len(calls) == 2
    assert state.plan(run_id).brief.markets["es-MX"].text.message == "Para cada día."


def test_local_composition_failure_recovers_saved_imagery_without_another_call(
    tmp_path, monkeypatch
):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body)

    write = state.storage.write

    def fail_output(reference, payload):
        if "/revisions/" in reference and reference.endswith(".png"):
            raise OSError("Synthetic disk failure")
        return write(reference, payload)

    monkeypatch.setattr(state.storage, "write", fail_output)
    failed = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    assert not failed["complete"] and len(calls) == 1
    assert state.attempts(run_id)[0]["state"] == "decoded"
    monkeypatch.setattr(state.storage, "write", write)
    recovered = runner.recover_local(run_id, failed["revision_id"])
    assert recovered["complete"] and len(calls) == 1


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
    assert partial["metrics"]["completed_images"] == 6
    assert state.attempts(run_id)[0]["state"] == "received"
    assert state.attempts(run_id)[0]["error_kind"] == "saved_response_storage"
    assert attempt_details(Workspace(state), state.attempts(run_id))[0]["response_recoverable"]
    assert json.loads(state.attempts(run_id)[0]["usage_json"]) == body["usage"]
    cost = state.campaign_metrics(state.run(run_id)["campaign_id"])["usage_cost_estimate"]
    assert cost["estimated_usd"] == 0.00108
    assert state.storage.path(attempt["response_ref"]).is_file()
    monkeypatch.setattr(state.storage, "write", write)
    complete = runner.recover_local(run_id)
    assert complete["complete"] and complete["metrics"]["completed_images"] == 12
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
            if reference.endswith("/source.png"):
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
    assert first["metrics"]["completed_images"] == 6 and attempt["state"] == "received"
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
    # A separate damaged supplied output remains locally recoverable in either case.
    damaged = next(item for item in first["outputs"] if item["file"])
    prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    state.storage.path(f"{prefix}/{damaged['file']}").write_bytes(b"damaged output")
    mixed = campaign_data(Workspace(state), state.run(run_id)["campaign_id"])
    assert damaged["id"] in mixed["local_recovery"]["output_ids"]
    assert len(mixed["local_recovery"]["output_ids"]) == (7 if failure == "source_write" else 1)
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


def test_changed_generated_master_needs_explicit_recovery_before_copy_revision(tmp_path):
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
    attempt = state.attempts(run_id)[0]
    original = state.storage.path(attempt["source_ref"]).read_bytes()
    assert attempt["source_sha256"] == hashlib.sha256(original).hexdigest()
    with Image.new("RGB", (1920, 1920), "red") as replacement:
        replacement.save(state.storage.path(attempt["source_ref"]))
    assert not state.source_is_intact(attempt)
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["es-MX"]["copy"]["message"] = "Para cada día."
    brief = Brief.model_validate(value)
    with pytest.raises(StateError, match="Saved inputs are missing or changed"):
        runner.revise(run_id, brief, first["revision_id"], make_plan(brief, state.storage).digest)
    assert state.run(run_id)["current_revision"] == first["revision_id"]
    assert not state.source_is_intact(attempt)
    recovered = runner.recover_local(run_id)
    assert all(
        a["pixel_sha256"] == b["pixel_sha256"]
        for a, b in zip(first["outputs"], recovered["outputs"], strict=True)
    )
    corrected = runner.revise(
        run_id, brief, recovered["revision_id"], make_plan(brief, state.storage).digest
    )
    assert corrected["complete"]
    assert state.storage.path(attempt["source_ref"]).read_bytes() == original
    assert state.source_is_intact(state.attempts(run_id)[0])
    assert len(calls) == 1 and len(state.attempts(run_id)) == 1
    assert all(
        old["sha256"] == new["sha256"]
        for old, new in zip(recovered["outputs"], corrected["outputs"], strict=True)
        if old["market"] == "en-US"
    )


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
    assert first["metrics"]["completed_images"] == 6
    assert state.attempts(run_id)[0]["error_kind"] == "saved_response_decode"
    recovered = runner.recover_local(run_id)
    assert recovered["metrics"]["completed_images"] == 6
    assert state.attempts(run_id)[0]["state"] == "received"
    assert state.attempts(run_id)[0]["error_kind"] == "saved_response_decode"
    assert len(calls) == 1 and len(state.attempts(run_id)) == 1
    usage = state.attempts(run_id)[0]["usage_json"]
    assert (json.loads(usage) if usage else None) == (body["usage"] if reported else None)
    cost = state.campaign_metrics(state.run(run_id)["campaign_id"])["usage_cost_estimate"]
    assert cost["estimated_usd"] == (0.00108 if reported else None)
    assert cost["unreported_attempts"] == (0 if reported else 1)
