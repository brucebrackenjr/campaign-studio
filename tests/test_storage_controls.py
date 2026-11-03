"""Connected storage contracts: committed deletion and cause-specific input refusals."""

import hashlib
import io
import json
import struct
import subprocess
import sys
import zlib

import pytest
from campaign_fixtures import authored_edit
from fastapi.testclient import TestClient
from PIL import Image, JpegImagePlugin, PngImagePlugin

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.intake import UPLOAD_LIMIT


def test_repeated_png_exif_is_bounded_and_refused_without_losing_valid_upload(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    with io.BytesIO() as buffer:
        Image.new("RGB", (2, 3), "#123456").save(buffer, format="PNG", exif=exif)
        valid = buffer.getvalue()
    # A tiny image with a large repeated EXIF prefix bypasses pixel-count limits.
    # Keep this CPU-regression check in a subprocess so a vulnerable dependency
    # cannot hang the test runner. No oversized pixel allocation is involved.
    data = b"Exif\0\0" * 1_000_000 + exif.tobytes()
    chunk = struct.pack(">I", len(data)) + b"eXIf" + data
    chunk += struct.pack(">I", zlib.crc32(b"eXIf" + data))
    (tmp_path / "valid.png").write_bytes(valid)
    with io.BytesIO() as buffer:
        Image.new("RGB", (2, 3), "#123456").save(buffer, format="PNG")
        plain = buffer.getvalue()
    (tmp_path / "repeated.png").write_bytes(plain[:33] + chunk + plain[33:])
    code = """
import hashlib
from pathlib import Path
import sys
from campaign_studio.imaging import load_image
from campaign_studio.intake import Intake
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.workspace import ActionError
root = Path(sys.argv[1])
storage = Storage(root / 'runtime')
State(storage)
intake = Intake(storage)
campaign, _ = intake.new()
payload = (root / 'valid.png').read_bytes()
accepted = intake.upload(payload, 'image/png', campaign)
image, kind = load_image(storage.path(accepted['reference']))
assert kind == 'PNG' and image.size == (3, 2)
assert image.getpixel((0, 0)) == (18, 52, 86)
image.close()
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in storage.path(storage.project(campaign).ref('Assets')).rglob('*.png')}
try:
    intake.upload((root / 'repeated.png').read_bytes(), 'image/png', campaign)
except ActionError as error:
    assert error.status == 422 and error.code == 'upload_image'
    assert error.input_image['kind'] == 'decode_failure'
else:
    raise AssertionError('Malformed repeated EXIF was accepted')
assert before == {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in storage.path(storage.project(campaign).ref('Assets')).rglob('*.png')}
assert storage.path(accepted['reference']).read_bytes() == payload
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "malformed,reader,content_type,reason",
    [
        (
            b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 0) + b"EJ\xfcK" + b"\0" * 4,
            PngImagePlugin.PngImageFile,
            "image/png",
            "broken PNG file",
        ),
        (
            b"\xff\xd8\xff\xd9",
            JpegImagePlugin.JpegImageFile,
            "image/jpeg",
            "not identified by this driver",
        ),
    ],
    ids=["png-chunk", "jpeg-missing-frame"],
)
def test_malformed_supported_header_is_safe_without_changing_saved_work(
    tmp_path, malformed, reader, content_type, reason
):
    # Genuine signatures select the direct readers. PNG rejects its chunk name;
    # JPEG rejects a stream without an image frame.
    with pytest.raises(SyntaxError, match=reason):
        reader(io.BytesIO(malformed))
    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Upload refusal must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        client.headers["x-campaign-token"] = app.state.csrf
        campaign = client.post("/api/motif/campaigns", json={"name": "Upload test"}).json()[
            "campaign_id"
        ]
        action = authored_edit(client, campaign)
        value = action["authored"]["brief"]
        headers = {"content-type": "image/png", "x-campaign-id": campaign}
        with io.BytesIO() as buffer:
            Image.new("RGB", (2, 2)).save(buffer, format="PNG")
            original = buffer.getvalue()
        attached = client.post("/api/uploads", content=original, headers=headers).json()
        value["audience"] = "Previously typed audience"
        value["brand"]["logo_path"] = attached["reference"]
        saved = client.post(f"/api/motif/campaigns/{campaign}/authored", json=action)
        assert saved.status_code == 200, saved.text
        storage = app.state.workspace.storage

        def stored_state():
            files = {
                path.relative_to(storage.root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in storage.root.rglob("*")
                if path.is_file() and not path.name.startswith("state.sqlite3")
            }
            with storage.connect() as db:
                tables = [
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                ]
                rows = {
                    name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                    for name in tables
                }
            return files, rows

        before = stored_state()
        refused = client.post(
            "/api/uploads", content=malformed, headers=headers | {"content-type": content_type}
        )
        assert refused.status_code == 422, refused.text
        assert refused.json() == {
            "code": "upload_image",
            "message": "This image could not be opened. Choose a valid PNG or JPEG.",
            "input_image": {"kind": "decode_failure"},
        }
        assert stored_state() == before
        assert client.get(attached["url"]).content == original
        reopened = client.get(f"/api/motif/campaigns/{campaign}").json()
        assert reopened["project"]["authored"]["brief"] == value
        assert app.state.workspace.runs() == []


def test_commit_receipt_precedes_unlink_and_restart_resumes_only_that_deletion(tmp_path):
    settings = Settings(data_dir=tmp_path / "runtime")
    app = create_app(settings, key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["x-campaign-token"] = app.state.csrf
        created = client.post(
            "/api/motif/campaigns",
            json={"name": "Disposable campaign", "brand": "Brand", "audience": "Audience"},
        ).json()
        campaign = created["campaign_id"]
        draft = app.state.storage.path(app.state.storage.project(campaign).draft)
        before = draft.read_bytes()
        base = f"/api/campaigns/{campaign}/cleanup"
        preview = client.post(base + "/preview", json={}).json()
        accepted = client.post(base + "/confirm", json={"token": preview["token"]}).json()
        assert accepted["state"] == "committed"
        assert draft.read_bytes() == before  # The response records commit, not invented completion.
        assert client.get(base + "/status").json()["campaign_state"] == "pending"
        assert client.get(f"/api/motif/campaigns/{campaign}").status_code == 409
        for page in ("campaign", "production", "studio", "handoff"):
            response = client.get(f"/campaigns/{campaign}/{page}", follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/"
        home = client.get("/api/motif/workspace").json()
        assert all(item["id"] != campaign for item in home["campaigns"])
        assert home["pending_cleanup"][0]["id"] == accepted["cleanup_id"]
        with app.state.workspace.storage.connect() as db:
            payload = json.loads(db.execute("SELECT value_json FROM cleanup_intents").fetchone()[0])
            assert "name" in payload and "value" not in payload
    # Losing the confirm response or closing the page needs no repeated approval:
    # startup resumes the already-committed exact file list, without a provider.
    with TestClient(
        create_app(settings, key_available=lambda: False),
        base_url="http://127.0.0.1",
    ) as client:
        deleted = client.get(base + "/status")
        assert deleted.status_code == 404 and deleted.json()["code"] == "not_found"
        assert not draft.exists()
        with client.app.state.workspace.storage.connect() as db:
            assert db.execute("SELECT id FROM campaigns WHERE id=?", (campaign,)).fetchone() is None
    assert not list((settings.data_dir / "Projects").glob("*/Brief/draft.json"))


def test_upload_cause_fields_preserve_existing_owned_input_and_draft(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "runtime"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["x-campaign-token"] = app.state.csrf
        campaign = client.post("/api/motif/campaigns", json={"name": "Upload causes"}).json()[
            "campaign_id"
        ]
        headers = {"content-type": "image/png", "x-campaign-id": campaign}
        with io.BytesIO() as buffer:
            Image.new("RGB", (2, 2)).save(buffer, format="PNG")
            png = buffer.getvalue()
        valid = client.post("/api/uploads", content=png, headers=headers).json()
        stored = app.state.workspace.storage.path(valid["reference"])
        draft = app.state.workspace.storage.path(
            app.state.storage.project(campaign).draft
        ).read_bytes()
        with io.BytesIO() as buffer:
            Image.new("RGB", (2, 2)).save(buffer, format="GIF")
            gif = buffer.getvalue()
        with io.BytesIO() as buffer:
            Image.new("CMYK", (2, 2)).save(buffer, format="JPEG")
            untagged_cmyk = buffer.getvalue()
        with io.BytesIO() as buffer:
            Image.new("RGB", (2, 2)).save(buffer, format="PNG", icc_profile=b"invalid ICC profile")
            invalid_profile = buffer.getvalue()
        over_pixels = bytearray(png)
        over_pixels[16:24] = struct.pack(">II", 10001, 4000)
        over_pixels[29:33] = struct.pack(">I", zlib.crc32(over_pixels[12:29]) & 0xFFFFFFFF)
        cases = [
            (gif, "image/png", "unsupported_type"),
            (b"invalid image", "image/png", "decode_failure"),
            (bytes(over_pixels), "image/png", "pixel_count"),
            (png + b"0" * (UPLOAD_LIMIT + 1 - len(png)), "image/png", "encoded_bytes"),
            (untagged_cmyk, "image/jpeg", "color_profile"),
            (invalid_profile, "image/png", "color_profile"),
        ]
        for payload, content_type, kind in cases:
            response = client.post(
                "/api/uploads", content=payload, headers=headers | {"content-type": content_type}
            )
            assert response.status_code in {413, 422}
            assert response.json()["input_image"]["kind"] == kind
            if kind == "color_profile":
                assert "color profile" in response.json()["message"]
                assert "sRGB" in response.json()["message"]
                assert list(stored.parent.iterdir()) == [stored]
            if kind == "pixel_count":
                assert response.json()["input_image"]["width"] == 10001
                assert response.json()["input_image"]["height"] == 4000
            assert stored.read_bytes() == png
            assert (
                app.state.workspace.storage.path(
                    app.state.storage.project(campaign).draft
                ).read_bytes()
                == draft
            )
        # A valid header above Pillow's generic bomb threshold still reports
        # pixel dimensions, without decoding or publishing this refused image.
        over_pixels[16:24] = struct.pack(">II", 20000, 10000)
        over_pixels[29:33] = struct.pack(">I", zlib.crc32(over_pixels[12:29]) & 0xFFFFFFFF)
        response = client.post("/api/uploads", content=bytes(over_pixels), headers=headers)
        assert response.json()["input_image"] == {
            "kind": "pixel_count",
            "width": 20000,
            "height": 10000,
            "limit_pixels": 40000000,
        }
        refused = client.post(
            "/api/uploads",
            content=b"<svg/>",
            headers={"content-type": "image/svg+xml", "x-campaign-id": campaign},
        )
        assert refused.status_code == 422
        assert refused.json()["input_image"]["kind"] == "decode_failure"
        assert "JSON" not in refused.json()["message"]
        exact = client.post(
            "/api/uploads", content=png + b"0" * (UPLOAD_LIMIT - len(png)), headers=headers
        )
        assert exact.status_code == 200
        assert (
            app.state.workspace.storage.path(exact.json()["reference"]).stat().st_size
            == UPLOAD_LIMIT
        )
        assert app.state.workspace.runs() == []
