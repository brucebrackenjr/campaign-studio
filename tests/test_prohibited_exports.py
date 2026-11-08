"""Current wording gates every delivery; autosave updates the one saved document."""

import copy
import io
import json
from contextlib import contextmanager
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from campaign_studio.app import create_app
from campaign_studio.brief import Brief, PhraseRule
from campaign_studio.campaign_drafts import write
from campaign_studio.config import Settings
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
        if generation_partial:
            value["products"][1]["source"] = {
                "role": "concept",
                "imagery_absent": True,
                "description": "A missing source that requires an explicit Production request.",
                "provenance": value["products"][1]["source"]["provenance"],
            }
        cid = value["campaign_id"]
        write(
            ws.storage,
            cid,
            {"authored": {"brief": value}, "version": uuid4().hex, "composition_schema": 1},
        )
        client.headers["X-Campaign-Token"] = app.state.csrf
        url = f"/api/motif/campaigns/{cid}"
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
    save(client, url, lambda a: a["brief"]["markets"]["en-US"].update(phrase_rules=rules))
    return client.get(url).json()


def export_urls(model):
    cid = model["value"]["campaign_id"]
    return {
        "zip": f"/api/campaigns/{cid}/delivery/zip",
        "preview": f"/api/campaigns/{cid}/delivery/report-preview",
        "body": {"version": model["version"], "allow_partial": True},
    }


def png_url(model, product="product-1"):
    cid = model["value"]["campaign_id"]
    return f"/api/campaigns/{cid}/composition/{model['version']}/download/{product}/en-US/1x1"


def test_all_campaign_export_paths_block_while_editable_preview_survives(tmp_path):
    with campaign(tmp_path) as (client, ws, url):
        before = client.get(url).json()
        paths = export_urls(before)
        downloaded = client.post(paths["zip"], json=paths["body"])
        preview = client.post(paths["preview"], json=paths["body"])
        assert downloaded.status_code == preview.status_code == 200
        downloaded_bytes = downloaded.content
        current = apply_rules(client, url, [{"id": "restricted", "phrase": "Restricted"}])
        assert current["wording_issue"]["code"] == "prohibited_phrase"
        assert len(current["wording_issue"]["matches"]) == 3
        for mode in (False, True):
            paths = export_urls(current)
            for kind in ("zip", "preview"):
                refused = client.post(paths[kind], json=paths["body"] | {"allow_partial": mode})
                assert refused.status_code == 409 and refused.json()["code"] == "prohibited_phrase"
        # Even an individually clean product cannot bypass the campaign wording gate.
        assert client.get(png_url(current, "product-2")).status_code == 409
        image = current["manifest"]["outputs"][0]["url"]
        assert client.get(image).status_code == 200
        assert client.get(preview.json()["url"]).status_code == 409
        assert client.get(png_url(before)).status_code == 409
        restored = apply_rules(client, url, [])
        assert restored["wording_issue"] is None
        paths = export_urls(restored)
        assert client.post(paths["zip"], json=paths["body"]).status_code == 200
        # A previously downloaded ZIP is already outside the application's storage.
        with ZipFile(io.BytesIO(downloaded_bytes)) as archive:
            assert archive.testzip() is None
        assert not list(ws.storage.root.rglob("Exports"))
        assert not list(ws.storage.root.rglob("Outputs"))
        with ws.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize("rules", [None, [], "omitted"])
def test_optional_rules_and_missing_sources_allow_only_explicit_partial(tmp_path, rules):
    with campaign(tmp_path, generation_partial=True) as (client, ws, url):

        def change(a):
            market = a["brief"]["markets"]["en-US"]
            if rules == "omitted":
                market.pop("phrase_rules", None)
            else:
                market["phrase_rules"] = rules

        save(client, url, change)
        current = client.get(url).json()
        paths = export_urls(current)
        denied = client.post(paths["zip"], json=paths["body"] | {"allow_partial": False})
        assert denied.status_code == 409 and denied.json()["code"] == "partial_export"
        result = client.post(paths["zip"], json=paths["body"])
        assert result.status_code == 200, result.text
        with ZipFile(io.BytesIO(result.content)) as archive:
            assert len([n for n in archive.namelist() if n.endswith(".png")]) == 3
        assert client.get(png_url(current)).status_code == 200
        assert client.get(png_url(current, "product-2")).status_code == 409


def test_invalid_saved_campaign_values_cannot_export_an_older_fallback(tmp_path):
    with campaign(tmp_path) as (client, _, url):
        save(client, url, lambda a: a["brief"]["brand"].update(text_color="raw invalid color"))
        current = client.get(url).json()
        paths = export_urls(current)
        for kind in ("zip", "preview"):
            assert client.post(paths[kind], json=paths["body"]).status_code in {409, 422}


def test_incomplete_existing_rule_blocks_export_until_corrected(tmp_path):
    with campaign(tmp_path) as (client, _, url):
        apply_rules(client, url, [{"id": "unused", "phrase": "ABSENT"}])
        apply_rules(client, url, [{"id": "unused", "phrase": ""}])
        paths = export_urls(client.get(url).json())
        for kind in ("zip", "preview"):
            assert client.post(paths[kind], json=paths["body"]).status_code in {409, 422}
        current = apply_rules(client, url, [{"id": "unused", "phrase": "ABSENT"}])
        paths = export_urls(current)
        assert client.post(paths["zip"], json=paths["body"]).status_code == 200


def test_unsubmitted_new_rule_modal_does_not_override_saved_rules(tmp_path):
    with campaign(tmp_path) as (client, _, url):
        save(
            client,
            url,
            lambda a: a.setdefault("form_drafts", {}).update(
                fields={"rule/en/-1": {"mw-rule-phrase": ""}}
            ),
        )
        current = client.get(url).json()
        assert current["wording_issue"] is None
        paths = export_urls(current)
        assert client.post(paths["zip"], json=paths["body"]).status_code == 200


def test_advisory_severity_is_not_an_accepted_rule_and_siempre_blocks(tmp_path):
    with pytest.raises(ValidationError):
        PhraseRule.model_validate({"id": "old", "phrase": "siempre", "severity": "warning"})
    with campaign(tmp_path) as (_, ws, _):
        _, brief = write_sample(ws.storage)
        value = brief.model_dump(mode="json")
        value["markets"]["es-MX"]["phrase_rules"] = [
            {"id": "prohibited-siempre", "phrase": "siempre"}
        ]
        plan = make_plan(Brief.model_validate(value), ws.storage)
        matches = [
            f for f in plan.findings if f.context and f.context.phrase and f.context.phrase.matches
        ]
        assert len(matches) == 6 and {f.severity for f in matches} == {"blocking"}


def test_all_withheld_wording_can_be_corrected_by_autosave_without_provider(tmp_path):
    with campaign(tmp_path, products=1) as (client, ws, url):
        blocked = apply_rules(client, url, [{"id": "restricted", "phrase": "Restricted"}])
        cid = blocked["value"]["campaign_id"]
        edit = {
            "revision_id": blocked["version"],
            "product_id": "product-1",
            "market": "en-US",
            "ratio": "1:1",
            "values": {"message": "Allowed wording."},
            "field_scopes": {"message": "product"},
        }
        saved = client.post(f"/api/campaigns/{cid}/text/save", json=edit)
        assert saved.status_code == 200, saved.text
        current = client.get(url).json()
        assert current["wording_issue"] is None
        assert {o["copy"]["message"] for o in current["manifest"]["outputs"]} == {
            "Allowed wording."
        }
        paths = export_urls(current)
        assert client.post(paths["zip"], json=paths["body"]).status_code == 200
        assert not list(ws.storage.root.rglob("Outputs"))


def test_unchanged_saved_rule_form_is_not_pending_and_stale_save_does_not_replace_newer_rules(
    tmp_path,
):
    with campaign(tmp_path) as (client, _, url):
        apply_rules(client, url, [{"id": "unused", "phrase": "ABSENT"}])
        save(
            client,
            url,
            lambda a: a.setdefault("form_drafts", {}).update(
                fields={"rule/en/0": {"mw-rule-phrase": "ABSENT"}}
            ),
        )
        assert client.get(url).json()["wording_issue"] is None
        stale = copy.deepcopy(client.get(url + "/authored").json())
        apply_rules(client, url, [{"id": "newer", "phrase": "NEWER RULE"}])
        response = client.post(
            url + "/authored", json={k: stale[k] for k in ("version", "base", "authored")}
        )
        assert response.status_code == 409
        current = client.get(url + "/authored").json()
        assert (
            current["authored"]["brief"]["markets"]["en-US"]["phrase_rules"][0]["phrase"]
            == "NEWER RULE"
        )


def test_rules_saved_during_export_do_not_mix_with_captured_package(tmp_path, monkeypatch):
    from campaign_studio import delivery

    with campaign(tmp_path) as (client, ws, url):
        old = client.get(url).json()
        paths = export_urls(old)
        original = delivery.render_png
        changed = False

        def update_during_render(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                apply_rules(client, url, [{"id": "later", "phrase": "Restricted"}])
            return original(*args, **kwargs)

        monkeypatch.setattr(delivery, "render_png", update_during_render)
        package = client.post(paths["zip"], json=paths["body"])
        assert package.status_code == 200, package.text
        with ZipFile(io.BytesIO(package.content)) as archive:
            name = next(n for n in archive.namelist() if n.endswith("manifest.json"))
            assert json.loads(archive.read(name))["composition_version"] == old["version"]
        current = client.get(url).json()
        paths = export_urls(current)
        assert client.post(paths["zip"], json=paths["body"]).status_code == 409


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
        saved = save(client, url, lambda a: a["brief"].update(name="Editable wording"))
        reopened = client.get(url).json()
        assert reopened["project"]["version"] == saved["version"]
        assert reopened["project"]["authored"]["brief"]["name"] == "Editable wording"
        for page in ("campaign", "production", "studio", "handoff"):
            assert client.get(f"/campaigns/{campaign_id}/{page}").status_code == 200
        for retired in ("review", "export"):
            assert client.get(f"/campaigns/{campaign_id}/{retired}").status_code == 404
        for removed in ("/api/sample", "/api/replay-sample"):
            assert client.post(removed, json={}).status_code == 404
        assert app.state.workspace.run_for(campaign_id) is None
