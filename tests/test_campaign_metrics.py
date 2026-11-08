"""Campaign totals derive from durable attempts, never saves or repeated image sets."""

import asyncio
import io
import json
import sqlite3
from contextlib import contextmanager
from zipfile import ZipFile

import httpx
import pytest
from campaign_fixtures import independent_brief, zip_root
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_workflow import edit_copy, revise

from campaign_studio.app import create_app
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.costing import (
    campaign_metric_display,
    compact_usd,
    estimate_recorded_usage,
    incomplete_reason,
)
from campaign_studio.exporting import render_manifest_report
from campaign_studio.generation import GenerationRequest
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.state import State
from campaign_studio.workspace import Workspace


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_campaign_metrics_reads_attempts_and_publication_from_one_snapshot(
    tmp_path, monkeypatch, journal
):
    state, _, run, _ = generated_case(tmp_path)
    cid = state.run(run)["campaign_id"]
    plan_ref = state.run(run)["plan_ref"]
    state.set_status(run, "running")
    manifest_ref = state.storage.project(cid).ref(f"Runs/{run}/controlled-publication.json")
    manifest = json.dumps(
        {"schema_version": 1, "campaign_id": cid, "metrics": {"attempted_requests": 1}}
    )
    with state.storage.connect() as db:
        db.execute(f"PRAGMA journal_mode={journal}")
    original = state.storage.connect
    interleaved = []

    def publish():
        # A second real SQLite connection commits a new attempt and its result
        # exactly between the projection's two SELECTs; no provider or sleeps.
        with original() as writer:
            writer.execute("PRAGMA busy_timeout=0")
            writer.execute(
                "INSERT INTO attempts (id,run_id,product_id,state,response_ref,source_ref,"
                "usage_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "interleaved",
                    run,
                    "product-1",
                    "failed",
                    "fixture-response",
                    "fixture-source",
                    '{"input_tokens":100,"output_tokens":200}',
                    "2026-10-30T00:00:00+00:00",
                ),
            )
            writer.execute(
                "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,"
                "created_at,manifest_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "interleaved-revision",
                    run,
                    plan_ref,
                    manifest_ref,
                    0,
                    "2026-10-30T00:00:01+00:00",
                    manifest,
                ),
            )
            writer.execute("UPDATE runs SET state='partial' WHERE id=?", (run,))

    class Rows:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            try:
                publish()
                interleaved.append("committed")
            except sqlite3.OperationalError as error:
                assert "locked" in str(error)
                interleaved.append("waits_for_snapshot")
            return rows

    class ReadConnection:
        def __init__(self, db):
            self.db = db

        @property
        def row_factory(self):
            return self.db.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.db.row_factory = value

        def execute(self, sql, parameters=()):
            cursor = self.db.execute(sql, parameters)
            return (
                Rows(cursor) if sql.startswith("SELECT a.*,r.plan_json FROM attempts") else cursor
            )

    @contextmanager
    def intercepted():
        with original() as db:
            yield ReadConnection(db)

    with monkeypatch.context() as patch:
        patch.setattr(state.storage, "connect", intercepted)
        before = state.campaign_metrics(cid)
    assert before["coverage"] == "complete"
    assert before["attempted_requests"] == 0
    if interleaved == ["waits_for_snapshot"]:
        publish()
    else:
        assert interleaved == ["committed"]
    after = state.campaign_metrics(cid)
    assert after["coverage"] == "complete" and after["attempted_requests"] == 1
    assert after["usage_cost_estimate"]["estimated_usd"] == 0.0065


def mocked_production(state, runner, run_id, response, usage):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={**response, "usage": usage})

    result = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
        )
    )
    assert len(calls) == 1 and result["complete"]
    return result


def test_campaign_totals_follow_ids_across_runs_switches_and_fresh_app(tmp_path):
    state, runner, run_a, response = generated_case(tmp_path)
    a = state.run(run_a)["campaign_id"]
    mocked_production(state, runner, run_a, response, {"input_tokens": 244, "output_tokens": 3276})
    first_total = state.campaign_metrics(a)
    assert first_total["attempted_requests"] == 1
    assert first_total["usage_cost_estimate"]["estimated_usd"] == 0.0995
    value = state.plan(run_a).brief.model_dump()
    value["campaign_id"] = "014c1210-29a9-50d2-823a-7a936aecd62b"
    b = value["campaign_id"]
    independent = independent_brief(state, Brief.model_validate(value), b)
    run_b = state.create(make_plan(independent, state.storage), 1)
    state.confirm(run_b, state.plan(run_b).digest)
    mocked_production(state, runner, run_b, response, {"input_tokens": 12, "output_tokens": 34})
    b_total = state.campaign_metrics(b)
    assert b_total["usage_cost_estimate"]["estimated_usd"] == 0.00108
    assert state.plan(run_a).brief.name == state.plan(run_b).brief.name

    # A second actual mock-transport production run adds exactly its own attempt.
    value = state.plan(run_a).brief.model_dump()
    value["audience"] += " A new campaign production run."
    from campaign_studio.intake import Intake

    selected = revise(state, runner, run_a, Brief.model_validate(value))
    Intake(state.storage).prepare(a, selected["version"], Workspace(state))
    run_a2 = Workspace(state).run_for(a)["id"]
    state.confirm(run_a2, state.plan(run_a2).digest)
    mocked_production(state, runner, run_a2, response, {"input_tokens": 100, "output_tokens": 200})
    total = state.campaign_metrics(a)
    assert total["attempted_requests"] == total["successful_requests"] == 2
    assert total["usage_cost_estimate"]["estimated_usd"] == 0.106
    assert total["usage_cost_estimate"]["actual_charge"] is None
    assert state.campaign_metrics(b) == b_total
    assert state.revision_count(run_a) == 0
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0

    def open_app():
        return create_app(
            Settings(data_dir=state.storage.root),
            key_available=lambda: False,
            provider_factory=lambda: pytest.fail("Metrics and authored saves cannot dispatch"),
        )

    app = open_app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        for campaign, expected in [(a, total), (b, b_total), (a, total)]:
            model = client.get(f"/api/motif/campaigns/{campaign}").json()
            assert model["campaign_metrics"] == expected
            assert len(model["manifest"]["outputs"]) == 12
        endpoint = f"/api/motif/campaigns/{a}/authored"
        # Manual Save and autosave use this same endpoint; pending edits add no attempt.
        for pending in ("Manual pending wording", "Newer autosaved wording"):
            saved = client.get(endpoint).json()
            action = {key: saved[key] for key in ("version", "base", "authored")}
            action["authored"]["form_drafts"] = {
                "fields": {"brief/copy/en": {"copy-headline": pending}}
            }
            assert client.post(endpoint, json=action).status_code == 200
            assert state.campaign_metrics(a) == total
        corrected = revise(state, runner, run_a2, edit_copy(state, run_a2, "Otra mirada."))
        assert len(corrected["plan"].outputs) == 12
        assert state.campaign_metrics(a) == total
        assert state.campaign_metrics(b) == b_total
    # A fresh application/State owner reads the same database, without an in-memory counter.
    with TestClient(open_app(), base_url="http://127.0.0.1") as client:
        for campaign, expected in [(a, total), (b, b_total), (a, total)]:
            assert (
                client.get(f"/api/motif/campaigns/{campaign}").json()["campaign_metrics"]
                == expected
            )
        assert client.get(f"/api/campaigns/{a}/status").json()["campaign_metrics"] == total


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "not JSON",
        "[]",
        '{"input_tokens":true,"output_tokens":100}',
        '{"input_tokens":-1,"output_tokens":100}',
    ],
)
def test_unusable_usage_preserves_count_and_known_subtotal(tmp_path, usage):
    state, _, run_id, _ = generated_case(tmp_path)
    with state.storage.connect() as db:
        db.execute("UPDATE runs SET allowance=2,state='running' WHERE id=?", (run_id,))
    known = state.reserve(run_id, "product-1")
    state.finish_attempt(
        known["id"], "received", usage={"input_tokens": 244, "output_tokens": 3276}
    )
    unknown = state.reserve(run_id, "product-1")
    state.finish_attempt(unknown["id"], "unknown", error="deadline")
    with state.storage.connect() as db:
        db.execute("UPDATE attempts SET usage_json=? WHERE id=?", (usage, unknown["id"]))
    summary = state.campaign_metrics(state.run(run_id)["campaign_id"])
    assert summary["attempted_requests"] == 2
    assert summary["received_pending_local"] == summary["unknown_requests"] == 1
    cost = summary["usage_cost_estimate"]
    assert cost["estimated_usd"] is None and cost["actual_charge"] is None
    assert cost["known_usage_subtotal_usd"] == 0.0995 and cost["unreported_attempts"] == 1


def test_each_run_uses_its_own_request_and_unreadable_history_stays_unknown(tmp_path):
    state, _, run_a, _ = generated_case(tmp_path)
    cid = state.run(run_a)["campaign_id"]
    run_b = state.create(make_plan(state.plan(run_a).brief, state.storage), 1)
    for run_id in (run_a, run_b):
        state.set_status(run_id, "running")
        attempt = state.reserve(run_id, "product-1")
        state.finish_attempt(
            attempt["id"], "failed", usage={"input_tokens": 100, "output_tokens": 200}
        )
        state.set_status(run_id, "paused")
    with state.storage.connect() as db:
        historical = json.loads(
            db.execute("SELECT plan_json FROM runs WHERE id=?", (run_b,)).fetchone()[0]
        )
        historical["generation_requests"]["product-1"]["model"] = "unpriced-historical-model"
        db.execute("UPDATE runs SET plan_json=? WHERE id=?", (json.dumps(historical), run_b))
    summary = state.campaign_metrics(cid)
    assert summary["attempted_requests"] == summary["failed_requests"] == 2
    assert summary["usage_cost_estimate"]["estimated_usd"] is None
    assert summary["usage_cost_estimate"]["known_usage_subtotal_usd"] == 0.0065
    # Loss of a historical request cannot turn its recorded attempt into free work.
    with state.storage.connect() as db:
        db.execute("UPDATE runs SET plan_json=NULL WHERE id=?", (run_b,))
    missing_request = state.campaign_metrics(cid)
    assert missing_request["attempted_requests"] == summary["attempted_requests"]
    assert missing_request["usage_cost_estimate"]["estimated_usd"] is None
    assert missing_request["usage_cost_estimate"]["known_usage_subtotal_usd"] == 0.0065
    assert summary["usage_cost_estimate"]["incomplete_reasons"] == [{"code": "pricing", "count": 1}]
    assert missing_request["usage_cost_estimate"]["incomplete_reasons"] == [
        {"code": "request", "count": 1}
    ]


def test_damaged_coverage_does_not_fabricate_complete_zero_or_drop_known_attempts(tmp_path):
    state, _, run_id, _ = generated_case(tmp_path)
    cid = state.run(run_id)["campaign_id"]
    clean = state.campaign_metrics(cid)
    assert clean["coverage"] == "complete" and clean["attempted_requests"] == 0
    assert campaign_metric_display(clean)["spend"] == "$0.00"
    state.set_status(run_id, "complete")  # Damaged completion claim with no output record.
    unknown = state.campaign_metrics(cid)
    assert unknown["coverage"] == "unknown"
    assert campaign_metric_display(unknown)["attempts"] == "Not recorded"
    assert campaign_metric_display(unknown)["detail"] == "Earlier records are incomplete."
    assert unknown["usage_cost_estimate"]["estimated_usd"] is None
    state.set_status(run_id, "running")
    attempt = state.reserve(run_id, "product-1")
    state.finish_attempt(attempt["id"], "failed", usage={"input_tokens": 100, "output_tokens": 200})
    reference = state.storage.project(cid).ref(f"Runs/{run_id}/damaged-manifest.json")
    manifest = json.dumps(
        {"schema_version": 1, "campaign_id": cid, "metrics": {"attempted_requests": 3}}
    )
    with state.storage.connect() as db:
        db.execute(
            "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,"
            "created_at,manifest_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "e" * 32,
                run_id,
                state.run(run_id)["plan_ref"],
                reference,
                1,
                state.run(run_id)["created_at"],
                manifest,
            ),
        )
    partial = state.campaign_metrics(cid)
    assert partial["coverage"] == "incomplete" and partial["attempted_requests"] == 1
    shown = campaign_metric_display(partial)
    assert shown["attempts"] == "1+" and shown["spend"] == "Incomplete"
    assert shown["attempts_helper"] == "Recorded attempts · history incomplete."
    assert shown["spend_helper"] == "Known portion: < $0.01 USD."
    assert shown["detail"] == (
        "Earlier request records are incomplete. The recorded count is a minimum."
    )
    assert partial["usage_cost_estimate"]["known_usage_subtotal_usd"] == 0.0065


@pytest.mark.parametrize(
    "amount,expected",
    [(0, "$0.00"), (0.0042, "< $0.01"), (0.125, "$0.13"), (1234.565, "$1,234.57")],
)
def test_compact_estimate_never_rounds_positive_subcent_to_zero(amount, expected):
    assert compact_usd(amount) == expected


def test_downloads_include_current_metrics_and_previous_download_keeps_its_summary(tmp_path):
    state, runner, run_id, response = generated_case(tmp_path)
    mocked_production(state, runner, run_id, response, {"input_tokens": 244, "output_tokens": 3276})
    cid = state.run(run_id)["campaign_id"]
    current = Workspace(state).current_composition(cid)
    handoff = Handoff(state)
    action = DeliveryAction(version=current["version"])
    with handoff.create_current_zip(cid, action) as first:
        first_bytes = first.stream.read()
    previous = tmp_path / "previous-download.zip"
    previous.write_bytes(first_bytes)
    original_total = state.campaign_metrics(cid)
    # A separately confirmed failed request adds accounting without replacing
    # the saved editable composition or mutating a producer's previous download.
    retry = state.create(make_plan(current["plan"].brief, state.storage), 1)
    state.set_status(retry, "running")
    attempt = state.reserve(retry, "product-1")
    state.finish_attempt(attempt["id"], "failed", error="http_503")
    state.set_status(retry, "paused")
    current_total = state.campaign_metrics(cid)
    assert current_total["attempted_requests"] == 2
    assert current_total["usage_cost_estimate"]["estimated_usd"] is None
    with handoff.create_current_zip(cid, action) as second:
        second_bytes = second.stream.read()
    assert previous.read_bytes() == first_bytes
    assert not list(state.storage.root.rglob("*.zip"))
    for payload, expected in [(first_bytes, original_total), (second_bytes, current_total)]:
        with ZipFile(io.BytesIO(payload)) as archive:
            public = json.loads(archive.read(f"{zip_root(archive)}/manifest.json"))
            assert public["campaign_metrics"] == expected
            assert public["report_created_at"].endswith("+00:00")
            assert not any(
                key in json.dumps(public["campaign_metrics"])
                for key in ("prompt", "response_ref", "plan_ref")
            )
    assert state.campaign_metrics(cid) == current_total
    assert State(state.storage).campaign_metrics(cid) == current_total


def test_incomplete_estimate_reason_uses_saved_evidence_without_inventing_a_cause():
    request = GenerationRequest(prompt="A synthetic object.")
    attempts = [{"product_id": "p", "usage_json": None, "error_kind": "APIConnectionError"}]
    cost = estimate_recorded_usage(attempts, {"p": request})
    assert incomplete_reason(cost) == "Usage was not recorded."
    assert "connection was lost" in incomplete_reason(cost, detail=True)
    cost = estimate_recorded_usage(
        attempts + [{"product_id": "p", "usage_json": None}], {"p": request}
    )
    assert incomplete_reason(cost, detail=True) == "Usage or pricing could not be verified."
    # A saved HTTP error does not establish connection loss or a charge.
    cost = estimate_recorded_usage(
        [{"product_id": "p", "usage_json": None, "error_kind": "http_503"}], {"p": request}
    )
    assert incomplete_reason(cost, detail=True) == "Usage was not recorded."
    assert cost["estimated_usd"] is None and cost["known_usage_subtotal_usd"] == 0


def test_expanded_report_estimate_states_never_repeat_reason_only_helpers(tmp_path):
    import re
    from html import unescape

    from test_workflow import setup_case

    state, runner, run_id, _ = setup_case(tmp_path, one_market=True)
    asyncio.run(runner.execute(run_id))
    from campaign_studio.delivery import delivery_manifest

    current = Workspace(state).current_composition(state.run(run_id)["campaign_id"])
    manifest = delivery_manifest(current)
    brand_name = current["plan"].brief.brand.name
    for coverage, count, subtotal, missing, reason in [
        ("complete", 2, 0, 2, "request"),
        ("complete", 1, 0, 1, "missing_usage"),
        ("complete", 2, 0.1, 1, "missing_usage"),
        ("complete", 2, 0.001, 1, "missing_usage"),
        ("incomplete", 2, 0, 1, "request"),
        ("incomplete", 2, 0.11, 1, "request"),
        ("unknown", None, 0, 0, "request"),
    ]:
        manifest["campaign_metrics"] = {
            "coverage": coverage,
            "attempted_requests": count,
            "usage_cost_estimate": {
                "estimated_usd": None,
                "known_usage_subtotal_usd": subtotal,
                "unreported_attempts": missing,
                "incomplete_reasons": [{"code": reason, "count": missing}],
            },
        }
        shown = campaign_metric_display(manifest["campaign_metrics"])
        assert shown["has_known_portion"] is (subtotal > 0)
        report = render_manifest_report(brand_name, manifest).decode()
        details = report.split("<summary>Full production details and estimates</summary>", 1)[
            1
        ].split("</details>", 1)[0]
        plain = unescape(re.sub("<[^>]+>", "", details))
        assert shown["detail"] in plain
        assert plain.count(shown["detail"]) == 1
        if subtotal == 0:
            assert plain.count(shown["spend_helper"]) <= 1
    manifest["campaign_metrics"] = state.campaign_metrics(state.run(run_id)["campaign_id"])
    assert (
        "<strong>Campaign estimate:</strong>"
        not in render_manifest_report(brand_name, manifest).decode()
    )
