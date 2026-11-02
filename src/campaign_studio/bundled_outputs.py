"""Install verified, curated PNGs as an ordinary editable campaign revision."""

import hashlib
import json
import time
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

from campaign_studio.brief import FORMATS
from campaign_studio.costing import estimate_recorded_usage
from campaign_studio.imaging import font_path
from campaign_studio.planning import Finding, canonical_hash, contextualize_findings
from campaign_studio.storage import StorageError

PIXEL_RULES = {"image_integrity", "logo_pixels", "controlled_text_color"}


def prepared_font_hashes():
    return {
        name: hashlib.sha256(Path(font_path(name)).read_bytes()).hexdigest()
        for name in ("noto_sans", "barlow_condensed")
    }


@dataclass(frozen=True)
class PreparedOutputs:
    source: Traversable
    descriptor: dict


def load_prepared_outputs(source, entry, original):
    """Validate the complete resource set before setup claims or creates a campaign."""
    prepared = entry["prepared_outputs"]
    if prepared is None:
        return None
    if prepared.get("path") != f"{entry['slug']}/prepared-outputs.json":
        raise StorageError("The bundled output descriptor has an invalid path.")
    payload = source.joinpath(prepared["path"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != prepared.get("sha256"):
        raise StorageError("The bundled output descriptor does not match its package.")
    descriptor = json.loads(payload)
    if (
        descriptor.get("version") != 1
        or descriptor.get("brief_sha256") != canonical_hash(original)
        or descriptor.get("assets") != entry["files"]
        or descriptor.get("fonts") != prepared_font_hashes()
    ):
        raise StorageError("The bundled outputs do not match their editable campaign inputs.")
    expected = {
        (product["id"], market, ratio)
        for product in original["products"]
        for market in original["markets"]
        for ratio in FORMATS
    }
    observed = set()
    for record in descriptor["outputs"]:
        output = record["output"]
        key = (output["product_id"], output["market"], output["ratio"])
        if key not in expected or key in observed:
            raise StorageError("The bundled output list does not match the campaign scope.")
        observed.add(key)
        product, market, ratio = key
        reference = f"{entry['slug']}/outputs/{product}/{ratio.replace(':', 'x')}/{market}.png"
        if record["file"] != reference:
            raise StorageError("A bundled output has an invalid file path.")
        checks = [Finding.model_validate(finding) for finding in record["findings"]]
        if not any(f.rule == "image_integrity" for f in checks) or any(
            f.rule not in PIXEL_RULES or f.severity != "pass" or f.item != output["id"]
            for f in checks
        ):
            raise StorageError("A bundled output is missing its verified pixel checks.")
        _read_output(source, record)
    if observed != expected:
        raise StorageError("The bundled output set is incomplete.")
    return PreparedOutputs(source, descriptor)


def _read_output(source, record):
    try:
        payload = source.joinpath(record["file"]).read_bytes()
    except OSError as error:
        raise StorageError("A bundled output is missing or unreadable.") from error
    if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise StorageError("A bundled output does not match its verified package.")
    return payload


def install_prepared_outputs(state, run_id, original, prepared):
    """Rebind trusted build-time pixel evidence to fresh local identities, then publish."""
    with state.storage.exclusive_run():
        started = time.monotonic()
        plan = state.plan(run_id)
        if plan.blocked or plan.generation_products or state.run(run_id)["current_revision"]:
            raise StorageError("Prepared examples require a fresh, complete supplied-image plan.")
        state.require_authored_plan(run_id)
        descriptor = prepared.descriptor
        products = dict(zip((p.id for p in plan.brief.products), original["products"], strict=True))
        if plan.assets["logo"].sha256 != descriptor["assets"][original["brand"]["logo_path"]]:
            raise StorageError("The prepared example logo does not match its saved source.")
        if plan.brief.brand.text_color != descriptor["text_color"]:
            raise StorageError("The prepared example text color does not match its plan.")
        for product_id, old in products.items():
            if (
                plan.assets[f"products/{product_id}"].sha256
                != descriptor["assets"][old["source"]["path"]]
            ):
                raise StorageError("A prepared example source does not match its saved input.")
        records = {
            (r["output"]["product_id"], r["output"]["market"], r["output"]["ratio"]): r
            for r in descriptor["outputs"]
        }
        selected = []
        for output in plan.outputs:
            old_id = products[output.product_id]["id"]
            record = records[(old_id, output.market, output.ratio)]
            rebound = dict(record["output"], id=output.id, product_id=output.product_id)
            if rebound != output.model_dump(mode="json"):
                raise StorageError("A prepared image does not match its planned wording or layout.")
            selected.append((output, record))
        storage = state.storage
        storage.require_capacity(sum(r["bytes"] for _, r in selected) + 1024 * 1024)
        revision_id = state.reserve_revision(run_id)
        prefix = f"campaigns/{run_id}/revisions/{revision_id}"
        items = []
        findings = list(plan.findings)
        for output, record in selected:
            payload = _read_output(prepared.source, record)
            relative = (
                f"images/{output.product_id}/{output.ratio.replace(':', 'x')}/{output.market}.png"
            )
            storage.write(f"{prefix}/{relative}", payload)
            if (
                hashlib.sha256(storage.path(f"{prefix}/{relative}").read_bytes()).hexdigest()
                != record["sha256"]
            ):
                raise StorageError("The installed example image failed its integrity check.")
            product = next(p for p in plan.brief.products if p.id == output.product_id)
            items.append(
                {
                    "id": output.id,
                    "product_id": output.product_id,
                    "market": output.market,
                    "ratio": output.ratio,
                    "width": output.width,
                    "height": output.height,
                    "copy": output.text.model_dump(),
                    "file": relative,
                    "sha256": record["sha256"],
                    "pixel_sha256": record["pixel_sha256"],
                    "source_role": product.source.role,
                    "synthetic": product.source.provenance.synthetic,
                }
            )
            for check in record["findings"]:
                check = dict(check, item=output.id)
                context = check.get("context") or {}
                check["context"] = (
                    {"text_block": context["text_block"]} if context.get("text_block") else None
                )
                findings.append(Finding.model_validate(check))
        contextualize_findings(plan.brief, plan.outputs, findings)
        manifest = {
            "schema_version": 1,
            "campaign_id": plan.brief.campaign_id,
            "campaign_name": plan.brief.name,
            "revision_id": revision_id,
            "complete": True,
            "evidence": "bundled_pre_rendered",
            "bundled_example": {
                "version": descriptor["version"],
                "rendered_with": descriptor["rendered_with"],
            },
            "production_cost_estimate": plan.cost_estimate,
            "usage_cost_estimate": estimate_recorded_usage([], plan.generation_requests),
            "outputs": items,
            "findings": [f.model_dump() for f in findings],
            "sources": [
                {
                    "product_id": p.id,
                    "role": p.source.role,
                    "provenance": p.source.provenance.model_dump(),
                }
                for p in plan.brief.products
            ],
            "metrics": {
                "requested_images": len(items),
                "completed_images": len(items),
                "distinct_campaigns": 1,
                "revisions": 1,
                "attempted_requests": 0,
                "request_allowance": 0,
                "reported_usage": [],
                "received_pending_local": 0,
                "successful_requests": 0,
                "failed_requests": 0,
                "unknown_requests": 0,
                "reused_sources": len(plan.brief.products),
                "local_render_seconds": 0,
                "elapsed_processing_seconds": round(time.monotonic() - started, 3),
                "time_saved": "not_measured",
                "cost_status": "no_requests",
                "confirmed_charge": None,
                "pre_rendered_images": len(items),
            },
        }
        plan_ref = f"private/runs/{run_id}/revisions/{revision_id}.json"
        manifest_ref = f"{prefix}/manifest.json"
        storage.write(plan_ref, plan.model_dump_json(indent=2).encode())
        storage.write(manifest_ref, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        state.publish_revision(run_id, revision_id, plan_ref, manifest_ref, True)
        return manifest
