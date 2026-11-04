import asyncio
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from campaign_fixtures import zip_root
from test_web_workflow import page_data, sample_plan, start, wait_for_work
from test_workflow import edit_copy, revise

from campaign_studio.brief import Brief
from campaign_studio.planning import make_plan
from campaign_studio.review import RevisionTarget
from campaign_studio.runner import Runner
from campaign_studio.state import StateError

pytest_plugins = ["test_web_workflow"]


@pytest.fixture
def produced(ui):
    client, workspace = ui
    data = sample_plan(client)
    start(client, data)
    wait_for_work(client, "80d58cbe-5e57-5544-9b07-a4f018714dde")
    data = page_data(client, client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio"))
    base = (
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde"
        f"/runs/{data['runId']}/revisions/{data['currentRevision']}"
    )
    return client, workspace, data, base


def action(**values):
    return {"mode": "current", "expected_version": 0, **values}


def registered_foreign_campaign(client):
    response = client.post(
        "/api/motif/campaigns",
        json={"name": "Separate campaign", "brand": "Another brand", "audience": "Audience"},
    )
    assert response.status_code == 200, response.text
    campaign_id = response.json()["campaign_id"]
    assert client.get(f"/api/motif/campaigns/{campaign_id}").status_code == 200
    return campaign_id


def test_metadata_http_is_protected_scoped_and_pixel_free(produced, monkeypatch):
    client, workspace, data, base = produced
    state = workspace.state
    creative = "product-1/en-US/1x1"
    initial = client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["metadata"]
    original_revision = state.revision(data["currentRevision"])
    original_manifest = state.storage.path(original_revision["manifest_ref"]).read_bytes()
    root = original_revision["manifest_ref"].rsplit("/", 1)[0]
    hashes = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in state.storage.path(root).rglob("*.png")
    }
    monkeypatch.setattr("campaign_studio.rendering.compose", lambda *a: pytest.fail("composed"))
    monkeypatch.setattr(
        "campaign_studio.provider.OpenAIImages.generate", lambda *a: pytest.fail("provider")
    )
    endpoint = base + "/metadata/" + creative
    for headers in ({"Origin": "https://other.example"}, {"X-Campaign-Token": ""}):
        assert (
            client.post(
                endpoint, json=action(description="Supplied description."), headers=headers
            ).status_code
            == 403
        )
        assert (
            client.post(
                base + "/zip", json={"snapshot_id": initial["snapshot_id"]}, headers=headers
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
    assert "marker" not in invalid.text  # Retired and unknown names use generic safe context.
    foreign = registered_foreign_campaign(client)
    for identity in ("foreign", "00000000-0000-4000-8000-000000000000"):
        missing = client.get(f"/api/motif/campaigns/{identity}")
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    for wrong in (
        base.replace(data["campaignId"], foreign),
        base.replace(data["runId"], "0" * 32),
        base.replace(data["currentRevision"], "0" * 32),
    ):
        assert (
            client.post(
                wrong + "/metadata/" + creative, json=action(description="Supplied description.")
            ).status_code
            == 409
        )
        assert (
            client.post(wrong + "/zip", json={"snapshot_id": initial["snapshot_id"]}).status_code
            == 409
        )
    assert (
        client.post(
            base + "/metadata/unknown/en-US/1x1", json=action(description="Supplied description.")
        ).status_code
        == 409
    )
    response = client.post(endpoint, json=action(description="Un café, una bolsa, y luz cálida."))
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["creatives"][creative]["version"] == 1
    assert saved["creatives"][creative]["description"] == "Un café, una bolsa, y luz cálida."
    assert saved["snapshot_id"] != initial["snapshot_id"]
    assert "_context" not in response.text and "private/" not in response.text
    assert client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["metadata"] == saved
    pending = action(description="Keep this conflicting draft.")
    assert client.post(endpoint, json=pending).status_code == 409
    assert pending["description"] == "Keep this conflicting draft."
    assert client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["metadata"] == saved
    assert state.storage.path(original_revision["manifest_ref"]).read_bytes() == original_manifest
    assert all(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        for path, digest in hashes.items()
    )
    assert state.revision_count(data["runId"]) == 1 and state.attempts(data["runId"]) == []


@pytest.mark.parametrize("change", ["revision", "run"])
def test_current_target_race_rejects_and_only_available_saved_target_can_be_described(
    produced, monkeypatch, change
):
    client, workspace, data, base = produced
    state = workspace.state
    entered, resume = Event(), Event()
    original = state.update_creative

    def held(*args, **kwargs):
        entered.set()
        assert resume.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "update_creative", held)
    endpoint = base + "/metadata/product-1/en-US/1x1"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            client.post, endpoint, json=action(description="Supplied description.")
        )
        assert entered.wait(5)
        if change == "revision":
            revise(
                state,
                Runner(state),
                data["runId"],
                edit_copy(state, data["runId"], "Para cada día."),
            )
        else:
            new_run = state.create(make_plan(state.plan(data["runId"]).brief, state.storage), 0)
            # An unstarted plan leaves the accepted image target current.
            assert state.run(new_run)["current_revision"] is None
        resume.set()
        response = future.result(timeout=10)
    assert response.status_code == (409 if change == "revision" else 200)
    if change == "revision":
        assert "no longer available" in response.text
    target = RevisionTarget(
        campaign_id=data["campaignId"],
        run_id=data["runId"],
        revision_id=data["currentRevision"],
        mode="current",
    )
    if change == "revision":
        with pytest.raises(StateError):
            state.metadata_snapshot(target.revision_id, target=target)
    else:
        assert state.metadata_snapshot(target.revision_id, target=target)
    saved_target = target.model_copy(update={"mode": "saved_version"})
    historical = state.metadata_snapshot(target.revision_id, target=saved_target)
    assert historical["creatives"]["product-1/en-US/1x1"]["description"] == (
        None if change == "revision" else "Supplied description."
    )
    pointers = [
        (run["id"], run["current_revision"])
        for run in workspace.runs("80d58cbe-5e57-5544-9b07-a4f018714dde")
    ]
    response = client.post(
        endpoint,
        json=action(
            mode="saved_version",
            description="Changed description.",
            expected_version=0 if change == "revision" else 1,
        ),
    )
    if change == "revision":
        assert response.status_code == 409 and "no longer available" in response.text
        assert state.metadata_snapshot(target.revision_id, target=saved_target) == historical
    else:
        # A new plan alone leaves the actual earlier images available as the
        # fallback. Their exact metadata target remains valid for descriptions.
        assert response.status_code == 200
        assert (
            response.json()["creatives"]["product-1/en-US/1x1"]["description"]
            == "Changed description."
        )
    assert pointers == [
        (run["id"], run["current_revision"])
        for run in workspace.runs("80d58cbe-5e57-5544-9b07-a4f018714dde")
    ]


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


def make_package(client, base, *, partial=False):
    campaign_id = base.split("/")[3]
    snapshot = client.get(f"/api/motif/campaigns/{campaign_id}").json()["metadata"]
    response = client.post(
        base + "/zip", json={"snapshot_id": snapshot["snapshot_id"], "allow_partial": partial}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_zip_download_alt_escaping_and_old_artifact_preservation(produced, tmp_path, monkeypatch):
    client, workspace, data, base = produced
    description = 'Café "naranja" & luz <script>unsafe</script> – detalle.'
    snapshot = client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["metadata"]
    response = client.post(
        base + "/metadata/product-1/en-US/1x1", json=action(description=description)
    )
    assert response.status_code == 200
    assert (
        client.post(base + "/zip", json={"snapshot_id": snapshot["snapshot_id"]}).status_code == 409
    )
    monkeypatch.setattr("campaign_studio.rendering.compose", lambda *a: pytest.fail("composed"))
    package = make_package(client, base)
    response = client.get(package["url"])
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith("attachment; filename*=UTF-8''")
    assert "\n" not in response.headers["content-disposition"]
    hostile = client.get(
        package["url"].replace("80d58cbe-5e57-5544-9b07-a4f018714dde", "bad%22%0d%0aX-Test%3Aevil")
    )
    assert hostile.status_code == 404 and hostile.json()["code"] == "not_found"
    assert "x-test" not in hostile.headers
    original_zip = response.content
    with ZipFile(io.BytesIO(original_zip)) as archive:
        archive.extractall(tmp_path / "moved")
        root = zip_root(archive)
        manifest = json.loads(archive.read(f"{root}/manifest.json"))
        parser = Links()
        parser.feed(archive.read(f"{zip_root(archive)}/index.html").decode())
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
        assert len([name for name in archive.namelist() if name.endswith(".png")]) == 12
        assert manifest["producer_metadata_snapshot"]["snapshot_id"] == package["snapshot_id"]
    assert all(
        f'id="{link[1:]}"' in (tmp_path / "moved" / root / "index.html").read_text()
        if link.startswith("#")
        else ":" not in link and (tmp_path / "moved" / root / link).is_file()
        for link in parser.links
    )
    assert (
        client.post(
            base + "/metadata/product-1/en-US/1x1",
            json=action(expected_version=1, description="A later description."),
        ).status_code
        == 200
    )
    assert client.get(package["url"]).content == original_zip
    assert make_package(client, base)["artifact_id"] != package["artifact_id"]
    assert workspace.state.attempts(data["runId"]) == []
    # The direct folder export uses the same current revision admission.
    folder = client.post(
        "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export",
        json={"run_id": data["runId"], "revision_id": data["currentRevision"]},
    )
    assert folder.status_code == 200
    assert {"location", "url", "included", "omitted"} <= folder.json().keys()


def test_partial_zip_intent_ownership_and_unavailable_artifacts(produced, tmp_path, monkeypatch):
    client, workspace, data, base = produced
    state = workspace.state
    value = state.plan(data["runId"]).brief.model_dump()
    value["markets"]["en-US"]["copy"]["message"] = "TESTBLOCK"
    diagnostic = revise(state, Runner(state), data["runId"], Brief.model_validate(value))
    diagnostic_base = base.replace(data["currentRevision"], diagnostic["revision_id"])
    snapshot = state.metadata_snapshot(diagnostic["revision_id"])
    assert (
        client.post(
            diagnostic_base + "/zip",
            json={"snapshot_id": snapshot["snapshot_id"], "allow_partial": True},
        ).status_code
        == 409
    )
    # Test partial-package ownership on an actual current first partial result,
    # not the nonaccepted diagnostic from a failed correction above.
    partial_run = state.create(make_plan(state.plan(data["runId"]).brief, state.storage), 0)
    state.confirm(partial_run, state.plan(partial_run).digest)
    original_write = state.storage.write

    def fail_english(reference, payload):
        if "/images/" in reference and reference.endswith("/en-US.png"):
            raise OSError("Controlled initial partial output")
        return original_write(reference, payload)

    with monkeypatch.context() as patch:
        patch.setattr(state.storage, "write", fail_english)
        partial = asyncio.run(Runner(state).execute(partial_run))
    partial_base = base.replace(data["runId"], partial_run).replace(
        data["currentRevision"], partial["revision_id"]
    )
    snapshot = client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["metadata"]
    for intent in (False, "true"):
        assert client.post(
            partial_base + "/zip",
            json={"snapshot_id": snapshot["snapshot_id"], "allow_partial": intent},
        ).status_code in {409, 422}
    package = make_package(client, partial_base, partial=True)
    assert package["included"] == 6 and package["omitted"] == 6 and not package["complete"]
    payload = client.get(package["url"]).content
    with ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(tmp_path / "partial-moved")
        root = zip_root(archive)
        manifest = json.loads(archive.read(f"{root}/manifest.json"))
        assert len(archive.namelist()) == 8 and not manifest["complete"]
        assert all(item.get("omission") for item in manifest["outputs"] if not item["file"])
        parser = Links()
        parser.feed(archive.read(f"{zip_root(archive)}/index.html").decode())
        assert all(
            f'id="{link[1:]}"' in (tmp_path / "partial-moved" / root / "index.html").read_text()
            if link.startswith("#")
            else ":" not in link and (tmp_path / "partial-moved" / root / link).is_file()
            for link in parser.links
        )
    foreign = registered_foreign_campaign(client)
    for identity in ("foreign", "00000000-0000-4000-8000-000000000000"):
        missing = client.get(package["url"].replace(data["campaignId"], identity))
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    for wrong in (
        package["url"].replace(data["campaignId"], foreign),
        package["url"].replace(partial_run, "0" * 32),
        package["url"].replace(partial["revision_id"], data["currentRevision"]),
        package["url"].replace(package["artifact_id"], "private"),
    ):
        assert client.get(wrong).status_code == 409
    target = RevisionTarget(
        campaign_id="80d58cbe-5e57-5544-9b07-a4f018714dde",
        run_id=partial_run,
        revision_id=partial["revision_id"],
        mode="saved_version",
    )
    record = state.package(target, package["artifact_id"])
    state.storage.path(record["reference"]).write_bytes(b"private synthetic canary")
    damaged = client.get(package["url"])
    assert damaged.status_code == 409 and "missing or changed" in damaged.text
    assert "private synthetic canary" not in damaged.text
    state.storage.path(record["reference"]).unlink()
    assert client.get(package["url"]).status_code == 409
    # An otherwise valid but unregistered artifact is never served by its filename.
    state.storage.write(
        workspace.storage.project("80d58cbe-5e57-5544-9b07-a4f018714dde").ref(
            "Exports/archives/unregistered.zip"
        ),
        payload,
    )
    assert (
        client.get(package["url"].replace(package["artifact_id"], "unregistered")).status_code
        == 409
    )
    assert state.run(data["runId"])["current_revision"] == data["currentRevision"]


def test_folder_export_binds_description_snapshot_and_preserves_prior_packages(
    produced, monkeypatch
):
    from campaign_studio.handoff import folder_matches
    from campaign_studio.review import CreativeUpdate

    client, workspace, data, base = produced
    state = workspace.state
    revision = data["currentRevision"]
    endpoint = "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/export"
    initial = client.get(f"/api/motif/campaigns/{data['campaignId']}").json()["metadata"]
    body = {"run_id": data["runId"], "revision_id": revision, "snapshot_id": initial["snapshot_id"]}
    first = client.post(endpoint, json=body)
    assert first.status_code == 200, first.text
    location = Path(first.json()["location"])
    report_url = first.json()["url"]
    report_id = report_url.split("/")[2]
    assert report_url == f"/reports/{UUID(report_id)}/index.html"
    assert revision not in report_url and data["campaignId"] not in report_url
    assert client.post(endpoint, json=body).json()["url"] == report_url
    report = client.get(report_url)
    assert report.status_code == 200 and report.content == (location / "index.html").read_bytes()
    assert report.headers["cache-control"] == "no-store"
    assert "style-src 'unsafe-inline'" in report.headers["content-security-policy"]
    parser = Links()
    parser.feed(report.text)
    assert parser.scripts == 0
    for reference in parser.images:
        image = client.get(report_url.replace("index.html", reference))
        assert image.status_code == 200 and image.content == (location / reference).read_bytes()

    # A browser requests its report images concurrently, under the same app loop.
    async def browser_images():
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=client.app), base_url="http://127.0.0.1"
        ) as browser:
            return await asyncio.gather(
                *(browser.get(report_url.replace("index.html", name)) for name in parser.images)
            )

    assert all(image.status_code == 200 for image in asyncio.run(browser_images()))
    assert str(workspace.storage.root) not in report.text
    for identity in ("not-a-uuid", uuid4().hex, str(uuid4())):
        missing = client.get(f"/reports/{identity}/index.html")
        assert missing.status_code == 404 and "Report not found" in missing.text
        assert missing.headers["content-type"].startswith("text/html")
    for reference in ("unknown.png", "private/state.sqlite3", "images/%2e%2e/manifest.json"):
        assert client.get(report_url.replace("index.html", reference)).status_code == 404
    retired = f"/packages/{data['campaignId']}/{location.name}/index.html"
    assert client.get(retired).status_code == 404
    old_manifest = (location / "manifest.json").read_bytes()
    creative = "product-1/en-US/1x1"
    changed = client.post(
        base + "/metadata/" + creative,
        json=action(description="A supplied geometric fixture."),
    ).json()
    stale = client.post(endpoint, json=body)
    assert stale.status_code == 409
    assert "changed in another tab" in stale.json()["message"]
    assert (location / "manifest.json").read_bytes() == old_manifest

    body["snapshot_id"] = changed["snapshot_id"]
    fresh = client.post(endpoint, json=body)
    assert fresh.status_code == 200, fresh.text
    current = json.loads((Path(fresh.json()["location"]) / "manifest.json").read_bytes())
    assert current["producer_metadata_snapshot"]["snapshot_id"] == changed["snapshot_id"]
    assert fresh.json()["location"] != first.json()["location"]
    assert fresh.json()["url"] != report_url
    assert client.get(report_url).content == report.content
    assert (location / "manifest.json").read_bytes() == old_manifest

    assert str(workspace.storage.root) not in json.dumps(current)

    # Notes can change after checking for an existing folder, before a new payload is read.
    # The second snapshot check must reject this race without publishing a mixed package.
    def change_after_reuse_check(*args, **kwargs):
        result = folder_matches(*args, **kwargs)
        state.update_creative(
            revision, creative, CreativeUpdate(expected_version=1, description="Newer notes")
        )
        return result

    before = sorted(
        workspace.storage.path(
            workspace.storage.project("80d58cbe-5e57-5544-9b07-a4f018714dde").ref("Exports")
        ).iterdir()
    )
    monkeypatch.setattr("campaign_studio.handoff.folder_matches", change_after_reuse_check)
    raced = client.post(endpoint, json=body)
    assert raced.status_code == 409 and "changed in another tab" in raced.json()["message"]
    assert (
        sorted(
            workspace.storage.path(
                workspace.storage.project("80d58cbe-5e57-5544-9b07-a4f018714dde").ref("Exports")
            ).iterdir()
        )
        == before
    )
    assert (location / "manifest.json").read_bytes() == old_manifest


def test_report_registry_integrity_kind_isolation_and_deletion(produced):
    from campaign_studio.cleanup import Cleanup

    client, workspace, data, base = produced
    state = workspace.state
    cid = data["campaignId"]
    endpoint = f"/api/campaigns/{cid}/export"
    body = {"run_id": data["runId"], "revision_id": data["currentRevision"]}
    folder = client.post(endpoint, json=body).json()
    report_id = folder["url"].split("/")[2]
    record = state.report(report_id)
    assert record["campaign_id"] == cid and record["run_id"] == data["runId"]
    assert record["revision_id"] == data["currentRevision"]
    zipped = make_package(client, base)
    zip_bytes = client.get(zipped["url"]).content
    assert make_package(client, base)["artifact_id"] == zipped["artifact_id"]
    assert client.get(zipped["url"].replace(zipped["artifact_id"], report_id)).status_code == 409
    assert client.get(f"/reports/{zipped['artifact_id']}/index.html").status_code == 404
    foreign = registered_foreign_campaign(client)
    assert client.get(zipped["url"].replace(cid, foreign)).status_code == 409
    target = RevisionTarget(
        campaign_id=foreign,
        run_id=data["runId"],
        revision_id=data["currentRevision"],
        mode="saved_version",
    )
    with pytest.raises(StateError):
        state.register_package(target, record["reference"], record["sha256"], record["snapshot_id"])
    # A valid revision still cannot register a file owned by another project.
    from campaign_studio.storage import StorageError

    valid_target = target.model_copy(update={"campaign_id": cid})
    with pytest.raises(StorageError, match="belonging to this campaign"):
        state.register_package(
            valid_target,
            state.storage.project(foreign).draft,
            record["sha256"],
            record["snapshot_id"],
        )
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
        response = client.get(folder["url"].replace("index.html", reference))
        assert response.status_code == 409 and "changed" in response.json()["message"]
        (root / reference).write_bytes(original[reference])
    assert client.get(folder["url"]).content == original["index.html"]
    assert client.get(zipped["url"]).content == zip_bytes
    cleanup = Cleanup(state)
    assert (
        cleanup.delete_campaign(cid, cleanup.preview_campaign(cid)["token"])["state"] == "complete"
    )
    assert client.get(folder["url"]).status_code == 404
    assert state.report(report_id) is None
    assert client.get(f"/api/motif/campaigns/{foreign}").status_code == 200


def test_new_report_contract_creates_new_packages_and_preserves_registered_bytes(
    produced, monkeypatch
):
    import campaign_studio.exporting as exporting

    client, workspace, data, base = produced
    public_manifest = exporting.public_manifest

    def previous_format(*args, **kwargs):
        manifest = public_manifest(*args, **kwargs)
        manifest["report_format_version"] = 8
        return manifest

    monkeypatch.setattr(exporting, "public_manifest", previous_format)
    previous = make_package(client, base)
    original = client.get(previous["url"]).content
    monkeypatch.setattr(exporting, "public_manifest", public_manifest)
    current = make_package(client, base)
    assert current["artifact_id"] != previous["artifact_id"]
    assert client.get(previous["url"]).content == original
    with ZipFile(io.BytesIO(client.get(current["url"]).content)) as archive:
        assert (
            json.loads(archive.read(f"{zip_root(archive)}/manifest.json"))["report_format_version"]
            == 9
        )
        assert b"geometric test fixtures for engineering verification" not in archive.read(
            f"{zip_root(archive)}/index.html"
        )
    assert make_package(client, base)["artifact_id"] == current["artifact_id"]
    assert workspace.state.attempts(data["runId"]) == []


def test_readable_unicode_delivery_names_and_encoded_report_paths(produced, tmp_path):
    from urllib.parse import quote, unquote

    client, workspace, data, _ = produced
    state = workspace.state
    value = state.plan(data["runId"]).brief.model_dump()
    value["name"] = "Café / 秋 – launch"
    value["products"][0]["name"] = "Café chair"
    value["products"][1]["name"] = "CAFÉ CHAIR"
    current = revise(state, Runner(state), data["runId"], Brief.model_validate(value))
    model = client.get(f"/api/motif/campaigns/{data['campaignId']}").json()
    body = {
        "run_id": data["runId"],
        "revision_id": current["revision_id"],
        "snapshot_id": model["metadata"]["snapshot_id"],
    }
    folder = client.post(f"/api/campaigns/{data['campaignId']}/export", json=body)
    assert folder.status_code == 200, folder.text
    folder = folder.json()
    base = (
        f"/api/campaigns/{data['campaignId']}/runs/{data['runId']}"
        f"/revisions/{current['revision_id']}"
    )
    package = client.post(base + "/zip", json={"snapshot_id": body["snapshot_id"]}).json()
    response = client.get(package["url"])
    assert response.status_code == 200
    assert unquote(response.headers["content-disposition"].split("''", 1)[1]) == package["filename"]
    with ZipFile(io.BytesIO(response.content)) as archive:
        root = zip_root(archive)
        assert package["filename"] == root + ".zip"
        assert Path(folder["location"]).name == root
        manifest = json.loads(archive.read(root + "/manifest.json"))
        assert len({o["file"].split("/")[1].casefold() for o in manifest["outputs"]}) == 2
        for item in manifest["outputs"]:
            encoded = quote(item["file"], safe="/")
            report_image = client.get(folder["url"].replace("index.html", encoded))
            assert report_image.status_code == 200
            assert report_image.content == archive.read(root + "/" + item["file"])
            assert (Path(folder["location"]) / item["file"]).read_bytes() == report_image.content
            assert hashlib.sha256(report_image.content).hexdigest() == item["sha256"]
        assert (
            client.get(
                folder["url"].replace("index.html", "images/%2e%2e/%2e%2e/private/state.sqlite3")
            ).status_code
            == 404
        )


def test_brand_package_name_replaces_old_title_root_without_mutating_old_zip(produced, monkeypatch):
    import campaign_studio.handoff as handoff

    client, workspace, data, base = produced
    original_payloads = handoff.package_payloads

    def old_title_root(*args, **kwargs):
        payloads = original_payloads(*args, **kwargs)
        manifest = json.loads(payloads["manifest.json"])
        manifest["package_name"] = "Previous campaign slogan - " + manifest["package_name"][-8:]
        payloads["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
        return payloads

    monkeypatch.setattr(handoff, "package_payloads", old_title_root)
    previous = make_package(client, base)
    original = client.get(previous["url"]).content
    monkeypatch.setattr(handoff, "package_payloads", original_payloads)
    current = make_package(client, base)
    assert current["artifact_id"] != previous["artifact_id"]
    assert current["filename"][-12:] == previous["filename"][-12:]
    assert client.get(previous["url"]).content == original
    with ZipFile(io.BytesIO(client.get(current["url"]).content)) as archive:
        name = zip_root(archive)
        brand = workspace.state.plan(data["runId"]).brief.brand.name
        assert name.startswith(brand + "-") and current["filename"] == name + ".zip"
    assert make_package(client, base)["artifact_id"] == current["artifact_id"]
    assert workspace.state.attempts(data["runId"]) == []
