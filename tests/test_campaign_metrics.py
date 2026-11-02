"""Campaign totals derive from durable attempts, never saves or repeated image sets."""

import asyncio
import io
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from campaign_fixtures import zip_root
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
from campaign_studio.exporting import package_payloads, render_report
from campaign_studio.generation import GenerationRequest
from campaign_studio.handoff import Handoff, PackageAction
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import RevisionTarget
from campaign_studio.state import State


@pytest.mark.parametrize("journal", ["DELETE", "WAL"])
def test_campaign_metrics_reads_attempts_and_publication_from_one_snapshot(
    tmp_path, monkeypatch, journal
):
    state, _, run, _ = generated_case(tmp_path)
    cid = state.run(run)["campaign_id"]
    plan_ref = state.run(run)["plan_ref"]
    state.set_status(run, "running")
    manifest_ref = f"private/runs/{run}/controlled-publication.json"
    state.storage.write(
        manifest_ref,
        json.dumps(
            {"schema_version": 1, "campaign_id": cid, "metrics": {"attempted_requests": 1}}
        ).encode(),
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
                "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "interleaved-revision",
                    run,
                    plan_ref,
                    manifest_ref,
                    0,
                    "2026-10-30T00:00:01+00:00",
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
            return Rows(cursor) if sql.startswith("SELECT a.*,r.plan_ref FROM attempts") else cursor

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
    first = mocked_production(
        state, runner, run_a, response, {"input_tokens": 244, "output_tokens": 3276}
    )
    first_total = state.campaign_metrics(a)
    assert first_total["attempted_requests"] == 1
    assert first_total["usage_cost_estimate"]["estimated_usd"] == 0.0995
    value = state.plan(run_a).brief.model_dump()
    value["campaign_id"] = "014c1210-29a9-50d2-823a-7a936aecd62b"
    b = value["campaign_id"]
    run_b = state.create(make_plan(Brief.model_validate(value), state.storage), 1)
    state.confirm(run_b, state.plan(run_b).digest)
    mocked_production(state, runner, run_b, response, {"input_tokens": 12, "output_tokens": 34})
    b_total = state.campaign_metrics(b)
    assert b_total["usage_cost_estimate"]["estimated_usd"] == 0.00108
    assert state.plan(run_a).brief.name == state.plan(run_b).brief.name

    # A second actual mock-transport production run adds exactly its own attempt.
    value = state.plan(run_a).brief.model_dump()
    value["audience"] += " A new campaign production run."
    run_a2 = state.create(make_plan(Brief.model_validate(value), state.storage), 1)
    state.confirm(run_a2, state.plan(run_a2).digest)
    mocked_production(state, runner, run_a2, response, {"input_tokens": 100, "output_tokens": 200})
    total = state.campaign_metrics(a)
    assert total["attempted_requests"] == total["successful_requests"] == 2
    assert total["usage_cost_estimate"]["estimated_usd"] == 0.106
    assert total["usage_cost_estimate"]["actual_charge"] is None
    assert state.campaign_metrics(b) == b_total
    assert state.revision(first["revision_id"])["run_id"] == run_a

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
            assert model["manifest"]["metrics"]["completed_images"] == 12
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
        assert corrected["metrics"]["completed_images"] == 12
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
    reference = state.run(run_b)["plan_ref"]
    historical = json.loads(state.storage.path(reference).read_bytes())
    historical["generation_requests"]["product-1"]["model"] = "unpriced-historical-model"
    state.storage.write(reference, json.dumps(historical).encode())
    summary = state.campaign_metrics(cid)
    assert summary["attempted_requests"] == summary["failed_requests"] == 2
    assert summary["usage_cost_estimate"]["estimated_usd"] is None
    assert summary["usage_cost_estimate"]["known_usage_subtotal_usd"] == 0.0065
    # Loss of a historical request cannot turn its recorded attempt into free work.
    state.storage.path(reference).unlink()
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
    reference = f"private/runs/{run_id}/damaged-manifest.json"
    state.storage.write(
        reference,
        json.dumps(
            {"schema_version": 1, "campaign_id": cid, "metrics": {"attempted_requests": 3}}
        ).encode(),
    )
    with state.storage.connect() as db:
        db.execute(
            "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "e" * 32,
                run_id,
                state.run(run_id)["plan_ref"],
                reference,
                1,
                state.run(run_id)["created_at"],
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


def test_package_reuse_tracks_campaign_summary_without_rewriting_prior_exports(tmp_path):
    state, runner, run_id, response = generated_case(tmp_path)
    manifest = mocked_production(
        state, runner, run_id, response, {"input_tokens": 244, "output_tokens": 3276}
    )
    cid = state.run(run_id)["campaign_id"]
    target = RevisionTarget(
        campaign_id=cid, run_id=run_id, revision_id=manifest["revision_id"], mode="current"
    )
    handoff = Handoff(state)
    action = PackageAction(snapshot_id=handoff.metadata(target)["snapshot_id"])
    first_zip = handoff.create_zip(target, action)
    first_bytes = handoff.download(target, first_zip["artifact_id"])
    first_folder = handoff.create_folder(target, action)
    first_files = {
        p.name: p.read_bytes() for p in Path(first_folder["location"]).iterdir() if p.is_file()
    }
    assert handoff.create_zip(target, action) == first_zip
    assert handoff.create_folder(target, action) == first_folder
    original_total = state.campaign_metrics(cid)

    # A retained failed retry adds an actual attempt without publishing new pixels.
    with state.storage.connect() as db:
        db.execute("UPDATE runs SET allowance=2,state='running' WHERE id=?", (run_id,))
    attempt = state.reserve(run_id, "product-1")
    state.finish_attempt(attempt["id"], "failed", error="http_503")
    state.set_status(run_id, "paused")
    current_total = state.campaign_metrics(cid)
    assert current_total["attempted_requests"] == 2
    assert current_total["usage_cost_estimate"]["estimated_usd"] is None
    assert handoff.metadata(target)["snapshot_id"] == action.snapshot_id
    second_zip = handoff.create_zip(target, action)
    second_folder = handoff.create_folder(target, action)
    assert second_zip["artifact_id"] != first_zip["artifact_id"]
    assert second_folder["location"] != first_folder["location"]
    assert handoff.create_zip(target, action) == second_zip
    assert handoff.create_folder(target, action) == second_folder
    assert handoff.download(target, first_zip["artifact_id"]) == first_bytes
    assert all(
        (Path(first_folder["location"]) / name).read_bytes() == value
        for name, value in first_files.items()
    )
    second_bytes = handoff.download(target, second_zip["artifact_id"])
    for payload, expected in [(first_bytes, original_total), (second_bytes, current_total)]:
        with ZipFile(io.BytesIO(payload)) as archive:
            public = json.loads(archive.read(f"{zip_root(archive)}/manifest.json"))
            assert public["campaign_metrics"] == expected
            assert public["report_created_at"].endswith("+00:00")
            assert public["metrics"]["attempted_requests"] == 1  # Saved output operation scope.
            assert not any(
                key in json.dumps(public["campaign_metrics"])
                for key in ("prompt", "response_ref", "plan_ref")
            )
    assert state.campaign_metrics(cid) == current_total  # Exports never add spend or attempts.
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
    result = asyncio.run(runner.execute(run_id))
    revision = state.revision(result["revision_id"])
    manifest = json.loads(package_payloads(state, result["revision_id"])["manifest.json"])
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
        report = render_report(state, revision, manifest).decode()
        paragraph = unescape(
            re.search(r"<strong>Campaign estimate:</strong> (.*?)</p>", report).group(1)
        )
        assert paragraph == (shown["spend_helper"] + " " if subtotal > 0 else "") + shown["detail"]
        if subtotal == 0:
            assert paragraph.count(shown["spend_helper"]) <= 1
    manifest["campaign_metrics"] = state.campaign_metrics(result["campaign_id"])
    assert (
        "<strong>Campaign estimate:</strong>"
        not in render_report(state, revision, manifest).decode()
    )
