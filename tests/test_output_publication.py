"""Interrupt metadata saves around their SQLite commit; editing never publishes PNGs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from campaign_fixtures import seed_campaign

from campaign_studio.brief import Framing
from campaign_studio.campaign_drafts import read
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.workspace import FrameEdit, Workspace


def interrupted_save(root, campaign_id, interruption):
    from campaign_studio import compositions

    workspace = Workspace(State(Storage(Path(root))))
    document = read(workspace.storage, campaign_id)
    edit = FrameEdit(
        revision_id=document["version"],
        product_id="product-1",
        ratio="1:1",
        market="en-US",
        framing=Framing(zoom=1.27),
    )
    write = compositions.write_draft

    def stop(storage, cid, value):
        if interruption == "before_commit":
            # A real interrupted SQL transaction must roll back after process exit.
            with storage.connect() as db:
                write(storage, cid, value, db=db)
                os._exit(42)
        write(storage, cid, value)
        os._exit(42)

    compositions.write_draft = stop
    workspace.save_composition(campaign_id, edit)


@pytest.mark.parametrize("interruption", ["before_commit", "after_commit"])
def test_interrupted_metadata_save_keeps_complete_old_or_new_document(tmp_path, interruption):
    workspace = Workspace(State(Storage(tmp_path / "runtime")))
    campaign_id = seed_campaign(workspace)
    before = read(workspace.storage, campaign_id)
    files = {
        p: p.read_bytes() for p in workspace.storage.root.glob("Campaigns/**/*") if p.is_file()
    }
    tests = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from test_output_publication import interrupted_save; "
            "import sys; interrupted_save(*sys.argv[1:])",
            str(workspace.storage.root),
            campaign_id,
            interruption,
        ],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (str(tests), str(tests.parent / "src"), os.environ.get("PYTHONPATH", ""))
            ),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 42, result.stderr
    reopened = Workspace(State(workspace.storage))
    after = read(reopened.storage, campaign_id)
    if interruption == "before_commit":
        assert after == before
    else:
        assert after["version"] != before["version"]
        assert after["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.27
    assert {p: p.read_bytes() for p in files} == files
    assert not list(workspace.storage.root.glob("Campaigns/**/Outputs"))
    with reopened.storage.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM revisions").fetchone() == (0,)
        assert db.execute("SELECT count(*) FROM staging_output_files").fetchone() == (0,)


def test_repeated_edits_keep_one_document_and_only_latest_retry_receipt(tmp_path):
    workspace = Workspace(State(Storage(tmp_path / "runtime")))
    campaign_id = seed_campaign(workspace)
    versions = []
    for index in range(150):
        document = read(workspace.storage, campaign_id)
        edit = FrameEdit(
            revision_id=document["version"],
            product_id="product-1",
            ratio="1:1",
            market="en-US",
            framing=Framing(zoom=1 + index / 200),
        )
        version = workspace.save_composition(campaign_id, edit)
        versions.append(version)
        assert workspace.save_composition(campaign_id, edit) == version
    with workspace.storage.connect() as db:
        rows = db.execute("SELECT document_json FROM campaign_drafts").fetchall()
        assert len(rows) == 1
        record = json.loads(rows[0][0])
        assert record["version"] == versions[-1]
        assert record["composition_receipt"]["previous_version"] == versions[-2]
        assert all(version not in rows[0][0] for version in versions[:-2])
        for table in ("revisions", "runs", "package_exports", "staging_output_files"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_save_and_reopen_never_write_plan_manifest_or_draft_sidecars(tmp_path, monkeypatch):
    workspace = Workspace(State(Storage(tmp_path / "runtime")))
    campaign_id = seed_campaign(workspace)
    original = {p for p in workspace.storage.root.rglob("*") if p.is_file()}

    def reject(*args, **kwargs):
        pytest.fail("A metadata save or reopening wrote an application file")

    with monkeypatch.context() as patch:
        patch.setattr(workspace.storage, "write", reject)
        patch.setattr(workspace.storage, "write_new", reject)
        document = read(workspace.storage, campaign_id)
        workspace.save_composition(
            campaign_id,
            FrameEdit(
                revision_id=document["version"],
                product_id="product-1",
                ratio="1:1",
                market="en-US",
                framing=Framing(zoom=1.19),
            ),
        )
        State(workspace.storage)
    after = read(workspace.storage, campaign_id)
    assert after["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]["zoom"] == 1.19
    assert {p for p in workspace.storage.root.rglob("*") if p.is_file()} == original
