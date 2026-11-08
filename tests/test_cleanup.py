import asyncio
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

import pytest
from campaign_fixtures import legacy_report, legacy_zip
from fastapi.testclient import TestClient
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.review import RevisionTarget
from campaign_studio.state import State, StateError
from campaign_studio.storage import WORKSPACE_SCHEMA, StorageError
from campaign_studio.workspace import ActionError, Workspace


def image_files(state, revision):
    if not state.identity_exists("revisions", revision):
        return {}
    record = state.revision(revision)
    if record["manifest_json"] is None:
        return {}
    # Enumerate the physical files referenced by this snapshot; unchanged PNGs
    # can be shared with a newer snapshot. The available sets are tracked in SQL.
    folder = state.storage.path(record["manifest_ref"]).parent
    paths = [
        folder / item["file"] for item in state.revision_manifest(record)["outputs"] if item["file"]
    ]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def owned_files(state, campaign_id):
    with state.storage.connect() as db:
        refs = [
            row[0]
            for row in db.execute(
                "SELECT reference FROM file_owners WHERE campaign_id=?", (campaign_id,)
            )
        ]
    return {
        str(state.storage.path(ref)): hashlib.sha256(
            state.storage.path(ref).read_bytes()
        ).hexdigest()
        for ref in refs
        if state.storage.path(ref).is_file()
    }


def legacy_zip_and_folder(state, run, revision):
    target = RevisionTarget(
        campaign_id=state.run(run)["campaign_id"],
        run_id=run,
        revision_id=revision,
        mode="saved_version",
    )
    return target, legacy_zip(state, target), legacy_report(state, target)


@pytest.mark.parametrize("metadata_added", ["before_preview", "after_preview", "after_commit"])
def test_campaign_deletion_removes_finder_metadata_and_empty_folders(tmp_path, metadata_added):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    intake = Intake(state.storage)
    intake.write(
        brief.campaign_id, brief.model_dump(mode="json"), intake.read(brief.campaign_id)["version"]
    )
    other, _ = intake.new()
    survivor = intake.read(other)
    project = state.storage.project(brief.campaign_id)
    root = state.storage.path(f"Campaigns/{project.folder}")
    folders = (root, root / "Assets", root / "Brief")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)

    def add_metadata():
        for folder in folders:
            (folder / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1" + b"Finder settings")

    cleanup = Cleanup(state)
    if metadata_added == "before_preview":
        add_metadata()
    preview = cleanup.preview_campaign(brief.campaign_id)
    assert preview["unmanaged_file_count"] == 0
    if metadata_added == "after_preview":
        add_metadata()
    cleanup.delete_campaign(brief.campaign_id, preview["token"], defer_cleanup=True)
    if metadata_added == "after_commit":
        add_metadata()
    result = Cleanup(State(state.storage)).resume_all()
    assert result[0]["state"] == "complete"
    assert not root.exists()
    assert intake.read(other) == survivor
    with state.storage.connect() as db:
        for table in ("campaigns", "campaign_drafts", "runs", "file_owners", "cleanup_intents"):
            column = "id" if table == "campaigns" else "campaign_id"
            assert (
                db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (brief.campaign_id,)
                ).fetchone()[0]
                == 0
            )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM uploads WHERE reference LIKE ?",
                (f"Campaigns/{project.folder}/%",),
            ).fetchone()[0]
            == 0
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("kind", ["notes", "not_finder", "symlink"])
def test_campaign_deletion_preserves_unmanaged_files_and_aliases(tmp_path, kind):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    root = state.storage.path(f"Campaigns/{state.storage.project(brief.campaign_id).folder}")
    metadata = root / ".DS_Store"
    external = tmp_path / "outside.bin"
    signature = b"\x00\x00\x00\x01Bud1"
    external.write_bytes(signature)
    if kind == "symlink":
        try:
            metadata.symlink_to(external)
        except OSError:
            pytest.skip("Creating symlinks is unavailable on this host.")
    elif kind == "not_finder":
        metadata.write_bytes(b"User file with a Finder filename")
    else:
        metadata.write_bytes(signature)
        (root / "producer notes.txt").write_text("Keep these notes.")
    before = metadata.read_bytes()
    cleaner = Cleanup(state)
    preview = cleaner.preview_campaign(brief.campaign_id)
    assert preview["unmanaged_file_count"] == 1
    assert cleaner.delete_campaign(brief.campaign_id, preview["token"])["state"] == "complete"
    assert metadata.read_bytes() == before
    assert external.read_bytes() == signature
    if kind == "symlink":
        assert metadata.is_symlink()
    if kind == "notes":
        assert (root / "producer notes.txt").read_text() == "Keep these notes."


def test_finder_metadata_removal_failure_remains_pending_for_retry(tmp_path, monkeypatch):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    root = state.storage.path(f"Campaigns/{state.storage.project(brief.campaign_id).folder}")
    metadata = root / ".DS_Store"
    metadata.write_bytes(b"\x00\x00\x00\x01Bud1")
    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == metadata:
            raise PermissionError("Finder metadata is held open")
        return unlink(path, *args, **kwargs)

    cleaner = Cleanup(state)
    preview = cleaner.preview_campaign(brief.campaign_id)
    monkeypatch.setattr(Path, "unlink", locked)
    result = cleaner.delete_campaign(brief.campaign_id, preview["token"])
    assert result["state"] == "pending"
    assert cleaner.status(brief.campaign_id)["campaign_state"] == "pending"
    monkeypatch.setattr(Path, "unlink", unlink)
    assert cleaner.retry(brief.campaign_id, result["cleanup_id"])["state"] == "complete"
    assert not root.exists()


def test_owned_replaced_uploads_delete_but_shared_and_external_files_survive(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    assert asyncio.run(runner.execute(run))["complete"]
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
    from campaign_studio.delivery import prepare_current_zip

    with prepare_current_zip(
        state, Workspace(state).current_composition(brief.campaign_id)
    ) as package:
        downloaded = tmp_path / "delivered.zip"
        downloaded.write_bytes(package.stream.read())
    delivered_bytes = downloaded.read_bytes()
    original = state.storage.path(brief.brand.logo_path).read_bytes()
    external = tmp_path / "user-original-logo.png"
    external.write_bytes(original)
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(brief.campaign_id)
    assert preview["output_png_count"] == 0
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
    assert downloaded.read_bytes() == delivered_bytes
    assert not project_root.exists()
    with state.storage.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM runs WHERE campaign_id=?", (brief.campaign_id,)
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM package_exports").fetchone() == (0,)
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
    Intake(state.storage).write(
        brief.campaign_id,
        brief.model_dump(mode="json"),
        Intake(state.storage).read(brief.campaign_id)["version"],
    )
    before = cleanup.preview_campaign(brief.campaign_id)
    assert before["name"] == brief.name
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
    before = set(state.storage.path("Campaigns").glob("*/Assets/*/*.png"))
    with state.storage.connect() as db:
        db.execute(
            "CREATE TRIGGER deny_owner BEFORE INSERT ON file_owners BEGIN SELECT "
            "RAISE(FAIL,'controlled registration failure'); END"
        )
    with pytest.raises(Exception, match="controlled registration failure"):
        intake.upload(payload, "image/png", first)
    assert set(state.storage.path("Campaigns").glob("*/Assets/*/*.png")) == before
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


def test_campaign_cleanup_http_guards_and_server_owned_confirmation(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    before = owned_files(state, brief.campaign_id)
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
        assert owned_files(state, brief.campaign_id) == before
        assert client.post("/api/campaigns/not-owned/cleanup/preview", json={}).status_code == 404
        response = client.post(base + "/confirm", json={"token": preview["token"]})
        assert response.json()["state"] == "committed"
        accepted = response.json()["cleanup_id"]
        assert client.get(base + "/status").json()["campaign_state"] == "pending"
        assert owned_files(state, brief.campaign_id) == before
        response = client.post(base + f"/{accepted}/retry", json={})
        assert response.json()["state"] == "complete"
        assert response.json()["removed_bytes"] == preview["removable_bytes"]
        assert client.get(base + "/status").status_code == 404
        assert client.get(base + "/status").json()["code"] == "not_found"
        assert all(not Path(path).exists() for path in before)
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


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


def test_pending_deletion_closes_cached_preview_and_owned_upload_reads(tmp_path, monkeypatch):
    from campaign_studio.workspace import TextEdit

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    current = Workspace(state).current_composition(brief.campaign_id)
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
        revision_id=current["version"],
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
        assert client.get(url).status_code in {404, 409}
        assert client.get(owned["url"]).status_code in {404, 409}
        assert client.get(shared["url"]).content == payload
        monkeypatch.setattr(Cleanup, "_resume", original)
        assert cleanup.retry(brief.campaign_id, pending["cleanup_id"])["state"] == "complete"
        assert client.get(url).status_code in {404, 409}
        assert client.get(owned["url"]).status_code in {404, 409}
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
    with state.storage.connect() as db:
        draft = db.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (target,)
        ).fetchone()[0]
    # An older deletion list could include a filename copied from a note.
    inventory["files"] = [item for item in inventory["files"] if item["reference"] != prose]
    inventory["files"].append(
        {"reference": prose, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    )
    cleanup._commit_and_resume(inventory, defer_cleanup=True)
    result = Cleanup(State(state.storage)).resume_all()[0]
    assert result["state"] == "pending"
    assert state.storage.path(reference).read_bytes() == payload
    with state.storage.connect() as db:
        assert (
            db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (target,)
            ).fetchone()[0]
            == draft
        )
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
        reference.replace("Campaigns/", "CAMPAIGNS/"),
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


@pytest.fixture
def v020_workspace(tmp_path):
    import shutil
    from types import SimpleNamespace
    from uuid import uuid4

    from campaign_studio.rendering import render_revision

    state, _, run, brief = setup_case(tmp_path, 2, True)
    plan = state.plan(run)
    first = render_revision(state, run, plan)["revision_id"]
    target, archive, folder = legacy_zip_and_folder(state, run, first)
    zip_bytes = Handoff(state).download(target, archive["artifact_id"])
    current = render_revision(state, run, plan)["revision_id"]
    storage = state.storage
    project = storage.project(brief.campaign_id)
    outputs = storage.path(project.ref("Outputs"))
    originals = {}
    # Build the previous format from the same campaign: independent revision
    # directories, a run pinned to a retry plan, and no SQL document snapshots.
    for identity in (first, current):
        record = state.revision(identity)
        manifest = state.revision_manifest(record)
        base = f"{project.run(run)}/Revisions/{identity}"
        project.claim(base)
        storage.path(base).mkdir(parents=True)
        for item in manifest["outputs"]:
            image = outputs / item["file"]
            ratio = item["ratio"].replace(":", "x")
            old_relative = f"images/{item['product_id']}/{ratio}/{item['market']}.png"
            item["file"] = old_relative
            if identity == current:
                originals[item["id"]] = image.read_bytes()
                project.write(f"{base}/{old_relative}", originals[item["id"]])
        old_plan, old_manifest = f"{base}/plan.json", f"{base}/manifest.json"
        project.write(old_plan, record["plan_json"].encode())
        project.write(old_manifest, json.dumps(manifest).encode())
        with storage.connect() as db:
            db.execute(
                "UPDATE revisions SET plan_json=NULL,manifest_json=NULL,"
                "plan_ref=?,manifest_ref=? WHERE id=?",
                (old_plan, old_manifest, identity),
            )
    abandoned = f"{project.run(run)}/Revisions/{uuid4().hex}"
    project.claim(abandoned)
    project.write(f"{abandoned}/unfinished.tmp", b"owned partial write")
    keep = storage.path(abandoned) / "producer notes.txt"
    keep.write_bytes(b"unmanaged notes")
    shutil.rmtree(outputs)
    with storage.connect() as db:
        db.execute(
            "DELETE FROM file_owners WHERE reference LIKE ?", (project.ref("Outputs") + "/%",)
        )
        db.execute("UPDATE runs SET plan_ref=? WHERE id=?", (old_plan, run))
        row = db.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (brief.campaign_id,)
        ).fetchone()
        if row:
            document = json.loads(row[0])
            document.pop("composition_schema", None)
            db.execute(
                "UPDATE campaign_drafts SET document_json=? WHERE campaign_id=?",
                (json.dumps(document), brief.campaign_id),
            )
        db.execute("PRAGMA user_version=9")

    return SimpleNamespace(
        storage=storage,
        project=project,
        run=run,
        first=first,
        current=current,
        keep=keep,
        outputs=outputs,
        old_manifest=old_manifest,
        archive=archive,
        folder=folder,
        target=target,
        zip_bytes=zip_bytes,
        originals=originals,
        cid=brief.campaign_id,
    )


@pytest.mark.parametrize("interrupted", ["before_commit", "after_commit"])
def test_v020_upgrade_resumes_current_outputs_and_preserves_exports(
    v020_workspace, monkeypatch, interrupted
):
    case = v020_workspace
    storage, current, first, run, keep = case.storage, case.current, case.first, case.run, case.keep
    target, archive, zip_bytes = case.target, case.archive, case.zip_bytes
    outputs, old_manifest = case.outputs, case.old_manifest

    def interruption(*args, **kwargs):
        raise SystemExit("controlled interruption")

    with monkeypatch.context() as patch:
        if interrupted == "before_commit":
            patch.setattr(Cleanup, "record_intent", staticmethod(interruption))
        else:
            patch.setattr(Cleanup, "_resume", interruption)
        with pytest.raises(SystemExit, match="controlled interruption"):
            State(storage)
    assert storage.path(old_manifest).is_file()
    with storage.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (WORKSPACE_SCHEMA,)
        assert set(db.execute("SELECT name FROM workspace_migrations").fetchall()) == {
            ("campaign_outputs",),
            ("database_plans",),
            ("completed_runs",),
        }
    reopened = State(storage)
    reopened.reconcile_interrupted()
    assert reopened.effective_revision(run) is None
    snapshot = Workspace(reopened).current_composition(case.cid)
    assert len(snapshot["plan"].outputs) == 6 and len(snapshot["sources"]) == 2
    assert not Cleanup(reopened).pending()
    assert Handoff(reopened).download(target, archive["artifact_id"]) == zip_bytes
    assert keep.read_bytes() == b"unmanaged notes"
    assert set(keep.parent.iterdir()) == {keep}
    assert set(keep.parent.parent.iterdir()) == {keep.parent}
    assert not storage.path(old_manifest).exists()
    assert not (outputs / "manifest.json").exists()
    assert not (outputs / "plan.json").exists()
    assert reopened.revision_plan(reopened.revision(first)).brief.campaign_id == case.cid
    assert reopened.plan(run).brief.campaign_id == case.cid
    assert reopened.source_generation(run) == {}
    with pytest.raises(StateError, match="complete"):
        reopened.plan(run, current=False)
    assert not outputs.exists()
    assert not reopened.identity_exists("revisions", current)
    # Only package-pinned history remains, as a reader for an already delivered ZIP.
    assert reopened.revision(first)["images_retired"]
    with storage.connect() as db:
        assert not db.execute("SELECT 1 FROM workspace_migrations").fetchone()
        assert not db.execute(
            "SELECT 1 FROM file_owners WHERE reference LIKE '%/Revisions/%'"
        ).fetchone()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("missing", ["plan.json", "manifest.json"])
def test_retired_damaged_history_does_not_block_healthy_saved_images(v020_workspace, missing):
    case = v020_workspace
    storage = case.storage
    neighbor, _ = Intake(storage).new(name="Neighbor")
    revision = storage.path(f"{case.project.run(case.run)}/Revisions/{case.first}")
    (revision / missing).unlink()
    surviving = next(revision.glob("*.json"))
    evidence = surviving.read_bytes()
    reopened = State(storage)
    assert reopened.plan(case.run).brief.campaign_id == case.cid
    snapshot = Workspace(reopened).current_composition(case.cid)
    assert len(snapshot["plan"].outputs) == 6 and len(snapshot["sources"]) == 2
    assert not reopened.identity_exists("revisions", case.current)
    assert Handoff(reopened).download(case.target, case.archive["artifact_id"]) == case.zip_bytes
    assert any(path.read_bytes() == case.zip_bytes for path in storage.root.rglob("*.zip"))
    assert surviving.read_bytes() == evidence
    assert not (revision / missing).exists()
    assert Intake(storage).read(neighbor)["value"]["name"] == "Neighbor"
    with storage.connect() as db:
        assert not db.execute("SELECT 1 FROM workspace_migrations").fetchone()


def test_active_damage_is_isolated_across_restarts_and_retries_after_restore(v020_workspace):
    case = v020_workspace
    storage = case.storage
    neighbor, _ = Intake(storage).new(name="Neighbor")
    manifest = storage.path(case.old_manifest)
    original = manifest.read_bytes()
    manifest.unlink()
    for _ in range(2):
        blocked = State(storage)
        with pytest.raises(StateError, match="restore any missing files") as error:
            blocked.plan(case.run)
        assert error.value.code == "workspace_upgrade"
        assert Intake(storage).read(neighbor)["value"]["name"] == "Neighbor"
        assert not manifest.exists()
        assert sorted((manifest.parent / "images").rglob("*.png"))
        with storage.connect() as db:
            assert db.execute("SELECT name FROM workspace_migrations").fetchall() == [
                (f"campaign_outputs:{case.cid}",)
            ]
    with TestClient(
        create_app(Settings(data_dir=storage.root)), base_url="http://127.0.0.1"
    ) as client:
        cards = {
            card["id"]: card for card in client.get("/api/motif/workspace").json()["campaigns"]
        }
        assert cards[case.cid]["status"] == "damaged"
        assert "restart" in cards[case.cid]["recovery_message"]
        assert client.get(f"/api/motif/campaigns/{case.cid}").json()["code"] == "workspace_upgrade"
        assert client.get(f"/api/motif/campaigns/{neighbor}").status_code == 200
    manifest.write_bytes(original)
    reopened = State(storage)
    assert reopened.plan(case.run).brief.campaign_id == case.cid
    snapshot = Workspace(reopened).current_composition(case.cid)
    assert len(snapshot["plan"].outputs) == 6 and len(snapshot["sources"]) == 2
    assert not reopened.identity_exists("revisions", case.current)
    assert Handoff(reopened).download(case.target, case.archive["artifact_id"]) == case.zip_bytes
    assert not manifest.exists()
    with storage.connect() as db:
        assert not db.execute("SELECT 1 FROM workspace_migrations").fetchone()


def test_v020_clears_unreferenced_retired_row_with_missing_documents(v020_workspace):
    case = v020_workspace
    storage = case.storage
    old = storage.path(f"{case.project.run(case.run)}/Revisions/{case.first}")
    for path in old.glob("*.json"):
        path.unlink()
    with storage.connect() as db:
        db.execute("DELETE FROM package_exports WHERE revision_id=?", (case.first,))
    reopened = State(storage)
    snapshot = Workspace(reopened).current_composition(case.cid)
    assert len(snapshot["plan"].outputs) == 6 and len(snapshot["sources"]) == 2
    assert not reopened.identity_exists("revisions", case.current)
    assert not old.exists()
    with storage.connect() as db:
        assert not db.execute("SELECT 1 FROM revisions WHERE id=?", (case.first,)).fetchone()
        assert not db.execute(
            "SELECT 1 FROM creative_metadata WHERE revision_id=?", (case.first,)
        ).fetchone()


def test_150_autosaves_keep_one_document_no_edit_history_and_sources_unchanged(
    tmp_path, monkeypatch
):
    from campaign_studio import delivery
    from campaign_studio.campaign_drafts import read
    from campaign_studio.workspace import TextEdit

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    ws = Workspace(state)
    current = ws.current_composition(brief.campaign_id)
    original_files = owned_files(state, brief.campaign_id)
    metrics = state.campaign_metrics(brief.campaign_id)
    monkeypatch.setattr(delivery, "compose", lambda *a, **k: pytest.fail("Autosave rendered a PNG"))
    for index in range(150):
        version = ws.save_composition(
            brief.campaign_id,
            TextEdit(
                ratio="1:1",
                revision_id=current["version"],
                market="en-US",
                product_id="product-1",
                values={"message": f"Saved headline {index}"},
                field_scopes={"message": "product"},
            ),
        )
        current = {"version": version}
    saved = read(state.storage, brief.campaign_id)
    assert (
        saved["authored"]["brief"]["products"][0]["copy_overrides"]["en-US"]["message"]
        == "Saved headline 149"
    )
    assert owned_files(state, brief.campaign_id) == original_files
    assert state.campaign_metrics(brief.campaign_id) == metrics
    assert not list(state.storage.root.rglob("Outputs"))
    assert not list(state.storage.root.rglob("Exports"))
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM campaign_drafts").fetchone() == (1,)
        for table in (
            "revisions",
            "creative_metadata",
            "package_exports",
            "cleanup_intents",
            "staging_output_files",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not Cleanup(state).pending()


def test_repeated_downloads_and_report_previews_do_not_add_managed_files(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    snapshot = Workspace(state).current_composition(brief.campaign_id)
    handoff = Handoff(state)
    before = owned_files(state, brief.campaign_id)
    action = DeliveryAction(version=snapshot["version"])
    for _ in range(5):
        assert handoff.create_current_preview(brief.campaign_id, action)["url"].startswith(
            "/reports/current/"
        )
        with handoff.create_current_zip(brief.campaign_id, action) as download:
            assert download.stream.read(4) == b"PK\x03\x04"
        assert not state.delivery_jobs
        assert owned_files(state, brief.campaign_id) == before
    preview = Cleanup(state).preview_campaign(brief.campaign_id)
    assert preview["output_png_count"] == preview["package_count"] == 0
    assert not list(state.storage.root.rglob("Outputs"))
    assert not list(state.storage.root.rglob("Exports"))
    with state.storage.connect() as db:
        assert not db.execute("SELECT 1 FROM revisions").fetchone()
        assert not db.execute("SELECT 1 FROM package_exports").fetchone()
        assert not db.execute("SELECT 1 FROM cleanup_intents").fetchone()


def test_failed_metadata_save_preserves_current_document_source_files_and_earlier_download(
    tmp_path,
):
    import sqlite3

    from campaign_studio.campaign_drafts import read
    from campaign_studio.workspace import FrameEdit

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    ws = Workspace(state)
    snapshot = ws.current_composition(brief.campaign_id)
    before = read(state.storage, brief.campaign_id)
    sources = owned_files(state, brief.campaign_id)
    with Handoff(state).create_current_zip(
        brief.campaign_id, DeliveryAction(version=before["version"])
    ) as download:
        previous_zip = tmp_path / "earlier-download.zip"
        previous_zip.write_bytes(download.stream.read())
    digest = hashlib.sha256(previous_zip.read_bytes()).hexdigest()
    with state.storage.connect() as db:
        db.execute(
            "CREATE TRIGGER deny_save BEFORE UPDATE ON campaign_drafts "
            "BEGIN SELECT RAISE(FAIL,'injected metadata failure'); END"
        )
    edit = FrameEdit(
        revision_id=snapshot["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        framing={"zoom": 1.1},
    )
    with pytest.raises(sqlite3.Error, match="injected metadata failure"):
        ws.save_composition(brief.campaign_id, edit)
    assert read(state.storage, brief.campaign_id) == before
    assert owned_files(state, brief.campaign_id) == sources
    assert hashlib.sha256(previous_zip.read_bytes()).hexdigest() == digest
    assert not Cleanup(state).pending()
    with state.storage.connect() as db:
        db.execute("DROP TRIGGER deny_save")
    assert ws.save_composition(brief.campaign_id, edit) != before["version"]
    assert owned_files(state, brief.campaign_id) == sources


def test_failed_export_cleanup_keeps_saved_document_and_deletes_only_temporary_job(
    tmp_path, monkeypatch
):
    from campaign_studio import delivery
    from campaign_studio.campaign_drafts import read

    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    snapshot = Workspace(state).current_composition(brief.campaign_id)
    handoff = Handoff(state)
    action = DeliveryAction(version=snapshot["version"], operation_id="5" * 32)
    before = read(state.storage, brief.campaign_id)
    sources = owned_files(state, brief.campaign_id)

    def fail(*_args, **_kwargs):
        raise OSError("Injected local rendering failure")

    monkeypatch.setattr(delivery, "render_png", fail)
    for _ in range(3):
        with pytest.raises(StateError) as error:
            handoff.create_current_zip(brief.campaign_id, action)
        assert error.value.code == "delivery_retry"
        assert len(state.delivery_jobs) == 1
        assert not state.delivery_jobs[action.operation_id].ready
        assert read(state.storage, brief.campaign_id) == before
        assert owned_files(state, brief.campaign_id) == sources
    held = list(state.delivery_jobs[action.operation_id].sources.values())
    handoff.cancel_current_delivery(brief.campaign_id, action.operation_id)
    assert not state.delivery_jobs and all(stream.closed for stream in held)
    assert not Cleanup(state).pending()
    assert not list(state.storage.root.rglob("Outputs"))
    assert not list(state.storage.root.rglob("Exports"))


@pytest.mark.parametrize("interface", ["service", "deferred_service", "http"])
def test_confirmed_campaign_deletion_immediately_closes_retained_export(
    tmp_path, monkeypatch, interface
):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    asyncio.run(runner.execute(run))
    with ExitStack() as stack:
        client = None
        if interface == "http":
            app = create_app(
                Settings(data_dir=state.storage.root),
                provider_factory=lambda: pytest.fail("provider construction"),
            )
            client = stack.enter_context(TestClient(app, base_url="http://127.0.0.1"))
            state = app.state.workspace.state
        _delete_retained_export(state, brief, monkeypatch, interface, client)


def _delete_retained_export(state, brief, monkeypatch, interface, client):
    from campaign_studio import delivery

    snapshot = Workspace(state).current_composition(brief.campaign_id)
    handoff = Handoff(state)
    action = DeliveryAction(version=snapshot["version"], operation_id="6" * 32)
    original = delivery.render_png

    def fail_first(saved, paths, output, destination, **kwargs):
        if output.id == snapshot["plan"].outputs[0].id:
            raise OSError("Injected failure")
        return original(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", fail_first)
    with pytest.raises(StateError) as error:
        handoff.create_current_zip(brief.campaign_id, action)
    assert error.value.code == "delivery_retry"
    job = state.delivery_jobs[action.operation_id]
    assert len(job.ready) == 5
    retained = list(job.ready.values()) + list(job.sources.values())
    assert retained and all(not stream.closed for stream in retained)
    cleanup = Cleanup(state)

    # Removing another campaign must not cancel this campaign's download.
    other, _ = Intake(state.storage).new()
    cleanup.delete_campaign(other, cleanup.preview_campaign(other)["token"])
    assert not job.closed and state.delivery_jobs[action.operation_id] is job
    preview = cleanup.preview_campaign(brief.campaign_id)
    with pytest.raises(StateError, match="changed"):
        cleanup.delete_campaign(brief.campaign_id, "0" * 64)
    assert not job.closed and all(not stream.closed for stream in retained)

    if client:
        endpoint = f"/api/campaigns/{brief.campaign_id}/cleanup/confirm"
        assert client.post(endpoint, json={"token": preview["token"]}).status_code == 403
        assert not job.closed
        client.headers["X-Campaign-Token"] = client.app.state.csrf
        response = client.post(endpoint, json={"token": preview["token"]})
        assert response.status_code == 200, response.text
        result = response.json()
    else:
        result = cleanup.delete_campaign(
            brief.campaign_id, preview["token"], defer_cleanup=interface == "deferred_service"
        )

    # No retry, progress request, timer expiry, or app shutdown is needed.
    assert job.closed and not state.delivery_jobs
    assert all(stream.closed for stream in retained)
    assert job.timer is not None and job.timer.finished.is_set()
    assert (
        handoff.current_progress(brief.campaign_id, action.operation_id)["status"] == "unavailable"
    )
    if interface != "service":
        assert result["state"] == "committed"
        with pytest.raises(StateError, match="being deleted"):
            handoff.create_current_zip(brief.campaign_id, action)
        assert cleanup.retry(brief.campaign_id, result["cleanup_id"])["state"] == "complete"
    else:
        assert result["state"] == "complete"
        with pytest.raises(StateError, match="find this campaign"):
            handoff.create_current_zip(brief.campaign_id, action)


def test_migrated_package_history_survives_new_autosaves_without_new_history(v020_workspace):
    from campaign_studio.workspace import FrameEdit

    case = v020_workspace
    state = State(case.storage)
    ws = Workspace(state)
    before = Path(case.folder["location"])
    delivered = {str(path): path.read_bytes() for path in before.rglob("*") if path.is_file()}
    snapshot = ws.current_composition(case.cid)
    version = snapshot["version"]
    for index in range(10):
        version = ws.save_composition(
            case.cid,
            FrameEdit(
                revision_id=version,
                product_id="product-1",
                market="en-US",
                ratio="1:1",
                framing={"zoom": 1.01 + index / 100},
            ),
        )
    assert {
        str(path): path.read_bytes() for path in before.rglob("*") if path.is_file()
    } == delivered
    assert Handoff(state).download(case.target, case.archive["artifact_id"]) == case.zip_bytes
    assert not case.outputs.exists()
    with case.storage.connect() as db:
        assert db.execute("SELECT id FROM revisions").fetchall() == [(case.first,)]
        assert db.execute("SELECT COUNT(*) FROM campaign_drafts").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM package_exports").fetchone() == (2,)
        assert db.execute(
            "SELECT current_revision FROM runs WHERE id=?", (case.run,)
        ).fetchone() == (None,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
