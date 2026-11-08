"""Saved metadata exports stay consistent while independent previews and saves run."""

import asyncio
import hashlib
import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest
from PIL import Image
from test_prohibited_exports import campaign, export_urls, png_url, save

from campaign_studio import compositions, delivery
from campaign_studio.campaign_drafts import read, write
from campaign_studio.handoff import Handoff
from campaign_studio.intake import Intake

EXPORT_KINDS = ("zip", "preview", "png", "report-html", "report-image")


@pytest.fixture
def package(tmp_path):
    with campaign(tmp_path) as (client, ws, url):
        current = client.get(url).json()
        paths = export_urls(current)
        first = client.post(paths["zip"], json=paths["body"])
        assert first.status_code == 200, first.text
        preview = client.post(paths["preview"], json=paths["body"])
        assert preview.status_code == 200, preview.text
        report_url = preview.json()["url"]
        report_manifest = client.get(report_url.replace("index.html", "manifest.json")).json()
        output = next(o for o in report_manifest["outputs"] if o["file"])
        yield SimpleNamespace(
            client=client,
            ws=ws,
            url=url,
            cid=current["value"]["campaign_id"],
            current=current,
            paths=paths,
            zip_bytes=first.content,
            png_url=png_url(current),
            preview_image=current["manifest"]["outputs"][0]["url"],
            report_urls={
                "report-html": report_url,
                "report-image": report_url.replace("index.html", output["file"]),
            },
        )


def request_package(p, kind):
    if kind in p.report_urls:
        return p.client.get(p.report_urls[kind])
    if kind == "png":
        return p.client.get(p.png_url)
    return p.client.post(p.paths[kind], json=p.paths["body"])


def assert_package_parity(p, response, kind):
    assert response.status_code == 200, response.text
    if kind in {"png", "report-image"}:
        assert response.content.startswith(b"\x89PNG")
        assert response.headers["content-type"] == "image/png"
    if kind == "png":
        assert response.headers["content-disposition"].startswith("attachment;")
    if kind == "zip":
        with (
            ZipFile(io.BytesIO(response.content)) as fresh,
            ZipFile(io.BytesIO(p.zip_bytes)) as previous,
        ):
            assert set(fresh.namelist()) == set(previous.namelist())
            for name in fresh.namelist():
                if name.endswith(".png"):
                    with (
                        Image.open(io.BytesIO(fresh.read(name))) as actual,
                        Image.open(io.BytesIO(previous.read(name))) as expected,
                    ):
                        assert actual.convert("RGB").tobytes() == expected.convert("RGB").tobytes()
                        assert actual.info["icc_profile"]
    if kind == "preview":
        assert p.client.get(response.json()["url"]).status_code == 200
    assert not list(p.ws.storage.root.rglob("Outputs"))
    assert not list(p.ws.storage.root.rglob("Exports"))
    with p.ws.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", EXPORT_KINDS)
@pytest.mark.parametrize("preview_fails", [False, True])
def test_active_preview_does_not_delay_or_poison_a_saved_export(
    package, monkeypatch, kind, preview_fails
):
    p = package
    entered, release = Event(), Event()
    compose = compositions.compose
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in p.ws.storage.root.rglob("*.png")
    }

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        if preview_fails:
            raise OSError("Injected visual preview failure")
        return compose(*args, **kwargs)

    monkeypatch.setattr(compositions, "compose", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        preview = pool.submit(p.client.get, p.preview_image)
        try:
            assert entered.wait(5)
            exported = pool.submit(request_package, p, kind).result(timeout=10)
            assert_package_parity(p, exported, kind)
            assert not preview.done()
            assert p.client.get("/").status_code == 200
        finally:
            release.set()
        if preview_fails:
            assert preview.result(timeout=10).status_code == 503
        else:
            assert preview.result(timeout=10).status_code == 200
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items()
    )


@pytest.mark.parametrize("kind", ["zip", "png"])
def test_cancelled_browser_preview_does_not_block_download(package, monkeypatch, kind):
    p = package
    entered, release = Event(), Event()
    compose = compositions.compose

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return compose(*args, **kwargs)

    monkeypatch.setattr(compositions, "compose", held)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=p.client.app),
            base_url="http://127.0.0.1",
            headers=p.client.headers,
        ) as client:
            preview = asyncio.create_task(client.get(p.preview_image))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                preview.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await preview
                if kind == "zip":
                    return await asyncio.wait_for(
                        client.post(p.paths["zip"], json=p.paths["body"]), 10
                    )
                return await asyncio.wait_for(client.get(p.png_url), 10)
            finally:
                release.set()

    assert_package_parity(p, asyncio.run(exercise()), kind)


@pytest.mark.parametrize("kind", EXPORT_KINDS)
@pytest.mark.parametrize("changed", ["description", "copy"])
def test_stale_delivery_and_preview_urls_refuse_new_saved_values(package, kind, changed):
    p = package
    if changed == "copy":
        save(
            p.client,
            p.url,
            lambda a: a["brief"]["markets"]["en-US"]["copy"].update(cta="New call to action"),
        )
    else:
        from campaign_studio.compositions import save_metadata
        from campaign_studio.review import CreativeUpdate

        save_metadata(
            p.ws,
            p.cid,
            p.current["version"],
            "product-1/en-US/1x1",
            CreativeUpdate(expected_version=0, description="A newer image description."),
        )
    result = request_package(p, kind)
    assert result.status_code == 409, result.text
    assert "busy" not in result.text.lower()
    with ZipFile(io.BytesIO(p.zip_bytes)) as archive:
        assert archive.testzip() is None  # An earlier downloaded file stays self-contained.


def test_production_lock_does_not_block_export_of_saved_sources(package):
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
            assert_package_parity(p, request_package(p, "zip"), "zip")
        finally:
            worker.communicate("release\n", timeout=5)
    assert worker.returncode == 0


def test_export_snapshot_is_unchanged_by_a_save_while_first_png_renders(package, monkeypatch):
    p = package
    entered, release = Event(), Event()
    original = delivery.render_png
    once = False

    def held(*args, **kwargs):
        nonlocal once
        if not once:
            once = True
            entered.set()
            assert release.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "render_png", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        exported = pool.submit(request_package, p, "zip")
        try:
            assert entered.wait(5)
            save(
                p.client,
                p.url,
                lambda a: a["brief"]["markets"]["en-US"]["copy"].update(cta="Later saved CTA"),
            )
            newer = p.client.get(p.url).json()
            assert newer["version"] != p.current["version"]
        finally:
            release.set()
        response = exported.result(timeout=10)
    assert_package_parity(p, response, "zip")
    with ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(
            archive.read(next(n for n in archive.namelist() if n.endswith("manifest.json")))
        )
        assert manifest["composition_version"] == p.current["version"]
        assert all(o["copy"]["cta"] != "Later saved CTA" for o in manifest["outputs"])


def test_source_changed_after_capture_is_rejected_and_no_files_are_delivered(package, monkeypatch):
    p = package
    original = Handoff.current_snapshot

    def changed_snapshot(self, *args, **kwargs):
        captured = original(self, *args, **kwargs)
        source = next(iter(captured["sources"].values()))
        p.ws.storage.path(source.reference).write_bytes(b"changed after snapshot")
        return captured

    monkeypatch.setattr(Handoff, "current_snapshot", changed_snapshot)
    result = request_package(p, "zip")
    assert result.status_code == 409 and result.json()["code"] == "source_changed", result.text
    assert not getattr(p.ws.state, "delivery_jobs", {})
    assert not list(p.ws.storage.root.rglob("Outputs"))


def test_current_download_checks_campaign_ownership(package):
    p = package
    foreign_id, _ = Intake(p.ws.storage).new()
    foreign = p.png_url.replace(p.cid, foreign_id)
    result = p.client.get(foreign)
    assert result.status_code in {404, 409}, result.text
    # Even forging another campaign's composition version does not grant its assets.
    document = read(p.ws.storage, p.cid)
    document["authored"]["brief"]["campaign_id"] = foreign_id
    write(p.ws.storage, foreign_id, document)
    result = p.client.get(foreign)
    assert result.status_code in {404, 409, 422}, result.text
