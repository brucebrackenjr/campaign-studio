"""Bounded, short-lived PNG previews. No revisions, attempts, or disk files are created."""

import io
import threading
import time
from collections import OrderedDict
from uuid import uuid4

from PIL import Image

from campaign_studio.imaging import (
    compose,
    composite_text_blocks,
    composition_base,
    creative_overlay,
    load_image,
    logo_placement,
    save_srgb,
    text_block_overlay,
    text_blocks,
    wording_overlay,
)
from campaign_studio.rendering import blocks_output
from campaign_studio.state import StateError


def diagnostic_overlay(logo, output, color):
    """Show the chosen text geometry for repair, without changing its saved settings."""
    layer = Image.new("RGBA", (output.width, output.height))
    try:
        if logo is not None:
            artwork, position = logo_placement(logo, layer.size, output.framing.logo_placement)
            try:
                layer.alpha_composite(artwork, position)
            finally:
                artwork.close()
        composite_text_blocks(
            layer, text_blocks(output.text, output.ratio, output.framing, validate=False), color
        )
        return layer
    except BaseException:
        layer.close()
        raise


class DraftPreviews:
    max_bytes = 64 * 1024 * 1024
    lifetime = 300

    def __init__(self, state):
        self.state = state
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def _prune(self):
        now = time.monotonic()
        for key, entry in list(self.entries.items()):
            if now - entry["created"] > self.lifetime:
                del self.entries[key]
        while (
            len(self.entries) > 4 or sum(e["size"] for e in self.entries.values()) > self.max_bytes
        ):
            self.entries.popitem(last=False)

    def render_brief(self, campaign_id, action):
        from campaign_studio.authored import AuthoredProjects
        from campaign_studio.brief_preview import representative_preview
        from campaign_studio.workspace import ActionError, Workspace

        with self.lock, self.state.storage.exclusive_run():
            result, payload = representative_preview(
                self.state, campaign_id, action, with_layers=True
            )
            current = AuthoredProjects(Workspace(self.state)).document(campaign_id)
            if current["version"] != action.version:
                raise ActionError("stale_brief", "The brief changed. Refresh its preview.")
            if payload is None:
                return result
            layer_payloads = result.pop("_layer_payloads", {})
            size = len(payload) + sum(len(layer) for layer in layer_payloads.values())
            if size > self.max_bytes:
                raise StateError("This preview is too large. Shorten the draft and try again.")
            token = uuid4().hex
            self.entries[token] = {
                "kind": "brief",
                "created": time.monotonic(),
                "campaign": campaign_id,
                "version": action.version,
                "size": size,
                "payload": payload,
                "layers": layer_payloads,
            }
            self._prune()
            url = f"/api/motif/campaigns/{campaign_id}/brief-preview/{token}"
            result["image"]["url"] = f"{url}.png"
            if layer_payloads:
                result["layers"]["base_url"] = f"{url}.base.png"
                for key, block in result["layers"]["text_blocks"].items():
                    block["url"] = f"{url}.{key}.png"
            return result

    def brief_image(self, campaign_id, token):
        from campaign_studio.authored import AuthoredProjects
        from campaign_studio.workspace import ActionError, Workspace

        with self.lock:
            self._prune()
            token, _, layer = token.partition(".")
            entry = self.entries.get(token)
            if not entry or entry.get("kind") != "brief" or entry["campaign"] != campaign_id:
                return None
            try:
                document = AuthoredProjects(Workspace(self.state)).document(campaign_id)
                if document["version"] == entry["version"]:
                    return entry.get("layers", {}).get(layer) if layer else entry["payload"]
            except ActionError, StateError:
                pass
            del self.entries[token]
            return None

    def render(
        self,
        campaign_id,
        edit,
        plan,
        impact,
        *,
        framing_preview=False,
        logo_preview=False,
        text_preview=False,
    ):
        # The same serial boundary prevents preview/save races across processes. It never
        # reserves an attempt, performs recovery, or changes the saved campaign pointer.
        with self.lock, self.state.storage.exclusive_run():
            from campaign_studio.workspace import Workspace

            current = Workspace(self.state).preview_composition(campaign_id, edit.revision_id)
            missing_logo = logo_preview and not impact.get("logo_available", True)
            required = impact["render_outputs"]
            if framing_preview:
                required = [f"{edit.product_id}/{edit.market}/{edit.ratio.replace(':', 'x')}"]
            if logo_preview:
                required = list(set(required) | {edit.output_id})
            if text_preview:
                required = list(set(required) | {edit.output_id})
            sources = {pid: asset.reference for pid, asset in current["sources"].items()}
            images = {}
            layers = {}
            selected = set(edit.preview_products)
            size = 0
            # Editors may show a clipped text layout so the producer can repair it.
            # These temporary diagnostics never relax gallery or export validation.
            editor_preview = framing_preview or logo_preview or text_preview
            can_render = not any(
                any(blocks_output(f, o) for o in plan.outputs if o.id in required)
                and not (framing_preview and f.rule == "subject_crop")
                and not (missing_logo and f.item == "logo")
                and not (editor_preview and f.rule == "text_fit")
                for f in plan.findings
            )
            if can_render:
                logo = (
                    load_image(self.state.storage.path(plan.assets["logo"].reference))[0]
                    if "logo" in plan.assets and not missing_logo
                    else None
                )
                try:
                    for output in plan.outputs:
                        if (
                            output.product_id not in selected
                            or ((logo_preview or text_preview) and output.id != edit.output_id)
                            or (
                                output.id not in impact["render_outputs"]
                                and not (
                                    (framing_preview or logo_preview or text_preview)
                                    and output.market == edit.market
                                    and output.ratio == edit.ratio
                                )
                            )
                            or any(
                                blocks_output(f, output)
                                and not (framing_preview and f.rule == "subject_crop")
                                and not (missing_logo and f.item == "logo")
                                and not (editor_preview and f.rule == "text_fit")
                                for f in plan.findings
                            )
                        ):
                            continue
                        product = next(p for p in plan.brief.products if p.id == output.product_id)
                        reference = (
                            plan.assets[f"products/{product.id}"].reference
                            if product.source.role == "finished_scene"
                            else sources.get(product.id)
                        )
                        if reference is None:
                            raise StateError(
                                "The saved image is unavailable. "
                                "Rebuild saved images on Create before editing."
                            )
                        scene, _ = load_image(self.state.storage.path(reference))
                        cutout = None
                        try:
                            if product.source.role == "prepared_product":
                                cutout, _ = load_image(
                                    self.state.storage.path(
                                        plan.assets[f"products/{product.id}"].reference
                                    )
                                )
                            diagnostic_text = editor_preview and any(
                                f.rule == "text_fit" and blocks_output(f, output)
                                for f in plan.findings
                            )
                            render_framing = (
                                output.framing.model_copy(update={"subject_box": None})
                                if framing_preview
                                else output.framing
                            )
                            if diagnostic_text:
                                with composition_base(
                                    scene, output.ratio, cutout, render_framing
                                ) as base:
                                    with diagnostic_overlay(
                                        logo, output, plan.brief.brand.text_color
                                    ) as overlay:
                                        base.alpha_composite(overlay)
                                    result = base.convert("RGB")
                            else:
                                result = compose(
                                    scene,
                                    logo,
                                    output.text,
                                    output.ratio,
                                    plan.brief.brand.text_color,
                                    cutout,
                                    render_framing,
                                )
                            with io.BytesIO() as buffer:
                                # Temporary logo previews favor encoding speed. Saved
                                # revisions and exports retain their PNG defaults.
                                save_srgb(
                                    result, buffer, compress_level=3 if logo_preview else None
                                )
                                payload = buffer.getvalue()
                                size += len(payload)
                                if size > self.max_bytes:
                                    result.close()
                                    raise StateError(
                                        "The preview is too large. Edit one product at a time."
                                    )
                                images[output.id] = payload
                            result.close()
                            if text_preview:
                                base = composition_base(scene, output.ratio, cutout, output.framing)
                                if logo is not None:
                                    artwork, position = logo_placement(
                                        logo, base.size, output.framing.logo_placement
                                    )
                                    base.alpha_composite(artwork, position)
                                    artwork.close()
                                blocks = text_blocks(
                                    output.text,
                                    output.ratio,
                                    output.framing,
                                    validate=not diagnostic_text,
                                )
                                geometry = {}
                                for block in blocks:
                                    layer = text_block_overlay(
                                        block["lines"], base.size, plan.brief.brand.text_color
                                    )
                                    with io.BytesIO() as buffer:
                                        save_srgb(layer, buffer)
                                        images[output.id + "-text-" + block["id"]] = (
                                            buffer.getvalue()
                                        )
                                    geometry[block["id"]] = {
                                        k: v for k, v in block.items() if k not in {"lines", "text"}
                                    }
                                    geometry[block["id"]]["color"] = (
                                        block["color"] or plan.brief.brand.text_color
                                    )
                                    layer.close()
                                with io.BytesIO() as buffer:
                                    save_srgb(base, buffer)
                                    images[output.id + "-base"] = buffer.getvalue()
                                layers[output.id] = {
                                    "canvas_size": [output.width, output.height],
                                    "text_blocks": geometry,
                                }
                                base.close()
                            if logo_preview:
                                base = composition_base(scene, output.ratio, cutout, output.framing)
                                text = (
                                    diagnostic_overlay(None, output, plan.brief.brand.text_color)
                                    if diagnostic_text
                                    else wording_overlay(
                                        output.text,
                                        output.ratio,
                                        plan.brief.brand.text_color,
                                        output.framing,
                                    )
                                )
                                try:
                                    for suffix, layer in (
                                        ("base", base),
                                        ("copy", text),
                                        ("logo", logo),
                                    ):
                                        if layer is None:
                                            continue
                                        with io.BytesIO() as buffer:
                                            save_srgb(layer, buffer, compress_level=3)
                                            images[output.id + "-" + suffix] = buffer.getvalue()
                                finally:
                                    base.close()
                                    text.close()
                                layers[output.id] = {
                                    "bounds": [0, 0, 0, 0],
                                    "default_bounds": [0, 0, 0, 0],
                                    "logo_size": None,
                                    "canvas_size": [output.width, output.height],
                                    "placement": None,
                                }
                                if logo is not None:
                                    placed, position = logo_placement(
                                        logo,
                                        (output.width, output.height),
                                        output.framing.logo_placement,
                                    )
                                    layers[output.id] = {
                                        "bounds": [
                                            *position,
                                            position[0] + placed.width,
                                            position[1] + placed.height,
                                        ],
                                        "logo_size": list(logo.size),
                                        "canvas_size": [output.width, output.height],
                                        "placement": output.framing.logo_placement.model_dump()
                                        if output.framing.logo_placement
                                        else None,
                                    }
                                    placed.close()
                                    default, position = logo_placement(
                                        logo, (output.width, output.height)
                                    )
                                    layers[output.id]["default_bounds"] = [
                                        *position,
                                        position[0] + default.width,
                                        position[1] + default.height,
                                    ]
                                    default.close()
                            if framing_preview:
                                overlay = (
                                    diagnostic_overlay(logo, output, plan.brief.brand.text_color)
                                    if diagnostic_text
                                    else creative_overlay(
                                        logo,
                                        output.text,
                                        output.ratio,
                                        plan.brief.brand.text_color,
                                        output.framing,
                                    )
                                )
                                with io.BytesIO() as buffer:
                                    save_srgb(overlay, buffer)
                                    images[output.id + "-overlay"] = buffer.getvalue()
                                overlay.close()
                        finally:
                            scene.close()
                            if cutout is not None:
                                cutout.close()
                finally:
                    if logo is not None:
                        logo.close()
            size = sum(map(len, images.values()))
            if size > self.max_bytes:
                raise StateError("This preview is too large. Shorten the draft and try again.")
            token = uuid4().hex
            self.entries[token] = {
                "created": time.monotonic(),
                "campaign": campaign_id,
                "run": edit.run_id,
                "revision": edit.revision_id,
                "digest": plan.digest,
                "images": images,
                "size": size,
            }
            self._prune()
            result = []
            for output in plan.outputs:
                if output.id not in images:
                    continue
                url = f"/api/campaigns/{campaign_id}/draft-images/{token}/{output.id}"
                item = {"id": output.id, "url": url}
                if framing_preview:
                    item["overlay_url"] = url + "-overlay"
                if output.id in layers:
                    item["layers"] = {**layers[output.id], "base_url": url + "-base"}
                    if text_preview:
                        item["layers"]["text_blocks"] = {
                            key: {**block, "url": url + "-text-" + key}
                            for key, block in layers[output.id]["text_blocks"].items()
                        }
                    else:
                        item["layers"].update(
                            {
                                suffix + "_url": url + "-" + suffix
                                if output.id + "-" + suffix in images
                                else None
                                for suffix in ("copy", "logo")
                            }
                        )
                result.append(item)
            return result

    def image(self, campaign_id, token, product, market, ratio):
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.state.storage, campaign_id)
        with self.lock:
            self._prune()
            entry = self.entries.get(token)
            if entry and entry.get("kind") == "brief":
                return None
            from campaign_studio.campaign_drafts import read as read_draft

            current = read_draft(self.state.storage, campaign_id)
            if not entry or entry["campaign"] != campaign_id or not current:
                return None
            return entry["images"].get(f"{product}/{market}/{ratio}")
