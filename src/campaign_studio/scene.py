"""A transient, layered Studio scene. Rendering never publishes editing state."""

import hashlib
import io
import time
from uuid import uuid4

from campaign_studio.brief import Framing, Provenance
from campaign_studio.imaging import (
    composition_base,
    load_image,
    logo_placement,
    save_srgb,
    text_block_overlay,
    text_blocks,
)
from campaign_studio.review import CreativeUpdate
from campaign_studio.workspace import ActionError, TextEdit


class SceneEdit(TextEdit):
    """One version owner for changes made across Studio's tools."""

    framing: Framing | None = None
    replacement: str | None = None
    provenance: Provenance | None = None
    description_edit: CreativeUpdate | None = None


def render_scene(previews, workspace, campaign_id, edit):
    from campaign_studio.compositions import candidate
    from campaign_studio.rendering import blocks_output

    # Source ownership, version checks, and reads share the existing preview boundary.
    with previews.lock, workspace.storage.exclusive_run():
        _, plan, impact = candidate(workspace, campaign_id, edit, preview=True)
        current = workspace.preview_composition(campaign_id, edit.revision_id)
        output = next(o for o in plan.outputs if o.id == edit.output_id)
        product = next(p for p in plan.brief.products if p.id == edit.product_id)
        findings = [f for f in plan.findings if blocks_output(f, output)]
        blocking = [
            f
            for f in findings
            if f.severity == "blocking"
            and f.rule not in {"text_fit", "subject_crop"}
            and not (f.context and f.context.phrase)
        ]
        if blocking:
            raise ActionError("scene_unavailable", blocking[0].evidence, 422)
        asset = current["sources"].get(edit.product_id)
        if asset is None:
            raise ActionError("source", "Prepare this source image in Production.", 404)

        def load(asset):
            reference = workspace.storage.project(campaign_id).require(asset.reference)
            payload = workspace.storage.path(reference).read_bytes()
            if hashlib.sha256(payload).hexdigest() != asset.sha256:
                raise ActionError(
                    "preview_source_changed", "The source changed. Reload this image."
                )
            return load_image(io.BytesIO(payload))[0]

        images = {}
        token = uuid4().hex
        prefix = f"/api/campaigns/{campaign_id}/draft-images/{token}/{output.id}"

        def encode(name, image):
            with io.BytesIO() as buffer:
                save_srgb(image, buffer, compress_level=3)
                images[output.id + "-" + name] = buffer.getvalue()
            return prefix + "-" + name

        source = None
        logo = cutout = None
        try:
            source_url = (
                f"/api/campaigns/{campaign_id}/composition/{edit.revision_id}/sources/{product.id}"
                f"?asset={asset.sha256}"
            )
            source_key = asset.sha256
            if product.source.role == "prepared_product":
                source = load(asset)
                cutout = load(plan.assets[f"products/{product.id}"])
                with composition_base(source, output.ratio, cutout, output.framing) as base:
                    source_url = encode("base", base)
                from campaign_studio.planning import canonical_hash

                source_key = canonical_hash(
                    [
                        asset.sha256,
                        plan.assets[f"products/{product.id}"].sha256,
                        output.ratio,
                        output.framing.model_dump(mode="json"),
                    ]
                )
            logo_data = None
            if "logo" in plan.assets:
                logo = load(plan.assets["logo"])
                placed, position = logo_placement(
                    logo, (output.width, output.height), output.framing.logo_placement
                )
                default, default_position = logo_placement(logo, (output.width, output.height))
                try:
                    logo_data = {
                        "url": encode("logo", logo),
                        "key": plan.assets["logo"].sha256,
                        "size": list(logo.size),
                        "bounds": [
                            *position,
                            position[0] + placed.width,
                            position[1] + placed.height,
                        ],
                        "default_bounds": [
                            *default_position,
                            default_position[0] + default.width,
                            default_position[1] + default.height,
                        ],
                    }
                finally:
                    placed.close()
                    default.close()
            blocks = {}
            for block in text_blocks(output.text, output.ratio, output.framing, validate=False):
                with text_block_overlay(
                    block["lines"], (output.width, output.height), plan.brief.brand.text_color
                ) as layer:
                    blocks[block["id"]] = {
                        **{k: v for k, v in block.items() if k not in {"lines", "text"}},
                        "color": block["color"] or plan.brief.brand.text_color,
                        "url": encode("text-" + block["id"], layer),
                    }
            size = sum(map(len, images.values()))
            if size > previews.max_bytes:
                raise ActionError(
                    "scene_size", "This preview is too large. Choose a smaller source image.", 422
                )
            previews.entries[token] = {
                "created": time.monotonic(),
                "campaign": campaign_id,
                "images": images,
                "size": size,
            }
            previews._prune()
            return {
                "revision_id": edit.revision_id,
                "target": output.id,
                "width": output.width,
                "height": output.height,
                "source": {
                    "url": source_url,
                    "size": [asset.width, asset.height],
                    "key": source_key,
                    "prepared": cutout is not None,
                },
                "logo": logo_data,
                "text_blocks": blocks,
                "framing": output.framing.model_dump(mode="json"),
                "findings": [f.model_dump(mode="json") for f in findings],
                "impact": impact,
            }
        finally:
            if source:
                source.close()
            if logo:
                logo.close()
            if cutout:
                cutout.close()
