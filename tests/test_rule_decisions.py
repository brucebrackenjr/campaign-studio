"""Current rule edits gate delivery without creating or restoring image versions."""

import asyncio
import io
import json
from contextlib import contextmanager
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient
from test_provider_runner import generated_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import write
from campaign_studio.compositions import save_metadata
from campaign_studio.config import Settings
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import CreativeUpdate
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.workspace import TextEdit, Workspace


@contextmanager
def prepare(tmp_path, initial_partial=False):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Rule edits must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, brief = write_sample(ws.storage, 2, True)
        value = brief.model_dump(mode="json")
        value["products"][0]["copy_overrides"] = {"en-US": {"message": "LOCAL CONTROL"}}
        value["markets"]["en-US"]["phrase_rules"] = []
        run = ws.state.create(make_plan(Brief.model_validate(value), ws.storage), 0)
        write(
            ws.storage,
            brief.campaign_id,
            {"version": uuid4().hex, "composition_schema": 1, "authored": {"brief": value}},
        )
        if initial_partial:
            value["products"][0]["source"] = {
                "role": "concept",
                "imagery_absent": True,
                "description": "Missing source.",
                "provenance": value["products"][0]["source"]["provenance"],
            }
            write(
                ws.storage,
                brief.campaign_id,
                {"version": uuid4().hex, "composition_schema": 1, "authored": {"brief": value}},
            )
        client.headers["X-Campaign-Token"] = app.state.csrf
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        yield app, client, run, client.get(url).json()["manifest"], url


def save(client, url, change):
    record = client.get(url + "/authored").json()
    body = {k: record[k] for k in ("version", "base", "authored")}
    change(body["authored"])
    result = client.post(url + "/authored", json=body)
    assert result.status_code == 200, result.text
    return result


def recheck(client, url, phrase, enabled=True):
    record = client.get(url + "/authored").json()
    body = {k: record[k] for k in ("version", "base", "authored")}
    body["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = (
        [{"id": "local-control", "phrase": phrase}] if enabled else []
    )
    return client.post(url + "/authored", json=body), body


def package(client, cid, rid, shown, *, preview=False):
    action = "report-preview" if preview else "zip"
    return client.post(
        f"/api/campaigns/{cid}/delivery/{action}",
        json={"version": shown["version"], "allow_partial": True},
    )


def files(ws):
    return {
        str(p): p.read_bytes()
        for p in ws.storage.root.rglob("*")
        if p.is_file()
        and p.suffix not in {".sqlite3", ".sqlite", ".db"}
        and "-wal" not in p.name
        and "-shm" not in p.name
    }


@pytest.mark.parametrize("mixed_generation", [False, True])
@pytest.mark.parametrize("instruction_change", [None, "before_save", "after_save"])
def test_generated_source_reuse_survives_copy_rule_edits_and_reopen(
    tmp_path, monkeypatch, mixed_generation, instruction_change
):
    state, runner, run, response = generated_case(tmp_path, 2 if mixed_generation else 1)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=response)

    asyncio.run(
        runner.execute(run, OpenAIImages("synthetic", transport=httpx.MockTransport(transport)))
    )
    ws = Workspace(state)
    cid = state.run(run)["campaign_id"]

    def save_brief(change):
        projects = AuthoredProjects(ws)
        record = projects.read(cid)
        change(record["authored"]["brief"])
        projects.save(cid, AuthoredSave(**{k: record[k] for k in ("version", "base", "authored")}))

    before = ws.current_composition(cid)
    sources = {pid: a.sha256 for pid, a in before["sources"].items()}
    attempts = state.attempts(run)
    metrics = state.campaign_metrics(cid)
    if instruction_change == "before_save":
        monkeypatch.setattr(
            "campaign_studio.generation.CONCEPT_FRAMING", "New provider instruction wording."
        )
    save_brief(lambda b: b["markets"]["en-US"]["copy"].update(message="Current shared wording."))
    snapshot = ws.current_composition(cid)
    ws.save_composition(
        cid,
        TextEdit(
            revision_id=snapshot["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            values={"cta": "Current product CTA."},
            field_scopes={"cta": "product"},
        ),
    )
    if instruction_change == "after_save":
        monkeypatch.setattr(
            "campaign_studio.generation.CONCEPT_FRAMING", "New provider instruction wording."
        )
    with monkeypatch.context() as patch:
        patch.setattr(
            "campaign_studio.delivery.compose", lambda *a, **k: pytest.fail("Rule save rendered")
        )
        save_brief(
            lambda b: b["markets"]["en-US"].update(
                phrase_rules=[{"id": "hold", "phrase": "Current shared"}]
            )
        )
        current = Workspace(State(state.storage)).current_composition(cid)
        assert {pid: a.sha256 for pid, a in current["sources"].items()} == sources
        assert current["plan"].generation_products == []
        with pytest.raises(StateError, match="prohibited wording"):
            Handoff(state).create_current_zip(cid, DeliveryAction(version=current["version"]))
        save_brief(lambda b: b["markets"]["en-US"].update(phrase_rules=[]))
    current = ws.current_composition(cid)
    with (
        Handoff(state).create_current_zip(
            cid, DeliveryAction(version=current["version"])
        ) as result,
        ZipFile(result.stream) as archive,
    ):
        manifest = json.loads(
            archive.read(next(n for n in archive.namelist() if n.endswith("/manifest.json")))
        )
    assert all(
        o["copy"]["message"] == "Current shared wording."
        for o in manifest["outputs"]
        if o["market"] == "en-US"
    )
    assert all(
        o["copy"]["cta"] == "Current product CTA."
        for o in manifest["outputs"]
        if o["product_id"] == "product-1" and o["market"] == "en-US"
    )
    assert state.attempts(run) == attempts and state.campaign_metrics(cid) == metrics
    assert len(calls) == (2 if mixed_generation else 1)
    assert not list(state.storage.root.rglob("Outputs"))


@pytest.mark.parametrize(
    "phrase,count", [("absent phrase", 0), ("LOCAL CONTROL", 3), ("Explore", 6)]
)
def test_rules_save_immediately_and_block_all_deliveries_until_removed(tmp_path, phrase, count):
    with prepare(tmp_path) as (app, client, run, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        original = client.get(url).json()
        downloaded = package(client, cid, run, original)
        assert downloaded.status_code == 200
        preview = package(client, cid, run, original, preview=True)
        assert preview.status_code == 200
        response, stale = recheck(client, url, phrase)
        assert response.status_code == 200, response.text
        shown = client.get(url).json()
        assert len(shown["rule_withheld_outputs"]) == count
        assert all(o["url"] for o in shown["manifest"]["outputs"])
        assert package(client, cid, run, shown).status_code == (409 if count else 200)
        assert package(client, cid, run, shown, preview=True).status_code == (409 if count else 200)
        assert package(client, cid, run, original).status_code == 409
        assert client.get(preview.json()["url"]).status_code == 409
        assert client.post(url + "/authored", json=stale).status_code == 200
        stale["authored"]["brief"]["name"] = "Stale name"
        assert client.post(url + "/authored", json=stale).status_code == 409
        assert recheck(client, url, phrase, False)[0].status_code == 200
        restored = client.get(url).json()
        assert restored["wording_issue"] is None
        assert package(client, cid, run, restored).status_code == 200
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            assert archive.testzip() is None
        assert ws.state.attempts(run) == [] and ws.state.revision_count(run) == 0
        assert not list(ws.storage.root.rglob("Exports"))


def test_rule_changes_and_copy_corrections_preserve_current_description_and_explicit_clear(
    tmp_path, monkeypatch
):
    with prepare(tmp_path) as (app, client, run, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        for creative in ("product-1/en-US/1x1", "product-2/en-US/1x1"):
            current = client.get(url).json()
            save_metadata(
                ws,
                cid,
                current["version"],
                creative,
                CreativeUpdate(expected_version=0, description="Producer supplied description."),
            )
        current = client.get(url).json()
        save_metadata(
            ws,
            cid,
            current["version"],
            "product-2/en-US/1x1",
            CreativeUpdate(expected_version=1, description=None),
        )
        before = client.get(url).json()["metadata"]["creatives"]
        with monkeypatch.context() as patch:
            patch.setattr(
                "campaign_studio.delivery.compose",
                lambda *a, **k: pytest.fail("Rule save rendered"),
            )
            for enabled in (True, False, True, False):
                assert recheck(client, url, "LOCAL CONTROL", enabled)[0].status_code == 200
                assert client.get(url).json()["metadata"]["creatives"] == before
        shown = client.get(url).json()
        ws.save_composition(
            cid,
            TextEdit(
                revision_id=shown["version"],
                product_id="product-1",
                market="en-US",
                ratio="1:1",
                values={"message": "Changed producer wording."},
                field_scopes={"message": "product"},
            ),
        )
        assert client.get(url).json()["metadata"]["creatives"] == before
        assert ws.state.attempts(run) == []


def test_failed_rule_save_preserves_previous_current_rules_and_delivery(tmp_path):
    with prepare(tmp_path) as (app, client, run, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        previous = client.get(url).json()
        with ws.storage.connect() as db:
            db.execute(
                "CREATE TRIGGER deny_rule_save BEFORE UPDATE ON campaign_drafts "
                "BEGIN SELECT RAISE(FAIL,'injected rule failure'); END"
            )
        response, _ = recheck(client, url, "LOCAL CONTROL")
        assert response.status_code in {409, 500, 503}, response.text
        assert "message" in response.json()
        with ws.storage.connect() as db:
            db.execute("DROP TRIGGER deny_rule_save")
        current = client.get(url).json()
        assert current["version"] == previous["version"] and current["wording_issue"] is None
        assert package(client, cid, run, current).status_code == 200


def test_missing_source_partial_choice_cannot_bypass_current_wording_rule(tmp_path):
    with prepare(tmp_path, True) as (app, client, run, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        shown = client.get(url).json()
        result = package(client, cid, run, shown)
        assert result.status_code == 200, result.text
        with ZipFile(io.BytesIO(result.content)) as archive:
            assert len([n for n in archive.namelist() if n.endswith(".png")]) == 3
        assert recheck(client, url, "LOCAL CONTROL")[0].status_code == 200
        assert package(client, cid, run, client.get(url).json()).status_code == 409
        assert recheck(client, url, "LOCAL CONTROL", False)[0].status_code == 200
        assert package(client, cid, run, client.get(url).json()).status_code == 200
        assert ws.state.attempts(run) == []


def test_unstarted_source_plan_cannot_restore_earlier_copy_or_rule_settings(tmp_path):
    with prepare(tmp_path) as (app, client, run, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        old = ws.current_composition(cid)
        pending = ws.state.create(make_plan(old["plan"].brief, ws.storage), 0)
        recheck(client, url, "LOCAL CONTROL")
        blocked = client.get(url).json()
        assert blocked["wording_issue"] and package(client, cid, run, blocked).status_code == 409
        assert ws.state.run(pending)["state"] == "planned"
        assert ws.state.attempts(pending) == []
        save(
            client,
            url,
            lambda a: a["brief"]["products"][0]["copy_overrides"]["en-US"].update(
                message="Corrected current copy."
            ),
        )
        current = client.get(url).json()
        assert (
            current["wording_issue"] is None
            and package(client, cid, run, current).status_code == 200
        )


def test_rule_changes_do_not_mask_source_corruption_or_recover_provider_work(tmp_path):
    with prepare(tmp_path) as (app, client, run, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        source = ws.current_composition(cid)["sources"]["product-2"]
        path = ws.storage.path(source.reference)
        original = path.read_bytes()
        path.write_bytes(b"Synthetic source corruption")
        assert recheck(client, url, "LOCAL CONTROL")[0].status_code == 200
        assert package(client, cid, run, client.get(url).json()).status_code == 409
        assert recheck(client, url, "LOCAL CONTROL", False)[0].status_code == 200
        current = client.get(url).json()
        assert current["wording_issue"] is None
        assert package(client, cid, run, current).status_code == 409
        path.write_bytes(original)
        assert package(client, cid, run, client.get(url).json()).status_code == 200
        assert ws.state.attempts(run) == []
