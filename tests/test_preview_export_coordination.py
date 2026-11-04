"""A retired editor's local preview must not make a first export fail as busy."""

import asyncio
import hashlib
import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

import httpx
import pytest
from campaign_fixtures import authored_edit

import campaign_studio.previews as previews
import campaign_studio.runner as runner_module
from campaign_studio.brief import Brief
from campaign_studio.handoff import Handoff
from campaign_studio.intake import Intake
from campaign_studio.planning import make_local_plan, make_plan
from campaign_studio.review import CreativeUpdate, RevisionTarget
from campaign_studio.sample import write_sample

pytest_plugins = ["test_web_workflow"]
EXPORT_KINDS = ("zip", "folder", "download", "png", "report-html", "report-image")


class ObservedMutex:
    """Keep real exclusion, while exposing when an HTTP worker has to wait."""

    def __init__(self):
        self.lock = Lock()
        self.waiting = Event()

    def __enter__(self):
        if self.lock.locked():
            self.waiting.set()
        self.lock.acquire()
        return self

    def __exit__(self, *_):
        self.lock.release()


@pytest.fixture
def package(ui):
    client, ws = ui
    _, brief = write_sample(ws.storage, 2, True)
    run = ws.state.create(make_plan(brief, ws.storage), 0)
    ws.state.confirm(run, ws.state.plan(run).digest)
    manifest = asyncio.run(client.app.state.runner.execute(run))
    revision = manifest["revision_id"]
    target = RevisionTarget(
        campaign_id=brief.campaign_id, run_id=run, revision_id=revision, mode="current"
    )
    action = {"snapshot_id": Handoff(ws.state).metadata(target)["snapshot_id"]}
    base = f"/api/campaigns/{brief.campaign_id}"
    zip_url = f"{base}/runs/{run}/revisions/{revision}/zip"
    first = client.post(zip_url, json=action)
    assert first.status_code == 200, first.text
    download = client.get(first.json()["url"])
    assert download.status_code == 200
    png_url = f"/campaigns/{brief.campaign_id}/images/{revision}/{manifest['outputs'][0]['id']}"
    png = client.get(png_url)
    assert png.status_code == 200
    folder = client.post(base + "/export", json=action | {"run_id": run, "revision_id": revision})
    assert folder.status_code == 200, folder.text
    folder_path = Path(folder.json()["location"])
    exported = json.loads((folder_path / "manifest.json").read_text())
    report_png = next(item["file"] for item in exported["outputs"] if item["file"])
    report_urls = {
        "report-html": folder.json()["url"],
        "report-image": folder.json()["url"].removesuffix("index.html") + report_png,
    }
    report_bytes = {kind: client.get(url).content for kind, url in report_urls.items()}
    mutex = ObservedMutex()
    client.app.state.previews.lock = mutex
    return SimpleNamespace(
        client=client,
        ws=ws,
        brief=brief,
        run=run,
        revision=revision,
        target=target,
        manifest=manifest,
        action=action,
        base=base,
        zip_url=zip_url,
        download_url=first.json()["url"],
        zip_bytes=download.content,
        png_url=png_url,
        png_bytes=png.content,
        report_urls=report_urls,
        report_bytes=report_bytes,
        folder_path=folder_path,
        report_png=report_png,
        mutex=mutex,
        edit=dict(
            run_id=run,
            revision_id=revision,
            product_id=brief.products[0].id,
            market="en-US",
            ratio="1:1",
        ),
    )


def request_package(p, kind):
    if kind == "png":
        return p.client.get(p.png_url + "?download=true")
    if kind in p.report_urls:
        return p.client.get(p.report_urls[kind])
    if kind == "download":
        return p.client.get(p.download_url)
    if kind == "zip":
        return p.client.post(p.zip_url, json=p.action)
    return p.client.post(
        p.base + "/export", json=p.action | {"run_id": p.run, "revision_id": p.revision}
    )


def assert_package_parity(p, response, kind):
    assert response.status_code == 200, response.text
    if kind == "png":
        assert response.content == p.png_bytes
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"].startswith("attachment;")
    elif kind in p.report_urls:
        assert response.content == p.report_bytes[kind]
    elif kind == "download":
        assert response.content == p.zip_bytes
    elif kind == "zip":
        assert p.client.get(response.json()["url"]).content == p.zip_bytes
    else:
        root = Path(response.json()["location"])
        assert (root / "index.html").is_file()
        with ZipFile(io.BytesIO(p.zip_bytes)) as archive:
            prefix = archive.namelist()[0].split("/", 1)[0] + "/"
            images = [name for name in archive.namelist() if name.endswith(".png")]
            assert len(images) == 6
            for name in images:
                assert (root / name.removeprefix(prefix)).read_bytes() == archive.read(name)
    assert p.ws.state.run(p.run)["current_revision"] == p.revision
    assert p.ws.state.attempts(p.run) == []


@contextmanager
def held_composition(*, fails=False):
    entered, release = Event(), Event()
    compose = previews.compose

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10), "Test did not release preview composition"
        if fails:
            raise OSError("Controlled local preview failure")
        return compose(*args, **kwargs)

    with patch.object(previews, "compose", held):
        try:
            yield entered, release
        finally:
            release.set()


@pytest.mark.parametrize("kind", EXPORT_KINDS)
@pytest.mark.parametrize("preview_fails", [False, True])
def test_package_waits_for_actual_text_preview_and_failure_releases_it(
    package, kind, preview_fails
):
    p = package
    # Keep hashes of existing files: folder export may create additional PNG copies.
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in p.ws.storage.root.rglob("*.png")
    }
    with (
        ThreadPoolExecutor(max_workers=2) as pool,
        held_composition(fails=preview_fails) as (entered, release),
    ):
        preview = pool.submit(p.client.post, p.base + "/text/preview", json=p.edit)
        assert entered.wait(5)
        exporting = pool.submit(request_package, p, kind)
        assert p.mutex.waiting.wait(5), "Export did not wait for the active preview"
        assert not exporting.done()
        # Waiting happens in a worker; other requests can still be served.
        assert p.client.get("/").status_code == 200
        displayed = p.client.get(p.png_url)
        assert displayed.status_code == 200 and displayed.content == p.png_bytes
        assert "content-disposition" not in displayed.headers
        release.set()
        result = preview.result(timeout=10)
        assert result.status_code == (503 if preview_fails else 200), result.text
        if not preview_fails:
            assert result.json()["impact"]["render_outputs"] == []
        assert_package_parity(p, exporting.result(timeout=10), kind)
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == sha for path, sha in before.items())


@pytest.mark.parametrize("kind", ["zip", "png", "report-html", "report-image"])
def test_export_waits_for_server_logo_preview_after_request_cancellation(package, kind):
    p = package

    async def cancelled_request():
        transport = httpx.ASGITransport(app=p.client.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"X-Campaign-Token": p.client.app.state.csrf},
        ) as client:
            with held_composition() as (entered, release):
                preview = asyncio.create_task(client.post(p.base + "/logo/preview", json=p.edit))
                assert await asyncio.to_thread(entered.wait, 5)
                preview.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await preview
                # Cancelling the request cannot stop its already-running renderer thread.
                if kind == "zip":
                    exporting = asyncio.create_task(client.post(p.zip_url, json=p.action))
                else:
                    url = p.png_url + "?download=true" if kind == "png" else p.report_urls[kind]
                    exporting = asyncio.create_task(client.get(url))
                assert await asyncio.to_thread(p.mutex.waiting.wait, 5)
                assert not exporting.done()
                assert (await asyncio.wait_for(client.get("/"), timeout=5)).status_code == 200
                release.set()
                return await asyncio.wait_for(exporting, timeout=10)

    assert_package_parity(p, asyncio.run(cancelled_request()), kind)


def change_revision(p):
    accepted = p.ws.state.plan(p.run)
    value = accepted.brief.model_dump(mode="json")
    value["markets"]["en-US"]["copy"]["message"] = "A different headline."
    brief = Brief.model_validate(value)
    candidate = make_local_plan(accepted, brief, p.ws.storage)
    revised = p.client.app.state.runner.revise(p.run, brief, p.revision, candidate.digest)
    assert revised["revision_id"] != p.revision


@pytest.mark.parametrize("kind", ["zip", "folder"])
@pytest.mark.parametrize("changed", ["snapshot", "revision"])
def test_waiting_package_rechecks_changes_before_export(package, kind, changed):
    p = package
    with ThreadPoolExecutor(max_workers=1) as pool:
        # The runner lock is free at the end of a preview, before its mutex exits.
        # A second operation may change the accepted state in this admission window.
        with p.mutex:
            exporting = pool.submit(request_package, p, kind)
            assert p.mutex.waiting.wait(5)
            if changed == "snapshot":
                p.ws.state.update_creative(
                    p.revision,
                    p.manifest["outputs"][0]["id"],
                    CreativeUpdate(expected_version=0, description="A newer image description."),
                    target=p.target,
                )
            else:
                change_revision(p)
            assert not exporting.done()
        refused = exporting.result(timeout=10)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] != "busy"
        assert "changed" in refused.json()["message"]


def test_other_process_runner_lock_still_refuses_without_waiting(package):
    p = package
    script = (
        "import sys\nfrom pathlib import Path\nfrom campaign_studio.storage import Storage\n"
        "with Storage(Path(sys.argv[1])).exclusive_run():\n"
        " print('LOCKED', flush=True)\n sys.stdin.readline()\n"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script, str(p.ws.storage.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as worker:
        try:
            assert worker.stdout.readline().strip() == "LOCKED"
            with ThreadPoolExecutor(max_workers=1) as pool:
                for kind in EXPORT_KINDS:
                    result = pool.submit(request_package, p, kind).result(timeout=5)
                    assert result.status_code == 409 and result.json()["code"] == "busy", (
                        result.text
                    )
        finally:
            worker.communicate("release\n", timeout=5)
    assert worker.returncode == 0
    assert_package_parity(p, request_package(p, "zip"), "zip")


def test_active_local_production_still_refuses_without_waiting(package):
    p = package
    entered, release = Event(), Event()
    render = runner_module.render_revision

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return render(*args, **kwargs)

    with (
        patch.object(runner_module, "render_revision", held),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        production = pool.submit(p.client.app.state.runner.recover_local, p.run)
        try:
            assert entered.wait(5)
            for kind in EXPORT_KINDS:
                result = pool.submit(request_package, p, kind).result(timeout=5)
                assert result.status_code == 409 and result.json()["code"] == "busy", result.text
        finally:
            release.set()
        production.result(timeout=10)
    assert p.ws.state.attempts(p.run) == []


def test_waiting_png_rejects_new_revision_but_saved_report_remains_historical(package):
    p = package
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p.mutex:
            downloading = pool.submit(request_package, p, "png")
            assert p.mutex.waiting.wait(5)
            change_revision(p)
            assert not downloading.done()
        refused = downloading.result(timeout=10)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] != "busy" and "changed" in refused.json()["message"]
    # Existing reports intentionally refer to their saved version, not the new image set.
    for kind, url in p.report_urls.items():
        response = p.client.get(url)
        assert response.status_code == 200 and response.content == p.report_bytes[kind]


@pytest.mark.parametrize("kind", ["png", "report-html", "report-image"])
@pytest.mark.parametrize("changed", ["integrity", "rules"])
def test_waiting_image_or_report_rechecks_integrity_and_export_rules(package, kind, changed):
    p = package
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p.mutex:
            downloading = pool.submit(request_package, p, kind)
            assert p.mutex.waiting.wait(5)
            if changed == "rules":
                body = authored_edit(p.client, p.brief.campaign_id)
                body["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = [
                    {
                        "id": "new-prohibited-phrase",
                        "phrase": p.manifest["outputs"][0]["copy"]["message"],
                    }
                ]
                saved = p.client.post(
                    f"/api/motif/campaigns/{p.brief.campaign_id}/authored", json=body
                )
                assert saved.status_code == 200, saved.text
            else:
                if kind == "png":
                    prefix = p.ws.state.revision(p.revision)["manifest_ref"].rsplit("/", 1)[0]
                    file = p.ws.storage.path(f"{prefix}/{p.manifest['outputs'][0]['file']}")
                else:
                    file = p.folder_path / ("index.html" if kind == "report-html" else p.report_png)
                file.write_bytes(b"Changed after the export request began")
            assert not downloading.done()
        refused = downloading.result(timeout=10)
        assert refused.status_code == 409, refused.text
        # Image ActionError uses the existing HTML error page; report/rule errors use JSON.
        assert "Another campaign operation" not in refused.text
        if changed == "integrity":
            assert "missing or changed" in refused.text
        else:
            assert "prohibited" in refused.text


def test_waiting_png_still_checks_campaign_ownership(package):
    p = package
    foreign_id, _ = Intake(p.ws.storage).new()
    foreign_url = p.png_url.replace(p.brief.campaign_id, foreign_id)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with p.mutex:
            downloading = pool.submit(p.client.get, foreign_url + "?download=true")
            assert p.mutex.waiting.wait(5)
            assert not downloading.done()
        refused = downloading.result(timeout=10)
        assert refused.status_code in {404, 409}, refused.text
        assert "Another campaign operation" not in refused.text
        assert refused.content != p.png_bytes
