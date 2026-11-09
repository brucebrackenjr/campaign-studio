"""Render one captured, saved composition into temporary delivery files.

No production runner, provider, accepted revision, or campaign output directory is
used here. The compressed source copies and rendered PNG are temporary; only the
response stream survives preparation, and its owner must close it after delivery.
"""

import hashlib
import json
import tempfile
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image

from campaign_studio.exporting import (
    PRIVATE_METADATA,
    ZipDownload,
    delivery_paths,
    render_manifest_report,
)
from campaign_studio.imaging import ImageProblem, compose, load_image, require_font, save_srgb
from campaign_studio.planning import canonical_hash, copy_findings
from campaign_studio.rendering import blocks_output, inspect_png
from campaign_studio.state import StateError
from campaign_studio.storage import delivery_component


def delivery_manifest(snapshot, *, allow_partial=False):
    """Public inventory from current saved instructions, without exposing local paths."""
    plan = snapshot["plan"]
    findings = list(plan.findings)
    for output in plan.outputs:
        findings.extend(copy_findings(plan.brief, output))
    if any(f.context and f.context.phrase and f.severity == "blocking" for f in findings):
        raise StateError("Correct prohibited wording before exporting.", code="prohibited_phrase")
    if any(f.item == "logo" and f.severity == "blocking" for f in findings):
        raise StateError("Correct the attached logo before exporting.", code="logo_presence")
    if any(f.severity == "blocking" for f in findings):
        raise StateError(
            "Correct the campaign's image or text issues before exporting. "
            "A partial download can omit missing sources, but cannot bypass these checks.",
            code="export_inputs",
        )
    metadata = snapshot.get("metadata", {})
    outputs = []
    for output in plan.outputs:
        problems = [f.evidence for f in findings if blocks_output(f, output)]
        if output.product_id not in snapshot["sources"]:
            problems.append("The source image is not ready. Open Production to prepare it.")
        detail = metadata.get(output.id, {})
        outputs.append(
            {
                "id": output.id,
                "product_id": output.product_id,
                "market": output.market,
                "ratio": output.ratio,
                "width": output.width,
                "height": output.height,
                "copy": output.text.model_dump(mode="json"),
                "file": None,
                "sha256": None,
                "omission": " ".join(dict.fromkeys(problems)) if problems else None,
                "accessible_description": detail.get("description") or None,
                "producer_metadata": {"description": detail.get("description") or None},
                "copy_provenance": {"origin": plan.brief.copy_provenance.model_dump(mode="json")},
            }
        )
    ready = [item for item in outputs if not item["omission"]]
    if not ready:
        raise StateError("No images are ready to export. Check Production and the campaign inputs.")
    if len(ready) != len(outputs) and not allow_partial:
        raise StateError(
            "Some images are not ready. Correct them, or explicitly download the available images.",
            code="partial_export",
        )
    summary = deepcopy(snapshot["campaign_metrics"])
    manifest = {
        "schema_version": 1,
        "report_format_version": 10,
        "campaign_id": plan.brief.campaign_id,
        "campaign_name": plan.brief.name,
        "composition_version": snapshot["version"],
        "complete": len(ready) == len(outputs),
        "product_names": {p.id: p.name for p in plan.brief.products},
        "logo_provenance": plan.brief.brand.logo_provenance.model_dump(mode="json"),
        "outputs": outputs,
        "sources": [
            {
                "product_id": p.id,
                "role": p.source.role,
                "provenance": p.source.provenance.model_dump(mode="json"),
                "sha256": snapshot["sources"][p.id].sha256 if p.id in snapshot["sources"] else None,
            }
            for p in plan.brief.products
        ],
        "findings": [f.model_dump(mode="json") for f in findings],
        "campaign_metrics": summary,
        "metrics": {
            "requested_images": len(outputs),
            "completed_images": len(ready),
            "local_render_seconds": None,
            "elapsed_processing_seconds": None,
        },
        "producer_metadata_snapshot": {"snapshot_id": canonical_hash(metadata)},
        "replay": (
            {"historical_requests": plan.brief.replay.historical_requests}
            if plan.brief.replay
            else None
        ),
    }
    paths = delivery_paths(manifest)
    for item in ready:
        item["file"] = paths[item["id"]]
    name = delivery_component(plan.brief.brand.name, "Campaign")
    manifest["package_name"] = f"{name}-{snapshot['version'][:8]}"
    require_public_metadata(manifest)
    return manifest


def require_public_metadata(manifest):
    serialized = json.dumps(manifest, ensure_ascii=False).replace("\\\\", "\\")
    if PRIVATE_METADATA.search(serialized):
        raise StateError("Remove private paths or credentials from campaign text before exporting.")


def source_assets(snapshot, outputs):
    plan = snapshot["plan"]
    wanted = {}
    for product_id in {o.product_id for o in outputs}:
        wanted[f"scene/{product_id}"] = snapshot["sources"][product_id]
        product = next(p for p in plan.brief.products if p.id == product_id)
        if product.source.role == "prepared_product":
            wanted[f"cutout/{product_id}"] = plan.assets[f"products/{product_id}"]
    if "logo" in plan.assets:
        wanted["logo"] = plan.assets["logo"]
    return wanted


@contextmanager
def captured_sources(state, snapshot, outputs):
    """Copy verified bytes before composition; later saves cannot alter this export."""
    project = state.storage.project(snapshot["plan"].brief.campaign_id)
    wanted = source_assets(snapshot, outputs)
    with tempfile.TemporaryDirectory(prefix="motif-delivery-") as directory:
        paths = {}
        for index, (key, asset) in enumerate(wanted.items()):
            project.require(asset.reference)
            destination = Path(directory) / f"source-{index}"
            digest = hashlib.sha256()
            try:
                with (
                    state.storage.path(asset.reference).open("rb") as source,
                    destination.open("xb") as target,
                ):
                    while chunk := source.read(256 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
            except OSError as error:
                raise StateError(
                    "A source image is unavailable. Reopen the campaign and try again."
                ) from error
            if digest.hexdigest() != asset.sha256:
                raise StateError(
                    "A source image changed. Reopen the campaign and try again.",
                    code="source_changed",
                )
            paths[key] = destination
        yield paths, Path(directory) / "rendered.png"


def image_input(value):
    if hasattr(value, "seek"):
        value.seek(0)
    return value


def image_bytes(value):
    if hasattr(value, "read_bytes"):
        return value.read_bytes()
    value.flush()
    value.seek(0)
    return value.read()


def render_png(snapshot, paths, output, destination, *, preview=False):
    plan = snapshot["plan"]
    require_font()
    with ExitStack() as resources:
        scene = resources.enter_context(
            load_image(image_input(paths[f"scene/{output.product_id}"]))[0]
        )
        logo = (
            resources.enter_context(load_image(image_input(paths["logo"]))[0])
            if "logo" in paths
            else None
        )
        key = f"cutout/{output.product_id}"
        cutout = (
            resources.enter_context(load_image(image_input(paths[key]))[0])
            if key in paths
            else None
        )
        image = resources.enter_context(
            compose(
                scene,
                logo,
                output.text,
                output.ratio,
                plan.brief.brand.text_color,
                cutout,
                output.framing,
            )
        )
        if preview:
            image.thumbnail((900, 900), Image.Resampling.LANCZOS)
        save_srgb(image, destination)
        checks = (
            [] if preview else inspect_png(destination, output, logo, plan.brief.brand.text_color)
        )
        if any(f.severity == "blocking" for f in checks):
            raise StateError(
                "An image could not be verified. No download was created. Try again.",
                code="delivery_render",
            )
        with Image.open(image_input(destination)) as saved:
            saved.load()
            pixel_digest = hashlib.sha256(saved.convert("RGB").tobytes()).hexdigest()
        return {
            "sha256": hashlib.sha256(image_bytes(destination)).hexdigest(),
            "pixel_sha256": pixel_digest,
        }, checks


def document(snapshot, manifest, reference):
    if reference == "manifest.json":
        return json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    if reference == "index.html":
        return render_manifest_report(snapshot["plan"].brief.brand.name, manifest)
    raise StateError("This file is not part of the report.", code="report_not_found")


def prepare_current_zip(state, snapshot, *, allow_partial=False, progress=None):
    progress = progress or (lambda *_args, **_kwargs: None)
    manifest = delivery_manifest(snapshot, allow_partial=allow_partial)
    ready = {item["id"]: item for item in manifest["outputs"] if item["file"]}
    outputs = [o for o in snapshot["plan"].outputs if o.id in ready]
    created = datetime.now(UTC)
    manifest["report_created_at"] = created.isoformat()
    stamp = created.astimezone().timetuple()[:6]
    root = manifest["package_name"]
    stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    started = time.monotonic()
    try:
        progress("Creating finished images", 0, len(outputs))
        with captured_sources(state, snapshot, outputs) as (paths, destination):
            rendered = []
            for index, output in enumerate(outputs):
                destination = destination.parent / f"output-{index}.png"
                hashes, checks = render_png(snapshot, paths, output, destination)
                record = ready[output.id]
                record.update(hashes)
                manifest["findings"].extend(f.model_dump(mode="json") for f in checks)
                rendered.append((record["file"], destination))
                progress("Creating finished images", index + 1, len(outputs))
            manifest["metrics"]["local_render_seconds"] = round(time.monotonic() - started, 3)
            progress("Preparing ZIP", len(outputs), len(outputs))
            with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
                for name, path in rendered:
                    archive.writestr(ZipInfo(f"{root}/{name}", stamp), path.read_bytes())
                manifest["metrics"]["elapsed_processing_seconds"] = round(
                    time.monotonic() - started, 3
                )
                require_public_metadata(manifest)
                for reference in ("manifest.json", "index.html"):
                    archive.writestr(
                        ZipInfo(f"{root}/{reference}", stamp),
                        document(snapshot, manifest, reference),
                        compress_type=ZIP_DEFLATED,
                    )
        progress("Checking download", len(outputs), len(outputs))
        stream.seek(0)
        with ZipFile(stream) as archive:
            if archive.testzip() is not None:
                raise StateError("The download failed its integrity check. Try again.")
        size = stream.seek(0, 2)
        stream.seek(0)
        progress("Download ready", len(outputs), len(outputs))
        return ZipDownload(f"{root}.zip", stream, size)
    except (ImageProblem, OSError) as error:
        stream.close()
        raise StateError(
            "There was an issue creating the download. Your saved edits are unchanged. Try again.",
            code="delivery_failed",
        ) from error
    except BaseException:
        stream.close()
        raise


def current_image(state, snapshot, output_id, *, preview=False):
    # The full campaign's prohibited wording and required-logo checks also apply
    # to an individual download. Partial readiness permits selecting one ready image.
    manifest = delivery_manifest(snapshot, allow_partial=True)
    record = next((o for o in manifest["outputs"] if o["id"] == output_id and o["file"]), None)
    if record is None:
        raise StateError("This image is not ready to download.", code="image_unavailable")
    output = next(o for o in snapshot["plan"].outputs if o.id == output_id)
    try:
        with captured_sources(state, snapshot, [output]) as (paths, destination):
            render_png(snapshot, paths, output, destination, preview=preview)
            name = delivery_component(manifest["product_names"][output.product_id], "Product")
            return destination.read_bytes(), f"{name} - {Path(record['file']).name}"
    except (ImageProblem, OSError) as error:
        raise StateError(
            "There was an issue creating the image. Your saved edits are unchanged. Try again.",
            code="delivery_failed",
        ) from error
