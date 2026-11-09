"""Saved-fact projections and local readiness never dispatch or rewrite campaigns."""

import asyncio
import copy
import json

import httpx
import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient
from test_provider_runner import generated_case, save_current

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.imaging import FontProblem
from campaign_studio.motif import attempt_details, campaign_data
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.sample import write_sample
from campaign_studio.workspace import Workspace, provider_notice


def test_readiness_requires_request_guards_and_reports_real_local_failures(tmp_path, monkeypatch):
    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("readiness must stay local"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/motif/readiness", json={}).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.post(
                "/api/motif/readiness", json={}, headers={"Origin": "https://bad.example"}
            ).status_code
            == 403
        )
        ready = client.post("/api/motif/readiness", json={}).json()
        assert ready["ready"] and ready["provider_checked"] is False
        assert ready["key_available"] is False
        assert not list((tmp_path / "private").glob("readiness-*"))
        monkeypatch.setattr(
            "campaign_studio.motif.require_font", lambda: (_ for _ in ()).throw(FontProblem())
        )

        def denied(*args):
            raise PermissionError("PRIVATE_PATH_CANARY")

        monkeypatch.setattr(app.state.workspace.storage, "write", denied)
        failed = client.post("/api/motif/readiness", json={})
        assert failed.status_code == 200
        assert not failed.json()["ready"]
        assert all(not row["ready"] for row in failed.json()["checks"])
        assert "Reinstall" in failed.text and "write permissions" in failed.text
        assert "PRIVATE_PATH_CANARY" not in failed.text
        assert str(tmp_path) not in json.dumps(failed.json()["checks"])
        assert failed.json()["storage"]["location"] == str(tmp_path)
        assert client.get("/health").status_code == 200
        assert app.state.workspace.runs() == []


@pytest.mark.parametrize("cause", ["phrase", "subject_crop"])
def test_rejected_plan_summary_is_exact_read_only_and_retains_previous_version(tmp_path, cause):
    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("blocked plan cannot dispatch"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        plan = make_plan(brief, ws.storage)
        run = ws.state.create(plan, 0)
        ws.state.confirm(run, ws.state.plan(run).digest)
        asyncio.run(app.state.runner.execute(run))
        value = ws.state.plan(run).brief.model_dump(mode="json")
        if cause == "phrase":
            value["markets"]["es-MX"]["phrase_rules"] = [{"id": "prohibited", "phrase": "siempre"}]
        else:
            value["products"][0]["source"]["subject_box"] = {
                "left": 0,
                "top": 0,
                "right": 1,
                "bottom": 1,
            }
        document = save_current(ws.state, brief.campaign_id, lambda b: b.update(value))
        before = copy.deepcopy(ws.state.run(run))
        response = client.post(
            f"/api/motif/campaigns/{brief.campaign_id}/plan",
            json={"version": document["version"]},
        )
        assert response.status_code == 422, response.text
        proposal = response.json()["proposal"]
        assert proposal["version"] == document["version"]
        assert len(proposal["outputs"]) == 12
        findings = [item for item in proposal["findings"] if item["context"]]
        if cause == "phrase":
            assert len(findings) == 6 and all(item["context"]["phrase"] for item in findings)
            assert {item["context"]["market"] for item in findings} == {"es-MX"}
            assert len({item["context"]["product_id"] for item in findings}) == 2
        else:
            assert len(findings) == 4 and all(item["rule"] == "subject_crop" for item in findings)
            assert {item["context"]["market"] for item in findings} == {"en-US", "es-MX"}
            assert len({item["context"]["product_id"] for item in findings}) == 1
        fields = response.json()["field_errors"]
        assert len(fields) == len({(tuple(field["path"]), field["code"]) for field in fields})
        assert len(fields) < sum(len(item["context"]["fields"]) for item in findings)
        assert ws.state.run(run) == before and len(ws.runs()) == 1
        assert ws.state.attempts(run) == []
        assert "prompt" not in json.dumps(proposal) and "reference" not in proposal


def test_attempt_projection_has_verified_receipts_and_only_allowlisted_usage(tmp_path):
    state, runner, run, response = generated_case(tmp_path)
    response["usage"] = {"input_tokens": 20, "output_tokens": 30, "private_note": "PRIVATE_CANARY"}
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    )
    asyncio.run(runner.execute(run, provider))
    ws = Workspace(state)
    attempts = state.attempts(run)
    facts = attempt_details(ws, attempts)
    assert facts[0]["source_available"] and facts[0]["response_retained"]
    assert facts[0]["usage"] == {"input_tokens": 20, "output_tokens": 30}
    text = json.dumps(facts)
    assert "PRIVATE_CANARY" not in text and "response_ref" not in text and "request_id" not in text
    state.storage.path(attempts[0]["source_ref"]).write_bytes(b"damaged")
    assert not attempt_details(ws, attempts)[0]["source_available"]
    projected = campaign_data(ws, state.run(run)["campaign_id"])
    assert projected["output_plan"]["allowance"] == 1
    assert projected["output_plan"]["generation_requests"]


@pytest.mark.parametrize("code", [401, 503])
def test_retained_http_error_is_diagnostics_not_recoverable_image(tmp_path, code):
    state, runner, run, _ = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(code, json={"error": {"message": "PRIVATE_CANARY"}})
        ),
    )
    asyncio.run(runner.execute(run, provider))
    fact = attempt_details(Workspace(state), state.attempts(run))[0]
    assert fact["state"] == "failed"
    assert fact["response_retained"] is True
    assert fact["response_recoverable"] is False and fact["source_available"] is False
    assert "PRIVATE_CANARY" not in json.dumps(fact)


def test_source_progress_is_separate_from_format_preview_coverage(tmp_path):
    state, runner, run, _ = generated_case(tmp_path)
    asyncio.run(
        runner.execute(
            run,
            OpenAIImages(
                "synthetic-test-credential",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(503, json={"error": {}})
                ),
            ),
        )
    )
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("status must never dispatch"),
    )

    class Active:
        def done(self):
            return False

    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        saved = campaign_data(ws, state.run(run)["campaign_id"])["manifest"]
        available = [item["id"] for item in saved["outputs"] if item["file"]]
        missing = [item["id"] for item in saved["outputs"] if not item["file"]]
        assert len(available) == len(missing) == 6
        app.state.operations[run] = Active()
        ws.state.live_progress[run] = {"stage": "Generating missing imagery", "completed": 0}
        url = f"/api/campaigns/{saved['campaign_id']}/status"
        status = client.get(url).json()
        assert status["completed"] == 6 and status["total"] == 12
        assert status["sources_completed"] == 1 and status["sources_total"] == 2
        ws.state.live_progress[run]["completed_ids"] = missing[:3] + available
        status = client.get(url).json()
        assert status["completed"] == 9
        assert status["sources_completed"] == 1 and status["sources_total"] == 2
        ws.state.live_progress[run]["completed_ids"] = []
        source = ws.current_composition(saved["campaign_id"])["sources"]["product-2"]
        ws.storage.path(source.reference).write_bytes(b"damaged source")
        # One lost source affects every format/market using it; no durable PNG exists.
        status = client.get(url).json()
        assert status["completed"] == status["sources_completed"] == 0
        assert status["sources_total"] == 2
        app.state.operations.clear()


def test_source_counts_advance_during_generation_and_survive_failure_and_retry(tmp_path):
    state, _, run, body = generated_case(tmp_path, concept_count=2)
    campaign_id = state.run(run)["campaign_id"]
    app = create_app(Settings(data_dir=state.storage.root))

    class Active:
        def done(self):
            return False

    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        app.state.operations[run] = Active()
        statuses = []

        def transport(request):
            statuses.append(client.get(f"/api/campaigns/{campaign_id}/status").json())
            return (
                httpx.Response(200, json=body)
                if len(statuses) == 1
                else httpx.Response(503, json={"error": {}})
            )

        provider = OpenAIImages(
            "synthetic-test-credential", transport=httpx.MockTransport(transport)
        )
        partial = asyncio.run(app.state.runner.execute(run, provider))
        assert not partial["complete"] and partial["outputs"] == []
        assert [(s["sources_completed"], s["sources_total"]) for s in statuses] == [(0, 2), (1, 2)]
        assert [s["stoppable"] for s in statuses] == [True, False]
        assert statuses[1]["attempts"][0]["source_available"] is True
        assert statuses[1]["attempts"][0]["response_recoverable"] is True
        assert statuses[1]["attempts"][1]["source_available"] is False
        assert "response_ref" not in json.dumps(statuses)
        app.state.operations.clear()
        settled = client.get(f"/api/campaigns/{campaign_id}/status").json()
        assert settled["state"] == "partial"
        assert (settled["sources_completed"], settled["sources_total"]) == (1, 2)
        assert (settled["completed"], settled["total"]) == (6, 12)
        ws.state.confirm(run, ws.state.plan(run).digest, retry_allowance=3)
        complete = asyncio.run(
            app.state.runner.execute(
                run,
                OpenAIImages(
                    "synthetic-test-credential",
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
                ),
            )
        )
        assert complete["complete"] and complete["outputs"] == []
        settled = client.get(f"/api/campaigns/{campaign_id}/status").json()
        assert settled["state"] == "complete"
        assert (settled["sources_completed"], settled["sources_total"]) == (2, 2)
        assert (settled["completed"], settled["total"]) == (12, 12)
        assert settled["attempted"] == 3
        assert ws.state.revision_count(run) == 0


def test_duplicate_start_reports_existing_operation_without_confirm_or_dispatch(
    tmp_path, monkeypatch
):
    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("duplicate must not dispatch"),
    )

    class Active:
        def done(self):
            return False

    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        campaign = seed_campaign(ws, planned=True)
        run = ws.run_for(campaign)["id"]
        plan = ws.state.plan(run)
        brief = plan.brief
        app.state.operations[run] = Active()
        monkeypatch.setattr(
            ws.state, "confirm", lambda *a, **k: pytest.fail("duplicate must not confirm")
        )
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(
            f"/api/campaigns/{brief.campaign_id}/start",
            json={"run_id": run, "digest": plan.digest, "allowance": 0},
        )
        assert response.status_code == 202 and response.json()["disposition"] == "already_active"
        assert response.json()["run_id"] == run
        assert ws.state.attempts(run) == []
        app.state.operations.clear()


@pytest.mark.parametrize("code", [401, 503])
def test_successful_retry_clears_current_notice_without_erasing_attempt_history(tmp_path, code):
    state, runner, run, response = generated_case(tmp_path)
    first = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(code, json={"error": {}})),
    )
    asyncio.run(runner.execute(run, first))
    assert provider_notice(state.attempts(run)) is not None
    state.confirm(run, state.plan(run).digest, retry_allowance=2)
    success = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    )
    result = asyncio.run(runner.execute(run, success))
    attempts = state.attempts(run)
    assert result["complete"] and result["metrics"]["sources_ready"] == 2
    assert [item["state"] for item in attempts] == ["failed", "decoded"]
    assert attempts[0]["error_kind"] == f"http_{code}"
    assert state.run(run)["allowance"] == 2
    assert provider_notice(attempts) is None
    app = create_app(Settings(data_dir=state.storage.root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        status = client.get(f"/api/campaigns/{result['campaign_id']}/status").json()
        data = client.get(f"/api/motif/campaigns/{result['campaign_id']}").json()
        assert status["notice"] is None and status["notice_code"] is None
        assert data["notice"] is None and len(data["run_attempts"]) == 2


def test_success_then_stop_does_not_resurrect_old_failure_or_hide_other_product_failure(tmp_path):
    state, runner, run, response = generated_case(tmp_path, concept_count=2)
    failed = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"error": {}})),
    )
    asyncio.run(runner.execute(run, failed))
    state.confirm(run, state.plan(run).digest, retry_allowance=3)

    def stop_after_response(request):
        state.stop(run)
        return httpx.Response(200, json=response)

    partial = asyncio.run(
        runner.execute(
            run,
            OpenAIImages(
                "synthetic-test-credential", transport=httpx.MockTransport(stop_after_response)
            ),
        )
    )
    assert not partial["complete"] and partial["metrics"]["sources_ready"] == 1
    assert len(state.attempts(run)) == 2 and provider_notice(state.attempts(run)) is None
    state.confirm(run, state.plan(run).digest, retry_allowance=3)
    asyncio.run(
        runner.execute(
            run,
            OpenAIImages(
                "synthetic-test-credential",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(401, json={"error": {}})
                ),
            ),
        )
    )
    attempts = state.attempts(run)
    assert [item["product_id"] for item in attempts] == ["product-1", "product-1", "product-2"]
    assert provider_notice(attempts).code == "rejected_key"
    assert attempts[0]["error_kind"] == "http_503" and attempts[1]["state"] == "decoded"
