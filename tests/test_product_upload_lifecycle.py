"""New-product selections remain browser-owned until an explicit, version-checked Add."""

import base64
import copy
import io
from uuid import uuid4

import pytest
from PIL import Image

from campaign_studio.authored import AuthoredProjects
from campaign_studio.intake import Intake
from campaign_studio.storage import StorageError

pytest_plugins = ["test_web_workflow"]


def photo(alpha=255):
    stream = io.BytesIO()
    Image.new("RGBA", (32, 32), (30, 90, 40, alpha)).save(stream, "PNG")
    return stream.getvalue()


def counts(ws):
    with ws.storage.connect() as db:
        rows = tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("uploads", "file_owners", "campaign_drafts")
        )
    files = {
        p.relative_to(ws.storage.root).as_posix(): p.read_bytes()
        for p in ws.storage.path("Campaigns").rglob("*")
        if p.is_file()
    }
    directories = {
        p.relative_to(ws.storage.root).as_posix()
        for p in ws.storage.path("Campaigns").rglob("*")
        if p.is_dir()
    }
    return rows, files, directories


def request_for(client, cid, image=None):
    current = client.get(f"/api/motif/campaigns/{cid}/authored").json()
    return {
        "version": current["version"],
        "base": current["base"],
        "product": {
            "id": "product-" + uuid4().hex,
            "name": "Test lantern",
            "description": "A brass lantern on a table",
            "source": {
                "role": "finished_scene",
                "path": "",
                "provenance": {"label": "Test", "rights": "Owned", "synthetic": False},
            },
        },
        "image_base64": base64.b64encode(image if image is not None else photo()).decode(),
    }


def test_validation_rejections_and_cancelled_valid_selection_leave_no_storage(ui):
    client, ws = ui
    cid, _ = Intake(ws.storage).new()
    baseline = counts(ws)
    for payload, expected in [(photo(0), 422), (b"not an image", 422), (photo(), 200)]:
        for _ in range(3):
            response = client.post(
                "/api/uploads/validate",
                content=payload,
                headers={"Content-Type": "image/png", "X-Source-Role": "finished_scene"},
            )
            assert response.status_code == expected, response.text
            if expected == 200:
                assert response.json()["opaque"] is True
                assert "reference" not in response.json()
            assert counts(ws) == baseline
    # Actual upload endpoint also refuses transparent scenes before publication.
    response = client.post(
        "/api/uploads",
        content=photo(0),
        headers={
            "Content-Type": "image/png",
            "X-Campaign-Id": cid,
            "X-Image-Role": "product",
            "X-Source-Role": "finished_scene",
        },
    )
    assert response.status_code == 422
    assert counts(ws) == baseline
    # Transparency remains supported by the other image roles.
    assert (
        client.post(
            "/api/uploads/validate",
            content=photo(0),
            headers={"Content-Type": "image/png", "X-Source-Role": "prepared_product"},
        ).status_code
        == 200
    )
    logo = client.post(
        "/api/uploads",
        content=photo(128),
        headers={"Content-Type": "image/png", "X-Campaign-Id": cid, "X-Image-Role": "logo"},
    )
    assert logo.status_code == 200


def test_add_commits_one_owned_photo_and_retry_does_not_duplicate_or_overwrite(ui):
    client, ws = ui
    cid, _ = Intake(ws.storage).new()
    request = request_for(client, cid)
    before = counts(ws)
    url = f"/api/motif/campaigns/{cid}/products/photo"
    result = client.post(url, json=request)
    assert result.status_code == 200, result.text
    product = result.json()["project"]["authored"]["brief"]["products"][0]
    path = product["source"]["path"]
    assert "/test-lantern-" in path
    assert ws.storage.path(path).read_bytes() == photo()
    saved = counts(ws)
    assert saved[0] == (before[0][0] + 1, before[0][1] + 1, before[0][2])
    assert client.post(url, json=request).status_code == 200
    assert counts(ws) == saved
    current = client.get(f"/api/motif/campaigns/{cid}/authored").json()
    action = {key: current[key] for key in ("version", "base", "authored")}
    action["authored"]["brief"]["products"][0]["name"] = "Later name"
    assert client.post(f"/api/motif/campaigns/{cid}/authored", json=action).status_code == 200
    changed = counts(ws)
    assert client.post(url, json=request).status_code == 409
    assert counts(ws) == changed
    assert (
        client.get(f"/api/motif/campaigns/{cid}/authored").json()["authored"]["brief"]["products"][
            0
        ]["name"]
        == "Later name"
    )


def test_invalid_or_stale_add_never_publishes_file(ui):
    client, ws = ui
    cid, _ = Intake(ws.storage).new()
    request = request_for(client, cid)
    baseline = counts(ws)
    for change, status in [
        ("transparent", 422),
        ("invalid", 422),
        ("stale", 409),
        ("bad-name", 422),
    ]:
        body = copy.deepcopy(request)
        if change == "transparent":
            body["image_base64"] = base64.b64encode(photo(0)).decode()
        if change == "invalid":
            body["image_base64"] = "not base64!"
        if change == "stale":
            body["version"] = "outdated"
        if change == "bad-name":
            body["product"]["name"] = ""
        result = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=body)
        assert result.status_code == status, result.text
        assert counts(ws) == baseline


@pytest.mark.parametrize("failure", ["publish", "save", "after-save", "race"])
def test_add_failure_releases_only_its_unadopted_upload(ui, monkeypatch, failure):
    client, ws = ui
    cid, _ = Intake(ws.storage).new()
    # An unrelated existing upload must survive every failure path.
    old = Intake(ws.storage).upload(photo(), "image/png", cid, "logo", "Saved brand")
    request = request_for(client, cid)
    baseline = counts(ws)
    original_write = ws.storage.write_new
    original_save = AuthoredProjects.save

    def write(reference, payload):
        original_write(reference, payload)
        raise StorageError("Injected failure after publication")

    def save(self, campaign_id, action):
        if failure == "after-save":
            original_save(self, campaign_id, action)
        if failure == "race":
            # A different browser saves after the initial check, before the final CAS.
            other = self.read(campaign_id)
            from campaign_studio.authored import AuthoredSave

            other["authored"]["brief"]["name"] = "Another browser"
            original_save(
                self,
                campaign_id,
                AuthoredSave(**{key: other[key] for key in ("version", "base", "authored")}),
            )
            return original_save(self, campaign_id, action)
        raise StorageError("Injected database save failure")

    if failure == "publish":
        monkeypatch.setattr(ws.storage, "write_new", write)
    else:
        monkeypatch.setattr(AuthoredProjects, "save", save)
    result = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=request)
    assert result.status_code >= 400, result.text
    assert ws.storage.path(old["reference"]).read_bytes() == photo()
    after = counts(ws)
    if failure == "after-save":
        assert after[0][0] == baseline[0][0] + 1
        monkeypatch.setattr(AuthoredProjects, "save", original_save)
        retry = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=request)
        assert retry.status_code == 200, retry.text
        assert counts(ws) == after
    else:
        assert after == baseline


def added_photo(client, ws, cid=None):
    cid = cid or Intake(ws.storage).new()[0]
    request = request_for(client, cid)
    response = client.post(f"/api/motif/campaigns/{cid}/products/photo", json=request)
    assert response.status_code == 200, response.text
    product = response.json()["project"]["authored"]["brief"]["products"][-1]
    return cid, product["id"], product["source"]["path"]


def saved_action(ws, cid):
    current = AuthoredProjects(ws).read(cid)
    return {key: current[key] for key in ("version", "base", "authored")}


def save_action(ws, cid, action):
    from campaign_studio.authored import AuthoredSave

    return AuthoredProjects(ws).save(cid, AuthoredSave(**action))


def remove_photo(ws, cid, pid):
    action = saved_action(ws, cid)
    action["authored"]["brief"]["products"] = [
        p for p in action["authored"]["brief"]["products"] if p["id"] != pid
    ]
    action["authored"].get("form_drafts", {}).get("product_images", {}).pop(pid, None)
    return save_action(ws, cid, action)


def upload_rows(ws, reference):
    with ws.storage.connect() as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table} WHERE reference=?", (reference,)).fetchone()[
                0
            ]
            for table in ("uploads", "file_owners")
        )


def pending_upload(ws, cid):
    from campaign_studio.cleanup import Cleanup

    return [item for item in Cleanup(ws.state).status(cid)["pending"] if item["kind"] == "upload"]


def test_repeated_add_remove_returns_files_and_rows_to_baseline(ui):
    client, ws = ui
    cid, _ = Intake(ws.storage).new()
    before = counts(ws)
    for _ in range(3):
        _, pid, reference = added_photo(client, ws, cid)
        remove_photo(ws, cid, pid)
        assert not ws.storage.path(reference).exists()
        assert upload_rows(ws, reference) == (0, 0)
        assert pending_upload(ws, cid) == []
        assert counts(ws)[:2] == before[:2]
        assert client.get("/uploads/" + reference.rsplit("/", 1)[1]).status_code == 404
    with ws.storage.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_replacement_retires_only_old_upload_and_ordinary_save_does_no_cleanup(ui, monkeypatch):
    from campaign_studio.cleanup import Cleanup

    client, ws = ui
    cid, _, reference = added_photo(client, ws)
    replacement = Intake(ws.storage).upload(photo(), "image/png", cid, "product", "Replacement")
    unused = Intake(ws.storage).upload(photo(), "image/png", cid, "product", "Still selecting")
    action = saved_action(ws, cid)
    action["authored"]["brief"]["products"][0]["source"]["path"] = replacement["reference"]
    save_action(ws, cid, action)
    assert upload_rows(ws, reference) == (0, 0)
    for kept in (replacement["reference"], unused["reference"]):
        assert upload_rows(ws, kept) == (1, 1)
        assert ws.storage.path(kept).read_bytes() == photo()
    monkeypatch.setattr(
        Cleanup, "plan_upload_retirement", lambda *a: pytest.fail("No detached image")
    )
    monkeypatch.setattr(
        Cleanup, "resume_upload", lambda *a: pytest.fail("No cleanup on ordinary saves")
    )
    action = saved_action(ws, cid)
    action["authored"]["brief"]["brand"]["text_color"] = "#334455"
    save_action(ws, cid, action)


@pytest.mark.parametrize(
    "retainer", ["product", "form", "logo", "source", "replay", "other-campaign", "run", "legacy"]
)
def test_detached_upload_is_preserved_when_a_saved_record_still_references_it(ui, retainer):
    import json

    from campaign_studio.campaign_drafts import read, write
    from campaign_studio.state import timestamp

    client, ws = ui
    cid, pid, reference = added_photo(client, ws)
    document = read(ws.storage, cid)
    if retainer == "product":
        duplicate = copy.deepcopy(document["authored"]["brief"]["products"][0])
        duplicate["id"] = "retained-product"
        document["authored"]["brief"]["products"].append(duplicate)
    elif retainer == "form":
        document["authored"].setdefault("form_drafts", {})["product_images"] = {
            "another-product": {"path": reference}
        }
    elif retainer == "logo":
        document["authored"]["brief"]["brand"]["logo_path"] = reference
    elif retainer == "source":
        document["source_assets"] = {pid: {"asset": {"reference": reference}}}
    elif retainer == "replay":
        document["authored"]["brief"]["replay"] = {"sources": {pid: {"path": reference}}}
    elif retainer == "other-campaign":
        other = Intake(ws.storage).new()[0]
        other_document = read(ws.storage, other)
        other_document["authored"]["brief"]["brand"]["logo_path"] = reference
        write(ws.storage, other, other_document)
    elif retainer in ("run", "legacy"):
        with ws.storage.connect() as db:
            db.execute(
                "INSERT INTO runs(id,campaign_id,plan_ref,plan_json,digest,state,allowance,"
                "created_at) "
                "VALUES(?,?,?,?,?,'complete',0,?)",
                (
                    uuid4().hex,
                    cid,
                    "legacy-plan.json",
                    json.dumps({"input": reference}) if retainer == "run" else None,
                    "digest",
                    timestamp(),
                ),
            )
    write(ws.storage, cid, document)
    remove_photo(ws, cid, pid)
    assert upload_rows(ws, reference) == (1, 1)
    assert ws.storage.path(reference).read_bytes() == photo()
    assert pending_upload(ws, cid) == []


def test_removing_unregistered_or_run_source_never_deletes_it(ui):
    from campaign_studio.campaign_drafts import read, write

    client, ws = ui
    cid, pid, original = added_photo(client, ws)
    project = ws.storage.project(cid)
    generated = project.ref(f"Runs/{uuid4().hex}/Requests/{uuid4().hex}/source.png")
    receipt = generated.rsplit("/", 1)[0] + "/response.json"
    project.write(generated, photo())
    project.write(receipt, b'{"fixture":true}')
    document = read(ws.storage, cid)
    document["authored"]["brief"]["products"][0]["source"]["path"] = generated
    write(ws.storage, cid, document)
    remove_photo(ws, cid, pid)
    assert ws.storage.path(generated).read_bytes() == photo()
    assert ws.storage.path(receipt).is_file()
    assert upload_rows(ws, original) == (1, 1)  # No broad sweep of older unused uploads.
    untracked = project.ref("Assets/Products/untracked.png")
    ws.storage.write_new(untracked, photo())
    document = read(ws.storage, cid)
    document["authored"]["brief"]["products"] = [{"id": pid, "source": {"path": untracked}}]
    write(ws.storage, cid, document)
    remove_photo(ws, cid, pid)
    assert ws.storage.path(untracked).read_bytes() == photo()


@pytest.mark.parametrize("failure", ["stale", "transaction"])
def test_rejected_or_failed_removal_preserves_photo_and_has_no_intent(ui, monkeypatch, failure):
    from campaign_studio import authored
    from campaign_studio.workspace import ActionError

    client, ws = ui
    cid, _, reference = added_photo(client, ws)
    request = saved_action(ws, cid)
    request["authored"]["brief"]["products"] = []
    if failure == "stale":
        request["version"] = "old-version"
    else:
        original = authored.write_draft

        def failed(*args, **kwargs):
            original(*args, **kwargs)
            raise StorageError("Injected error before transaction commit")

        monkeypatch.setattr(authored, "write_draft", failed)
    with pytest.raises((ActionError, StorageError)):
        save_action(ws, cid, request)
    assert upload_rows(ws, reference) == (1, 1)
    assert ws.storage.path(reference).read_bytes() == photo()
    assert pending_upload(ws, cid) == []
    assert len(AuthoredProjects(ws).read(cid)["authored"]["brief"]["products"]) == 1


@pytest.mark.parametrize("recovery", ["retry", "restart", "readopt"])
def test_unlink_failure_retains_ownership_and_recovers_safely(ui, monkeypatch, recovery):
    from campaign_studio.campaign_drafts import read, write
    from campaign_studio.cleanup import Cleanup
    from campaign_studio.state import State

    client, ws = ui
    cid, pid, reference = added_photo(client, ws)
    original = ws.storage.unlink

    def fail(path, **kwargs):
        if path == ws.storage.path(reference):
            raise PermissionError("Injected file in use")
        return original(path, **kwargs)

    monkeypatch.setattr(ws.storage, "unlink", fail)
    removed = remove_photo(ws, cid, pid)
    assert removed["version"] == AuthoredProjects(ws).read(cid)["version"]
    assert upload_rows(ws, reference) == (1, 1)
    assert ws.storage.path(reference).read_bytes() == photo()
    intent = pending_upload(ws, cid)[0]["id"]
    monkeypatch.setattr(ws.storage, "unlink", original)
    if recovery == "readopt":
        document = read(ws.storage, cid)
        document["authored"]["brief"]["brand"]["logo_path"] = reference
        write(ws.storage, cid, document)
    result = (
        Cleanup(State(ws.storage)).resume_all()
        if recovery == "restart"
        else [Cleanup(ws.state).retry(cid, intent)]
    )
    assert result[0]["state"] == "complete"
    assert pending_upload(ws, cid) == []
    assert upload_rows(ws, reference) == ((1, 1) if recovery == "readopt" else (0, 0))
    assert ws.storage.path(reference).exists() == (recovery == "readopt")


def test_active_export_snapshot_defers_detached_upload_cleanup(ui):
    from types import SimpleNamespace

    from campaign_studio.cleanup import Cleanup

    client, ws = ui
    cid, pid, reference = added_photo(client, ws)
    job = SimpleNamespace(
        closed=False,
        snapshot={
            "plan": SimpleNamespace(assets={}),
            "sources": {pid: SimpleNamespace(reference=reference)},
        },
    )
    ws.state.delivery_jobs = {"active": job}
    remove_photo(ws, cid, pid)
    assert upload_rows(ws, reference) == (1, 1)
    assert ws.storage.path(reference).read_bytes() == photo()
    intent = pending_upload(ws, cid)[0]["id"]
    job.closed = True
    assert Cleanup(ws.state).retry(cid, intent)["state"] == "complete"
    assert upload_rows(ws, reference) == (0, 0)
    ws.state.delivery_jobs.clear()


def test_intent_and_removal_roll_back_together_if_the_save_transaction_fails(ui, monkeypatch):
    from campaign_studio.cleanup import Cleanup

    client, ws = ui
    cid, pid, reference = added_photo(client, ws)
    before = saved_action(ws, cid)
    original = Cleanup.plan_upload_retirement

    def fail(self, *args):
        assert original(self, *args)
        raise StorageError("Injected failure after retirement intent, before commit")

    monkeypatch.setattr(Cleanup, "plan_upload_retirement", fail)
    with pytest.raises(StorageError):
        remove_photo(ws, cid, pid)
    assert saved_action(ws, cid) == before
    assert upload_rows(ws, reference) == (1, 1)
    assert ws.storage.path(reference).read_bytes() == photo()
    assert pending_upload(ws, cid) == []


@pytest.mark.parametrize("busy", ["export", "production"])
def test_busy_export_or_production_defers_cleanup_without_blocking_the_save(ui, busy):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from campaign_studio.cleanup import Cleanup

    client, ws = ui
    cid, pid, reference = added_photo(client, ws)
    guard = ws.state.__dict__.setdefault("delivery_guard", threading.RLock())
    ready, release = threading.Event(), threading.Event()

    def hold():
        with guard if busy == "export" else ws.storage.exclusive_run():
            ready.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold)
        assert ready.wait(2)
        try:
            result = pool.submit(remove_photo, ws, cid, pid).result(timeout=2)
            assert result["version"] == saved_action(ws, cid)["version"]
            assert not holder.done()
            assert upload_rows(ws, reference) == (1, 1)
            intent = pending_upload(ws, cid)[0]["id"]
        finally:
            release.set()
        holder.result(timeout=2)
    assert Cleanup(ws.state).retry(cid, intent)["state"] == "complete"
    assert upload_rows(ws, reference) == (0, 0)
