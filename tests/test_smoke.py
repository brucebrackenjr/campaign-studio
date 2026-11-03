import hashlib
import json
import socket

import pytest
from PIL import Image

from campaign_studio.smoke import run_smoke
from campaign_studio.storage import Storage


def test_smoke_round_trip_is_synthetic_and_never_uses_network(tmp_path, monkeypatch):
    def forbid_network(*args, **kwargs):
        pytest.fail("The key-free installation check tried to use the network.")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbid_network)
    storage = Storage(tmp_path / "runtime")
    first = run_smoke(storage)
    second = run_smoke(storage)
    assert first.artifact_reference != second.artifact_reference
    image_path = storage.path(f"{first.artifact_reference}/synthetic.png")
    with Image.open(image_path) as image:
        image.load()
        assert image.size == (128, 128)
        assert image.getpixel((20, 20)) == (23, 59, 69)
        assert "Synthetic" in image.info["Evidence"]
    manifest = json.loads(storage.path(f"{first.artifact_reference}/manifest.json").read_text())
    assert manifest["evidence"] == "synthetic-installation-check"
    assert manifest["provider_requests"] == 0
    assert manifest["photographic_evidence"] is False
    assert manifest["image_sha256"] == hashlib.sha256(image_path.read_bytes()).hexdigest()
    assert list(storage.path("Campaigns").iterdir()) == []
