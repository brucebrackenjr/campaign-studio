"""Safe field addresses and producer messages; never serialize validation inputs."""

import re

from pydantic import ValidationError

from campaign_studio.brief import MAX_PHRASE_RULES, Contract, Market


class FieldIssue(Contract):
    path: list[str | int]
    label: str
    code: str
    message: str
    product_index: int | None = None
    market: Market | None = None


# This is an address allowlist, not a second validation model. Unknown dictionary
# keys are collapsed to their nearest recognized parent, never echoed in errors.
_TEXT = None
_COPY = dict.fromkeys(("message", "cta"))
_ORIGIN = dict.fromkeys(("label", "rights", "synthetic"))
_SOURCE = dict.fromkeys(
    ("role", "path", "description", "imagery_absent", "opaque_nonreflective_product_confirmed")
)
_SOURCE.update(provenance=_ORIGIN, subject_box=dict.fromkeys(("left", "top", "right", "bottom")))
_RULE = dict.fromkeys(("id", "phrase", "match", "case_sensitive"))
_MARKET = {"copy": _COPY, "phrase_rules": [_RULE]}
_PRODUCT = dict.fromkeys(("id", "name", "description"))
_FRAMING = dict.fromkeys(("focal_x", "focal_y", "copy_placement"))
_FRAMING["subject_box"] = dict.fromkeys(("left", "top", "right", "bottom"))
_FRAMING["logo_placement"] = dict.fromkeys(("left", "top", "width"))
_TEXT_PLACEMENT = dict.fromkeys(("left", "top", "width", "font_size", "font_id", "color"))
_FRAMING["text_placements"] = {key: _TEXT_PLACEMENT for key in _COPY}
_PRODUCT.update(
    source=_SOURCE,
    copy_overrides={"en-US": _COPY, "es-MX": _COPY},
    framing={
        market: {ratio: _FRAMING for ratio in ("1:1", "9:16", "16:9")}
        for market in ("en-US", "es-MX")
    },
)
_BRAND = dict.fromkeys(("id", "name", "logo_path", "text_color", "require_logo"))
_BRAND.update(palette=[_TEXT], logo_provenance=_ORIGIN)
_REPLAY_MASTER = dict.fromkeys(("path", "sha256", "model", "generated_on"))
_REPLAY_MASTER["provenance"] = _ORIGIN
_ROOT = dict.fromkeys(("schema_version", "campaign_id", "name", "audience", "creative_direction"))
_ROOT.update(
    brand=_BRAND,
    copy_provenance=_ORIGIN,
    products=[_PRODUCT],
    markets={"en-US": _MARKET, "es-MX": _MARKET},
    replay=dict.fromkeys(("sources", "historical_requests", "history")),
)
_ACTION = dict.fromkeys(
    (
        "version",
        "base_revision",
        "run_id",
        "revision_id",
        "digest",
        "allowance",
        "retry",
        "market",
        "preview_digest",
        "mode",
        "expected_version",
        "description",
        "snapshot_id",
        "allow_partial",
    )
)
_ACTION["values"] = _COPY
_ACTION["field_scopes"] = _COPY
_ACTION["placements"] = {key: _TEXT_PLACEMENT for key in _COPY}
_ACTION.update(framing=_FRAMING, product_id=None, ratio=None)
_LABELS = {
    "name": "Campaign name",
    "audience": "Audience",
    "creative_direction": "Scene direction",
    "brand": "Brand",
    "logo_path": "Logo",
    "require_logo": "Require logo",
    "logo_placement": "Logo position and size",
    "logo_provenance": "Logo origin",
    "palette": "Brand colors",
    "text_color": "Text color",
    "font_size": "Text size",
    "font_id": "Text font",
    "color": "Text color",
    "text_placements": "Text layout",
    "placements": "Text layout",
    "products": "Products",
    "source": "Image treatment",
    "role": "Image treatment",
    "path": "Product image",
    "subject_box": "Important subject area",
    "description": "Product description",
    "markets": "Languages",
    "copy": "Wording",
    "copy_overrides": "Product wording",
    "message": "Message",
    "cta": "Call to action",
    "phrase_rules": "Prohibited words",
    "phrase": "Word or phrase",
    "match": "Phrase matching",
    "case_sensitive": "Match case",
    "provenance": "Image origin",
    "copy_provenance": "Copy origin",
    "rights": "Rights statement",
    "label": "Origin description",
    "replay": "Saved image sources",
    "sources": "Saved image sources",
    "model": "Replay model",
    "history": "Replay history",
    "opaque_nonreflective_product_confirmed": "Prepared product confirmation",
    "imagery_absent": "Missing product imagery",
    "version": "Saved draft version",
    "base_revision": "Saved image version",
    "run_id": "Production run",
    "revision_id": "Saved image version",
    "digest": "Production plan",
    "allowance": "Request allowance",
    "market": "Language",
    "scope": "Wording target",
    "values": "Wording",
    "preview_digest": "Draft preview",
    "mode": "Saved version target",
    "expected_version": "Saved metadata version",
    "snapshot_id": "Image description snapshot",
    "allow_partial": "Partial package",
}


def safe_path(location) -> tuple[list[str | int], bool]:
    parts = list(location)
    while parts and parts[0] in {"body", "query", "path", "value"}:
        parts.pop(0)
    tree = {**_ROOT, **_ACTION}
    result = []
    for part in parts:
        if result == ["brand", "logo_path"] and part in {"constrained-str", "literal['']"}:
            continue  # Validation union branches are not additional fields.
        if (
            result == ["replay", "sources"]
            and isinstance(part, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", part)
        ):
            result.append(part)
            tree = _REPLAY_MASTER
            continue
        if (
            result
            and result[-1] == "source"
            and part in {"finished_scene", "prepared_product", "concept"}
        ):
            continue  # Pydantic discriminator branch, not a producer field.
        if isinstance(tree, list) and type(part) is int and 0 <= part < 100:
            result.append(part)
            tree = tree[0]
        elif isinstance(tree, dict) and isinstance(part, str) and part in tree:
            result.append(part)
            tree = tree[part]
        else:
            return result, False
    return result, True


def field_issue(location, code: str, message: str) -> dict:
    path, _ = safe_path(location)
    product_index = (
        path[1] if len(path) > 1 and path[0] == "products" and type(path[1]) is int else None
    )
    market = next((part for part in path if part in ("en-US", "es-MX")), None)
    key = next((part for part in reversed(path) if isinstance(part, str)), "brief")
    label = _LABELS.get(key, "Brief field")
    if path == ["description"]:
        label = "Image description"
    if key == "name" and product_index is not None:
        label = "Product name"
    elif key == "name" and path[0] == "brand":
        label = "Brand name"
    if product_index is not None:
        label = f"Product {product_index + 1} – {label}"
    if "phrase_rules" in path:
        position = path.index("phrase_rules") + 1
        index = path[position] if len(path) > position else None
        if type(index) is int:
            context = f"Prohibited word {index + 1}"
            label = f"{context} – {label}" if key in _RULE and key != "id" else context
        else:
            label = "Prohibited words"
    if market:
        label += " – " + {"en-US": "English (United States)", "es-MX": "Spanish (Mexico)"}[market]
    return FieldIssue(
        path=path,
        label=label,
        code=code,
        message=message,
        product_index=product_index,
        market=market,
    ).model_dump()


def validation_issues(error, *, imported_fields: bool = False) -> list[dict]:
    errors = (
        error.errors(include_input=False, include_url=False)
        if isinstance(error, ValidationError)
        else error.errors()
    )
    result = []
    for item in errors:
        path, known = safe_path(item.get("loc", ()))
        kind = item.get("type")
        message = "Check this field and supply a supported value."
        code = "invalid_value"
        if not known or kind == "extra_forbidden":
            code, message = (
                "unsupported_field",
                "Remove unsupported fields from this part of the brief.",
            )
            # Imported JSON is edited outside Motif. Name a bounded field key,
            # never its value; action payload errors keep their strict allowlist.
            key = item.get("loc", ())[-1] if item.get("loc") else None
            if (
                imported_fields
                and kind == "extra_forbidden"
                and isinstance(key, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ -]{0,79}", key)
            ):
                message = f"Remove the unsupported field “{key}”."
        elif kind == "missing":
            code, message = "required", "Provide this required field."
        elif kind in {"union_tag_invalid", "union_tag_not_found"}:
            code, message = (
                "image_treatment",
                "Choose a photo, a prepared product in a new setting, "
                "or a description-only concept.",
            )
        elif kind in {"string_too_short", "string_pattern_mismatch"}:
            code = "invalid_text"
            message = {
                "name": "Enter a name.",
                "audience": "Describe who the campaign is for.",
                "creative_direction": "Describe the scene and photographic direction.",
                "description": "Describe the product and its important visible details.",
                "message": "Supply the exact headline for this language.",
                "logo_path": "Attach a visible PNG or JPEG logo in Brand.",
                "path": "Attach a PNG or JPEG product image.",
                "phrase": "Enter the phrase to check.",
            }.get(path[-1] if path else None, "Provide a nonempty value in the supported format.")
        elif kind == "too_long" and path and path[-1] == "phrase_rules":
            code, message = (
                "too_many_rules",
                f"Use up to {MAX_PHRASE_RULES} prohibited phrases per language.",
            )
        elif kind == "string_too_long":
            limit = item.get("ctx", {}).get("max_length")
            code = "text_too_long"
            message = f"Use no more than {limit:,} characters." if limit else "Shorten this field."
        elif path and path[-1] == "font_size" and kind in {"less_than_equal", "greater_than_equal"}:
            code, message = (
                "text_size",
                "Use a text size from 1 to 65,535 output pixels that fits inside the image.",
            )
        elif kind in {
            "list_type",
            "dict_type",
            "model_type",
            "string_type",
            "bool_parsing",
            "bool_type",
            "int_type",
        }:
            code, message = (
                "invalid_type",
                "Use the expected field type; keep lists, text, and choices "
                "in their respective fields.",
            )
        elif kind in {"too_short", "too_long"} and path == ["products"]:
            code, message = "product_count", "Use between 1 and 5 products."
        elif kind == "value_error":
            # Only our known constant validator messages may refine the safe address.
            detail = item.get("msg", "").removeprefix("Value error, ")
            if detail == "text_color must be one of the controlled palette colors":
                path = ["brand", "text_color"]
                message = "Choose a text color included in the brand colors."
            elif detail == "text placement color must be one of the controlled palette colors":
                path = ["brand", "palette"]
                message = (
                    "A text block uses a color outside this palette. Restore that brand color, "
                    "or change the block’s color before removing it."
                )
            elif detail == "product IDs must be unique":
                path, message = ["products"], "Each product needs a distinct identity."
            elif detail == "copy must contain visible text; use ordinary spaces and LF breaks":
                message = "Use visible wording, ordinary spaces, and line breaks."
            elif detail == "subject bounds must have positive width and height":
                message = "Set a subject area with positive width and height."
        if (
            path
            and ("palette" in path or path[-1] == "text_color")
            and kind == "string_pattern_mismatch"
        ):
            message = "Use a six-digit color, such as #FFFFFF."
        result.append(field_issue(path, code, message))
    return result


def issue_messages(issues: list[dict]) -> list[str]:
    return [f"{item['label']}: {item['message']}" for item in issues]
