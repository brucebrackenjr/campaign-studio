"""Prepared reference resources use temporary delivery, not retained campaign PNGs."""

import hashlib
import json

import pytest
from test_bundled import build_prepared_outputs
from test_bundled import bundle as bundle

from campaign_studio import delivery
from campaign_studio.bundled import load_campaigns
from campaign_studio.state import StateError
from campaign_studio.storage import Storage


def test_prepared_builder_contract_is_verified_without_working_outputs(tmp_path, bundle):
    _, definitions = load_campaigns(bundle)
    supplied = definitions[0]
    assert len(supplied[3].descriptor["outputs"]) == 30
    workspace = tmp_path / "build-supplied"
    assert not list(workspace.rglob("Outputs"))
    assert not list(workspace.rglob("Exports"))
    with Storage(workspace).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_failed_prepared_builder_preserves_existing_resource_package(tmp_path, bundle, monkeypatch):
    entry = json.loads((bundle / "manifest.json").read_bytes())["campaigns"][0]
    before = {
        str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in bundle.rglob("*")
        if p.is_file()
    }

    def fail(*_args, **_kwargs):
        raise OSError("Injected build render failure")

    monkeypatch.setattr(delivery, "render_png", fail)
    with pytest.raises(StateError, match="issue creating the download"):
        build_prepared_outputs(bundle, entry, Storage(tmp_path / "failed-build"))
    assert {
        str(p.relative_to(bundle)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in bundle.rglob("*")
        if p.is_file()
    } == before
