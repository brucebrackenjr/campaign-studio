"""Legacy edits survive the one-current-composition cutover without file buildup."""

import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import read
from campaign_studio.cleanup import Cleanup
from campaign_studio.composition_migration import migrate_current
from campaign_studio.plan_storage import completed_run, dump_plan
from campaign_studio.planning import make_plan
from campaign_studio.projects import output_file, product_folders
from campaign_studio.sample import write_sample
from campaign_studio.state import State, timestamp
from campaign_studio.storage import Storage, StorageError


@pytest.fixture
def legacy(tmp_path):
    storage = Storage(tmp_path / "Workspace")
    _, brief = write_sample(storage, one_market=True)
    state = State(storage)
    value = brief.model_dump(mode="json")
    value["products"][0]["framing"] = {"en-US": {"1:1": {"zoom": 1.23}}}
    value["products"][0]["copy_overrides"] = {
        "en-US": {"message": "A deliberate product headline."}
    }
    plan = make_plan(Brief.model_validate(value), storage)
    run = state.create(plan, 0)
    revision = uuid4().hex
    project = storage.project(brief.campaign_id)
    files = []
    items = []
    folders = product_folders(plan.brief.products)
    for output in plan.outputs:
        relative = output_file(
            output.product_id,
            output.ratio,
            output.market,
            revision,
            folder=folders[output.product_id],
        )
        reference = project.ref(f"Outputs/{relative}")
        payload = b"legacy derived bytes: " + output.id.encode()
        project.write(reference, payload)
        files.append(reference)
        items.append(
            {
                "id": output.id,
                "product_id": output.product_id,
                "market": output.market,
                "ratio": output.ratio,
                "file": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "width": 100,
                "height": 100,
                "copy": {"message": "Old headline", "cta": None},
            }
        )
    current = copy.deepcopy(brief.model_dump(mode="json"))
    current["markets"]["en-US"]["copy"]["message"] = "New Campaign headline"
    current["products"][0].pop("framing", None)
    current["products"][0].pop("copy_overrides", None)
    record = {
        "authored": {"brief": current},
        "authored_saved": True,
        "brand_id": brief.brand.id,
        "version": uuid4().hex,
    }
    with storage.connect() as db:
        db.execute(
            "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,created_at,"
            "plan_json,manifest_json,image_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                revision,
                run,
                project.ref("Outputs/plan.json"),
                project.ref("Outputs/manifest.json"),
                1,
                timestamp(),
                dump_plan(plan),
                json.dumps(
                    {
                        "campaign_id": brief.campaign_id,
                        "revision_id": revision,
                        "complete": True,
                        "outputs": items,
                    }
                ),
                len(items),
            ),
        )
        db.execute(
            "UPDATE runs SET state='complete',current_revision=? WHERE id=?", (revision, run)
        )
        db.execute(
            "INSERT INTO campaign_drafts VALUES (?,?)", (brief.campaign_id, json.dumps(record))
        )
    return SimpleNamespace(
        storage=storage,
        state=state,
        cid=brief.campaign_id,
        project=project,
        run=run,
        revision=revision,
        files=files,
        record=record,
        plan=plan,
    )


def assert_current(case):
    doc = read(case.storage, case.cid)
    assert doc["composition_schema"] == 1
    brief = doc["authored"]["brief"]
    assert brief["markets"]["en-US"]["copy"]["message"] == "New Campaign headline"
    assert brief["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.23
    assert (
        brief["products"][0]["copy_overrides"]["en-US"]["message"]
        == "A deliberate product headline."
    )
    assert len(doc["source_assets"]) == 2
    assert len(doc["creative_metadata"]) == 6
    with case.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM campaign_drafts").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        raw = json.loads(db.execute("SELECT document_json FROM campaign_drafts").fetchone()[0])
        assert "value" not in raw
        assert completed_run(db.execute("SELECT plan_json FROM runs").fetchone()[0]) is not None
    return doc


def test_migration_keeps_current_fields_and_saved_geometry_retires_only_derived_files(legacy):
    original = legacy.project.ref("Brief/original.json")
    legacy.project.write(original, b'{"original": "unchanged import"}')
    migrate_current(legacy.state, legacy.cid)
    doc = assert_current(legacy)
    assert doc["version"] != legacy.record["version"]
    assert all(not legacy.storage.path(ref).exists() for ref in legacy.files)
    assert not legacy.storage.path(legacy.project.ref("Outputs")).exists()
    assert legacy.storage.path(original).read_bytes() == b'{"original": "unchanged import"}'
    for source in doc["source_assets"].values():
        assert legacy.storage.path(source["asset"]["reference"]).is_file()
    assert Cleanup(legacy.state)._current_inputs(legacy.cid) >= {
        item["asset"]["reference"] for item in doc["source_assets"].values()
    }
    before = legacy.storage.path("private/state.sqlite3").read_bytes()
    for _ in range(150):
        assert migrate_current(legacy.state, legacy.cid)["version"] == doc["version"]
    assert legacy.storage.path("private/state.sqlite3").read_bytes() == before


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_migration_is_atomic_and_cleanup_can_resume(legacy, monkeypatch, after_commit):
    def interrupt(*args, **kwargs):
        raise SystemExit("controlled stop")

    with monkeypatch.context() as patch:
        patch.setattr(Cleanup, "_resume" if after_commit else "record_intent", interrupt)
        with pytest.raises(SystemExit, match="controlled stop"):
            migrate_current(legacy.state, legacy.cid)
    assert all(legacy.storage.path(ref).exists() for ref in legacy.files)
    assert (read(legacy.storage, legacy.cid).get("composition_schema") == 1) is after_commit
    migrate_current(legacy.state, legacy.cid)
    Cleanup(legacy.state).resume_all()
    assert_current(legacy)
    assert all(not legacy.storage.path(ref).exists() for ref in legacy.files)


def test_modified_output_and_unregistered_notes_are_preserved(legacy):
    modified = legacy.storage.path(legacy.files[0])
    modified.write_bytes(b"User replaced this file")
    notes = modified.parent / "producer notes.txt"
    notes.write_text("Keep this file")
    migrate_current(legacy.state, legacy.cid)
    assert_current(legacy)
    assert modified.read_bytes() == b"User replaced this file"
    assert notes.read_text() == "Keep this file"
    assert all(not legacy.storage.path(ref).exists() for ref in legacy.files[1:])


def test_invalid_source_does_not_destroy_old_settings_or_images(legacy):
    # Corrupt the scene used as a source, not just the separately managed logo.
    source = legacy.plan.assets["products/product-1"]
    legacy.storage.path(source.reference).write_bytes(b"damaged")
    with pytest.raises(StorageError, match="source image changed"):
        migrate_current(legacy.state, legacy.cid)
    assert read(legacy.storage, legacy.cid).get("composition_schema") != 1
    assert all(legacy.storage.path(ref).exists() for ref in legacy.files)


def test_old_export_package_is_not_deleted_and_no_new_package_is_created(legacy):
    package = legacy.project.ref("Exports/Archives/previous-delivery.zip")
    legacy.project.write(package, b"Existing user delivery")
    with legacy.storage.connect() as db:
        db.execute(
            "INSERT INTO package_exports VALUES (?,?,?,?,?,?,?)",
            (
                uuid4().hex,
                legacy.run,
                legacy.revision,
                "old-snapshot",
                package,
                hashlib.sha256(b"Existing user delivery").hexdigest(),
                timestamp(),
            ),
        )
    migrate_current(legacy.state, legacy.cid)
    assert legacy.storage.path(package).read_bytes() == b"Existing user delivery"
    with legacy.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM package_exports").fetchone() == (1,)
        assert db.execute("SELECT images_retired FROM revisions").fetchone() == (1,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert all(not legacy.storage.path(ref).exists() for ref in legacy.files)


def test_explicit_current_framing_and_removed_override_win_over_old_plan(legacy):
    record = copy.deepcopy(legacy.record)
    product = record["authored"]["brief"]["products"][0]
    product["framing"] = {"en-US": {"1:1": {"zoom": 1.08}}}
    product["copy_overrides"] = {}
    with legacy.storage.connect() as db:
        db.execute("UPDATE campaign_drafts SET document_json=?", (json.dumps(record),))
    doc = migrate_current(legacy.state, legacy.cid)
    product = doc["authored"]["brief"]["products"][0]
    assert product["framing"]["en-US"]["1:1"]["zoom"] == 1.08
    assert product["copy_overrides"] == {}
