"""A one-image autosave touches only SQLite; a later export renders the saved set."""

import hashlib
import io
import json
import os
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.campaign_drafts import read, write
from campaign_studio.config import Settings
from campaign_studio.sample import write_sample
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.workspace import Workspace


def five_product_campaign(tmp_path):
    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage, 3)
    for index in (4, 5):
        brief.products.append(
            brief.products[0].model_copy(
                update={"id": f"product-{index}", "name": f"Product {index}"}
            )
        )
    state = State(storage)
    value = brief.model_dump(mode="json")
    write(
        storage,
        brief.campaign_id,
        {
            "value": value,
            "authored": {"brief": value},
            "version": uuid4().hex,
            "composition_schema": 1,
        },
    )
    return state, brief


def test_single_image_autosave_reads_no_sources_and_preserves_other_settings(tmp_path, monkeypatch):
    state, brief = five_product_campaign(tmp_path)
    campaign = brief.campaign_id
    app = create_app(
        Settings(data_dir=state.storage.root),
        provider_factory=lambda: pytest.fail("Editing and export must not call a provider"),
        key_available=lambda: False,
    )
    paths = {
        p: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in state.storage.root.glob("Campaigns/**/*")
        if p.is_file()
    }
    protected = {os.fspath(p) for p in paths}
    before = Workspace(state).current_composition(campaign)["plan"]
    assert len(before.outputs) == 30
    other_settings = {
        o.id: o.model_dump(mode="json") for o in before.outputs if o.id != "product-1/en-US/1x1"
    }

    def guarded(fn):
        def check(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)):
                assert os.fspath(path) not in protected, f"{fn.__name__} accessed {path}"
            return fn(path, *args, **kwargs)

        return check

    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update({"X-Campaign-Token": app.state.csrf, "Origin": "http://127.0.0.1"})
        version = read(state.storage, campaign)["version"]
        for left in (0.12, 0.14):
            command = {
                "revision_id": version,
                "product_id": "product-1",
                "market": "en-US",
                "ratio": "1:1",
                "placements": {
                    "message": {"left": left, "top": 0.12, "width": 0.66, "font_size": 48}
                },
            }
            with monkeypatch.context() as guard:
                for module, name in (
                    (io, "open"),
                    (os, "open"),
                    (os, "stat"),
                    (os, "lstat"),
                    (os, "unlink"),
                ):
                    guard.setattr(module, name, guarded(getattr(module, name)))
                guard.setattr(
                    "campaign_studio.compositions.make_plan",
                    lambda *a, **kw: pytest.fail("Autosave replanned the campaign"),
                )
                response = client.post(f"/api/campaigns/{campaign}/text/save", json=command)
                assert response.status_code == 200, response.text
            version = response.json()["version"]
        # Reopening derives the display from the sole current document.
        refreshed = client.get(f"/api/motif/campaigns/{campaign}")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["version"] == version
        after = Workspace(State(state.storage)).current_composition(campaign)["plan"]
        assert {
            o.id: o.model_dump(mode="json") for o in after.outputs if o.id != "product-1/en-US/1x1"
        } == other_settings
        assert after.outputs[0].framing.text_placements["message"].left == 0.14
        assert paths == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        downloaded = client.post(
            f"/api/campaigns/{campaign}/delivery/zip", json={"version": version}
        )
        assert downloaded.status_code == 200, downloaded.text
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            assert archive.testzip() is None
            manifest = json.loads(
                archive.read(next(n for n in archive.namelist() if n.endswith("/manifest.json")))
            )
            assert len(manifest["outputs"]) == 30
            prefix = manifest["package_name"] + "/"
            for output in manifest["outputs"]:
                assert (
                    hashlib.sha256(archive.read(prefix + output["file"])).hexdigest()
                    == output["sha256"]
                )
    assert not list(state.storage.root.glob("Campaigns/**/Outputs"))
    assert not list(state.storage.root.glob("Campaigns/**/Exports"))
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM runs").fetchone() == (0,)
