"""Current metadata/delivery services and retained legacy packages stay correctly scoped."""

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from urllib.parse import quote, unquote
from zipfile import ZipFile

import pytest
from campaign_fixtures import authored_edit, legacy_report, legacy_zip, zip_root
from test_web_workflow import page_data, sample_plan

from campaign_studio.planning import make_plan
from campaign_studio.review import RevisionTarget

pytest_plugins = ["test_web_workflow"]


@pytest.fixture
def produced(ui):
    client, workspace = ui
    data = sample_plan(client)
    data = page_data(client, client.get(f"/campaigns/{data['campaignId']}/studio"))
    return client, workspace, data, f"/api/campaigns/{data['campaignId']}/delivery"


def action(**values):
    return {"mode": "current", "expected_version": 0, **values}


def current(client, data):
    return client.get(f"/api/motif/campaigns/{data['campaignId']}").json()


def metadata_url(data, version=None, creative="product-1/en-US/1x1"):
    base = f"/api/campaigns/{data['campaignId']}/composition/{version or data['version']}"
    return base + f"/metadata/{creative}"


def describe(client, data, description, expected=0):
    model = current(client, data)
    result = client.post(
        metadata_url(data, model["version"]),
        json=action(description=description, expected_version=expected),
    )
    assert result.status_code == 200, result.text
    return result.json()


def change_brief(client, data, change):
    patch = authored_edit(client, data["campaignId"])
    change(patch["authored"]["brief"])
    response = client.post(f"/api/motif/campaigns/{data['campaignId']}/authored", json=patch)
    assert response.status_code == 200, response.text
    return current(client, data)


def registered_foreign_campaign(client):
    response = client.post(
        "/api/motif/campaigns",
        json={"name": "Separate campaign", "brand": "Another brand", "audience": "Audience"},
    )
    assert response.status_code == 200, response.text
    return response.json()["campaign_id"]


def make_package(client, base, *, partial=False):
    campaign_id = base.split("/")[3]
    model = client.get(f"/api/motif/campaigns/{campaign_id}").json()
    response = client.post(
        base + "/zip", json={"version": model["version"], "allow_partial": partial}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return response


def legacy_target(workspace, data):
    """Explicitly seed old output records solely to exercise upgrade read support."""
    from campaign_studio.rendering import render_revision

    plan = workspace.current_composition(data["campaignId"])["plan"]
    run = workspace.state.create(make_plan(plan.brief, workspace.storage), 0)
    result = render_revision(workspace.state, run, workspace.state.plan(run))
    return RevisionTarget(
        campaign_id=data["campaignId"],
        run_id=run,
        revision_id=result["revision_id"],
        mode="saved_version",
    )


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.images, self.scripts = [], {}, 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.links.extend(value for name, value in attrs if name in {"src", "href"})
        if tag == "img":
            self.images[values["src"]] = values["alt"]
        if tag == "script":
            self.scripts += 1


def test_metadata_http_is_protected_scoped_and_pixel_free(produced, monkeypatch):
    client, workspace, data, base = produced
    creative = "product-1/en-US/1x1"
    initial = data["metadata"]
    before = {str(p): p.read_bytes() for p in workspace.storage.root.rglob("*.png")}
    monkeypatch.setattr("campaign_studio.rendering.compose", lambda *a: pytest.fail("composed"))
    monkeypatch.setattr(
        "campaign_studio.provider.OpenAIImages.generate", lambda *a: pytest.fail("provider")
    )
    endpoint = metadata_url(data)
    for headers in ({"Origin": "https://other.example"}, {"X-Campaign-Token": ""}):
        assert (
            client.post(
                endpoint, json=action(description="Supplied description."), headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                base + "/zip", json={"version": data["version"]}, headers=headers
            ).status_code
            == 403
        )
    assert (
        client.post(
            endpoint, content=b"not JSON", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    invalid = client.post(
        endpoint, json=action(marker="approved", PRIVATE_INPUT_SENTINEL="private")
    )
    assert invalid.status_code == 422 and "PRIVATE_INPUT_SENTINEL" not in invalid.text
    assert any(item["code"] == "unsupported_field" for item in invalid.json()["field_errors"])
    foreign = registered_foreign_campaign(client)
    for identity in ("foreign", "00000000-0000-4000-8000-000000000000"):
        missing = client.get(f"/api/motif/campaigns/{identity}")
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    for wrong in (
        endpoint.replace(data["campaignId"], foreign),
        endpoint.replace(data["version"], "0" * 32),
    ):
        assert client.post(wrong, json=action(description="Supplied description.")).status_code in {
            404,
            409,
        }
    assert (
        client.post(
            metadata_url(data, creative="unknown/en-US/1x1"),
            json=action(description="Supplied description."),
        ).status_code
        == 404
    )
    response = client.post(endpoint, json=action(description="Un café, una bolsa, y luz cálida."))
    assert response.status_code == 200, response.text
    saved = response.json()["metadata"]
    assert saved["creatives"][creative]["version"] == 1
    assert saved["creatives"][creative]["description"] == "Un café, una bolsa, y luz cálida."
    assert saved["snapshot_id"] != initial["snapshot_id"]
    assert "_context" not in json.dumps(saved) and "private/" not in json.dumps(saved)
    assert current(client, data)["metadata"] == saved
    assert (
        client.post(endpoint, json=action(description="Keep this conflicting draft.")).status_code
        == 409
    )
    assert all(Path(path).read_bytes() == payload for path, payload in before.items())
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["composition", "source_plan"])
def test_metadata_save_checks_current_document_after_concurrent_work(produced, monkeypatch, change):
    import campaign_studio.compositions as compositions

    client, workspace, data, _ = produced
    entered, resume = Event(), Event()
    original = compositions.snapshot
    calls = 0

    def held(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            entered.set()
            assert resume.wait(10)
        return result

    monkeypatch.setattr(compositions, "snapshot", held)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(
            client.post, metadata_url(data), json=action(description="Supplied description.")
        )
        assert entered.wait(5)
        if change == "composition":
            change_brief(client, data, lambda value: value.update(name="New current name"))
        else:
            # Reviewing a source plan alone does not change the current editing document.
            workspace.state.create(
                make_plan(original(workspace, data["campaignId"])["plan"].brief, workspace.storage),
                0,
            )
        resume.set()
        response = future.result(10)
    assert response.status_code == (409 if change == "composition" else 200)
    description = current(client, data)["metadata"]["creatives"]["product-1/en-US/1x1"][
        "description"
    ]
    assert description == (None if change == "composition" else "Supplied description.")


def test_zip_download_alt_escaping_and_previous_download_preservation(produced, tmp_path):
    client, workspace, data, base = produced
    description = 'Café "naranja" & luz <script>unsafe</script> – detalle.'
    describe(client, data, description)
    assert client.post(base + "/zip", json={"version": data["version"]}).status_code == 409
    response = make_package(client, base)
    assert response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert "\n" not in response.headers["content-disposition"]
    hostile = client.post(
        base.replace(data["campaignId"], "bad%22%0d%0aX-Test%3Aevil") + "/zip",
        json={"version": data["version"]},
    )
    assert hostile.status_code == 404 and "x-test" not in hostile.headers
    original_zip = response.content
    with ZipFile(io.BytesIO(original_zip)) as archive:
        archive.extractall(tmp_path / "moved")
        root = zip_root(archive)
        manifest = json.loads(archive.read(f"{root}/manifest.json"))
        parser = Links()
        parser.feed(archive.read(f"{root}/index.html").decode())
        assert parser.scripts == 0
        assert (
            parser.images[
                next(o["file"] for o in manifest["outputs"] if o["id"] == "product-1/en-US/1x1")
            ]
            == description
        )
        assert parser.images[
            next(o["file"] for o in manifest["outputs"] if o["id"] == "product-1/en-US/9x16")
        ].startswith("Image description not supplied.")
        assert sum(name.endswith(".png") for name in archive.namelist()) == 12
        assert archive.testzip() is None
        assert all(
            item.date_time[:3]
            == datetime.fromisoformat(manifest["report_created_at"]).astimezone().timetuple()[:3]
            for item in archive.infolist()
        )
    assert all(
        f'id="{link[1:]}"' in (tmp_path / "moved" / root / "index.html").read_text()
        if link.startswith("#")
        else ":" not in link and (tmp_path / "moved" / root / link).is_file()
        for link in parser.links
    )
    describe(client, data, "A later description.", expected=1)
    assert make_package(client, base).content != original_zip
    assert (tmp_path / "moved" / root / "index.html").read_bytes()
    with workspace.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM package_exports").fetchone() == (0,)
        assert not db.execute("SELECT 1 FROM file_owners WHERE reference LIKE '%.zip'").fetchone()
    assert not list(workspace.storage.root.rglob("*.zip"))


def test_partial_zip_requires_explicit_intent_and_never_bypasses_prohibited_copy(produced):
    client, _, data, base = produced
    blocked = change_brief(
        client, data, lambda value: value["markets"]["en-US"]["copy"].update(message="TESTBLOCK")
    )
    assert (
        client.post(
            base + "/zip", json={"version": blocked["version"], "allow_partial": True}
        ).status_code
        == 409
    )

    def missing(value):
        value["markets"]["en-US"]["copy"]["message"] = "Corrected"
        value["products"][0]["source"] = {
            "role": "concept",
            "imagery_absent": True,
            "description": "Source not generated",
            "provenance": value["products"][0]["source"]["provenance"],
        }

    model = change_brief(client, data, missing)
    for endpoint in ("/zip", "/report-preview"):
        for intent in (False, "true"):
            assert client.post(
                base + endpoint, json={"version": model["version"], "allow_partial": intent}
            ).status_code in {409, 422}
    preview = client.post(
        base + "/report-preview", json={"version": model["version"], "allow_partial": True}
    )
    assert preview.status_code == 200
    with ZipFile(io.BytesIO(make_package(client, base, partial=True).content)) as archive:
        manifest = json.loads(archive.read(zip_root(archive) + "/manifest.json"))
        assert len(archive.namelist()) == 8 and not manifest["complete"]
        assert all(item.get("omission") for item in manifest["outputs"] if not item["file"])


def test_preview_is_read_only_matches_saved_copy_and_expires_after_metadata_changes(produced):
    client, workspace, data, base = produced
    body = {"version": data["version"]}
    endpoint = base + "/report-preview"
    for headers in ({"Origin": "https://other.example"}, {"X-Campaign-Token": ""}):
        assert client.post(endpoint, json=body, headers=headers).status_code == 403
    opened = client.post(endpoint, json=body)
    assert opened.status_code == 200, opened.text
    url = opened.json()["url"]
    assert url.startswith("/reports/current/")
    report = client.get(url)
    assert report.headers["cache-control"] == "no-store"
    assert "style-src 'unsafe-inline'" in report.headers["content-security-policy"]
    parser = Links()
    parser.feed(report.text)
    assert parser.scripts == 0 and len(parser.images) == 12
    manifest_url = url.removesuffix("index.html") + "manifest.json"
    manifest = client.get(manifest_url).json()
    with ZipFile(io.BytesIO(make_package(client, base).content)) as archive:
        delivered = json.loads(archive.read(zip_root(archive) + "/manifest.json"))
        assert [(o["id"], o["copy"], o["accessible_description"]) for o in manifest["outputs"]] == [
            (o["id"], o["copy"], o["accessible_description"]) for o in delivered["outputs"]
        ]
        assert all(
            client.get(url.replace("index.html", quote(item["file"], safe="/"))).status_code == 200
            for item in manifest["outputs"]
        )
    assert client.get(url.replace("index.html", "private/state.sqlite3")).status_code == 404
    describe(client, data, "Updated.")
    assert client.post(endpoint, json=body).status_code == 409
    for old in (url, manifest_url, url.replace("index.html", next(iter(parser.images)))):
        assert client.get(old).status_code == 409
    model = current(client, data)
    fresh = client.post(endpoint, json={"version": model["version"]}).json()["url"]
    assert fresh != url and "Updated." in client.get(fresh).text
    assert not list(workspace.storage.root.glob("Campaigns/*/Exports"))
    source = workspace.current_composition(data["campaignId"])["sources"]["product-1"]
    workspace.storage.path(source.reference).write_bytes(b"changed invalid source")
    assert client.get(fresh).status_code == 409
    assert client.post(base + "/zip", json={"version": model["version"]}).status_code == 409


def test_export_uses_one_captured_description_snapshot_during_concurrent_save(
    produced, monkeypatch
):
    import campaign_studio.delivery as delivery

    client, _, data, base = produced
    original = delivery.render_png
    changed = False

    def changing(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            describe(client, data, "Concurrent description.")
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "render_png", changing)
    response = make_package(client, base)
    with ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read(zip_root(archive) + "/manifest.json"))
        assert all(o["accessible_description"] is None for o in manifest["outputs"])
        assert manifest["composition_version"] == data["version"]
    assert (
        current(client, data)["metadata"]["creatives"]["product-1/en-US/1x1"]["description"]
        == "Concurrent description."
    )


def test_zip_closes_when_the_browser_disconnects():
    from starlette.requests import ClientDisconnect

    from campaign_studio.exporting import ZipDownload
    from campaign_studio.web import ZipAttachment

    stream = io.BytesIO(b"partial transfer")
    response = ZipAttachment(ZipDownload("demo.zip", stream, len(stream.getvalue())))

    async def send(event):
        if event["type"] == "http.response.body":
            raise OSError("browser disconnected")

    with pytest.raises(ClientDisconnect):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send))
    assert stream.closed


def test_legacy_report_registry_integrity_kind_isolation_and_deletion(produced):
    from campaign_studio.cleanup import Cleanup

    client, workspace, data, base = produced
    state, cid = workspace.state, data["campaignId"]
    target = legacy_target(workspace, data)
    folder, zipped = legacy_report(state, target), legacy_zip(state, target)
    report_id = folder["artifact_id"]
    assert state.report(report_id)["campaign_id"] == cid
    zip_bytes = client.get(zipped["url"]).content
    assert make_package(client, base).status_code == 200
    assert client.get(zipped["url"].replace(zipped["artifact_id"], report_id)).status_code == 409
    assert client.get(f"/reports/{zipped['artifact_id']}/index.html").status_code == 404
    foreign = registered_foreign_campaign(client)
    assert client.get(zipped["url"].replace(cid, foreign)).status_code == 409
    root = Path(folder["location"])
    original = {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    for reference in (
        "manifest.json",
        "index.html",
        next(p for p in original if p.endswith(".png")),
    ):
        (root / reference).write_bytes(b"changed")
        assert client.get(folder["url"].replace("index.html", reference)).status_code == 409
        (root / reference).write_bytes(original[reference])
    assert client.get(zipped["url"]).content == zip_bytes
    # Registered ZIP hash, missing-file, and unregistered-name checks remain for
    # existing installations even though the new application creates no archives.
    path = state.storage.path(zipped["reference"])
    path.write_bytes(b"private canary")
    damaged = client.get(zipped["url"])
    assert damaged.status_code == 409 and "private canary" not in damaged.text
    path.unlink()
    assert client.get(zipped["url"]).status_code == 409
    assert (
        client.get(zipped["url"].replace(zipped["artifact_id"], "unregistered")).status_code == 409
    )
    cleanup = Cleanup(state)
    assert (
        cleanup.delete_campaign(cid, cleanup.preview_campaign(cid)["token"])["state"] == "complete"
    )
    assert client.get(folder["url"]).status_code == 404 and state.report(report_id) is None
    assert client.get(f"/api/motif/campaigns/{foreign}").status_code == 200


def test_new_report_contract_preserves_registered_old_zip_bytes(produced, monkeypatch):
    import campaign_studio.exporting as exporting

    client, workspace, data, base = produced
    target = legacy_target(workspace, data)
    public_manifest = exporting.public_manifest

    def previous_format(*args, **kwargs):
        manifest = public_manifest(*args, **kwargs)
        manifest["report_format_version"] = 8
        return manifest

    with monkeypatch.context() as patch:
        patch.setattr(exporting, "public_manifest", previous_format)
        previous = legacy_zip(workspace.state, target)
    delivered = make_package(client, base)
    assert client.get(previous["url"]).content == previous["content"]
    with ZipFile(io.BytesIO(delivered.content)) as archive:
        assert (
            json.loads(archive.read(zip_root(archive) + "/manifest.json"))["report_format_version"]
            == 10
        )
    assert (
        make_package(client, base).headers["content-disposition"]
        == delivered.headers["content-disposition"]
    )


def test_readable_unicode_delivery_names_and_encoded_report_paths(produced):
    client, _, data, base = produced

    def rename(value):
        value["name"] = "Café / 秋 – launch"
        value["products"][0]["name"] = "Café chair"
        value["products"][1]["name"] = "CAFÉ CHAIR"

    model = change_brief(client, data, rename)
    preview = client.post(base + "/report-preview", json={"version": model["version"]}).json()
    response = make_package(client, base)
    filename = unquote(response.headers["content-disposition"].split("''", 1)[1])
    with ZipFile(io.BytesIO(response.content)) as archive:
        root = zip_root(archive)
        assert filename == root + ".zip"
        manifest = json.loads(archive.read(root + "/manifest.json"))
        assert len({o["file"].split("/")[1].casefold() for o in manifest["outputs"]}) == 2
        assert all(
            client.get(preview["url"].replace("index.html", quote(o["file"], safe="/"))).status_code
            == 200
            for o in manifest["outputs"]
        )
        assert (
            client.get(
                preview["url"].replace("index.html", "images/%2e%2e/%2e%2e/private/state.sqlite3")
            ).status_code
            == 404
        )


def test_brand_delivery_name_changes_without_mutating_an_old_download(produced):
    client, _, data, base = produced
    first = make_package(client, base)
    before = first.content
    change_brief(client, data, lambda value: value["brand"].update(name="Renamed brand"))
    second = make_package(client, base)
    with ZipFile(io.BytesIO(second.content)) as archive:
        name = zip_root(archive)
        assert name.startswith("Renamed brand-")
        assert unquote(second.headers["content-disposition"].split("''", 1)[1]) == name + ".zip"
    assert first.content == before
