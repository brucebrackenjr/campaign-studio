"""Current-only draft storage, safe migration, and explicit Studio reconciliation."""

import asyncio
import copy
import json

import pytest

from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Framing
from campaign_studio.campaign_drafts import migrate_drafts, read, write
from campaign_studio.intake import Intake
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.workspace import FrameEdit, TextEdit, Workspace


def stored(storage, campaign):
    with storage.connect() as db:
        rows = db.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (campaign,)
        ).fetchall()
    assert len(rows) == 1
    return json.loads(rows[0][0])


def test_current_draft_has_campaign_fields_and_saved_studio_settings_once(tmp_path):
    from campaign_fixtures import seed_campaign

    workspace = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(workspace)
    storage = workspace.storage
    projects = AuthoredProjects(workspace)
    current = projects.read(cid)
    current["authored"]["brief"]["audience"] = "  Current Campaign audience  "
    current["authored"]["form_drafts"] = {"fields": {"brief/copy/en": {"copy-cta": "draft"}}}
    projects.save(
        cid, AuthoredSave.model_validate({k: current[k] for k in ("version", "base", "authored")})
    )
    version = workspace.save_composition(
        cid,
        TextEdit(
            revision_id=projects.read(cid)["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            values={"message": "A deliberate new headline."},
            field_scopes={"message": "all_products"},
        ),
    )
    workspace.save_composition(
        cid,
        FrameEdit(
            revision_id=version,
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            framing=Framing(zoom=1.1),
        ),
    )
    reopened = AuthoredProjects(Workspace(State(storage))).read(cid)
    brief = reopened["authored"]["brief"]
    assert brief["markets"]["en-US"]["copy"]["message"] == "A deliberate new headline."
    assert brief["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.1
    assert brief["audience"] == "  Current Campaign audience  "
    assert reopened["authored"]["form_drafts"] == current["authored"]["form_drafts"]
    record = stored(storage, cid)
    assert "value" not in record and "plan" not in record
    assert record["authored"]["brief"] == brief
    assert not storage.path(storage.project(cid).draft).exists()
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM runs").fetchone() == (0,)


@pytest.mark.parametrize("keep_original", [False, True])
def test_owned_legacy_draft_migration_survives_interrupted_delete(
    tmp_path, monkeypatch, keep_original
):
    storage = Storage(tmp_path / "runtime")
    State(storage)
    cid, document = Intake(storage).new(name="Incomplete campaign")
    document["authored"] = {
        "brief": copy.deepcopy(document["value"]),
        "form_drafts": {"fields": {"brand": {"text-color": "#12"}}},
    }
    document["authored"]["brief"]["brand"]["text_color"] = "#12"
    project = storage.project(cid)
    with storage.connect() as db:
        db.execute("DELETE FROM campaign_drafts WHERE campaign_id=?", (cid,))
        project.claim(project.draft, db)
    payload = json.dumps(document).encode()
    storage.write_new(project.draft, payload)
    original = project.ref("Brief/original.json")
    if keep_original:
        project.claim(original)
        storage.write_new(original, b'{"original": "imported brief"}')
    original_unlink = storage.unlink

    def fail_delete(path, *args, **kwargs):
        if path == storage.path(project.draft):
            raise OSError("Stopped after database commit")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(storage, "unlink", fail_delete)
    with pytest.raises(OSError, match="database commit"):
        migrate_drafts(storage)
    assert stored(storage, cid)["authored"] == document["authored"]
    assert storage.path(project.draft).read_bytes() == payload
    monkeypatch.setattr(storage, "unlink", original_unlink)
    migrate_drafts(storage)
    assert read(storage, cid)["authored"] == document["authored"]
    assert "legacy_sha256" not in stored(storage, cid)
    assert not storage.path(project.draft).exists()
    if keep_original:
        assert storage.path(original).read_bytes() == b'{"original": "imported brief"}'
    else:
        assert not storage.path(project.ref("Brief")).exists()
    migrate_drafts(storage)


def test_initial_unproduced_framing_survives_and_invalid_raw_draft_is_lossless(tmp_path):
    storage = Storage(tmp_path / "runtime")
    State(storage)
    cid, document = Intake(storage).new()
    brief = copy.deepcopy(document["value"])
    brief["products"] = [{"id": "new-product", "framing": {"en-US": {"1:1": {"zoom": 1.2}}}}]
    document["authored"] = {
        "brief": brief,
        "form_drafts": {
            "brand_color_defaults": [
                {"product_id": "new-product", "market": "en-US", "ratio": "1:1"}
            ]
        },
    }
    write(storage, cid, document)
    assert read(storage, cid)["authored"] == document["authored"]
    assert stored(storage, cid)["authored"] == document["authored"]
    assert read(storage, cid)["value"]["products"] == brief["products"]
    document["authored"]["brief"]["products"][0]["framing"] = "still typing"
    write(storage, cid, document)
    assert read(storage, cid)["authored"] == document["authored"]


def test_legacy_migration_does_not_delete_unregistered_user_file(tmp_path):
    storage = Storage(tmp_path / "runtime")
    State(storage)
    cid, _ = Intake(storage).new()
    path = storage.path(storage.project(cid).draft)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"User-owned notes, not a Motif draft")
    migrate_drafts(storage)
    assert path.read_bytes() == b"User-owned notes, not a Motif draft"
    assert "value" not in stored(storage, cid)


def test_source_preparation_keeps_framing_in_the_current_document(tmp_path):
    from campaign_studio.planning import make_plan
    from campaign_studio.runner import Runner
    from campaign_studio.sample import write_sample

    storage = Storage(tmp_path / "runtime")
    _, brief = write_sample(storage, 2, True)
    brief.products[0].framing = {"en-US": {"1:1": Framing(zoom=1.2)}}
    state = State(storage)
    run = state.create(make_plan(brief, storage), 0)
    value = state.plan(run).brief.model_dump(mode="json")
    document = {
        "value": value,
        "authored": {"brief": copy.deepcopy(value)},
        "version": "initial-import",
        "composition_schema": 1,
        "planned_run": run,
        "authored_base": {"run_id": run, "revision_id": None, "metadata_snapshot_id": None},
    }
    write(storage, brief.campaign_id, document)
    assert stored(storage, brief.campaign_id)["authored"]["brief"]["products"][0]["framing"]
    state.confirm(run, state.plan(run).digest)
    assert asyncio.run(Runner(state).execute(run))["complete"]
    record = stored(storage, brief.campaign_id)
    assert record["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.2
    assert (
        read(storage, brief.campaign_id)["authored"]["brief"]["products"][0]["framing"]["en-US"][
            "1:1"
        ]["zoom"]
        == 1.2
    )
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (0,)
