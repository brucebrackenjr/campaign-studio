"""Fresh project ownership and the user-visible file layout."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.bundled import load_campaigns
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.handoff import Handoff, PackageAction
from campaign_studio.review import RevisionTarget
from campaign_studio.state import State
from campaign_studio.storage import EarlierWorkspace, Storage


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_project_registry_covers_exports_and_preserves_user_files(tmp_path):
    root = tmp_path / "Motif"
    app = create_app(
        Settings(data_dir=root),
        key_available=lambda: False,
        seed_examples=True,
        provider_factory=lambda: pytest.fail("No provider call expected"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.workspace.state
        campaigns = app.state.workspace.catalog()
        autumn, bridle = (
            next(c for c in campaigns if c["name"].startswith(name))["id"]
            for name in ("Autumn Winds", "Bridle & Bough")
        )
        a, b = state.storage.project(autumn), state.storage.project(bridle)
        project_dir, survivor = root / "Projects" / a.folder, root / "Projects" / b.folder
        assert a.folder == f"Autumn Winds-{autumn[:8]}"
        before = hashes(survivor)
        run, revision = state.current_output(autumn)
        target = RevisionTarget(
            campaign_id=autumn, run_id=run, revision_id=revision, mode="current"
        )
        handoff = Handoff(state)
        action = PackageAction(snapshot_id=handoff.metadata(target)["snapshot_id"])
        archive = handoff.create_zip(target, action)
        folder = Path(handoff.create_folder(target, action)["location"])
        external = tmp_path / "Downloads" / archive["filename"]
        external.parent.mkdir()
        external.write_bytes(handoff.download(target, archive["artifact_id"]))
        external_hash = hashlib.sha256(external.read_bytes()).hexdigest()
        assert folder.is_relative_to(project_dir / "Exports")
        assert len(list(folder.rglob("*.png"))) == 30
        with app.state.storage.connect() as db:
            registered = {
                row[0]
                for row in db.execute(
                    "SELECT reference FROM file_owners WHERE campaign_id=?", (autumn,)
                )
            }
        actual = {p.relative_to(root).as_posix() for p in project_dir.rglob("*") if p.is_file()}
        assert actual <= registered
        assert (folder / "index.html").relative_to(root).as_posix() in registered
        assert (folder / "manifest.json").relative_to(root).as_posix() in registered
        personal = folder / "producer notes.txt"
        personal.write_text("Keep these notes.")
        cleaner = Cleanup(state)
        preview = cleaner.preview_campaign(autumn)
        assert preview["unmanaged_file_count"] == 1
        committed = cleaner.delete_campaign(autumn, preview["token"], defer_cleanup=True)
        assert committed["state"] == "committed"
        assert Cleanup(State(state.storage)).resume_all()[0]["state"] == "complete"
        assert list(p for p in project_dir.rglob("*") if p.is_file()) == [personal]
        assert personal.read_text() == "Keep these notes."
        assert hashlib.sha256(external.read_bytes()).hexdigest() == external_hash
        assert hashes(survivor) == before
        assert client.get(f"/api/motif/campaigns/{bridle}").status_code == 200
        assert (
            cleaner.delete_campaign(bridle, cleaner.preview_campaign(bridle)["token"])["state"]
            == "complete"
        )
        assert not survivor.exists()


def test_import_keeps_original_request_bytes_and_stable_folder_on_rename(tmp_path):
    root = tmp_path / "Motif"
    app = create_app(Settings(data_dir=root), key_available=lambda: False)
    _, examples = load_campaigns()
    raw = examples[0][1] | {"name": "Été – café"}
    data = (" \r\n" + json.dumps(raw, ensure_ascii=False, indent="\t") + "\r\n").encode()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers.update(
            {"Content-Type": "application/json", "X-Campaign-Token": app.state.csrf}
        )
        first = client.post("/api/motif/import", content=data)
        assert first.status_code == 200, first.text
        cid = first.json()["campaign_id"]
        project = app.state.storage.project(cid)
        original = project.ref("Brief/Imports/original.json")
        assert app.state.storage.path(project.require(original)).read_bytes() == data
        url = f"/api/motif/campaigns/{cid}/authored"
        model = client.get(url).json()
        model["authored"]["brief"]["name"] = "New title"
        assert (
            client.post(
                url, json={k: model[k] for k in ("version", "base", "authored")}
            ).status_code
            == 200
        )
        assert app.state.storage.project(cid).folder == project.folder
        assert app.state.storage.path(original).read_bytes() == data


def test_old_workspace_is_refused_byte_for_byte_before_projects_are_created(tmp_path):
    root = tmp_path / "old"
    (root / "private").mkdir(parents=True)
    with closing(sqlite3.connect(root / "private/state.sqlite3")) as db, db:
        db.execute("CREATE TABLE campaigns(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO campaigns VALUES ('existing')")
        db.execute("PRAGMA user_version=6")
    (root / "campaigns").mkdir()
    (root / "campaigns" / "keep.png").write_bytes(b"existing work")
    before = hashes(root)
    with pytest.raises(EarlierWorkspace, match="--data-dir ./motif-projects"):
        State(Storage(root))
    assert hashes(root) == before
    assert not (root / "Projects").exists()


def test_foreign_reference_cannot_be_saved_or_used_from_a_modified_snapshot(tmp_path):
    from test_workflow import setup_case

    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.intake import Intake
    from campaign_studio.planning import make_plan
    from campaign_studio.storage import StorageError
    from campaign_studio.workspace import ActionError, Workspace

    state, _, run, brief = setup_case(tmp_path, 2, True)
    projects = AuthoredProjects(Workspace(state))
    intake = Intake(state.storage)
    other, _ = intake.new(name="Other campaign")
    original = state.storage.path(brief.brand.logo_path).read_bytes()
    foreign = intake.upload(original, "image/png", other)["reference"]
    for reference in (foreign, foreign.replace("Projects/", "PROJECTS/"), foreign[:-3] + "PNG"):
        value = projects.read(brief.campaign_id)
        value["authored"]["brief"]["brand"]["logo_path"] = reference
        with pytest.raises(ActionError, match="file picker"):
            projects.save(
                brief.campaign_id,
                AuthoredSave(**{k: value[k] for k in ("version", "base", "authored")}),
            )
        bad = brief.model_copy(
            update={"brand": brief.brand.model_copy(update={"logo_path": reference})}
        )
        assert make_plan(bad, state.storage).blocked
    plan_path = state.storage.path(state.run(run)["plan_ref"])
    plan = json.loads(plan_path.read_bytes())
    plan["assets"]["logo"]["reference"] = foreign
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(StorageError, match="belonging to this campaign"):
        state.plan(run)
    assert state.storage.path(foreign).read_bytes() == original


def test_killed_import_retains_registered_original_and_deletes_only_its_project(tmp_path):
    import subprocess
    import sys
    from uuid import uuid4

    root = tmp_path / "workspace"
    cid = str(uuid4())
    snippet = """
import os, sys
from pathlib import Path
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.intake import Intake
storage = Storage(Path(sys.argv[1]))
State(storage)
intake = Intake(storage)
real_write = storage.write_new
def stop_after_original(ref, data):
    real_write(ref, data)
    if ref.endswith('/Imports/original.json'):
        os._exit(73)
storage.write_new = stop_after_original
brief = {'campaign_id': sys.argv[2], 'name':'Interrupted', 'brand':{}}
intake.create(sys.argv[2], brief, imported_bytes=b'{}')
"""
    stopped = subprocess.run(
        [sys.executable, "-c", snippet, str(root), cid], capture_output=True, timeout=30
    )
    assert stopped.returncode == 73, stopped.stderr
    state = State(Storage(root))
    project = state.storage.project(cid)
    first = project.ref("Brief/Imports/original.json")
    assert state.storage.path(project.require(first)).read_bytes() == b"{}"
    assert not state.storage.path(project.require(project.draft)).exists()
    assert not state.attempts("nonexistent")
    cleaner = Cleanup(state)
    assert cleaner.preview_campaign(cid)["file_count"] == 1
    assert (
        cleaner.delete_campaign(cid, cleaner.preview_campaign(cid)["token"])["state"] == "complete"
    )
    assert not (root / "Projects" / project.folder).exists()


def test_mock_request_reserves_its_destinations_before_receiving_project_files(tmp_path):
    import asyncio

    import httpx
    from test_provider_runner import generated_case

    from campaign_studio.provider import OpenAIImages

    state, runner, run_id, response = generated_case(tmp_path)
    project = state.storage.project(state.run(run_id)["campaign_id"])
    checked = []

    def receive(request):
        (attempt,) = state.attempts(run_id)
        assert attempt["state"] == "dispatching"
        for field in ("response_ref", "source_ref"):
            ref = project.require(attempt[field])
            assert ref.startswith(project.run(run_id) + "/Requests/")
            assert not state.storage.path(ref).exists()
            checked.append(ref)
        return httpx.Response(200, json=response)

    provider = OpenAIImages("local-test-only", transport=httpx.MockTransport(receive))
    result = asyncio.run(runner.execute(run_id, provider))
    assert result["complete"] and state.attempts(run_id)[0]["state"] == "decoded"
    assert len(checked) == 2 and all(state.storage.path(ref).is_file() for ref in checked)
    assert state.storage.path(checked[1]).read_bytes().startswith(b"\x89PNG")
