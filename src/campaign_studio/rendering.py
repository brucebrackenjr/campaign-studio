"""Compose complete or explicitly partial revisions and inspect actual PNG pixels."""

import hashlib
import io
import json
import time
from contextlib import ExitStack

from PIL import Image, ImageChops

from campaign_studio.capacity import revision_bytes
from campaign_studio.costing import estimate_recorded_usage
from campaign_studio.imaging import (
    ImageProblem,
    compose,
    load_image,
    logo_placement,
    require_font,
    save_srgb,
    text_block_overlay,
    text_blocks,
    wording_mask,
)
from campaign_studio.planning import (
    Finding,
    FindingContext,
    Output,
    Plan,
    compare_plans,
    contextualize_findings,
)
from campaign_studio.state import State, StateError


def inspect_png(path, output: Output, logo: Image.Image, color: str) -> list[Finding]:
    findings = []
    try:
        with Image.open(path) as decoded:
            decoded.load()
            if decoded.format != "PNG" or decoded.size != (output.width, output.height):
                raise ImageProblem("The output is not a PNG at the planned dimensions.")
            if not decoded.info.get("icc_profile"):
                raise ImageProblem("The output has no embedded color profile.")
            actual = decoded.convert("RGB")
        findings.append(
            Finding(
                item=output.id,
                rule="image_integrity",
                severity="pass",
                evidence="The exported PNG decodes at its declared dimensions.",
            )
        )
        logo_image, position = logo_placement(logo, actual.size, output.framing.logo_placement)
        opaque = logo_image.getchannel("A").point(lambda alpha: 255 if alpha == 255 else 0)
        with wording_mask(output.text, output.ratio, output.framing) as text:
            # Copy is printed above the logo. Its antialiased edges also
            # obscure the original artwork, so compare only exposed logo pixels.
            covered = text.crop(
                (*position, position[0] + logo_image.width, position[1] + logo_image.height)
            ).point(lambda value: 255 if value else 0)
            opaque = ImageChops.subtract(opaque, covered)
            covered.close()
        # Intentionally covered/translucent artwork has no opaque reference to
        # compare. Do not invent a failure or a successful comparison for it.
        if opaque.getbbox() is not None:
            cropped = actual.crop(
                (*position, position[0] + logo_image.width, position[1] + logo_image.height)
            )
            difference = ImageChops.difference(cropped, logo_image.convert("RGB"))
            masked = Image.composite(difference, Image.new("RGB", difference.size), opaque)
            findings.append(
                Finding(
                    item=output.id,
                    rule="logo_pixels",
                    severity="blocking" if masked.getbbox() else "pass",
                    evidence="Compared exported opaque logo pixels with the supplied logo.",
                    remedy="Restore the supplied logo and recompose this output.",
                )
            )
        blocks = text_blocks(output.text, output.ratio, output.framing)
        with ExitStack() as resources:
            layers = [
                resources.enter_context(text_block_overlay(block["lines"], actual.size, color))
                for block in blocks
            ]
            for index, (block, layer) in enumerate(zip(blocks, layers, strict=True)):
                visible = layer.getchannel("A").point(lambda value: 255 if value == 255 else 0)
                for later in layers[index + 1 :]:
                    covered = later.getchannel("A").point(lambda value: 255 if value else 0)
                    visible = ImageChops.subtract(visible, covered)
                observed = visible.getbbox() is not None
                if not observed:
                    continue
                difference = ImageChops.difference(actual, layer.convert("RGB"))
                wrong = Image.composite(difference, Image.new("RGB", actual.size), visible)
                label = {
                    "message": "Headline",
                    "cta": "Call to action",
                }[block["id"]]
                findings.append(
                    Finding(
                        item=output.id,
                        rule="controlled_text_color",
                        severity="blocking" if wrong.getbbox() else "pass",
                        evidence=(
                            f"{label}: compared exposed opaque text pixels with the selected "
                            "brand color; not OCR."
                        ),
                        remedy="Restore the supplied text color and recompose this output.",
                        context=FindingContext(text_block=block["id"]),
                    )
                )
    except (ImageProblem, OSError, ValueError) as error:
        findings.append(
            Finding(
                item=output.id,
                rule="image_integrity",
                severity="blocking",
                evidence=str(error)
                if isinstance(error, ImageProblem)
                else "The exported PNG could not be inspected.",
                remedy="Recompose locally from the saved source.",
            )
        )
    return findings


def blocks_output(finding: Finding, output: Output) -> bool:
    return finding.severity == "blocking" and finding.item in {
        output.id,
        output.product_id,
        f"products/{output.product_id}",
        f"masters/{output.product_id}",
        f"{output.product_id}/{output.ratio}",
        "logo",
    }


def rule_recheck_files(state: State, run_id: str, revision_id: str, plan: Plan) -> dict:
    """Find exact compatible pixels; a rule decision never recomposes or repairs them.

    Current files are authoritative. Only an output deliberately withheld by the
    current rules may borrow intact pixels from an older, matching composition.
    Metadata ancestry remains the current revision, regardless of pixel origin.
    """
    current = state.revision(revision_id)
    current_plan = state.plan(run_id)
    saved = json.loads(state.storage.path(current["manifest_ref"]).read_bytes())
    current_outputs = {item["id"]: item for item in saved["outputs"]}
    with state.storage.connect() as db:
        older = db.execute(
            "SELECT id,plan_ref,manifest_ref FROM revisions WHERE run_id=? "
            "AND images_retired=0 AND image_count>0 AND id<>? ORDER BY rowid DESC",
            (run_id, revision_id),
        ).fetchall()
    fallback = []
    for _, plan_ref, manifest_ref in older:
        prior_plan = Plan.model_validate_json(state.storage.path(plan_ref).read_bytes())
        prior = json.loads(state.storage.path(manifest_ref).read_bytes())
        fallback.append(
            (
                set(compare_plans(prior_plan, plan).render_outputs),
                {item["id"]: item for item in prior["outputs"]},
                manifest_ref.rsplit("/", 1)[0],
            )
        )
    result = {}
    for output in plan.outputs:
        if any(blocks_output(finding, output) for finding in plan.findings):
            continue
        item = current_outputs[output.id]
        candidates = [(item, current["manifest_ref"].rsplit("/", 1)[0])]
        if not item["file"] and any(
            blocks_output(finding, output) for finding in current_plan.findings
        ):
            candidates = [
                (outputs[output.id], prefix)
                for changed, outputs, prefix in fallback
                if output.id not in changed and output.id in outputs
            ]
        for candidate, prefix in candidates:
            try:
                intact = (
                    candidate["file"]
                    and hashlib.sha256(
                        state.storage.path(f"{prefix}/{candidate['file']}").read_bytes()
                    ).hexdigest()
                    == candidate["sha256"]
                )
            except OSError:
                intact = False
            if intact:
                result[output.id] = (candidate, prefix)
                break
        else:
            product = next(p for p in plan.brief.products if p.id == output.product_id)
            language = "English (United States)" if output.market == "en-US" else "Spanish (Mexico)"
            context = f"{product.name} – {language} – {output.ratio}"
            if not item["file"]:
                raise StateError(
                    f"No intact matching image is available for {context}. "
                    "Keep your rule changes and review the updated production plan "
                    "to recreate this image from its source. Recheck does not compose images."
                )
            raise StateError(
                "Recover the saved images locally before rechecking rules. "
                f"No intact matching image is available for {context}."
            )
    return result


def render_revision(
    state: State,
    run_id: str,
    plan: Plan,
    *,
    reuse_revision: str | None = None,
    operation_started: float | None = None,
    retirement_files: dict | None = None,
    edit_receipt: dict | None = None,
    preserve_omissions: bool = False,
    rule_recheck: bool = False,
) -> dict:
    state.require_image_cleanup(run_id)
    recheck_files = (
        rule_recheck_files(state, run_id, reuse_revision, plan) if rule_recheck else None
    )
    if retirement_files is None:
        from campaign_studio.cleanup import Cleanup

        retirement_files = Cleanup(state).prepare_retirement(plan.brief.campaign_id)
    require_font()
    started = time.monotonic()
    storage = state.storage
    storage.require_capacity(revision_bytes(plan))
    revision_id = state.reserve_revision(run_id)
    prefix = f"campaigns/{run_id}/revisions/{revision_id}"
    previous_files = {}
    previous_missing = {}
    previous_findings = []
    changed = {output.id for output in plan.outputs}
    current = state.run(run_id)["current_revision"]
    accepted_partial = bool(
        current
        and not state.revision(current)["complete"]
        and plan.digest == state.plan(run_id).digest
    )
    if reuse_revision:
        previous = state.revision(reuse_revision)
        # An accepted rule decision or explicit edit may pin useful partial work.
        # A successful later operation may advance it; a local failure may not.
        accepted_partial = (
            not previous["complete"] and state.run(run_id)["current_revision"] == reuse_revision
        )
        old_plan = Plan.model_validate_json(storage.path(previous["plan_ref"]).read_bytes())
        impact = compare_plans(old_plan, plan)
        changed = set(impact.render_outputs)
        old_manifest = json.loads(storage.path(previous["manifest_ref"]).read_bytes())
        previous_missing = {
            item["id"]: item for item in old_manifest["outputs"] if not item["file"]
        }
        if preserve_omissions:
            changed.difference_update(previous_missing)
        previous_findings = old_manifest["findings"]
        previous_files = {
            item["id"]: (item, previous["manifest_ref"].rsplit("/", 1)[0])
            for item in old_manifest["outputs"]
            if item["file"]
        }
    if recheck_files is not None:
        previous_files = recheck_files
        previous_missing = {}
        changed = set()
    state.live_progress[run_id] = {
        "stage": "Creating campaign sizes",
        "completed": 0,
        "completed_ids": [],
    }
    source_by_product = {}
    if plan.brief.replay:
        source_by_product.update(
            {
                product: plan.assets[f"masters/{product}"].reference
                for product in plan.brief.replay.sources
                if f"masters/{product}" in plan.assets
            }
        )
    for attempt in state.attempts(run_id):
        if attempt["state"] == "decoded" and state.source_is_intact(attempt):
            source_by_product[attempt["product_id"]] = attempt["source_ref"]
    logo = None
    if "logo" in plan.assets:
        logo, _ = load_image(storage.path(plan.assets["logo"].reference))
    items = []
    findings = list(plan.findings)
    local_failure = False
    for output in plan.outputs:
        product = next(item for item in plan.brief.products if item.id == output.product_id)
        relative = f"images/{product.id}/{output.ratio.replace(':', 'x')}/{output.market}.png"
        record = {
            "id": output.id,
            "product_id": product.id,
            "market": output.market,
            "ratio": output.ratio,
            "width": output.width,
            "height": output.height,
            "copy": output.text.model_dump(),
            "file": None,
            "sha256": None,
            "source_role": product.source.role,
            "synthetic": product.source.provenance.synthetic,
        }
        if output.id not in changed and output.id in previous_missing:
            # A correction only changes its declared impact. Recovery is a separate action.
            items.append(dict(previous_missing[output.id]))
            findings.extend(
                Finding.model_validate(f)
                for f in previous_findings
                if f["item"] == output.id and f["rule"] == "composition"
            )
            continue
        if any(blocks_output(finding, output) for finding in plan.findings):
            record["omission"] = (
                "Campaign details need correcting before this image can be produced."
            )
            items.append(record)
            continue
        try:
            reused = None
            if output.id not in changed and output.id in previous_files:
                old, old_prefix = previous_files[output.id]
                payload = storage.path(f"{old_prefix}/{old['file']}").read_bytes()
                if hashlib.sha256(payload).hexdigest() != old["sha256"]:
                    raise ImageProblem("The saved unchanged image failed its integrity check.")
                if edit_receipt is not None and not rule_recheck:
                    checks = [
                        Finding.model_validate(f)
                        for f in previous_findings
                        if f["item"] == output.id
                        and f["rule"] in {"image_integrity", "logo_pixels", "controlled_text_color"}
                    ]
                    if (
                        old.get("pixel_sha256")
                        and any(
                            f.rule == "image_integrity" and f.severity == "pass" for f in checks
                        )
                        and all(f.severity == "pass" for f in checks)
                    ):
                        reused = (checks, old["pixel_sha256"])
            else:
                if rule_recheck:
                    raise ImageProblem("The verified recheck image is unavailable.")
                reference = (
                    plan.assets[f"products/{product.id}"].reference
                    if product.source.role == "finished_scene"
                    else source_by_product.get(product.id)
                )
                if reference is None:
                    raise ImageProblem(
                        "The planned generated source is unavailable; no request was retried."
                    )
                scene, _ = load_image(storage.path(reference))
                cutout = None
                try:
                    if product.source.role == "prepared_product":
                        cutout, _ = load_image(
                            storage.path(plan.assets[f"products/{product.id}"].reference)
                        )
                    image = compose(
                        scene,
                        logo,
                        output.text,
                        output.ratio,
                        plan.brief.brand.text_color,
                        cutout,
                        output.framing,
                    )
                    with io.BytesIO() as buffer:
                        save_srgb(image, buffer)
                        payload = buffer.getvalue()
                    image.close()
                finally:
                    scene.close()
                    if cutout is not None:
                        cutout.close()
            reference = f"{prefix}/{relative}"
            storage.write(reference, payload)
            if reused is not None:
                # The composition contract and encoded bytes are unchanged. Verify
                # the new copy, then retain the inspection of those exact pixels.
                if (
                    hashlib.sha256(storage.path(reference).read_bytes()).hexdigest()
                    != old["sha256"]
                ):
                    raise ImageProblem("The copied unchanged image failed its integrity check.")
                checks, pixels = reused
            else:
                checks = inspect_png(
                    storage.path(reference), output, logo, plan.brief.brand.text_color
                )
                with Image.open(io.BytesIO(payload)) as saved:
                    pixels = hashlib.sha256(saved.convert("RGB").tobytes()).hexdigest()
            findings.extend(checks)
            if any(item.severity == "blocking" for item in checks):
                raise ImageProblem("The new file failed exported-pixel checks.")
            record.update(
                file=relative, sha256=hashlib.sha256(payload).hexdigest(), pixel_sha256=pixels
            )
        except (ImageProblem, OSError, ValueError) as error:
            local_failure = True
            record["omission"] = (
                str(error) if isinstance(error, ImageProblem) else "Local output writing failed."
            )
            findings.append(
                Finding(
                    item=output.id,
                    rule="composition",
                    severity="blocking",
                    evidence=record["omission"],
                    remedy="Recover locally from the saved source.",
                )
            )
        items.append(record)
        state.live_progress[run_id] = {
            "stage": f"Creating sizes for {product.name}",
            "completed": sum(bool(item["file"]) for item in items),
            "completed_ids": [item["id"] for item in items if item["file"]],
        }
    if logo is not None:
        logo.close()
    attempts = state.attempts(run_id)
    reported_usage = []
    for index, attempt in enumerate(attempts, start=1):
        usage = json.loads(attempt["usage_json"]) if attempt["usage_json"] else {}
        tokens = {
            name: usage[name]
            for name in ("input_tokens", "output_tokens", "total_tokens")
            if type(usage.get(name)) is int and usage[name] >= 0
        }
        reported_usage.append(
            {
                "attempt": index,
                "tokens": tokens or None,
                "status": "reported" if tokens else "not_reported",
            }
        )
    contextualize_findings(plan.brief, plan.outputs, findings)
    complete = all(item["file"] for item in items)
    eligible_saved = all(
        item["file"] or any(blocks_output(f, output) for f in plan.findings)
        for item, output in zip(items, plan.outputs, strict=True)
    )
    manifest = {
        "schema_version": 1,
        "campaign_id": plan.brief.campaign_id,
        "campaign_name": plan.brief.name,
        "revision_id": revision_id,
        **({"edit_receipt": edit_receipt} if edit_receipt else {}),
        "complete": complete,
        "evidence": "synthetic"
        if all(item["synthetic"] for item in items)
        else (
            "genuine_saved_source_replay"
            if plan.brief.replay and not plan.generation_requests
            else "composed_images"
        ),
        "production_cost_estimate": plan.cost_estimate,
        "usage_cost_estimate": estimate_recorded_usage(attempts, plan.generation_requests),
        "outputs": items,
        "findings": [finding.model_dump() for finding in findings],
        "sources": [
            {
                "product_id": product.id,
                "role": product.source.role,
                "provenance": product.source.provenance.model_dump(),
            }
            for product in plan.brief.products
        ],
        "metrics": {
            "requested_images": len(items),
            "completed_images": sum(bool(item["file"]) for item in items),
            "distinct_campaigns": 1,
            "revisions": state.revision_count(run_id) + 1,
            "attempted_requests": len(attempts),
            "request_allowance": state.run(run_id)["allowance"],
            "reported_usage": reported_usage,
            "received_pending_local": sum(item["state"] == "received" for item in attempts),
            "successful_requests": sum(item["state"] == "decoded" for item in attempts),
            "failed_requests": sum(item["state"] == "failed" for item in attempts),
            "unknown_requests": sum(
                item["state"] in {"unknown", "dispatching"} for item in attempts
            ),
            "reused_sources": sum(
                product.source.role != "concept"
                or bool(plan.brief.replay and product.id in plan.brief.replay.sources)
                for product in plan.brief.products
            ),
            "local_render_seconds": round(time.monotonic() - started, 3),
            "elapsed_processing_seconds": round(
                time.monotonic()
                - (operation_started if operation_started is not None else started),
                3,
            ),
            "time_saved": "not_measured",
            "cost_status": "unknown" if attempts else "no_requests",
            "confirmed_charge": None,
        },
    }
    if plan.brief.replay:
        replay = plan.brief.replay.model_dump(mode="json")
        for source in replay["sources"].values():
            source.pop("path")
        manifest["replay"] = replay
    plan_ref = f"private/runs/{run_id}/revisions/{revision_id}.json"
    manifest_ref = f"{prefix}/manifest.json"
    storage.write(plan_ref, plan.model_dump_json(indent=2).encode())
    storage.write(manifest_ref, json.dumps(manifest, indent=2, ensure_ascii=False).encode())
    # An explicit edit carries existing omissions without treating them as a
    # new failure. Newly failed writes must keep its base current, including the
    # first useful partial that did not previously need a pinned pointer.
    state.publish_revision(
        run_id,
        revision_id,
        plan_ref,
        manifest_ref,
        complete,
        parent_revision=reuse_revision,
        retirement_files=retirement_files,
        accept_partial=((rule_recheck or accepted_partial) and eligible_saved)
        or (edit_receipt is not None and not local_failure),
        preserve_current=edit_receipt is not None and local_failure,
        rule_withheld_outputs=frozenset(
            output.id
            for output in plan.outputs
            if rule_recheck and any(blocks_output(finding, output) for finding in plan.findings)
        ),
    )
    if rule_recheck and not eligible_saved:
        # Keep the diagnostic record, but never call a failed local write an
        # accepted rule decision or let Intake overwrite its current rule context.
        raise StateError(
            "Rule recheck could not save every eligible image. Your current images and rules "
            "are unchanged. Keep your rule edits and retry the recheck after resolving the "
            "local output failure. No image request was made."
        )
    return manifest
