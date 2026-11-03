"""Prohibited wording blocks campaign exports, including subset/download bypasses."""

import asyncio
import copy
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from campaign_studio.app import create_app
from campaign_studio.brief import Brief, PhraseRule
from campaign_studio.config import Settings
from campaign_studio.exporting import wording_export_issue
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_sample


@contextmanager
def campaign(tmp_path, *, generation_partial=False, products=2):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Local correction must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, initial = write_sample(ws.storage, one_market=True)
        value = initial.model_dump(mode="json")
        value["products"] = value["products"][:products]
        value["markets"]["en-US"]["phrase_rules"] = []
        value["products"][0]["copy_overrides"] = {"en-US": {"message": "Restricted wording."}}
        brief = Brief.model_validate(value)
        rid = ws.state.create(make_plan(brief, ws.storage), 0)
        ws.state.confirm(rid, ws.state.plan(rid).digest)
        original_write = ws.storage.write

        def fail_second(reference, payload):
            if "/images/product-2/" in reference:
                raise OSError("Controlled local output failure")
            return original_write(reference, payload)

        if generation_partial:
            ws.storage.write = fail_second
        try:
            asyncio.run(app.state.runner.execute(rid))
        finally:
            ws.storage.write = original_write
        client.headers["X-Campaign-Token"] = app.state.csrf
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        save(client, url)
        yield client, ws, url


def save(client, url, change=None):
    project = client.get(url + "/authored").json()
    body = {key: project[key] for key in ("version", "base", "authored")}
    if change:
        change(body["authored"])
    result = client.post(url + "/authored", json=body)
    assert result.status_code == 200, result.text
    return result.json()


def apply_rules(client, url, rules):
    current = client.get(url).json()
    value = copy.deepcopy(current["project"]["authored"]["brief"])
    value["markets"]["en-US"]["phrase_rules"] = rules
    result = client.post(
        url + "/recheck",
        json={
            "value": value,
            "version": current["version"],
            "base_revision": current["base_revision"],
            "run_id": current["run"]["id"],
        },
    )
    assert result.status_code == 200, result.text
    return client.get(url).json()


def export_urls(model):
    cid, rid, revision = (
        model["value"]["campaign_id"],
        model["output_run_id"],
        model["manifest"]["revision_id"],
    )
    return {
        "zip": f"/api/campaigns/{cid}/runs/{rid}/revisions/{revision}/zip",
        "folder": f"/api/campaigns/{cid}/export",
        "body": {
            "run_id": rid,
            "revision_id": revision,
            "snapshot_id": model["metadata"]["snapshot_id"],
            "allow_partial": True,
        },
    }


def test_all_campaign_export_paths_block_while_diagnostic_display_and_artifacts_survive(tmp_path):
    with campaign(tmp_path) as (client, ws, url):
        before = client.get(url).json()
        paths = export_urls(before)
        zipped = client.post(
            paths["zip"], json={k: paths["body"][k] for k in ("snapshot_id", "allow_partial")}
        )
        folder = client.post(paths["folder"], json=paths["body"])
        assert zipped.status_code == folder.status_code == 200
        zip_url, folder_url = zipped.json()["url"], folder.json()["url"]
        zip_bytes, html = client.get(zip_url).content, client.get(folder_url).content
        files = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in ws.storage.path("Projects").glob("*/Exports/**/*")
            if p.is_file()
        }
        old_image = next(o["url"] for o in before["manifest"]["outputs"] if o["file"])
        rule = {
            "id": "restricted",
            "phrase": "Restricted",
        }
        save(client, url, lambda a: a["brief"]["markets"]["en-US"].update(phrase_rules=[rule]))
        assert client.get(url).json()["wording_issue"]["code"] == "pending_rules"
        for route in (zip_url, folder_url, old_image + "?download=true"):
            assert client.get(route).status_code == 409
        assert client.get(old_image).status_code == 200
        partial = apply_rules(client, url, [rule])
        assert partial["manifest"]["metrics"]["completed_images"] == 3
        assert len(partial["wording_issue"]["matches"]) == 3
        current = export_urls(partial)
        for mode in (False, True):
            assert (
                client.post(
                    current["zip"],
                    json={"snapshot_id": current["body"]["snapshot_id"], "allow_partial": mode},
                ).status_code
                == 409
            )
            assert (
                client.post(
                    current["folder"], json={**current["body"], "allow_partial": mode}
                ).status_code
                == 409
            )
        clear_image = next(o["url"] for o in partial["manifest"]["outputs"] if o["file"])
        assert client.get(clear_image).status_code == 200
        assert client.get(clear_image + "?download=true").status_code == 409
        assert client.get(zip_url).status_code == client.get(folder_url).status_code == 409
        assert files == {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in ws.storage.path("Projects").glob("*/Exports/**/*")
            if p.is_file()
        }
        restored = apply_rules(client, url, [])
        assert restored["wording_issue"] is None
        assert [o["sha256"] for o in restored["manifest"]["outputs"]] == [
            o["sha256"] for o in before["manifest"]["outputs"]
        ]
        assert client.get(zip_url).content == zip_bytes
        assert client.get(folder_url).content == html
        assert client.get(old_image + "?download=true").status_code == 409  # stale target
        assert ws.state.attempts(before["run"]["id"]) == []


@pytest.mark.parametrize("rules", [None, [], "omitted"])
def test_unrelated_unfinished_work_and_optional_rules_do_not_block_export(tmp_path, rules):
    with campaign(tmp_path, generation_partial=True) as (client, ws, url):

        def unfinished(a):
            a["brief"]["brand"]["text_color"] = "raw invalid color"
            a["brief"]["markets"]["en-US"]["copy"]["message"] = ""
            market = a["brief"]["markets"]["en-US"]
            if rules == "omitted":
                market.pop("phrase_rules", None)
            else:
                market["phrase_rules"] = rules

        save(client, url, unfinished)
        current = client.get(url).json()
        assert current["manifest"]["metrics"]["completed_images"] == 3
        assert wording_export_issue(ws.state, current["value"]["campaign_id"]) is None
        paths = export_urls(current)
        assert client.post(paths["folder"], json=paths["body"]).status_code == 200
        image = next(o["url"] for o in current["manifest"]["outputs"] if o["file"])
        assert client.get(image + "?download=true").status_code == 200


def test_pending_invalid_rule_form_is_addressed_and_cancel_restores_export(tmp_path):
    with campaign(tmp_path) as (client, _, url):
        save(
            client,
            url,
            lambda a: a["form_drafts"].update(
                fields={
                    "rule/en/-1": {"mw-rule-phrase": ""},
                }
            ),
        )
        issue = client.get(url).json()["wording_issue"]
        assert issue["code"] == "pending_rules"
        assert issue["pending_rule_targets"] == [{"market": "en-US", "rule_index": -1}]
        save(client, url, lambda a: a["form_drafts"].pop("fields"))
        assert client.get(url).json()["wording_issue"] is None


def test_advisory_severity_is_not_an_accepted_rule_and_siempre_blocks(tmp_path):
    with pytest.raises(ValidationError):
        PhraseRule.model_validate(
            {
                "id": "old",
                "phrase": "siempre",
                "severity": "warning",
            }
        )
    with campaign(tmp_path) as (_, ws, _):
        _, brief = write_sample(ws.storage)
        value = brief.model_dump(mode="json")
        value["markets"]["es-MX"]["phrase_rules"] = [
            {
                "id": "prohibited-siempre",
                "phrase": "siempre",
            }
        ]
        plan = make_plan(Brief.model_validate(value), ws.storage)
        matches = [
            f for f in plan.findings if f.context and f.context.phrase and f.context.phrase.matches
        ]
        assert len(matches) == 6 and {f.severity for f in matches} == {"blocking"}


def test_all_withheld_can_correct_wording_with_exact_preview_and_no_provider(tmp_path):
    with campaign(tmp_path, products=1) as (client, ws, url):
        before = client.get(url).json()
        rule = {
            "id": "restricted",
            "phrase": "Restricted",
        }
        blocked = apply_rules(client, url, [rule])
        assert blocked["manifest"]["metrics"]["completed_images"] == 0
        cid = blocked["value"]["campaign_id"]
        edit = {
            "ratio": "1:1",
            "run_id": blocked["run"]["id"],
            "revision_id": blocked["manifest"]["revision_id"],
            "product_id": "product-1",
            "market": "en-US",
            "values": {"message": "Allowed wording."},
            "field_scopes": {"message": "product"},
        }
        preview = client.post(f"/api/campaigns/{cid}/text/preview", json=edit)
        assert preview.status_code == 200, preview.text
        assert not preview.json()["blocked"] and len(preview.json()["images"]) == 1
        assert (
            client.get(url).json()["manifest"]["revision_id"] == blocked["manifest"]["revision_id"]
        )
        applied = client.post(
            f"/api/campaigns/{cid}/text/apply",
            json={**edit, "preview_digest": preview.json()["digest"]},
        )
        assert applied.status_code == 200, applied.text
        current = client.get(url).json()
        assert current["manifest"]["metrics"]["completed_images"] == 3
        assert current["wording_issue"] is None
        assert current["campaign_metrics"] == before["campaign_metrics"]
        paths = export_urls(current)
        assert client.post(paths["folder"], json=paths["body"]).status_code == 200


def test_unchanged_saved_rule_form_is_not_pending_and_newer_rule_save_survives_ack(
    tmp_path, monkeypatch
):
    from campaign_studio import rendering
    from campaign_studio.authored import AuthoredProjects, AuthoredSave

    with campaign(tmp_path) as (client, ws, url):
        rule = {
            "id": "unused",
            "phrase": "ABSENT",
        }
        apply_rules(client, url, [rule])
        save(
            client,
            url,
            lambda a: a["form_drafts"].update(
                fields={
                    "rule/en/0": {
                        "mw-rule-phrase": "ABSENT",
                    },
                }
            ),
        )
        assert client.get(url).json()["wording_issue"] is None
        save(client, url, lambda a: a["form_drafts"].pop("fields"))
        original = rendering.render_revision

        def save_during_recheck(*args, **kwargs):
            manifest = original(*args, **kwargs)
            projects = AuthoredProjects(ws)
            current = projects.read(manifest["campaign_id"])
            current["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = [
                dict(rule, phrase="NEWER RAW RULE")
            ]
            current["authored"]["brief"]["brand"]["text_color"] = "unfinished purple"
            projects.save(
                manifest["campaign_id"],
                AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}),
            )
            return manifest

        monkeypatch.setattr(rendering, "render_revision", save_during_recheck)
        current = apply_rules(client, url, [])
        assert current["wording_issue"]["code"] == "pending_rules"
        authored = current["project"]["authored"]["brief"]
        assert authored["markets"]["en-US"]["phrase_rules"][0]["phrase"] == "NEWER RAW RULE"
        assert authored["brand"]["text_color"] == "unfinished purple"
        assert current["output_brief"]["markets"]["en-US"]["phrase_rules"] == []


def test_rule_save_cannot_cross_checked_package_publication(tmp_path, monkeypatch):
    from campaign_studio import handoff
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.handoff import Handoff, PackageAction
    from campaign_studio.review import RevisionTarget

    with campaign(tmp_path) as (client, ws, url):
        current = client.get(url).json()
        cid, rid = current["value"]["campaign_id"], current["output_run_id"]
        projects = AuthoredProjects(ws)
        later = projects.read(cid)
        later["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = [
            {
                "id": "later",
                "phrase": "Restricted",
            }
        ]
        target = RevisionTarget(
            campaign_id=cid,
            run_id=rid,
            revision_id=current["manifest"]["revision_id"],
            mode="current",
        )
        action = PackageAction(snapshot_id=current["metadata"]["snapshot_id"])
        writing, release, save_waiting, save_acquired = (threading.Event() for _ in range(4))
        export_zip, draft_lock = handoff.export_zip, ws.storage.exclusive_draft

        def held_export(*args, **kwargs):
            writing.set()
            assert release.wait(10)
            return export_zip(*args, **kwargs)

        @contextmanager
        def observed_draft_lock():
            saving = threading.current_thread().name.startswith("later-save")
            if saving:
                save_waiting.set()
            with draft_lock():
                if saving:
                    save_acquired.set()
                yield

        monkeypatch.setattr(handoff, "export_zip", held_export)
        monkeypatch.setattr(ws.storage, "exclusive_draft", observed_draft_lock)
        with (
            ThreadPoolExecutor(max_workers=1) as exports,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="later-save") as saves,
        ):
            exported = exports.submit(Handoff(ws.state).create_zip, target, action)
            assert writing.wait(10)
            saved = saves.submit(
                projects.save,
                cid,
                AuthoredSave(**{k: later[k] for k in ("version", "base", "authored")}),
            )
            try:
                assert save_waiting.wait(10)
                assert not save_acquired.wait(0.1)
            finally:
                release.set()
            artifact = exported.result(timeout=10)
            saved.result(timeout=10)
        assert save_acquired.is_set()
        assert ws.state.package(target, artifact["artifact_id"])
        assert wording_export_issue(ws.state, cid)["code"] == "pending_rules"
        with pytest.raises(ValueError, match="saved prohibited word"):
            Handoff(ws.state).download(target, artifact["artifact_id"])


def test_prohibited_content_is_importable_editable_and_saveable_before_planning(tmp_path):
    from campaign_studio.sample import write_replay

    app = create_app(Settings(data_dir=tmp_path / "data"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        original = write_replay(app.state.workspace.storage).model_dump(mode="json")
        original["markets"]["es-MX"]["phrase_rules"] = [{"id": "word", "phrase": "siempre"}]
        result = client.post("/api/motif/import", json=original)
        assert result.status_code == 200, result.text
        campaign_id = result.json()["campaign_id"]
        url = f"/api/motif/campaigns/{campaign_id}"
        opened = client.get(url).json()
        assert opened["value"]["markets"]["es-MX"]["copy"]["message"] == "Siempre contigo."
        assert opened["value"]["markets"]["es-MX"]["phrase_rules"][0]["phrase"] == "siempre"
        assert opened["run"] is None
        saved = save(client, url, lambda a: a["brief"].update(name="Editable prohibited wording"))
        reopened = client.get(url).json()
        assert reopened["project"]["version"] == saved["version"]
        assert reopened["project"]["authored"]["brief"]["name"] == "Editable prohibited wording"
        for page in ("campaign", "production", "studio", "handoff"):
            assert client.get(f"/campaigns/{campaign_id}/{page}").status_code == 200
        for retired in ("review", "export"):
            assert client.get(f"/campaigns/{campaign_id}/{retired}").status_code == 404
        for removed in ("/api/sample", "/api/replay-sample"):
            assert client.post(removed, json={}).status_code == 404
        assert app.state.workspace.run_for(campaign_id) is None
