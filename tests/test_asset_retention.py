"""Removing an attachment preserves its file until the campaign is deleted."""

import pytest
from test_product_upload_lifecycle import (
    added_photo,
    photo,
    save_action,
    saved_action,
    upload_rows,
)

from campaign_studio.authored import AuthoredProjects
from campaign_studio.cleanup import Cleanup
from campaign_studio.intake import Intake
from campaign_studio.state import State
from campaign_studio.workspace import Workspace

pytest_plugins = ["test_web_workflow"]


@pytest.mark.parametrize("kind", ["logo", "product"])
def test_removed_asset_survives_reload_and_reattachment_then_campaign_delete(ui, kind):
    client, ws = ui
    cid, _, product_ref = added_photo(client, ws)
    other_cid, _, other_ref = added_photo(client, ws)
    external = ws.storage.root.parent / "original.png"
    external.write_bytes(photo())
    intake = Intake(ws.storage)
    reference = (
        intake.upload(photo(128), "image/png", cid, "logo", "Original logo")["reference"]
        if kind == "logo"
        else product_ref
    )
    original_bytes = ws.storage.path(reference).read_bytes()
    request = saved_action(ws, cid)
    if kind == "logo":
        request["authored"]["brief"]["brand"]["logo_path"] = reference
        save_action(ws, cid, request)
    original_product = saved_action(ws, cid)["authored"]["brief"]["products"][0]
    root = ws.storage.path(f"Campaigns/{ws.storage.project(cid).folder}")

    for _ in range(3):
        request = saved_action(ws, cid)
        if kind == "logo":
            request["authored"]["brief"]["brand"]["logo_path"] = ""
        else:
            request["authored"]["brief"]["products"] = []
        save_action(ws, cid, request)
        # A new Workspace plus cleanup models reopening the application.
        reopened = Workspace(State(ws.storage))
        assert Cleanup(reopened.state).resume_all() == []
        current = AuthoredProjects(reopened).read(cid)["authored"]["brief"]
        if kind == "logo":
            assert current["brand"]["logo_path"] == ""
        else:
            assert current["products"] == []
        assert ws.storage.path(reference).read_bytes() == original_bytes
        assert upload_rows(ws, reference) == (1, 1)
        assert client.get("/uploads/" + reference.rsplit("/", 1)[1]).content == original_bytes

        # An existing registered reference remains valid for attachment.
        request = saved_action(ws, cid)
        if kind == "logo":
            request["authored"]["brief"]["brand"]["logo_path"] = reference
        else:
            request["authored"]["brief"]["products"] = [original_product]
        save_action(ws, cid, request)

    # Selecting the retained file through a picker uploads another registered copy.
    uploaded = client.post(
        "/api/uploads",
        content=ws.storage.path(reference).read_bytes(),
        headers={
            "Content-Type": "image/png",
            "X-Campaign-Id": cid,
            "X-Image-Role": kind,
            **({"X-Source-Role": "finished_scene"} if kind == "product" else {}),
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    replacement = uploaded.json()["reference"]
    request = saved_action(ws, cid)
    if kind == "logo":
        request["authored"]["brief"]["brand"]["logo_path"] = replacement
    else:
        request["authored"]["brief"]["products"][0]["source"]["path"] = replacement
    save_action(ws, cid, request)
    assert ws.storage.path(reference).read_bytes() == original_bytes
    assert ws.storage.path(replacement).read_bytes() == original_bytes

    cleanup = Cleanup(ws.state)
    inventory = cleanup.preview_campaign(cid)
    assert inventory["unmanaged_file_count"] == 0
    assert cleanup.delete_campaign(cid, inventory["token"])["state"] == "complete"
    assert not root.exists()
    for ref in {reference, replacement, product_ref}:
        assert upload_rows(ws, ref) == (0, 0)
    assert ws.storage.path(other_ref).read_bytes() == photo()
    assert AuthoredProjects(ws).read(other_cid)["authored"]["brief"]["products"]
    assert external.read_bytes() == photo()
    with ws.storage.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
