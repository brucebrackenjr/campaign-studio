import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.handoff import Handoff, PackageAction
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.planning import make_plan
from campaign_studio.review import RevisionTarget
from campaign_studio.state import State, StateError
from campaign_studio.storage import StorageError
from campaign_studio.workspace import ActionError, Workspace


def image_files(state, revision):
    record = state.revision(revision)
    folder = state.storage.path(record["manifest_ref"]).parent / "images"
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*.png")}


def make_package(state, run, revision):
    target = RevisionTarget(
        campaign_id=state.run(run)["campaign_id"],
        run_id=run,
        revision_id=revision,
        mode="saved_version",
    )
    handoff = Handoff(state)
    action = PackageAction(snapshot_id=handoff.metadata(target)["snapshot_id"])
    return target, handoff.create_zip(target, action), handoff.create_folder(target, action)


def test_owned_replaced_uploads_delete_but_shared_and_external_files_survive(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    manifest = asyncio.run(runner.execute(run))
    intake = Intake(state.storage)
    other, _ = intake.new()
    logo = state.storage.path(brief.brand.logo_path).read_bytes()
    canceled = intake.upload(logo, "image/png", brief.campaign_id)
    replacement = intake.upload(logo, "image/png", brief.campaign_id)
    shared = intake.upload(logo, "image/png", brief.campaign_id)
    other_document = intake.read(other)
    other_owned = intake.upload(logo, "image/png", other)
    other_document["value"]["brand"]["logo_path"] = other_owned["reference"]
    intake.write(other, other_document["value"], other_document["version"])
    project_root = state.storage.path(state.storage.project(brief.campaign_id).draft).parents[1]
    _, archive, folder = make_package(state, run, manifest["revision_id"])
    original = state.storage.path(brief.brand.logo_path).read_bytes()
    external = tmp_path / "user-original-logo.png"
    external.write_bytes(original)
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(brief.campaign_id)
    assert preview["output_png_count"] == len([o for o in manifest["outputs"] if o["file"]])
    assert "image_version_count" not in preview
    assert preview["unmanaged_file_count"] == 0
    assert preview["removable_bytes"] > 0
    # Preview/Cancel does not commit a cleanup or remove even a canceled attachment.
    assert state.storage.path(canceled["reference"]).is_file()
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM cleanup_intents").fetchone()[0] == 0
    result = cleanup.delete_campaign(brief.campaign_id, preview["token"])
    assert result["state"] == "complete"
    assert not state.storage.path(canceled["reference"]).exists()
    assert not state.storage.path(replacement["reference"]).exists()
    assert not state.storage.path(shared["reference"]).exists()
    assert state.storage.path(other_owned["reference"]).read_bytes() == logo
    assert not state.storage.path(brief.brand.logo_path).exists()  # App-owned synthetic source.
    assert external.read_bytes() == original
    assert not Path(folder["location"]).exists()
    assert not project_root.exists()
    with state.storage.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM runs WHERE campaign_id=?", (brief.campaign_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM package_exports WHERE id=?", (archive["artifact_id"],)
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT COUNT(*) FROM cleanup_intents").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert intake.read(other)["value"]["brand"]["logo_path"] == other_owned["reference"]


@pytest.mark.parametrize(
    "name, expected", [("Saved new name", "Saved new name"), ("", "Untitled campaign")]
)
def test_deletion_names_current_authored_campaign_without_changing_identity(
    tmp_path, name, expected
):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    cleanup = Cleanup(state)
    before = cleanup.preview_campaign(brief.campaign_id)
    assert before["name"] == brief.name  # Older run-only record remains supported.
    projects = AuthoredProjects(Workspace(state))
    current = projects.read(brief.campaign_id)
    current["authored"]["brief"]["name"] = name
    projects.save(
        brief.campaign_id,
        AuthoredSave(**{key: current[key] for key in ("version", "base", "authored")}),
    )
    after = cleanup.preview_campaign(brief.campaign_id)
    assert after["campaign_id"] == before["campaign_id"]
    assert after["name"] == expected
    assert after["token"] != before["token"]
    with pytest.raises(StateError, match="changed"):
        cleanup.delete_campaign(brief.campaign_id, before["token"])
    assert projects.read(brief.campaign_id)["authored"]["brief"]["name"] == name


def test_stale_inventory_and_active_operation_refuse_before_deletion(tmp_path):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(brief.campaign_id)
    intake = Intake(state.storage)
    upload = intake.upload(
        state.storage.path(brief.brand.logo_path).read_bytes(), "image/png", brief.campaign_id
    )
    with pytest.raises(StateError, match="changed"):
        cleanup.delete_campaign(brief.campaign_id, preview["token"])
    assert state.storage.path(upload["reference"]).is_file()
    with state.storage.exclusive_run(), pytest.raises(StorageError, match="already active"):
        cleanup.preview_campaign(brief.campaign_id)
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM cleanup_intents").fetchone()[0] == 0
        db.execute(
            "CREATE TRIGGER deny_cleanup BEFORE INSERT ON cleanup_intents "
            "BEGIN SELECT RAISE(FAIL,'controlled intent failure'); END"
        )
    preview = cleanup.preview_campaign(brief.campaign_id)
    with pytest.raises(Exception, match="controlled intent failure"):
        cleanup.delete_campaign(brief.campaign_id, preview["token"])
    assert state.storage.path(upload["reference"]).is_file()


def test_ownership_failure_and_foreign_attachment_do_not_create_or_steal_uploads(
    tmp_path, monkeypatch
):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    first, first_doc = intake.new()
    second, second_doc = intake.new()
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    upload = intake.upload(payload, "image/png", first)
    second_doc["value"]["brand"]["logo_path"] = upload["reference"]
    with pytest.raises(ActionError, match="selected campaign"):
        intake.save(
            second,
            BriefDraft(value=second_doc["value"], version=second_doc["version"]),
            Workspace(state),
        )
    before = set(state.storage.path("Projects").glob("*/Assets/*/*.png"))
    with state.storage.connect() as db:
        db.execute(
            "CREATE TRIGGER deny_owner BEFORE INSERT ON file_owners BEGIN SELECT "
            "RAISE(FAIL,'controlled registration failure'); END"
        )
    with pytest.raises(Exception, match="controlled registration failure"):
        intake.upload(payload, "image/png", first)
    assert set(state.storage.path("Projects").glob("*/Assets/*/*.png")) == before
    assert intake.read(first) == first_doc


def test_interrupted_cleanup_resumes_exact_files_and_does_not_block_other_campaign(
    tmp_path, monkeypatch
):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    other, _ = intake.new()
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(brief.campaign_id)
    original = Path.unlink
    calls = 0

    class ProcessInterrupted(BaseException):
        pass

    def interrupted(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProcessInterrupted()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises(ProcessInterrupted):
        cleanup.delete_campaign(brief.campaign_id, preview["token"])
    assert cleanup.status(brief.campaign_id)["pending"]
    with pytest.raises(StateError, match="being deleted"):
        Workspace(state).run_for(brief.campaign_id)
    assert intake.read(other)
    monkeypatch.setattr(Path, "unlink", original)
    reopened = State(state.storage)
    assert Cleanup(reopened).resume_all()[0]["state"] == "complete"
    assert Cleanup(reopened).resume_all() == []
    assert Intake(state.storage).read(other)


def test_cleanup_changed_file_stays_pending_and_no_parent_with_shared_file_is_removed(
    tmp_path, monkeypatch
):
    state, _, run, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    other, document = intake.new()
    source = state.plan(run).assets["products/product-1"].reference
    document["value"]["products"] = [brief.products[0].model_dump(mode="json")]
    document["value"]["products"][0]["source"]["path"] = intake.upload(
        state.storage.path(source).read_bytes(), None, other
    )["reference"]
    intake.write(other, document["value"], document["version"])
    before = state.storage.path(source).read_bytes()
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(brief.campaign_id)
    original = cleanup._resume
    monkeypatch.setattr(
        cleanup, "_resume", lambda identity: {"state": "pending", "cleanup_id": identity}
    )
    pending = cleanup.delete_campaign(brief.campaign_id, preview["token"])
    with state.storage.connect() as db:
        intent = json.loads(
            db.execute(
                "SELECT value_json FROM cleanup_intents WHERE id=?", (pending["cleanup_id"],)
            ).fetchone()[0]
        )
    assert intent["name"] == brief.name and "value" not in intent
    changed = intent["files"][0]
    state.storage.write(changed["reference"], b"changed after approval")
    monkeypatch.setattr(cleanup, "_resume", original)
    assert cleanup.retry(brief.campaign_id, pending["cleanup_id"])["state"] == "pending"
    assert state.storage.path(changed["reference"]).read_bytes() == b"changed after approval"
    assert state.storage.path(source).read_bytes() == before


def test_repeated_applies_keep_only_current_outputs_and_preserve_packages_metrics_and_saves(
    tmp_path,
):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.workspace import TextEdit

    target, archive, folder = make_package(state, run, first["revision_id"])
    zip_bytes = Handoff(state).download(target, archive["artifact_id"])
    folder_before = {
        str(p): p.read_bytes() for p in Path(folder["location"]).rglob("*") if p.is_file()
    }
    inputs = {
        a.reference: state.storage.path(a.reference).read_bytes()
        for a in state.plan(run).assets.values()
    }
    metrics = state.campaign_metrics(brief.campaign_id)
    versions = [first["revision_id"]]
    ws = Workspace(state)
    for index in range(5):
        command = TextEdit(
            ratio="1:1",
            run_id=run,
            revision_id=state.effective_revision(run),
            market="en-US",
            product_id="product-1",
            values={"message": f"Local headline {index}"},
            field_scopes={"message": "product"},
        )
        updated, plan, _ = ws.text(brief.campaign_id, command)
        versions.append(
            runner.revise(run, updated, command.revision_id, plan.digest)["revision_id"]
        )
    assert [v["id"] for v in ws.revisions(run)] == versions[-1:]
    assert state.run(run)["current_revision"] == versions[-1]
    for revision in versions[:-1]:
        assert not image_files(state, revision)
        record = state.revision(revision)
        assert state.storage.path(record["manifest_ref"]).is_file()
        assert state.storage.path(record["plan_ref"]).is_file()
        assert state.metadata_snapshot(revision)["creatives"]
        with pytest.raises(StateError, match="no longer available"):
            ws.manifest(state.run(run), revision)
    assert len(image_files(state, versions[-1])) == 6
    assert Handoff(state).download(target, archive["artifact_id"]) == zip_bytes
    assert {
        str(p): p.read_bytes() for p in Path(folder["location"]).rglob("*") if p.is_file()
    } == folder_before
    assert all(state.storage.path(ref).read_bytes() == data for ref, data in inputs.items())
    assert Cleanup(state).status(brief.campaign_id) == {
        "campaign_state": "available",
        "pending": [],
    }
    assert state.campaign_metrics(brief.campaign_id) == metrics and state.attempts(run) == []
    before = image_files(state, versions[-1])
    projects = AuthoredProjects(ws)
    for index in range(5):
        project = projects.read(brief.campaign_id)
        project["authored"]["brief"]["audience"] = f"Unfinished main-project edit {index}"
        projects.save(
            brief.campaign_id,
            AuthoredSave(**{key: project[key] for key in ("version", "base", "authored")}),
        )
    assert image_files(state, versions[-1]) == before
    assert state.revision_count(run) == len(versions)
    assert state.campaign_metrics(brief.campaign_id) == metrics


def test_partial_versions_never_retire_effective_complete_and_failed_retirement_blocks_growth(
    tmp_path, monkeypatch
):
    import campaign_studio.rendering as rendering

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))["revision_id"]
    original = rendering.compose
    calls = 0

    def partial(*args, **kwargs):
        nonlocal calls
        calls += 1
        if (calls - 1) % 6 < 3:
            raise OSError("controlled output failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(rendering, "compose", partial)
    for _ in range(3):
        assert not runner.recover_local(run)["complete"]
    assert state.run(run)["current_revision"] == first
    assert len(Workspace(state).revisions(run)) == 2 and image_files(state, first)
    monkeypatch.setattr(rendering, "compose", original)
    # A later full revision can publish even if removing an older set is denied.
    old_unlink = Path.unlink
    denied_prefix = str(state.storage.path(state.revision(first)["manifest_ref"]).parent / "images")

    def denied(path, *args, **kwargs):
        if str(path).startswith(denied_prefix):
            raise PermissionError("controlled retirement denial")
        return old_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    result = runner.recover_local(run)
    assert result["complete"] and Cleanup(state).status(brief.campaign_id)["pending"]
    with pytest.raises(StateError, match="pending image cleanup"):
        runner.recover_local(run)
    assert state.attempts(run) == []
    monkeypatch.setattr(Path, "unlink", old_unlink)
    assert Cleanup(state).resume_all()[0]["state"] == "complete"
    assert len(Workspace(state).revisions(run)) == 1


def test_campaign_cleanup_http_guards_and_server_owned_confirmation(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    result = asyncio.run(runner.execute(run))
    before = image_files(state, result["revision_id"])
    app = create_app(
        Settings(data_dir=state.storage.root),
        provider_factory=lambda: pytest.fail("provider construction"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        base = f"/api/campaigns/{brief.campaign_id}/cleanup"
        assert client.get(base + "/status").json()["campaign_state"] == "available"
        assert client.post(base + "/preview", json={}).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.post(
                base + "/preview", json={}, headers={"Origin": "https://wrong.example"}
            ).status_code
            == 403
        )
        preview = client.post(base + "/preview", json={}).json()
        assert "files" not in preview and "record_fingerprint" not in preview
        assert (
            client.post(
                base + "/confirm", json={"token": preview["token"], "path": "private/state.sqlite3"}
            ).status_code
            == 422
        )
        assert client.post(base + "/confirm", json={"token": "0" * 64}).status_code == 409
        assert image_files(state, result["revision_id"]) == before
        assert client.post("/api/campaigns/not-owned/cleanup/preview", json={}).status_code == 404
        response = client.post(base + "/confirm", json={"token": preview["token"]})
        assert response.json()["state"] == "committed"
        accepted = response.json()["cleanup_id"]
        assert client.get(base + "/status").json()["campaign_state"] == "pending"
        assert image_files(state, result["revision_id"]) == before
        response = client.post(base + f"/{accepted}/retry", json={})
        assert response.json()["state"] == "complete"
        assert response.json()["removed_bytes"] == preview["removable_bytes"]
        assert client.get(base + "/status").status_code == 404
        assert client.get(base + "/status").json()["code"] == "not_found"
        assert all(not Path(path).exists() for path in before)
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_cross_run_partial_keeps_only_latest_partial_and_necessary_complete_fallback(
    tmp_path, monkeypatch
):
    import campaign_studio.rendering as rendering

    state, runner, first_run, brief = setup_case(tmp_path, 2, True)
    complete = asyncio.run(runner.execute(first_run))["revision_id"]
    complete_bytes = image_files(state, complete)
    second_run = state.create(make_plan(brief, state.storage), 0)
    state.confirm(second_run, state.plan(second_run).digest)
    compose = rendering.compose
    calls = 0

    def partial(*args, **kwargs):
        nonlocal calls
        calls += 1
        if (calls - 1) % 6 < 3:
            raise OSError("controlled selected product failure")
        return compose(*args, **kwargs)

    monkeypatch.setattr(rendering, "compose", partial)
    partials = [asyncio.run(runner.execute(second_run))["revision_id"]]
    for _ in range(4):
        partials.append(runner.recover_local(second_run)["revision_id"])
    assert state.run(second_run)["current_revision"] is None
    assert state.effective_revision(second_run) == partials[-1]
    available = Workspace(state).revisions(second_run) + Workspace(state).revisions(first_run)
    assert {r["id"] for r in available} == {complete, partials[-1]}
    assert image_files(state, complete) == complete_bytes
    assert all(not image_files(state, revision) for revision in partials[:-1])
    assert state.revision_number(partials[-1]) == 6
    assert state.attempts(first_run) == state.attempts(second_run) == []
    monkeypatch.setattr(rendering, "compose", compose)
    final = runner.recover_local(second_run)
    assert final["complete"] and state.effective_revision(second_run) == final["revision_id"]
    assert not image_files(state, complete) and not image_files(state, partials[-1])
    assert [r["id"] for r in Workspace(state).revisions(second_run)] == [final["revision_id"]]


def test_symlink_and_unreadable_sharing_inventory_refuse_without_intent(tmp_path, monkeypatch):
    state, _, run, brief = setup_case(tmp_path, 2, True)
    cleanup = Cleanup(state)
    external = tmp_path / "original.png"
    external.write_bytes(b"external original")
    alias = state.storage.path(
        state.storage.project(brief.campaign_id).ref(f"Runs/{run}/alias.png")
    )
    state.storage.project(brief.campaign_id).claim(alias.relative_to(state.storage.root).as_posix())
    alias.symlink_to(external)
    with pytest.raises(StorageError, match="Symbolic links"):
        cleanup.preview_campaign(brief.campaign_id)
    alias.unlink()
    intake = Intake(state.storage)
    intake.new()
    protected = state.storage.path(brief.brand.logo_path)
    original = Path.read_bytes

    def denied(path):
        if path == protected:
            raise PermissionError("controlled file inventory denial")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(PermissionError):
        cleanup.preview_campaign(brief.campaign_id)
    monkeypatch.setattr(Path, "read_bytes", original)
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM cleanup_intents").fetchone()[0] == 0
    assert external.read_bytes() == b"external original"


def test_existing_history_is_not_pruned_on_upgrade_or_startup(tmp_path, monkeypatch):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    original = Cleanup.retire_after_publish
    monkeypatch.setattr(Cleanup, "retire_after_publish", lambda *a: None)
    versions = [asyncio.run(runner.execute(run))["revision_id"]]
    for _ in range(3):
        versions.append(runner.recover_local(run)["revision_id"])
    before = {v: image_files(state, v) for v in versions}
    monkeypatch.setattr(Cleanup, "retire_after_publish", original)
    app = create_app(Settings(data_dir=state.storage.root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/health").status_code == 200
        assert {v: image_files(state, v) for v in versions} == before
        assert len(Workspace(state).revisions(run)) == 4
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.post(
                f"/api/campaigns/{brief.campaign_id}/retention/acknowledge", json={}
            ).status_code
            == 404
        )
    with state.storage.connect() as db:
        assert "retention_acknowledged" not in {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    newer = runner.recover_local(run)["revision_id"]
    assert [r["id"] for r in Workspace(state).revisions(run)] == [newer]
    assert state.run(run)["current_revision"] == newer
    assert all(not image_files(state, revision) for revision in versions)


def test_upload_publication_error_never_leaves_a_new_unowned_file(tmp_path, monkeypatch):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    campaign, _ = intake.new()
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    original = state.storage.write_new

    def fail_after_publication(reference, data):
        original(reference, data)
        raise OSError("controlled directory fsync error")

    monkeypatch.setattr(state.storage, "write_new", fail_after_publication)
    with pytest.raises(ActionError):
        intake.upload(payload, "image/png", campaign)
    assert (
        list(state.storage.path(state.storage.project(campaign).ref("Assets")).rglob("*.png")) == []
    )
    with state.storage.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM uploads JOIN file_owners USING(reference) "
                "WHERE campaign_id=?",
                (campaign,),
            ).fetchone()[0]
            == 0
        )


def test_empty_failure_does_not_displace_current_images(tmp_path, monkeypatch):
    import campaign_studio.rendering as rendering

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    versions = [asyncio.run(runner.execute(run))["revision_id"]]
    for _ in range(2):
        versions.append(runner.recover_local(run)["revision_id"])
    before = {v: image_files(state, v) for v in versions}
    monkeypatch.setattr(
        rendering,
        "compose",
        lambda *a, **k: (_ for _ in ()).throw(OSError("controlled all-output failure")),
    )
    failed = runner.recover_local(run)
    assert failed["metrics"]["completed_images"] == 0
    assert state.revision(failed["revision_id"])["image_count"] == 0
    assert {v: image_files(state, v) for v in versions} == before
    assert len(Workspace(state).revisions(run)) == 1
    assert state.run(run)["current_revision"] == versions[-1]


def test_same_photo_is_copied_into_each_project_and_deleted_independently(tmp_path):
    state, _, run, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    other, document = intake.new()
    shared = state.plan(run).assets["products/product-1"].reference
    original = state.storage.path(shared).read_bytes()
    product = brief.products[0].model_dump(mode="json")
    product["source"]["path"] = shared
    document["value"]["products"] = [product]
    with pytest.raises(StorageError, match="belonging to this campaign"):
        intake.write(other, document["value"], document["version"])
    copied = intake.upload(original, None, other)["reference"]
    product["source"]["path"] = copied
    intake.write(other, document["value"], document["version"])
    cleanup = Cleanup(state)
    assert (
        cleanup.delete_campaign(
            brief.campaign_id, cleanup.preview_campaign(brief.campaign_id)["token"]
        )["state"]
        == "complete"
    )
    assert not state.storage.path(shared).exists()
    assert state.storage.path(copied).read_bytes() == original
    assert intake.read(other)["value"]["products"][0]["source"]["path"] == copied
    assert (
        cleanup.delete_campaign(other, cleanup.preview_campaign(other)["token"])["state"]
        == "complete"
    )
    assert not state.storage.path(copied).exists()


def test_one_pending_failure_does_not_stop_other_cleanup_and_never_widens_scope(
    tmp_path, monkeypatch
):
    state, _, run, brief = setup_case(tmp_path, 2, True)
    other, _ = Intake(state.storage).new()
    cleanup = Cleanup(state)
    original = cleanup._resume
    monkeypatch.setattr(
        cleanup, "_resume", lambda identity: {"state": "pending", "cleanup_id": identity}
    )
    first = cleanup.delete_campaign(
        brief.campaign_id, cleanup.preview_campaign(brief.campaign_id)["token"]
    )
    second = cleanup.delete_campaign(other, cleanup.preview_campaign(other)["token"])
    with state.storage.connect() as db:
        intent = json.loads(
            db.execute(
                "SELECT value_json FROM cleanup_intents WHERE id=?", (first["cleanup_id"],)
            ).fetchone()[0]
        )
    victim = intent["files"][0]["reference"]
    state.storage.write(victim, b"changed since confirmation")
    sentinel = state.storage.project(brief.campaign_id).ref(f"Runs/{run}/not-in-approved-list.txt")
    state.storage.write(sentinel, b"keep this new file")
    monkeypatch.setattr(cleanup, "_resume", original)
    results = {item["cleanup_id"]: item["state"] for item in cleanup.resume_all()}
    assert results == {first["cleanup_id"]: "pending", second["cleanup_id"]: "complete"}
    assert state.storage.path(sentinel).read_bytes() == b"keep this new file"
    assert state.storage.path(victim).read_bytes() == b"changed since confirmation"
    assert all(item["campaign_id"] != other for item in cleanup.pending())


def test_protected_complete_does_not_hide_a_shared_partial_retirement_candidate(
    tmp_path, monkeypatch
):
    import campaign_studio.rendering as rendering
    from campaign_studio.brief import Brief

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    complete = asyncio.run(runner.execute(run))
    later = state.create(make_plan(brief, state.storage), 0)
    state.confirm(later, state.plan(later).digest)
    compose = rendering.compose
    calls = 0

    def partial(*args, **kwargs):
        nonlocal calls
        calls += 1
        if (calls - 1) % 6 < 3:
            raise OSError("controlled missing product")
        return compose(*args, **kwargs)

    monkeypatch.setattr(rendering, "compose", partial)
    first = asyncio.run(runner.execute(later))
    shared = next(item for item in first["outputs"] if item["file"])
    prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    reference = f"{prefix}/{shared['file']}"
    intake = Intake(state.storage)
    other = brief.campaign_id
    value = brief.model_dump(mode="json")
    value["campaign_id"] = other
    value["products"][0]["source"].update(path=reference, subject_box=None)
    intake.write(other, value, None)
    assert not make_plan(Brief.model_validate(value), state.storage).blocked
    versions = [complete["revision_id"], first["revision_id"]]
    before = {v: image_files(state, v) for v in versions}
    with pytest.raises(StateError, match="used as an input"):
        runner.recover_local(later)
    assert {v: image_files(state, v) for v in versions} == before
    assert state.revision_count(later) == 1 and calls == 6
    assert not make_plan(Brief.model_validate(value), state.storage).blocked
    assert not Cleanup(state).status(brief.campaign_id)["pending"]
    # Once the reference is deliberately removed, the exact same operation can
    # retire P1, keeping only the older complete fallback and newest partial.
    document = intake.read(other)
    document["value"]["products"][0]["source"]["path"] = brief.products[0].source.path
    intake.write(other, document["value"], document["version"])
    second = runner.recover_local(later)
    assert state.revision(first["revision_id"])["images_retired"]
    assert {r["id"] for r in Workspace(state).revisions(later)} == {
        second["revision_id"],
    }
    assert image_files(state, complete["revision_id"]) == before[complete["revision_id"]]
    assert state.attempts(run) == state.attempts(later) == []


def test_current_authored_input_in_same_campaign_blocks_superseded_png_cleanup(tmp_path):
    from campaign_studio.authored import AuthoredProjects, AuthoredSave

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    before = image_files(state, first["revision_id"])
    prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    reference = f"{prefix}/{first['outputs'][0]['file']}"
    # This existing saved input may come from an imported brief. Its current
    # authored form is authoritative even when the validated brief differs.
    intake = Intake(state.storage)
    value = brief.model_dump(mode="json")
    value["products"][0]["source"].update(path=reference, subject_box=None)
    intake.write(brief.campaign_id, value, None)
    projects = AuthoredProjects(Workspace(state))
    project = projects.read(brief.campaign_id)
    project["authored"]["brief"]["products"][0]["source"]["path"] = reference
    saved = projects.save(
        brief.campaign_id,
        AuthoredSave(**{key: project[key] for key in ("version", "base", "authored")}),
    )
    with pytest.raises(StateError, match="used as an input"):
        runner.recover_local(run)
    assert image_files(state, first["revision_id"]) == before
    assert state.revision_count(run) == 1 and not Cleanup(state).pending()
    # Resolving only the current input releases the obsolete PNG, without a
    # permanent pin from its saved validated projection or historical plan.
    project = projects.read(brief.campaign_id)
    assert project["version"] == saved["version"]
    project["authored"]["brief"]["products"][0]["source"]["path"] = brief.products[0].source.path
    projects.save(
        brief.campaign_id,
        AuthoredSave(**{key: project[key] for key in ("version", "base", "authored")}),
    )
    newer = runner.recover_local(run)
    assert newer["complete"] and not image_files(state, first["revision_id"])
    assert not state.attempts(run)


def test_late_authored_input_is_refused_before_commit_then_explicit_logo_retry_succeeds(
    tmp_path, monkeypatch
):
    from test_authored import action
    from test_logo_editing import command, replacement

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    cid = brief.campaign_id
    before = image_files(state, first["revision_id"])
    prefix = state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
    reference = f"{prefix}/{first['outputs'][0]['file']}"
    intake = Intake(state.storage)
    # A previously authorized current-output reference can be restored by Save.
    # Picker uploads copy assets, so they do not create this shared dependency.
    value = brief.model_dump(mode="json")
    value["products"][0]["source"].update(path=reference, subject_box=None)
    intake.write(cid, value, None)
    upload = intake.upload(
        state.storage.path(brief.products[0].source.path).read_bytes(), "image/png", cid
    )["reference"]
    chosen = replacement(state, cid, (400, 300))
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Local Apply must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        authored_url = f"/api/motif/campaigns/{cid}/authored"
        draft = action(client, cid)
        draft["authored"]["brief"]["products"][0]["source"]["path"] = upload
        draft["authored"]["brief"]["audience"] = "  Keep unfinished work  "
        draft["authored"]["form_drafts"] = {
            "fields": {"brief/copy/en": {"copy-headline": "Keep this"}}
        }
        saved = client.post(authored_url, json=draft)
        assert saved.status_code == 200, saved.text
        untouched_logo = intake.read(cid)["value"]["brand"]
        Cleanup(state).prepare_retirement(cid)

        def preview():
            edit = command(
                state, run, replacement=chosen, draft_version=action(client, cid)["version"]
            ).model_dump(mode="json")
            result = client.post(f"/api/campaigns/{cid}/logo/preview", json=edit)
            assert result.status_code == 200 and not result.json()["blocked"], result.text
            return edit | {"preview_digest": result.json()["digest"]}

        edit = preview()
        publisher = app.state.workspace.state
        original = publisher.publish_revision

        def restore_input(*args, **kwargs):
            latest = action(client, cid)
            latest["authored"]["brief"]["products"][0]["source"]["path"] = reference
            restored = client.post(authored_url, json=latest)
            assert restored.status_code == 200, restored.text
            return original(*args, **kwargs)

        monkeypatch.setattr(publisher, "publish_revision", restore_input)
        refused = client.post(f"/api/campaigns/{cid}/logo/apply", json=edit)
        assert refused.status_code == 409 and "used as an input" in refused.json()["message"]
        assert state.effective_revision(run) == first["revision_id"]
        assert state.revision_count(run) == 1 and not Cleanup(state).pending()
        assert image_files(state, first["revision_id"]) == before
        assert intake.read(cid)["value"]["brand"] == untouched_logo
        latest = action(client, cid)
        assert latest["authored"]["brief"]["products"][0]["source"]["path"] == reference

        monkeypatch.setattr(publisher, "publish_revision", original)
        latest["authored"]["brief"]["products"][0]["source"]["path"] = upload
        assert client.post(authored_url, json=latest).status_code == 200
        applied = client.post(f"/api/campaigns/{cid}/logo/apply", json=preview())
        assert applied.status_code == 200 and applied.json()["complete"], applied.text
        assert state.revision_count(run) == 2 and not image_files(state, first["revision_id"])
        current = state.plan(run).brief.brand.logo_path
        assert intake.read(cid)["value"]["brand"]["logo_path"] == current
        reopened = action(client, cid)["authored"]
        assert reopened["brief"]["brand"]["logo_path"] == current
        assert reopened["brief"]["audience"] == "  Keep unfinished work  "
        assert reopened["form_drafts"] == draft["authored"]["form_drafts"]
        assert state.storage.path(upload).is_file()
        assert state.storage.path(current).read_bytes() == state.storage.path(chosen).read_bytes()
        assert not state.attempts(run)


def test_save_after_commit_waits_for_cleanup_and_gets_real_base_conflict(tmp_path, monkeypatch):
    import fcntl
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from test_authored import action
    from test_logo_editing import command, replacement

    from campaign_studio.authored import AuthoredProjects

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    cid = brief.campaign_id
    chosen = replacement(state, cid, (400, 300))
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Provider forbidden"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client, ThreadPoolExecutor(1) as pool:
        client.headers["X-Campaign-Token"] = app.state.csrf
        url = f"/api/motif/campaigns/{cid}/authored"
        draft = action(client, cid)
        assert client.post(url, json=draft).status_code == 200
        draft = action(client, cid)
        edit = command(state, run, replacement=chosen, draft_version=draft["version"]).model_dump(
            mode="json"
        )
        preview = client.post(f"/api/campaigns/{cid}/logo/preview", json=edit)
        assert preview.status_code == 200 and not preview.json()["blocked"]
        edit["preview_digest"] = preview.json()["digest"]
        entered = Event()
        original_save, original_cleanup = AuthoredProjects.save, Cleanup.retire_after_publish
        flights = []

        def save(projects, *args, **kwargs):
            entered.set()
            return original_save(projects, *args, **kwargs)

        def cleanup(cleaner, *args, **kwargs):
            assert state.effective_revision(run) != first["revision_id"]
            # Same guard protects the committed interval through intent selection.
            with (
                state.storage.path("private/authored.lock").open("ab") as lock,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            draft["authored"]["brief"]["audience"] = "A late competing tab"
            flights.append(pool.submit(client.post, url, json=draft))
            assert entered.wait(5)
            assert not flights[0].done()
            return original_cleanup(cleaner, *args, **kwargs)

        monkeypatch.setattr(AuthoredProjects, "save", save)
        monkeypatch.setattr(Cleanup, "retire_after_publish", cleanup)
        applied = client.post(f"/api/campaigns/{cid}/logo/apply", json=edit)
        assert applied.status_code == 200 and applied.json()["complete"], applied.text
        conflicted = flights[0].result(timeout=5)
        assert conflicted.status_code == 409 and conflicted.json()["code"] == "authored_conflict"
        assert not image_files(state, first["revision_id"]) and not Cleanup(state).pending()
        current = state.plan(run).brief.brand.logo_path
        assert Intake(state.storage).read(cid)["value"]["brand"]["logo_path"] == current
        reopened = action(client, cid)["authored"]["brief"]
        assert reopened["brand"]["logo_path"] == current
        assert reopened["audience"] != "A late competing tab"
        assert not state.attempts(run)


def test_failed_publication_does_not_remove_previous_output_or_package(tmp_path):
    import sqlite3

    state, runner, run, _ = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    target, archive, _ = make_package(state, run, first["revision_id"])
    before = image_files(state, first["revision_id"])
    package = Handoff(state).download(target, archive["artifact_id"])
    with state.storage.connect() as db:
        db.execute(
            "CREATE TRIGGER deny_publication BEFORE INSERT ON revisions "
            "BEGIN SELECT RAISE(FAIL,'controlled publication failure'); END"
        )
    with pytest.raises(sqlite3.Error, match="controlled publication failure"):
        runner.recover_local(run)
    assert state.effective_revision(run) == first["revision_id"]
    assert state.revision_count(run) == 1 and not Cleanup(state).pending()
    assert image_files(state, first["revision_id"]) == before
    assert Handoff(state).download(target, archive["artifact_id"]) == package
    with state.storage.connect() as db:
        db.execute("DROP TRIGGER deny_publication")
    newer = runner.recover_local(run)
    assert newer["complete"] and not image_files(state, first["revision_id"])
    assert Handoff(state).download(target, archive["artifact_id"]) == package
    assert not state.attempts(run)


def test_zero_output_diagnostic_preserves_usable_partial_as_current(tmp_path, monkeypatch):
    import campaign_studio.rendering as rendering
    from campaign_studio.review import CreativeUpdate

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    compose = rendering.compose
    count = 0

    def partial(*args, **kwargs):
        nonlocal count
        count += 1
        if count <= 3:
            raise OSError("controlled partial fixture")
        return compose(*args, **kwargs)

    monkeypatch.setattr(rendering, "compose", partial)
    first = asyncio.run(runner.execute(run))
    before = image_files(state, first["revision_id"])
    monkeypatch.setattr(
        rendering, "compose", lambda *a, **k: (_ for _ in ()).throw(OSError("zero"))
    )
    empty = runner.recover_local(run)
    assert empty["metrics"]["completed_images"] == 0
    assert state.effective_revision(run) == first["revision_id"]
    assert state.run(run)["current_revision"] is None
    item = next(item for item in first["outputs"] if item["file"])
    target = RevisionTarget(
        campaign_id=brief.campaign_id, run_id=run, revision_id=first["revision_id"], mode="current"
    )
    state.update_creative(
        first["revision_id"],
        item["id"],
        CreativeUpdate(expected_version=0, description="Retained partial."),
        target=target,
    )
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Provider forbidden"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        data = client.get(f"/api/motif/campaigns/{brief.campaign_id}").json()
        status = client.get(f"/api/campaigns/{brief.campaign_id}/status").json()
        assert data["can_edit"] and data["can_describe"]
        assert data["base_revision"] == status["revision_id"] == first["revision_id"]
        assert status["completed"] == 3
    assert image_files(state, first["revision_id"]) == before
    assert state.revision(empty["revision_id"])["image_count"] == 0


def test_pending_deletion_closes_cached_preview_and_owned_upload_reads(tmp_path, monkeypatch):
    from campaign_studio.workspace import TextEdit

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    intake = Intake(state.storage)
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    owned = intake.upload(payload, "image/png", brief.campaign_id)
    other, document = intake.new()
    shared = intake.upload(payload, "image/png", other)
    document["value"]["brand"]["logo_path"] = shared["reference"]
    intake.write(other, document["value"], document["version"])
    edit = TextEdit(
        ratio="1:1",
        run_id=run,
        revision_id=first["revision_id"],
        market="en-US",
        product_id="product-2",
        field_scopes={"message": "product", "cta": "product"},
        values={"message": "A retained draft", "cta": "Explore"},
    )
    _, plan, impact = Workspace(state).text(brief.campaign_id, edit)
    app = create_app(Settings(data_dir=state.storage.root))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        url = app.state.previews.render(brief.campaign_id, edit, plan, impact)[0]["url"]
        assert client.get(url).status_code == 200
        assert client.get(owned["url"]).status_code == 200
        original = Cleanup._resume
        monkeypatch.setattr(
            Cleanup, "_resume", lambda self, identity: {"state": "pending", "cleanup_id": identity}
        )
        cleanup = Cleanup(state)
        pending = cleanup.delete_campaign(
            brief.campaign_id, cleanup.preview_campaign(brief.campaign_id)["token"]
        )
        assert state.storage.path(owned["reference"]).is_file()
        assert client.get(url).status_code == 404
        assert client.get(owned["url"]).status_code == 404
        assert client.get(shared["url"]).content == payload
        monkeypatch.setattr(Cleanup, "_resume", original)
        assert cleanup.retry(brief.campaign_id, pending["cleanup_id"])["state"] == "complete"
        assert client.get(url).status_code == 404
        assert client.get(owned["url"]).status_code == 404
        assert client.get(shared["url"]).content == payload


@pytest.mark.parametrize("case_alias", [False, True], ids=["exact", "case-alias"])
def test_imported_prose_cannot_inventory_or_delete_another_campaigns_upload(tmp_path, case_alias):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    reference = intake.upload(payload, "image/png", brief.campaign_id)["reference"]
    prose = reference[:-3] + "PNG" if case_alias else reference
    campaign = intake.new_campaign_id()
    imported = intake.import_value(campaign, brief.model_dump(mode="json") | {"audience": prose})
    intake.create(campaign, imported)
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(campaign)
    result = cleanup.delete_campaign(campaign, preview["token"])
    assert result["state"] == "complete"
    assert state.storage.path(reference).read_bytes() == payload
    assert preview["unmanaged_file_count"] == 0 and preview["source_file_count"] == 0
    with state.storage.connect() as db:
        assert db.execute(
            "SELECT campaign_id FROM file_owners WHERE reference=?", (reference,)
        ).fetchall() == [(brief.campaign_id,)]


def test_surviving_prose_does_not_take_ownership_of_deleted_campaigns_upload(tmp_path):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    reference = intake.upload(
        state.storage.path(brief.brand.logo_path).read_bytes(), "image/png", brief.campaign_id
    )["reference"]
    survivor, _ = intake.new(audience=reference)
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(brief.campaign_id)
    assert cleanup.delete_campaign(brief.campaign_id, preview["token"])["state"] == "complete"
    assert not state.storage.path(reference).exists()
    assert intake.read(survivor)["value"]["audience"] == reference
    with state.storage.connect() as db:
        assert (
            db.execute("SELECT 1 FROM file_owners WHERE reference=?", (reference,)).fetchall() == []
        )


@pytest.mark.parametrize("case_alias", [False, True], ids=["exact", "case-alias"])
def test_resume_does_not_unlink_foreign_upload_in_older_confirmed_inventory(tmp_path, case_alias):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    reference = intake.upload(payload, "image/png", brief.campaign_id)["reference"]
    prose = reference[:-3] + "PNG" if case_alias else reference
    target, _ = intake.new(audience=prose)
    cleanup = Cleanup(state)
    inventory = cleanup._campaign_inventory(target)
    draft = state.storage.path(state.storage.project(target).draft)
    draft_bytes = draft.read_bytes()
    # An older deletion list could include a filename copied from a note.
    inventory["files"] = [item for item in inventory["files"] if item["reference"] != prose]
    inventory["files"].append(
        {"reference": prose, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    )
    cleanup._commit_and_resume(inventory, defer_cleanup=True)
    result = Cleanup(State(state.storage)).resume_all()[0]
    assert result["state"] == "pending"
    assert state.storage.path(reference).read_bytes() == payload
    assert draft.read_bytes() == draft_bytes
    assert cleanup.status(target)["pending"]


def test_foreign_attachment_alias_is_refused_and_original_bytes_survive(tmp_path):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    payload = state.storage.path(brief.brand.logo_path).read_bytes()
    reference = intake.upload(payload, "image/png", brief.campaign_id)["reference"]
    other, document = intake.new()
    for selected in (
        reference,
        reference[:-3] + "PNG",
        reference.replace("Projects/", "PROJECTS/"),
    ):
        document["value"]["brand"]["logo_path"] = selected
        with pytest.raises(StorageError):
            intake.write(other, document["value"], document["version"])
    cleanup = Cleanup(state)
    assert (
        cleanup.delete_campaign(other, cleanup.preview_campaign(other)["token"])["state"]
        == "complete"
    )
    assert state.storage.path(reference).read_bytes() == payload
