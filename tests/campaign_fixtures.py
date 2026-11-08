"""Explicit test setup; the application has no dedicated example-opening route."""

import hashlib
import io
import json
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.exporting import package_plan, prepare_zip_download, render_report
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_replay, write_sample
from campaign_studio.state import timestamp


def authored_edit(client, campaign_id):
    """Capture the current HTTP Save payload, including its actual concurrency tokens."""
    response = client.get(f"/api/motif/campaigns/{campaign_id}/authored")
    assert response.status_code == 200, response.text
    current = response.json()
    return {key: current[key] for key in ("version", "base", "authored")}


def seed_campaign(workspace, *, replay=False, planned=False):
    """Prepare the current document and supplied sources without creating final PNGs."""
    brief = write_replay(workspace.storage) if replay else write_sample(workspace.storage)[1]
    value = brief.model_dump(mode="json")
    write_draft(
        workspace.storage,
        brief.campaign_id,
        {
            "value": value,
            "authored": {"brief": value},
            "version": uuid4().hex,
            "composition_schema": 1,
        },
    )
    if planned:
        workspace.state.create(make_plan(brief, workspace.storage), 0)
    return brief.campaign_id


def zip_root(archive):
    """Every current delivery ZIP has exactly one portable campaign directory."""
    roots = {name.split("/", 1)[0] for name in archive.namelist()}
    assert len(roots) == 1
    root = roots.pop()
    assert root not in {"", ".", ".."}
    assert f"{root}/manifest.json" in archive.namelist()
    return root


def run_folder(state, run_id):
    return state.storage.project(state.run(run_id)["campaign_id"]).run(run_id)


def export_ref(state, revision_id, relative):
    run_id = state.revision(revision_id)["run_id"]
    return state.storage.project(state.run(run_id)["campaign_id"]).ref("Exports/" + relative)


def download_payloads(state, revision_id, *, allow_partial=False, expected_snapshot=None):
    """Open the actual ZIP and return each file under its portable name."""
    with (
        prepare_zip_download(
            state, revision_id, allow_partial=allow_partial, expected_snapshot=expected_snapshot
        ) as download,
        ZipFile(download.stream) as archive,
    ):
        prefix = zip_root(archive) + "/"
        return {name.removeprefix(prefix): archive.read(name) for name in archive.namelist()}


def _legacy_payloads(state, target, allow_partial):
    snapshot = state.metadata_snapshot(target.revision_id, target=target)["snapshot_id"]
    revision, manifest, images = package_plan(
        state, target.revision_id, allow_partial=allow_partial, expected_snapshot=snapshot
    )
    manifest["report_created_at"] = timestamp()
    payloads = {}
    for name, reference, digest in images:
        data = state.storage.path(reference).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        payloads[name] = data
    payloads["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    payloads["index.html"] = render_report(state, revision, manifest)
    return manifest["package_name"], snapshot, payloads


def _register_legacy_export(state, target, snapshot, reference, digest):
    artifact = str(uuid4())
    with state.storage.connect() as db:
        db.execute(
            "INSERT INTO package_exports VALUES (?,?,?,?,?,?,?)",
            (artifact, target.run_id, target.revision_id, snapshot, reference, digest, timestamp()),
        )
    return artifact


def legacy_report(state, target, *, allow_partial=False):
    """Seed an exported v0.2.0 folder with its original ownership and integrity record."""
    root, snapshot, payloads = _legacy_payloads(state, target, allow_partial)
    project = state.storage.project(target.campaign_id)
    destination = project.ref(f"Exports/{root}")
    for name, data in payloads.items():
        project.write(f"{destination}/{name}", data)
    ref = f"{destination}/index.html"
    digest = hashlib.sha256(payloads["manifest.json"] + payloads["index.html"]).hexdigest()
    report_id = _register_legacy_export(state, target, snapshot, ref, digest)
    return {
        "artifact_id": report_id,
        "location": str(state.storage.path(destination)),
        "reference": ref,
        "url": f"/reports/{report_id}/index.html",
    }


def legacy_zip(state, target, *, allow_partial=False):
    """Seed the managed archive and database entry produced by v0.2.0 Download ZIP."""
    root, snapshot, payloads = _legacy_payloads(state, target, allow_partial)
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(f"{root}/{name}", payload)
    content = stream.getvalue()
    project = state.storage.project(target.campaign_id)
    ref = project.ref(f"Exports/archives/{uuid4().hex}.zip")
    project.write(ref, content)
    artifact = _register_legacy_export(
        state, target, snapshot, ref, hashlib.sha256(content).hexdigest()
    )
    base = (
        f"/api/campaigns/{target.campaign_id}/runs/{target.run_id}/revisions/{target.revision_id}"
    )
    return {
        "artifact_id": artifact,
        "content": content,
        "filename": root + ".zip",
        "reference": ref,
        "url": f"{base}/packages/{artifact}/download",
    }


def independent_brief(state, brief, campaign_id):
    from campaign_studio.brief import Brief
    from campaign_studio.intake import Intake

    state.register_campaign(campaign_id, brief.brand.name)
    intake = Intake(state.storage)
    value = brief.model_dump(mode="json") | {"campaign_id": campaign_id}

    def copied(ref, role):
        return intake.upload(state.storage.path(ref).read_bytes(), None, campaign_id, role)[
            "reference"
        ]

    value["brand"]["logo_path"] = copied(value["brand"]["logo_path"], "logo")
    for product in value["products"]:
        if product["source"].get("path"):
            product["source"]["path"] = copied(product["source"]["path"], "product")
    if value.get("replay"):
        for entry in value["replay"]["sources"].values():
            entry["path"] = copied(entry["path"], "product")
    return Brief.model_validate(value)
