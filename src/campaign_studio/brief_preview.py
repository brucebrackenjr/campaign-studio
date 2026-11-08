"""One representative Brief image, using the renderer without publishing work."""

import copy
import hashlib
import io

from PIL import Image
from pydantic import Field, TypeAdapter, ValidationError

from campaign_studio.authored import AuthoredProjects
from campaign_studio.brief import (
    FORMATS,
    Brand,
    Contract,
    Copy,
    Framing,
    Identifier,
    Market,
    Ratio,
    Source,
)
from campaign_studio.imaging import (
    ImageProblem,
    compose,
    composition_base,
    creative_overlay,
    fit_logo_placement,
    load_image,
    logo_placement,
    placed_logo_bounds,
    require_visible_logo,
    save_srgb,
    text_block_overlay,
    text_blocks,
)
from campaign_studio.planning import canonical_hash
from campaign_studio.validation import field_issue, validation_issues
from campaign_studio.workspace import ActionError, Workspace

SOURCE = TypeAdapter(Source)


def same_imagery_intent(plan, raw, product_id):
    """A saved scene is relevant only to the same authored imagery request."""
    previous = next((p for p in plan.brief.products if p.id == product_id), None)
    products = raw.get("products")
    selected = (
        next((p for p in products if isinstance(p, dict) and p.get("id") == product_id), None)
        if isinstance(products, list)
        else None
    )
    if previous is None or selected is None:
        return False
    try:
        source = SOURCE.validate_python(selected.get("source"))
    except ValidationError:
        return False
    return (
        previous.source == source
        and previous.description == selected.get("description")
        and plan.brief.audience == raw.get("audience")
        and plan.brief.creative_direction == raw.get("creative_direction")
    )


class BriefPreview(Contract):
    version: str | None = Field(max_length=64)
    brief: dict
    product_id: Identifier
    market: Market
    ratio: Ratio


def representative_preview(state, campaign_id, action, *, with_layers=False):
    """Validate only visible inputs; never substitute earlier valid authoring values."""
    workspace = Workspace(state)
    projects = AuthoredProjects(workspace)
    document = projects.document(campaign_id)
    if action.version != document["version"]:
        raise ActionError("stale_brief", "The brief changed. Refresh its preview.")
    projects.check_envelope({"brief": action.brief})
    target = {
        "campaign_id": campaign_id,
        "product_id": action.product_id,
        "market": action.market,
        "ratio": action.ratio,
    }
    result = {
        "target": target,
        "request_digest": canonical_hash(action.model_dump(mode="json")),
        "status": "invalid",
        "kind": None,
        "image": None,
        "issues": [],
        "fitted_outputs": [],
    }
    opened = []
    validation_prefix = []

    def problem(path, code, message):
        raise ActionError(code, message, 422, field_errors=[field_issue(path, code, message)])

    def attachment(reference, path):
        if not reference:
            return None
        if not isinstance(reference, str):
            problem(path, "image", "Choose an image with this campaign’s file picker.")
        # Raw hints and arbitrary paths are saveable text, not file authority.
        # Previously selected references or this campaign's owned uploads are the
        # only readable attachments in a representative preview.
        references = set()
        for value in (document["value"], document.get("authored", {}).get("brief", {})):
            brand = value.get("brand", {})
            if isinstance(brand, dict) and isinstance(brand.get("logo_path"), str):
                references.add(brand.get("logo_path"))
            products = value.get("products", [])
            for product in products if isinstance(products, list) else []:
                if (
                    isinstance(product, dict)
                    and isinstance(product.get("source"), dict)
                    and isinstance(product["source"].get("path"), str)
                ):
                    references.add(product["source"].get("path"))
        try:
            project = state.storage.project(campaign_id)
            project.require(reference)
            if reference not in references:
                project.upload_name(reference)
        except ValueError:
            problem(path, "image", "Attach this image with this campaign’s file picker.")
            return None
        try:
            image, kind = load_image(state.storage.path(reference))
            opened.append(image)
            if path == ["brand", "logo_path"]:
                result["logo"] = {
                    "reference": reference,
                    "width": image.width,
                    "height": image.height,
                    "type": kind,
                    "opaque": "A" not in image.getbands()
                    or image.getchannel("A").getextrema()[0] == 255,
                }
            return image
        except (OSError, ValueError) as error:
            problem(
                path,
                "image",
                str(error)
                if isinstance(error, ImageProblem)
                else "This selected image is unavailable. Attach it again.",
            )

    try:
        raw = action.brief
        products = raw.get("products")
        products = products if isinstance(products, list) else []
        selected = next(
            (p for p in products if isinstance(p, dict) and p.get("id") == action.product_id), None
        )
        if selected is None:
            problem(["products"], "product", "Choose a product for this preview.")
        index = products.index(selected)
        brand_value = raw.get("brand")
        if not isinstance(brand_value, dict):
            problem(["brand"], "brand", "Choose a palette and default text color.")
        logo = attachment(brand_value.get("logo_path"), ["brand", "logo_path"])
        if logo is None:
            result["logo_missing"] = True
        else:
            require_visible_logo(logo)
        # Names/provenance are not painted by this preview. Their unfinished
        # values must not prevent feedback on the actual visual fields.
        validation_prefix = ["brand"]
        brand = Brand.model_validate(
            {
                "id": "preview-brand",
                "name": "Preview",
                "logo_path": "preview",
                "logo_provenance": {"label": "Representative preview", "rights": "Not provided"},
                "palette": brand_value.get("palette"),
                "text_color": brand_value.get("text_color"),
            }
        )
        market = (
            raw.get("markets", {}).get(action.market)
            if isinstance(raw.get("markets"), dict)
            else None
        )
        if not isinstance(market, dict) or not isinstance(market.get("copy"), dict):
            problem(["markets", action.market, "copy"], "copy", "Enter wording for this language.")
        wording = copy.deepcopy(market["copy"])
        overrides = selected.get("copy_overrides", {})
        override = overrides.get(action.market, {}) if isinstance(overrides, dict) else None
        if not isinstance(override, dict):
            problem(
                ["products", index, "copy_overrides", action.market],
                "copy",
                "Check this product’s wording.",
            )
        wording.update(override)
        validation_prefix = ["markets", action.market, "copy"]
        text = Copy.model_validate(wording)
        all_framing = selected.get("framing", {})
        formats = all_framing.get(action.market, {}) if isinstance(all_framing, dict) else None
        validation_prefix = ["products", index, "framing", action.market, action.ratio]
        if not isinstance(formats, dict):
            problem(validation_prefix, "framing", "Check this image’s saved layout.")
        framing = Framing.model_validate(formats.get(action.ratio, {}))
        for block, placement in framing.text_placements.items():
            if placement.color and placement.color not in brand.palette:
                problem(
                    [
                        "products",
                        index,
                        "framing",
                        action.market,
                        action.ratio,
                        "text_placements",
                        block,
                        "color",
                    ],
                    "palette",
                    "Keep this text color in the palette, or choose another color in Adjust text.",
                )
        source = selected.get("source")
        if not isinstance(source, dict) or source.get("role") not in {
            "concept",
            "finished_scene",
            "prepared_product",
        }:
            problem(["products", index, "source"], "source", "Choose this product’s image source.")
        run = workspace.run_for(campaign_id)
        old_plan = state.plan(run["id"]) if run else None
        old_logo = old_plan.brief.brand.logo_path if old_plan else None
        if (
            framing.logo_placement
            and not result.get("logo_missing")
            and brand_value.get("logo_path") != old_logo
        ):
            try:
                placed_logo_bounds(logo.size, FORMATS[action.ratio], framing.logo_placement)
            except ImageProblem:
                requested = framing.logo_placement
                fitted = fit_logo_placement(logo.size, FORMATS[action.ratio], requested)
                framing = framing.model_copy(update={"logo_placement": fitted})
                result["fitted_outputs"] = [
                    {
                        "id": "/".join(
                            (action.product_id, action.market, action.ratio.replace(":", "x"))
                        ),
                        "requested": requested.model_dump(),
                        "placement": fitted.model_dump(),
                        "bounds": list(
                            placed_logo_bounds(logo.size, FORMATS[action.ratio], fitted)
                        ),
                    }
                ]
        scene = None
        cutout = None
        if source["role"] != "concept":
            supplied = attachment(source.get("path"), ["products", index, "source", "path"])
            if source["role"] == "finished_scene":
                scene = supplied
                if (
                    scene is not None
                    and "A" in scene.getbands()
                    and scene.getchannel("A").getextrema()[0] < 255
                ):
                    problem(
                        ["products", index, "source"],
                        "scene_coverage",
                        "This image has transparent areas. "
                        "Select a product image without transparency.",
                    )
                if (
                    framing.subject_box is None
                    and framing.inherit_subject
                    and source.get("subject_box")
                ):
                    framing = Framing.model_validate(
                        {**framing.model_dump(), "subject_box": source["subject_box"]}
                    )
            else:
                cutout = supplied
                if cutout is not None and "A" not in cutout.getbands():
                    problem(
                        ["products", index, "source"],
                        "prepared_product",
                        "Choose a prepared product PNG with transparency, or use a finished photo.",
                    )
        if scene is None and source["role"] != "finished_scene" and old_plan:
            plan = old_plan
            # A retained source is relevant only to the same authored imagery
            # intent. Changed/missing direction never silently shows an old scene.
            if same_imagery_intent(plan, raw, action.product_id):
                asset = plan.assets.get(f"masters/{action.product_id}")
                reference, expected = (asset.reference, asset.sha256) if asset else (None, None)
                if reference is None:
                    for attempt in reversed(state.attempts(run["id"])):
                        if (
                            attempt["product_id"] == action.product_id
                            and attempt["state"] == "decoded"
                            and state.source_is_intact(attempt)
                        ):
                            reference, expected = (
                                attempt["source_ref"],
                                attempt["source_sha256"],
                            )
                            break
                if reference:
                    path = state.storage.path(reference)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        problem(
                            ["products", index, "source"],
                            "source",
                            "The retained image is unavailable. "
                            "Attach a suitable image or review Production.",
                        )
                    scene, _ = load_image(path)
                    opened.append(scene)
        if scene is None:
            rendered = creative_overlay(logo, text, action.ratio, brand.text_color, framing)
            kind = "typography"
        else:
            rendered = compose(scene, logo, text, action.ratio, brand.text_color, cutout, framing)
            kind = "composition"
        opened.append(rendered)
        with io.BytesIO() as buffer:
            save_srgb(rendered, buffer, compress_level=3)
            payload = buffer.getvalue()
        if with_layers:
            # These exact renderer layers let the browser recolor inherited text
            # immediately. Custom block colors remain explicit and unchanged.
            base = (
                composition_base(scene, action.ratio, cutout, framing)
                if scene is not None
                else Image.new("RGBA", FORMATS[action.ratio])
            )
            opened.append(base)
            if logo is not None:
                artwork, position = logo_placement(logo, base.size, framing.logo_placement)
                try:
                    base.alpha_composite(artwork, position)
                finally:
                    artwork.close()
            layers = {}
            with io.BytesIO() as buffer:
                save_srgb(base, buffer, compress_level=3)
                layers["base"] = buffer.getvalue()
            blocks = {}
            for block in text_blocks(text, action.ratio, framing):
                layer = text_block_overlay(block["lines"], base.size, "#FFFFFF")
                try:
                    with io.BytesIO() as buffer:
                        save_srgb(layer, buffer, compress_level=3)
                        layers[block["id"]] = buffer.getvalue()
                finally:
                    layer.close()
                blocks[block["id"]] = {"color": block["placement"].get("color")}
            result["layers"] = {"text_blocks": blocks}
            result["_layer_payloads"] = layers
        result.update(status="ready", kind=kind)
        result["image"] = {
            "width": rendered.width,
            "height": rendered.height,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        return result, payload
    except ValidationError as error:
        errors = error.errors(include_url=False)
        for item in errors:
            item["loc"] = (*validation_prefix, *item["loc"])
        result["issues"] = validation_issues(ValidationError.from_exception_data("Brief", errors))
    except ActionError as error:
        result["issues"] = error.field_errors or [field_issue([], error.code, error.message)]
    except (ImageProblem, OSError) as error:
        result["issues"] = [
            field_issue(
                [],
                "preview",
                str(error)
                if isinstance(error, ImageProblem)
                else "The selected image is unavailable. Attach it again.",
            )
        ]
    finally:
        for image in opened:
            image.close()
    return result, None
