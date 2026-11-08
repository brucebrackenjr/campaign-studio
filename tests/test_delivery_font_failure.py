"""An unavailable composition font stops delivery with its installation remedy."""

# ruff: noqa: F811

from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from test_current_composition import current  # noqa: F401

from campaign_studio import delivery, delivery_jobs, imaging
from campaign_studio.app import create_app
from campaign_studio.campaign_drafts import read
from campaign_studio.config import Settings
from campaign_studio.delivery_jobs import DeliveryJob


@pytest.mark.parametrize("corrupt", [False, True], ids=["missing", "corrupt"])
def test_font_failure_during_render_returns_remedy_and_closes_the_job(
    current, monkeypatch, tmp_path, corrupt
):
    ws, cid = current
    original_document = read(ws.storage, cid)
    original_sources = {p: p.read_bytes() for p in ws.storage.root.rglob("*.png")}
    damaged_font = tmp_path / "unavailable.ttf"
    if corrupt:
        damaged_font.write_bytes(b"not a font")
    streams, jobs, calls = [], [], []
    temporary_file = delivery_jobs.tempfile.TemporaryFile
    run = DeliveryJob.run
    require_font = delivery.require_font

    def temporary(*args, **kwargs):
        stream = temporary_file(*args, **kwargs)
        streams.append(stream)
        return stream

    def capture_job(job):
        jobs.append(job)
        return run(job)

    def unavailable_after_two_images():
        calls.append("font check")
        if len(calls) < 3:
            return require_font()
        # Planning has already succeeded. Exercise a real damaged resource at
        # the per-image rendering boundary, after two successful PNGs exist.
        with monkeypatch.context() as damage:
            damage.setattr(imaging, "font_path", lambda: str(damaged_font))
            imaging.require_font()

    monkeypatch.setattr(delivery_jobs.tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(DeliveryJob, "run", capture_job)
    monkeypatch.setattr(delivery, "require_font", unavailable_after_two_images)
    app = create_app(
        Settings(data_dir=ws.storage.root),
        provider_factory=lambda: pytest.fail("A local export must not construct a provider"),
        key_available=lambda: False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        operation_id = uuid4().hex
        action = {"version": original_document["version"], "operation_id": operation_id}
        response = client.post(f"/api/campaigns/{cid}/delivery/zip", json=action)
        assert response.status_code == 503, response.text
        error = response.json()
        assert error["code"] == "font_unavailable"
        assert "Reinstall Motif – Campaign Studio to restore it" in error["message"]
        assert "Your campaign settings and source images are unchanged." in error["message"]
        assert "Retry failed images" not in error["message"]
        assert str(damaged_font) not in response.text
        assert len(calls) == 3  # Stop immediately; do not retry every output.
        assert len(jobs) == 1 and jobs[0].closed and not jobs[0].active
        assert jobs[0].ready == {} and jobs[0].sources == {}
        assert jobs[0].timer is None
        assert streams and all(stream.closed for stream in streams)
        assert app.state.workspace.state.__dict__["delivery_jobs"] == {}
        progress = client.get(f"/api/campaigns/{cid}/delivery/progress/{operation_id}")
        assert progress.json() == {"stage": None, "status": "unavailable"}
        assert read(ws.storage, cid) == original_document
        assert all(path.read_bytes() == payload for path, payload in original_sources.items())
        assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))
        assert not list(ws.storage.root.glob("Campaigns/**/Exports"))
        with ws.storage.connect() as db:
            for table in ("runs", "attempts", "revisions", "package_exports"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

        # Restoring the font permits a fresh download of the same saved work.
        monkeypatch.setattr(delivery, "require_font", require_font)
        downloaded = client.post(f"/api/campaigns/{cid}/delivery/zip", json=action)
        assert downloaded.status_code == 200, downloaded.text[:200]
        with ZipFile(BytesIO(downloaded.content)) as archive:
            assert archive.testzip() is None
            assert len([name for name in archive.namelist() if name.endswith(".png")]) == 6
        assert all(stream.closed for stream in streams)
        assert app.state.workspace.state.__dict__["delivery_jobs"] == {}
        assert read(ws.storage, cid) == original_document
