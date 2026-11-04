"""Damaged neighbors and interrupted actions must not strand unrelated campaigns."""

import asyncio
import copy
import json
import sqlite3

import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.brief import MAX_PHRASE_LENGTH, MAX_PHRASE_RULES, Brief
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.planning import copy_findings, make_plan
from campaign_studio.sample import write_sample
from campaign_studio.storage import Storage


@pytest.mark.parametrize("damage", ["missing_manifest", "malformed_manifest", "wrong_shape"])
def test_damaged_saved_campaign_does_not_hide_healthy_workbench_cards(tmp_path, damage):
    app = create_app(Settings(data_dir=tmp_path), key_available=lambda: False)
    with TestClient(app, base_url="http://localhost") as client:
        ws = app.state.workspace
        damaged = seed_campaign(ws, replay=True)
        run = ws.run_for(damaged)["id"]
        ws.state.confirm(run, ws.state.plan(run).digest)
        saved = asyncio.run(app.state.runner.execute(run))
        healthy, _ = Intake(ws.storage).new(name="Keep editing me")
        ref = ws.state.revision(saved["revision_id"])["manifest_ref"]
        path = ws.storage.path(ref)
        if damage == "missing_manifest":
            path.unlink()
        else:
            path.write_bytes(b"{" if damage == "malformed_manifest" else b"[]")
        response = client.get("/api/motif/workspace")
        assert response.status_code == 200, response.text
        rows = {c["id"]: c for c in response.json()["campaigns"]}
        assert rows[damaged]["status"] == "damaged"
        assert "Restore" in rows[damaged]["recovery_message"]
        assert rows[healthy]["status"] == "draft"
        assert client.get(f"/api/motif/campaigns/{healthy}").status_code == 200


def test_catalog_deletion_overlap_is_skipped_but_database_failure_is_not_hidden(
    tmp_path, monkeypatch
):
    app = create_app(Settings(data_dir=tmp_path), key_available=lambda: False)
    with TestClient(app, base_url="http://localhost") as client:
        ws = app.state.workspace
        removed, _ = Intake(ws.storage).new(name="Deleting")
        other, _ = Intake(ws.storage).new(name="Other")
        cleanup = Cleanup(ws.state)
        preview = cleanup.preview_campaign(removed)
        catalog = ws.catalog

        def enumerate_then_delete():
            entries = catalog()
            cleanup.delete_campaign(removed, preview["token"], defer_cleanup=True)
            return entries

        monkeypatch.setattr(ws, "catalog", enumerate_then_delete)
        response = client.get("/api/motif/workspace")
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["campaigns"]] == [other]
        assert response.json()["pending_cleanup"]
        monkeypatch.setattr(ws, "catalog", catalog)

        def database_failed(*args):
            raise sqlite3.DatabaseError("controlled database failure")

        monkeypatch.setattr(ws.state, "saved_activity", database_failed)
        assert client.get("/api/motif/workspace").status_code == 503


@pytest.mark.parametrize("bad_draft", [b"{", b"[]", b'{"value":null}'])
def test_damaged_draft_can_be_deleted_only_with_owned_inventory_and_current_token(
    tmp_path, bad_draft
):
    app = create_app(Settings(data_dir=tmp_path), key_available=lambda: False)
    with TestClient(app, base_url="http://localhost"):
        ws = app.state.workspace
        intake, cleanup = Intake(ws.storage), Cleanup(ws.state)
        damaged, _ = intake.new(name="Damaged")
        healthy, original = intake.new(name="Healthy")
        root = ws.storage.path(ws.storage.project(damaged).draft).parents[1]
        unmanaged = root / "keep-my-file.txt"
        unmanaged.write_text("not managed by Motif")
        draft = ws.storage.path(ws.storage.project(damaged).draft)
        draft.write_bytes(bad_draft)
        before = ws.storage.path(ws.storage.project(healthy).draft).read_bytes()
        preview = cleanup.preview_campaign(damaged)
        assert preview["unmanaged_file_count"] == 1
        assert preview["name"] == f"Campaign {damaged[:8]}"
        result = cleanup.delete_campaign(damaged, preview["token"])
        assert result["state"] == "complete"
        assert not draft.exists() and unmanaged.read_text() == "not managed by Motif"
        assert ws.storage.path(ws.storage.project(healthy).draft).read_bytes() == before
        assert intake.read(healthy) == original


def test_phrase_rule_limits_and_evidence_preserve_all_decisions_without_repeating_full_copy(
    tmp_path,
):
    storage = Storage(tmp_path)
    _, brief = write_sample(storage, 3)
    raw = brief.model_dump(mode="json")
    content = raw["markets"]["en-US"]
    content["copy"] = {"message": "X" * 120, "cta": "X" * 60}
    content["phrase_rules"] = [
        {"id": f"rule-{n}", "phrase": "X" * (n + 1)} for n in range(MAX_PHRASE_RULES)
    ]
    parsed = Brief.model_validate(raw)
    output = (
        make_plan(brief, storage)
        .outputs[0]
        .model_copy(update={"text": parsed.markets["en-US"].text})
    )
    findings = copy_findings(parsed, output)
    assert len(findings) == MAX_PHRASE_RULES
    assert all(
        item.severity == "blocking" and len(item.context.phrase.matches) == 2 for item in findings
    )
    values = [m.value for f in findings for m in f.context.phrase.matches if m.value is not None]
    assert values == ["X" * 120, "X" * 60]
    assert len(json.dumps([f.model_dump(mode="json") for f in findings])) < 50_000
    too_many = copy.deepcopy(raw)
    too_many["markets"]["en-US"]["phrase_rules"].append({"id": "extra", "phrase": "Extra"})
    with pytest.raises(ValueError, match="at most 32"):
        Brief.model_validate(too_many)
    content["phrase_rules"][0]["phrase"] = "X" * (MAX_PHRASE_LENGTH + 1)
    with pytest.raises(ValueError, match="at most 256"):
        Brief.model_validate(raw)
