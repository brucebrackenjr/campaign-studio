"""Final PNGs are delivery work, independent of later edits and without retained history."""

import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image

from campaign_studio import delivery
from campaign_studio.brief import Brief
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.imaging import compose, load_image
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_sample
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage, StorageError


@pytest.fixture
def case(tmp_path):
    storage = Storage(tmp_path / "workspace")
    _, brief = write_sample(storage, 2, True)
    state = State(storage)
    plan = make_plan(brief, storage)
    # Three distinct formats, one product, real full-size compositing and inspection.
    plan.outputs = plan.outputs[:3]
    snapshot = {
        "plan": plan,
        "version": "a" * 32,
        "sources": {p.id: plan.assets[f"products/{p.id}"] for p in brief.products},
        "metadata": {plan.outputs[0].id: {"description": "A producer's approved description."}},
        "campaign_metrics": state.campaign_metrics(brief.campaign_id),
    }
    return state, snapshot


def files(storage):
    return {
        str(p.relative_to(storage.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in storage.root.rglob("*")
        if p.is_file()
    }


def unpack(download):
    with ZipFile(download.stream) as archive:
        assert archive.testzip() is None
        name = next(n for n in archive.namelist() if n.endswith("/manifest.json"))
        manifest = json.loads(archive.read(name))
        return manifest, {n: archive.read(n) for n in archive.namelist()}


def test_export_uses_current_copy_color_crop_logo_and_real_pngs(case):
    state, original = case
    snapshot = deepcopy(original)
    value = snapshot["plan"].brief.model_dump(mode="json")
    value["markets"]["en-US"]["copy"]["message"] = "Current saved message"
    value["brand"]["text_color"] = "#33CC99"
    value["brand"]["palette"].append("#33CC99")
    value["products"][0]["framing"] = {
        "en-US": {
            "1:1": {
                "zoom": 1.3,
                "focal_x": 0.6,
                "focal_y": 0.4,
                "logo_placement": {"left": 0.6, "top": 0.1, "width": 0.25},
            }
        }
    }
    snapshot["plan"] = make_plan(Brief.model_validate(value), state.storage)
    snapshot["plan"].outputs = snapshot["plan"].outputs[:3]
    before = files(state.storage)
    with delivery.prepare_current_zip(state, snapshot) as download:
        manifest, contents = unpack(download)
        assert download.size == download.stream.seek(0, 2)
    assert files(state.storage) == before
    assert manifest["composition_version"] == snapshot["version"]
    assert len([n for n in contents if n.endswith(".png")]) == 3
    assert manifest["outputs"][0]["accessible_description"] == "A producer's approved description."
    record = manifest["outputs"][0]
    payload = contents[f"{manifest['package_name']}/{record['file']}"]
    assert hashlib.sha256(payload).hexdigest() == record["sha256"]
    with Image.open(io.BytesIO(payload)) as actual:
        assert actual.size == (1080, 1080)
        assert actual.info["icc_profile"]
        output = snapshot["plan"].outputs[0]
        with (
            load_image(state.storage.path(snapshot["sources"][output.product_id].reference))[
                0
            ] as scene,
            load_image(state.storage.path(snapshot["plan"].assets["logo"].reference))[0] as logo,
            compose(
                scene, logo, output.text, output.ratio, "#33CC99", None, output.framing
            ) as expected,
        ):
            assert actual.convert("RGB").tobytes() == expected.convert("RGB").tobytes()
    report = contents[f"{manifest['package_name']}/index.html"].decode()
    assert "Saved campaign export" in report and "works offline" in report
    assert "Current saved message" not in report  # Wording is on the actual PNG, in manifest.
    assert record["copy"]["message"] == "Current saved message"
    assert "Saved revision" not in report


def test_repeated_exports_and_individual_downloads_do_not_create_files_or_rows(
    case, monkeypatch, tmp_path
):
    state, snapshot = case
    snapshot["plan"].outputs = snapshot["plan"].outputs[:1]
    temp = tmp_path / "temporary"
    temp.mkdir()
    monkeypatch.setattr(delivery.tempfile, "tempdir", str(temp))
    before = files(state.storage)
    for _ in range(5):
        with delivery.prepare_current_zip(state, snapshot) as download:
            assert unpack(download)[0]["complete"]
        payload, filename = delivery.current_image(state, snapshot, snapshot["plan"].outputs[0].id)
        assert payload.startswith(b"\x89PNG") and filename.endswith(".png")
        assert list(temp.iterdir()) == []
    assert files(state.storage) == before
    assert not list(state.storage.root.rglob("Exports"))
    assert not list(state.storage.root.rglob("Outputs"))


def test_export_captures_sources_before_later_save_or_source_removal(case, monkeypatch):
    state, snapshot = case
    snapshot["plan"].outputs = snapshot["plan"].outputs[:2]
    render = delivery.render_png
    captured = deepcopy(snapshot)
    count = 0

    def concurrent_edit(saved, paths, output, destination, **kwargs):
        nonlocal count
        if count == 0:
            # Simulate a later autosave and attachment deletion after snapshot capture.
            snapshot["version"] = "b" * 32
            snapshot["plan"].brief.markets["en-US"].text.message = "A later edit"
            for asset in snapshot["sources"].values():
                state.storage.path(asset.reference).unlink()
        count += 1
        return render(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", concurrent_edit)
    with delivery.prepare_current_zip(state, captured) as download:
        manifest, _ = unpack(download)
    assert manifest["composition_version"] == "a" * 32
    assert all(o["copy"]["message"] != "A later edit" for o in manifest["outputs"])


@pytest.mark.parametrize("failure", ["render", "report", "source", "interrupt"])
def test_failure_cleans_temporary_files_and_closes_download_stream(
    case, monkeypatch, tmp_path, failure
):
    state, snapshot = case
    temp = tmp_path / "temporary"
    temp.mkdir()
    monkeypatch.setattr(delivery.tempfile, "tempdir", str(temp))
    streams = []
    factory = delivery.tempfile.SpooledTemporaryFile

    def spool(*args, **kwargs):
        stream = factory(*args, **kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(delivery.tempfile, "SpooledTemporaryFile", spool)
    if failure == "source":
        asset = next(iter(snapshot["sources"].values()))
        state.storage.path(asset.reference).write_bytes(b"changed")
    else:

        def fail(*_args, **_kwargs):
            raise KeyboardInterrupt if failure == "interrupt" else OSError("injected failure")

        monkeypatch.setattr(delivery, "document" if failure == "report" else "render_png", fail)
    before = files(state.storage)
    with pytest.raises((OSError, StateError, KeyboardInterrupt)):
        delivery.prepare_current_zip(state, snapshot)
    assert streams and all(s.closed for s in streams)
    assert list(temp.iterdir()) == []
    assert files(state.storage) == before


def test_missing_sources_require_explicit_partial_and_never_dispatch_generation(case):
    state, snapshot = case
    snapshot["plan"].outputs = make_plan(snapshot["plan"].brief, state.storage).outputs
    snapshot["sources"].pop("product-2")
    with pytest.raises(StateError, match="Some images"):
        delivery.prepare_current_zip(state, snapshot)
    with delivery.prepare_current_zip(state, snapshot, allow_partial=True) as download:
        manifest, _ = unpack(download)
    assert not manifest["complete"]
    assert sum(bool(o["file"]) for o in manifest["outputs"]) == 3
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_partial_export_blocks_prohibited_copy_but_legacy_logo_flag_is_optional(case):
    state, snapshot = case
    plan = snapshot["plan"]
    plan.outputs[-1].text.message = "TESTBLOCK"
    with pytest.raises(StateError, match="prohibited wording"):
        delivery.prepare_current_zip(state, snapshot, allow_partial=True)
    plan.outputs[-1].text.message = "Clean message"
    plan.brief.brand.require_logo = True
    plan.assets.pop("logo")
    result = delivery.prepare_current_zip(state, snapshot, allow_partial=True)
    result.close()


def test_unregistered_source_is_rejected_even_with_matching_hash(case):
    state, snapshot = case
    asset = next(iter(snapshot["sources"].values()))
    with state.storage.connect() as db:
        db.execute("DELETE FROM file_owners WHERE reference=?", (asset.reference,))
    with pytest.raises(StorageError, match="registered"):
        delivery.prepare_current_zip(state, snapshot)


def test_live_report_uses_current_version_no_permanent_images_and_rejects_changed_report(
    case, monkeypatch
):
    state, snapshot = case
    before = files(state.storage)
    handoff = Handoff(state)
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    created = handoff.create_current_preview(
        snapshot["plan"].brief.campaign_id, DeliveryAction(version=snapshot["version"])
    )
    campaign, version, partial, timestamp, fingerprint, _ = created["url"].split("/")[3:]
    args = (campaign, version, bool(int(partial)), int(timestamp), fingerprint)
    html = handoff.current_preview_file(*args, "index.html").decode()
    assert "Keep Motif running" in html and "Download full-size PNG" not in html
    manifest = json.loads(handoff.current_preview_file(*args, "manifest.json"))
    payload = handoff.current_preview_file(*args, manifest["outputs"][0]["file"])
    with Image.open(io.BytesIO(payload)) as image:
        assert max(image.size) == 900
    assert files(state.storage) == before
    snapshot["metadata"] = {}
    with pytest.raises(StateError, match="Reopen the report"):
        handoff.current_preview_file(*args, "index.html")


def test_manifest_and_report_escape_text_and_refuse_private_metadata(case):
    _, snapshot = case
    snapshot["plan"].brief.name = "<script>alert('x')</script>"
    manifest = delivery.delivery_manifest(snapshot)
    html = delivery.document(snapshot, manifest, "index.html").decode()
    assert "<script>" not in html and "&lt;script&gt;" in html
    snapshot["metadata"][snapshot["plan"].outputs[0].id]["description"] = (
        "/Users/private/secret.txt"
    )
    with pytest.raises(StateError, match="private paths"):
        delivery.delivery_manifest(snapshot)


def test_export_progress_reports_real_work_and_disappears_when_response_closes(case, monkeypatch):
    from campaign_studio.delivery_jobs import DeliveryJob

    state, snapshot = case
    snapshot["plan"].outputs = snapshot["plan"].outputs[:1]
    handoff = Handoff(state)
    campaign_id = snapshot["plan"].brief.campaign_id
    operation_id = "b" * 32
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    observed = []
    original_render = delivery.render_png
    original_document = delivery.document
    original_package = DeliveryJob._package

    def render(*args, **kwargs):
        observed.append(handoff.current_progress(campaign_id, operation_id)["stage"])
        return original_render(*args, **kwargs)

    def document(*args, **kwargs):
        observed.append(handoff.current_progress(campaign_id, operation_id)["stage"])
        return original_document(*args, **kwargs)

    def package(job):
        result = original_package(job)
        observed.append(handoff.current_progress(campaign_id, operation_id)["stage"])
        return result

    monkeypatch.setattr(delivery, "render_png", render)
    monkeypatch.setattr(delivery, "document", document)
    monkeypatch.setattr(DeliveryJob, "_package", package)
    download = handoff.create_current_zip(
        campaign_id, DeliveryAction(version=snapshot["version"], operation_id=operation_id)
    )
    assert list(dict.fromkeys(observed)) == [
        "Creating finished images",
        "Preparing ZIP",
        "Download ready",
    ]
    assert handoff.current_progress(campaign_id, operation_id)["status"] == "ready"
    download.close()
    assert handoff.current_progress(campaign_id, operation_id)["status"] == "unavailable"
    assert not state.delivery_jobs


def test_real_saved_composition_handoff_uses_current_document(case):
    from campaign_studio.campaign_drafts import write
    from campaign_studio.workspace import Workspace

    state, snapshot = case
    campaign_id = snapshot["plan"].brief.campaign_id
    value = snapshot["plan"].brief.model_dump(mode="json")
    value["markets"]["en-US"]["copy"]["message"] = "Saved directly in Campaign"
    write(
        state.storage,
        campaign_id,
        {
            "authored": {"brief": value},
            "version": snapshot["version"],
            "composition_schema": 1,
            "creative_metadata": snapshot["metadata"],
        },
    )
    current = Workspace(state).current_composition(campaign_id)
    assert current["plan"].outputs[0].text.message == "Saved directly in Campaign"
    before = files(state.storage)
    handoff = Handoff(state)
    with handoff.create_current_zip(
        campaign_id, DeliveryAction(version=current["version"])
    ) as download:
        manifest, _ = unpack(download)
    assert all(o["copy"]["message"] == "Saved directly in Campaign" for o in manifest["outputs"])
    assert len(manifest["outputs"]) == 6
    assert files(state.storage) == before
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_http_delivery_streams_current_zip_png_and_report_without_provider(case):
    from fastapi.testclient import TestClient

    from campaign_studio.app import create_app
    from campaign_studio.campaign_drafts import write
    from campaign_studio.config import Settings

    state, snapshot = case
    campaign_id = snapshot["plan"].brief.campaign_id
    write(
        state.storage,
        campaign_id,
        {
            "authored": {"brief": snapshot["plan"].brief.model_dump(mode="json")},
            "version": snapshot["version"],
            "composition_schema": 1,
        },
    )
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Exports must never construct an AI provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        base = f"/api/campaigns/{campaign_id}"
        body = {"version": snapshot["version"], "operation_id": "c" * 32}
        response = client.post(f"{base}/delivery/zip", json=body)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        with ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.testzip() is None
            assert len([n for n in archive.namelist() if n.endswith(".png")]) == 6
        assert (
            client.get(f"{base}/delivery/progress/{body['operation_id']}").json()["status"]
            == "unavailable"
        )
        preview = client.post(
            f"{base}/delivery/report-preview", json={"version": snapshot["version"]}
        )
        assert preview.status_code == 200, preview.text
        report = client.get(preview.json()["url"])
        assert report.status_code == 200 and "Keep Motif running" in report.text
        png = client.get(f"{base}/composition/{snapshot['version']}/download/product-1/en-US/1x1")
        assert png.status_code == 200 and png.headers["content-type"] == "image/png"
        with Image.open(io.BytesIO(png.content)) as image:
            assert image.size == (1080, 1080)
        stale = client.post(f"{base}/delivery/zip", json={"version": "d" * 32})
        assert stale.status_code == 409
        assert not app.state.workspace.state.live_progress
        assert not list(state.storage.root.rglob("Exports"))
        assert not list(state.storage.root.rglob("Outputs"))


@pytest.mark.parametrize("replay", [False, True])
def test_sample_cli_renders_only_during_download_without_retained_outputs(tmp_path, capsys, replay):
    from campaign_studio.cli import main

    workspace = tmp_path / "sample"
    args = ["sample-run", "--data-dir", str(workspace)] + (["--replay"] if replay else [])
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["complete"] and result["completed_images"] == 12
    assert result["corrected_images"] == 6
    assert result["provider_requests"] == 0
    assert result["handoff_zip_bytes"] > 1000
    assert result["composition_version"]
    assert not list(workspace.rglob("Exports"))
    assert not list(workspace.rglob("Outputs"))
    with Storage(workspace).connect() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM campaign_drafts").fetchone()[0] == 1


def thirty_image_case(state, snapshot):
    from campaign_studio.campaign_drafts import write

    value = snapshot["plan"].brief.model_dump(mode="json")
    value["markets"]["es-MX"] = deepcopy(value["markets"]["en-US"])
    for index in range(3, 6):
        product = deepcopy(value["products"][0])
        product.update(id=f"product-{index}", name=f"Product {index}")
        value["products"].append(product)
    snapshot["plan"] = make_plan(Brief.model_validate(value), state.storage)
    snapshot["sources"] = {
        p.id: snapshot["plan"].assets[f"products/{p.id}"] for p in snapshot["plan"].brief.products
    }
    write(
        state.storage,
        value["campaign_id"],
        {
            "authored": {"brief": value},
            "version": snapshot["version"],
            "composition_schema": 1,
        },
    )
    return snapshot


def test_thirtieth_image_export_retries_failed_fifteenth_without_redoing_verified_images(
    case, monkeypatch, tmp_path
):
    from collections import Counter

    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    handoff = Handoff(state)
    campaign_id = snapshot["plan"].brief.campaign_id
    action = DeliveryAction(version=snapshot["version"], operation_id="e" * 32)
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(delivery.tempfile, "tempdir", str(temporary))
    original = delivery.render_png
    counts = Counter()
    failing = snapshot["plan"].outputs[14].id
    fail = True

    def controlled(saved, paths, output, destination, **kwargs):
        counts[output.id] += 1
        if output.id == failing and fail:
            raise OSError("Injected fifteenth-image render failure")
        return original(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", controlled)
    before = files(state.storage)
    retained_names = None
    for _ in range(3):
        with pytest.raises(StateError) as error:
            handoff.create_current_zip(campaign_id, action)
        assert error.value.code == "delivery_retry"
        progress = handoff.current_progress(campaign_id, action.operation_id)
        assert progress["completed"] == 29 and progress["total"] == 30
        assert progress["failed_output"]["id"] == failing
        assert progress["status"] == "failed"
        assert all(counts[o.id] == 1 for o in snapshot["plan"].outputs[:14])
        assert len(state.delivery_jobs) == 1
        job = state.delivery_jobs[action.operation_id]
        assert len(job.ready) == 29
        retained = [*job.ready.values(), *job.sources.values()]
        assert all(not stream.closed for stream in retained)
        # Windows TemporaryFile entries remain visible while their handles are
        # open; POSIX unlinks them immediately. Only the retry job's retained
        # streams may exist, and repeated retries must not add temporary files.
        expected_names = {
            Path(stream.name).resolve() for stream in retained if isinstance(stream.name, str)
        }
        assert {path.resolve() for path in temporary.iterdir()} == expected_names
        if retained_names is None:
            retained_names = expected_names
        assert expected_names == retained_names
        assert files(state.storage) == before
    job = state.delivery_jobs[action.operation_id]
    retained = list(job.ready.values()) + list(job.sources.values())
    fail = False
    with handoff.create_current_zip(campaign_id, action) as download:
        manifest, contents = unpack(download)
        assert manifest["complete"]
        assert len([name for name in contents if name.endswith(".png")]) == 30
        for item in manifest["outputs"]:
            payload = contents[f"{manifest['package_name']}/{item['file']}"]
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]
        assert len(job.ready) == 30
    assert all(counts[o.id] == 1 for o in snapshot["plan"].outputs if o.id != failing)
    assert counts[failing] == 7
    assert all(stream.closed for stream in retained)
    assert state.delivery_jobs == {} and files(state.storage) == before
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize("finish", ["cancel", "changed", "expire"])
def test_failed_export_job_is_discarded_on_cancel_changed_composition_or_expiry(
    case, monkeypatch, finish
):
    from campaign_studio.campaign_drafts import read, write

    state, snapshot = case
    value = snapshot["plan"].brief.model_dump(mode="json")
    campaign_id = value["campaign_id"]
    write(
        state.storage,
        campaign_id,
        {"authored": {"brief": value}, "version": snapshot["version"], "composition_schema": 1},
    )
    handoff = Handoff(state)
    action = DeliveryAction(version=snapshot["version"], operation_id="f" * 32)
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    original = delivery.render_png

    def fail_second(saved, paths, output, destination, **kwargs):
        if output.id == snapshot["plan"].outputs[1].id:
            raise OSError("Injected second image failure")
        return original(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", fail_second)
    with pytest.raises(StateError, match="Created 2 of 3 images"):
        handoff.create_current_zip(campaign_id, action)
    job = state.delivery_jobs[action.operation_id]
    retained = list(job.sources.values()) + list(job.ready.values())
    if finish == "cancel":
        assert (
            handoff.cancel_current_delivery(campaign_id, action.operation_id)["status"]
            == "cancelled"
        )
    elif finish == "changed":
        current = read(state.storage, campaign_id)
        current["version"] = "1" * 32
        write(state.storage, campaign_id, current)
        with pytest.raises(StateError) as error:
            handoff.create_current_zip(campaign_id, action)
        assert error.value.code == "composition_conflict"
    else:
        job.timer.function()  # Invoke the actual expiry callback without waiting ten minutes.
    assert all(stream.closed for stream in retained)
    assert job.closed and not state.delivery_jobs


def test_zip_packaging_failure_retries_packaging_without_rendering_again(case, monkeypatch):
    from campaign_studio.campaign_drafts import write

    state, snapshot = case
    campaign_id = snapshot["plan"].brief.campaign_id
    write(
        state.storage,
        campaign_id,
        {
            "authored": {"brief": snapshot["plan"].brief.model_dump(mode="json")},
            "version": snapshot["version"],
            "composition_schema": 1,
        },
    )
    handoff = Handoff(state)
    action = DeliveryAction(version=snapshot["version"], operation_id="2" * 32)
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    original_document = delivery.document
    original_render = delivery.render_png
    count = 0

    def render(*args, **kwargs):
        nonlocal count
        count += 1
        return original_render(*args, **kwargs)

    def fail(*_args, **_kwargs):
        raise OSError("Injected ZIP report failure")

    monkeypatch.setattr(delivery, "render_png", render)
    monkeypatch.setattr(delivery, "document", fail)
    with pytest.raises(StateError) as error:
        handoff.create_current_zip(campaign_id, action)
    assert error.value.code == "delivery_retry"
    assert handoff.current_progress(campaign_id, action.operation_id)["completed"] == 3
    monkeypatch.setattr(delivery, "document", original_document)
    with handoff.create_current_zip(campaign_id, action) as download:
        assert unpack(download)[0]["complete"]
    assert count == 3 and not state.delivery_jobs


def test_export_continues_after_early_and_late_failure_then_retries_each_once(case, monkeypatch):
    from collections import Counter

    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    handoff = Handoff(state)
    action = DeliveryAction(version=snapshot["version"], operation_id="3" * 32)
    campaign_id = snapshot["plan"].brief.campaign_id
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    original = delivery.render_png
    counts = Counter()
    failed_ids = {snapshot["plan"].outputs[0].id, snapshot["plan"].outputs[14].id}
    call_order = []
    messages = []

    def fail_once(saved, paths, output, destination, **kwargs):
        counts[output.id] += 1
        call_order.append(output.id)
        messages.append(handoff.current_progress(campaign_id, action.operation_id))
        if output.id in failed_ids and counts[output.id] == 1:
            raise OSError("Single local image failure")
        return original(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", fail_once)
    with handoff.create_current_zip(campaign_id, action) as download:
        assert len(unpack(download)[0]["outputs"]) == 30
    assert call_order[:30] == [o.id for o in snapshot["plan"].outputs]
    assert set(call_order[30:]) == failed_ids
    assert all(counts[o.id] == (2 if o.id in failed_ids else 1) for o in snapshot["plan"].outputs)
    assert any("Continuing with the other images" in (m["message"] or "") for m in messages)
    assert all(
        m["status"] == "retrying" and m["message"].startswith("Retrying ") for m in messages[30:]
    )
    assert not state.delivery_jobs


def test_four_nonconsecutive_failures_keep_successes_and_retry_only_the_last_missing_image(
    case, monkeypatch
):
    from collections import Counter

    state, snapshot = case
    snapshot = thirty_image_case(state, snapshot)
    handoff = Handoff(state)
    action = DeliveryAction(version=snapshot["version"], operation_id="4" * 32)
    campaign_id = snapshot["plan"].brief.campaign_id
    monkeypatch.setattr(handoff, "current_snapshot", lambda *_: deepcopy(snapshot))
    outputs = snapshot["plan"].outputs
    failures = {outputs[index - 1].id for index in (3, 10, 12, 28)}
    persistent = outputs[27].id
    original = delivery.render_png
    counts, order, progress = Counter(), [], []
    keep_failing = True

    def controlled(saved, paths, output, destination, **kwargs):
        counts[output.id] += 1
        order.append(output.id)
        progress.append(handoff.current_progress(campaign_id, action.operation_id))
        if (output.id in failures and counts[output.id] == 1) or (
            output.id == persistent and keep_failing
        ):
            raise OSError("Injected nonconsecutive image failure")
        return original(saved, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", controlled)
    before = files(state.storage)
    with pytest.raises(StateError) as error:
        handoff.create_current_zip(campaign_id, action)
    assert error.value.code == "delivery_retry"
    # Every requested image was attempted before the automatic retry pass.
    assert order[:30] == [output.id for output in outputs]
    assert set(order[30:]) == failures and len(order[30:]) == 4
    retry_start = progress[30]
    assert retry_start["completed"] == 26 and retry_start["total"] == 30
    assert {item["id"] for item in retry_start["failed_outputs"]} == failures
    assert [len(item["failed_outputs"]) for item in progress[30:]] == [4, 3, 2, 1]
    assert [item["completed"] for item in progress[30:]] == [26, 27, 28, 29]
    assert all(item["status"] == "retrying" for item in progress[30:])
    final_failure = handoff.current_progress(campaign_id, action.operation_id)
    assert final_failure["completed"] == 29
    assert [item["id"] for item in final_failure["failed_outputs"]] == [persistent]
    detail = final_failure["failed_outputs"][0]
    assert detail["product_name"] and detail["ratio"] and detail["market"]
    job = state.delivery_jobs[action.operation_id]
    assert job.zip_stream is None and len(job.ready) == 29
    verified_streams = dict(job.ready)
    assert files(state.storage) == before
    keep_failing = False
    with handoff.create_current_zip(campaign_id, action) as download:
        manifest, content = unpack(download)
        assert manifest["complete"]
        assert len([name for name in content if name.endswith(".png")]) == 30
        final_progress = handoff.current_progress(campaign_id, action.operation_id)
        assert final_progress["completed"] == 30 and final_progress["failed_outputs"] == []
        assert all(job.ready[key] is stream for key, stream in verified_streams.items())
    assert order[34:] == [persistent]
    assert counts[persistent] == 3
    assert all(counts[item] == 2 for item in failures - {persistent})
    assert all(counts[item.id] == 1 for item in outputs if item.id not in failures)
    assert not state.delivery_jobs and files(state.storage) == before
    assert all(stream.closed for stream in verified_streams.values())
