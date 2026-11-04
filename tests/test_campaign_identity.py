"""Campaign UUIDs are database identities, including drafts before any run."""

import copy
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from campaign_studio.app import create_app
from campaign_studio.brief import Brief
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.review import RevisionTarget
from campaign_studio.state import State
from campaign_studio.storage import Storage


@pytest.fixture
def client(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Identity operations must never generate images"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as result:
        result.headers["X-Campaign-Token"] = app.state.csrf
        yield result


def create(client, name="Same campaign name"):
    response = client.post("/api/motif/campaigns", json={"name": name, "brand": "Brand"})
    assert response.status_code == 200, response.text
    return response.json()["campaign_id"]


def test_campaign_and_empty_production_open_without_a_plan_and_brief_is_not_an_alias(client):
    campaign = create(client)
    model = client.get(f"/api/motif/campaigns/{campaign}").json()
    assert model["value"]["products"] == []
    assert model["run"] is None
    entry = next(
        item
        for item in client.get("/api/motif/workspace").json()["campaigns"]
        if item["id"] == campaign
    )
    assert entry["page"] == "campaign"
    assert client.get(f"/campaigns/{campaign}/{entry['page']}").status_code == 200
    for page in ("campaign", "production"):
        response = client.get(f"/campaigns/{campaign}/{page}")
        assert response.status_code == 200
        assert f'data-page="{page}"' in response.text
    for path in (
        "/brief",
        "/setup",
        f"/campaigns/{campaign}/brief",
        f"/campaigns/{campaign}/setup",
    ):
        assert client.get(path, follow_redirects=False).status_code == 404
    assert client.get(f"/api/motif/campaigns/{campaign}").json()["run"] is None


def save_name(client, campaign_id, name):
    url = f"/api/motif/campaigns/{campaign_id}"
    project = client.get(url).json()["project"]
    authored = copy.deepcopy(project["authored"])
    authored["brief"]["name"] = name
    response = client.post(
        url + "/authored",
        json={
            "version": project["version"],
            "base": project["base"],
            "authored": authored,
        },
    )
    assert response.status_code == 200, response.text


def test_create_import_same_names_register_distinct_uuid_strings_and_rename_is_stable(client):
    created = [create(client), create(client)]
    original = json.loads(
        files("campaign_studio").joinpath("resources/replay/brief.json").read_bytes()
    )
    original["name"] = "Same campaign name"
    for _ in range(2):
        response = client.post("/api/motif/import", json=original)
        assert response.status_code == 200, response.text
        created.append(response.json()["campaign_id"])
    assert len(set(created)) == 4
    assert all(str(UUID(identity)) == identity for identity in created)
    state = client.app.state.workspace.state
    with state.storage.connect() as db:
        assert {row[0] for row in db.execute("SELECT id FROM campaigns")} == set(created)
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        for table in ("runs", "file_owners", "cleanup_intents"):
            assert any(
                row[2:5] == ("campaigns", "campaign_id", "id")
                for row in db.execute(f"PRAGMA foreign_key_list({table})")
            )
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    for identity in created:
        for page in ("campaign", "production", "studio", "handoff"):
            assert client.get(f"/campaigns/{identity}/{page}").status_code == 200
    save_name(client, created[0], "Renamed, same campaign")
    reopened = client.get(f"/api/motif/campaigns/{created[0]}").json()
    assert reopened["project"]["authored"]["brief"]["campaign_id"] == created[0]
    assert reopened["project"]["authored"]["brief"]["name"] == "Renamed, same campaign"
    other = client.get(f"/api/motif/campaigns/{created[1]}").json()
    assert other["project"]["authored"]["brief"]["name"] == "Same campaign name"
    project = reopened["project"]
    project["authored"]["brief"]["campaign_id"] = created[1]
    refused = client.post(
        f"/api/motif/campaigns/{created[0]}/authored",
        json={
            "authored": project["authored"],
            "version": project["version"],
            "base": project["base"],
        },
    )
    assert refused.status_code == 422 and refused.json()["code"] == "campaign_identity"
    assert {row["id"] for row in client.get("/api/motif/workspace").json()["campaigns"]} == set(
        created
    )


def test_unknown_malformed_deleted_and_phantom_ids_share_not_found_without_registration(client):
    known = create(client)
    state = client.app.state.workspace.state
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(known)
    assert cleanup.delete_campaign(known, preview["token"])["state"] == "complete"
    phantom = str(uuid4())
    state.storage.write(
        f"private/brief-drafts/{phantom}.json",
        json.dumps(
            {
                "value": {"campaign_id": phantom, "name": "Not registered"},
                "version": "unowned",
            }
        ).encode(),
    )
    identities = [
        "named-campaign",
        "campaign-" + uuid4().hex,
        uuid4().hex,
        str(uuid4()),
        phantom,
        known,
    ]
    for identity in identities:
        for page in ("campaign", "studio", "handoff", "production"):
            response = client.get(f"/campaigns/{identity}/{page}")
            assert response.status_code == 404
            assert "Campaign not found" in response.text
            assert "We couldn’t find this campaign in your workspace." in response.text
            assert 'href="/?action=new"' in response.text
            assert "Back to workbench" in response.text
        response = client.get(f"/api/motif/campaigns/{identity}")
        assert response.status_code == 404 and response.json()["code"] == "not_found"
        response = client.post(f"/api/motif/campaigns/{identity}/authored", json={})
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert client.get("/api/motif/workspace").json()["campaigns"] == []
    assert client.get("/?action=new").status_code == 200  # A GET never creates a record.
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


def test_creation_failure_rolls_back_registry_and_never_overwrites_an_existing_draft(
    tmp_path, monkeypatch
):
    state = State(Storage(tmp_path / "runtime"))
    intake = Intake(state.storage)
    original_write = state.storage.write_new

    def fail(reference, payload):
        raise OSError("controlled draft write failure")

    monkeypatch.setattr(state.storage, "write_new", fail)
    with pytest.raises(OSError, match="controlled"):
        intake.new()
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0
    assert not list(state.storage.path("Campaigns").glob("*/Brief/draft.json"))
    monkeypatch.setattr(state.storage, "write_new", original_write)
    original_connect = state.storage.connect
    candidate = str(uuid4())

    @contextmanager
    def fail_commit():
        with original_connect() as db:
            yield db
            if (
                db.in_transaction
                and db.execute("SELECT 1 FROM campaigns WHERE id=?", (candidate,)).fetchone()
            ):
                raise sqlite3.OperationalError("controlled registry commit failure")

    with monkeypatch.context() as m:
        m.setattr(state.storage, "connect", fail_commit)
        with pytest.raises(sqlite3.OperationalError, match="controlled registry"):
            intake.create(candidate, {"campaign_id": candidate})
    assert not state.storage.path(f"private/brief-drafts/{candidate}.json").exists()
    with state.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0
    campaign, document = intake.new()
    path = state.storage.path(state.storage.project(campaign).draft)
    before = path.read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        intake.create(campaign, document["value"] | {"name": "Must not replace"})
    assert path.read_bytes() == before
    orphan = str(uuid4())
    folder = f"Untitled campaign-{orphan[:8]}"
    orphan_path = state.storage.path(f"Campaigns/{folder}/Brief/draft.json")
    state.storage.write(orphan_path.relative_to(state.storage.root).as_posix(), before)
    intake.create(orphan, document["value"] | {"campaign_id": orphan})
    assert state.storage.project(orphan).folder != folder
    assert orphan_path.read_bytes() == before
    assert state.storage.path(state.storage.project(orphan).draft).is_file()
    assert intake.read(campaign) == document


@pytest.mark.skipif(
    os.name == "nt", reason="Windows publishes with MoveFileExW, not directory fsync"
)
def test_creation_directory_sync_failure_removes_only_its_newly_linked_draft(tmp_path, monkeypatch):
    state = State(Storage(tmp_path / "runtime"))
    intake = Intake(state.storage)
    existing, _ = intake.new()
    existing_path = state.storage.path(state.storage.project(existing).draft)
    before = existing_path.read_bytes()
    original_sync = os.fsync
    failure = OSError("controlled post-publication directory sync failure")
    attempted = []

    def fail_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            attempted.append(descriptor)
            raise failure
        return original_sync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_directory)
        with pytest.raises(OSError) as error:
            intake.new()
    assert error.value is failure
    assert len(attempted) == 1
    assert existing_path.read_bytes() == before
    assert list(existing_path.parent.iterdir()) == [existing_path]
    with state.storage.connect() as db:
        assert [row[0] for row in db.execute("SELECT id FROM campaigns")] == [existing]
    created, _ = intake.new()
    assert created != existing


def test_registered_damage_does_not_reinitialize_and_pending_deletion_keeps_retry(
    client, monkeypatch
):
    campaign = create(client)
    state = client.app.state.workspace.state
    path = state.storage.path(state.storage.project(campaign).draft)
    exact = path.read_bytes()
    path.unlink()
    response = client.get(f"/campaigns/{campaign}/campaign")
    assert response.status_code == 409 and "saved data is unavailable" in response.text
    assert not path.exists()
    rows = client.get("/api/motif/workspace").json()["campaigns"]
    assert rows[0]["id"] == campaign and rows[0]["status"] == "damaged"
    path.write_bytes(exact)
    cleanup = Cleanup(state)
    preview = cleanup.preview_campaign(campaign)
    original_unlink = Path.unlink
    with monkeypatch.context() as m:

        def fail(target, *args, **kwargs):
            if target == path:
                raise OSError("controlled deletion failure")
            return original_unlink(target, *args, **kwargs)

        m.setattr(Path, "unlink", fail)
        pending = cleanup.delete_campaign(campaign, preview["token"])
    assert pending["state"] == "pending"
    with state.storage.connect() as db:
        assert db.execute("SELECT id FROM campaigns WHERE id=?", (campaign,)).fetchone()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    response = client.post(
        f"/api/campaigns/{campaign}/cleanup/{pending['cleanup_id']}/retry", json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "complete"
    with state.storage.connect() as db:
        assert not db.execute("SELECT id FROM campaigns WHERE id=?", (campaign,)).fetchone()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert client.get(f"/campaigns/{campaign}/studio").status_code == 404


def test_campaign_schema_and_revision_payload_keep_uuid_strings():
    value = json.loads(
        files("campaign_studio").joinpath("resources/replay/brief.json").read_bytes()
    )
    identity = str(uuid4())
    value["campaign_id"] = identity
    assert Brief.model_validate(value).campaign_id == identity
    target = dict(campaign_id=identity, run_id="a" * 32, revision_id="b" * 32, mode="current")
    assert RevisionTarget(**target).campaign_id == identity
    for invalid in ("named", "campaign-" + uuid4().hex, uuid4().hex, identity.upper()):
        with pytest.raises(ValidationError):
            Brief.model_validate(value | {"campaign_id": invalid})
        with pytest.raises(ValidationError):
            RevisionTarget(**(target | {"campaign_id": invalid}))


def test_registry_only_failed_preparation_can_be_deleted_without_claiming_other_files(client):
    state = client.app.state.workspace.state
    campaign = str(uuid4())
    state.register_campaign(campaign)
    # An old incoming folder with this ID contains unregistered files.
    reference = f"incoming/{campaign}/unowned.txt"
    state.storage.write(reference, b"Unrelated content stays exact")
    preview = client.post(f"/api/campaigns/{campaign}/cleanup/preview", json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["file_count"] == preview.json()["run_count"] == 0
    result = Cleanup(state).delete_campaign(campaign, preview.json()["token"])
    assert result["state"] == "complete"
    assert state.storage.path(reference).read_bytes() == b"Unrelated content stays exact"
    with state.storage.connect() as db:
        assert not db.execute("SELECT id FROM campaigns WHERE id=?", (campaign,)).fetchone()
        assert not db.execute(
            "SELECT 1 FROM cleanup_intents WHERE campaign_id=?", (campaign,)
        ).fetchone()
    assert client.get(f"/campaigns/{campaign}/campaign").status_code == 404
