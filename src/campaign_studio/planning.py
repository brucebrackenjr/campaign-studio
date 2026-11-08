"""Deterministic preflight and output impact, with no provider access."""

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import Field

from campaign_studio.brief import FORMATS, Brief, Contract, Copy, Framing, Market, Ratio
from campaign_studio.costing import estimate_requests
from campaign_studio.generation import GenerationRequest, request_for
from campaign_studio.imaging import (
    ImageProblem,
    crop_box,
    load_image,
    placed_logo_bounds,
    protects_subject,
    require_font,
    require_visible_logo,
    text_layout,
)
from campaign_studio.storage import Storage, StorageError
from campaign_studio.validation import FieldIssue, field_issue


class MatchedField(Contract):
    field: Literal["message", "cta"]
    value: str | None = None
    origin: Literal["market_shared", "product_override"]
    path: list[str | int]


class PhraseContext(Contract):
    configured_phrase: str
    market: Market
    match: Literal["literal", "whole_word"]
    case_sensitive: bool
    normalization: Literal["NFC", "NFC_then_casefold"]
    word_boundary: Literal["none", "Unicode_word_characters"]
    matches: list[MatchedField]


class FindingContext(Contract):
    product_id: str | None = None
    product_name: str | None = None
    market: Market | None = None
    ratio: Ratio | None = None
    text_block: Literal["message", "cta"] | None = None
    fields: list[FieldIssue] = Field(default_factory=list)
    phrase: PhraseContext | None = None


class Finding(Contract):
    item: str
    rule: str
    severity: Literal["pass", "warning", "blocking"]
    evidence: str
    remedy: str = "Correct this input before continuing."
    context: FindingContext | None = None


class Output(Contract):
    id: str
    product_id: str
    market: Market
    ratio: Ratio
    width: int
    height: int
    text: Copy = Field(alias="copy")
    framing: Framing = Field(default_factory=Framing)


def expand_outputs(brief: Brief) -> list[Output]:
    """Resolve the output matrix from its brief without reading assets or creating requests."""
    outputs = []
    for product in brief.products:
        for market in brief.markets:
            copy = brief.effective_copy(product, market)
            for ratio, (width, height) in FORMATS.items():
                framing = (
                    product.framing.get(market, {}).get(ratio, Framing()).model_copy(deep=True)
                )
                if (
                    product.source.role == "finished_scene"
                    and product.source.subject_box
                    and framing.subject_box is None
                    and framing.inherit_subject
                ):
                    framing.subject_box = product.source.subject_box.model_copy(deep=True)
                outputs.append(
                    Output(
                        id=f"{product.id}/{market}/{ratio.replace(':', 'x')}",
                        product_id=product.id,
                        market=market,
                        ratio=ratio,
                        width=width,
                        height=height,
                        copy=copy.model_copy(deep=True),
                        framing=framing,
                    )
                )
    return outputs


class Asset(Contract):
    reference: str
    sha256: str
    width: int
    height: int


class Plan(Contract):
    cost_estimate: dict | None = None
    schema_version: Literal[1] = 1
    digest: str
    brief: Brief
    assets: dict[str, Asset]
    outputs: list[Output]
    generation_requests: dict[str, GenerationRequest]
    transmitted_data: dict[str, str]
    findings: list[Finding]
    cost_status: Literal["unknown", "no_new_requests"]

    @property
    def generation_products(self) -> list[str]:
        return list(self.generation_requests)

    @property
    def blocked(self) -> bool:
        return any(finding.severity == "blocking" for finding in self.findings)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def copy_findings(brief: Brief, output: Output) -> list[Finding]:
    visible = output.text.model_dump()
    product_index, product = next(
        (index, product)
        for index, product in enumerate(brief.products)
        if product.id == output.product_id
    )
    rules = brief.markets[output.market].phrase_rules
    findings = []
    included_fields = set()
    for rule in rules or []:
        phrase = unicodedata.normalize("NFC", rule.phrase)
        if not rule.case_sensitive:
            phrase = phrase.casefold()
        pattern = re.escape(phrase)
        if rule.match == "whole_word":
            pattern = rf"(?<!\w){pattern}(?!\w)"
        matches = []
        for field, text in visible.items():
            if not text:
                continue
            value = unicodedata.normalize("NFC", text)
            if not rule.case_sensitive:
                value = value.casefold()
            if re.search(pattern, value):
                override = product.copy_overrides.get(output.market)
                if override and field in override.model_fields_set:
                    origin, path = (
                        "product_override",
                        ["products", product_index, "copy_overrides", output.market, field],
                    )
                else:
                    origin, path = "market_shared", ["markets", output.market, "copy", field]
                # Every rule keeps its decision and field address. Include the
                # full editable copy once per output field, not once per rule.
                matches.append(
                    MatchedField(
                        field=field,
                        value=text if field not in included_fields else None,
                        origin=origin,
                        path=path,
                    )
                )
                included_fields.add(field)
        findings.append(
            Finding(
                item=output.id,
                rule=rule.id,
                severity="blocking" if matches else "pass",
                evidence=f"The prohibited phrase “{rule.phrase}” appears in printed wording."
                if matches
                else "The literal rule did not match visible copy.",
                remedy="Correct the wording, or edit or remove the prohibited phrase rule.",
                context=FindingContext(
                    phrase=PhraseContext(
                        configured_phrase=rule.phrase,
                        market=output.market,
                        match=rule.match,
                        case_sensitive=rule.case_sensitive,
                        normalization="NFC" if rule.case_sensitive else "NFC_then_casefold",
                        word_boundary="Unicode_word_characters"
                        if rule.match == "whole_word"
                        else "none",
                        matches=matches,
                    )
                ),
            )
        )
    return findings


def contextualize_findings(brief: Brief, outputs: list[Output], findings: list[Finding]) -> None:
    """Attach producer addresses and names to known scopes without echoing private input."""
    for finding in findings:
        output = next((item for item in outputs if item.id == finding.item), None)
        match = next(
            (
                (index, product)
                for index, product in enumerate(brief.products)
                if finding.item in {product.id, f"products/{product.id}", f"masters/{product.id}"}
                or finding.item in {f"{product.id}/{ratio}" for ratio in FORMATS}
                or (output is not None and product.id == output.product_id)
            ),
            None,
        )
        context = finding.context or FindingContext()
        fields = []
        if match:
            index, product = match
            context.product_id, context.product_name = product.id, product.name
            if output:
                context.market, context.ratio = output.market, output.ratio
            elif finding.item.startswith(f"{product.id}/"):
                context.ratio = finding.item.removeprefix(f"{product.id}/")
            if context.phrase:
                fields = [item.path for item in context.phrase.matches]
            elif finding.rule in {"text_fit"} and output:
                for field in ("message", "cta"):
                    if getattr(output.text, field) is None:
                        continue
                    override = product.copy_overrides.get(output.market)
                    fields.append(
                        ["products", index, "copy_overrides", output.market, field]
                        if override and field in override.model_fields_set
                        else ["markets", output.market, "copy", field]
                    )
            elif finding.rule == "subject_crop" and output:
                framing = product.framing.get(output.market, {}).get(output.ratio)
                fields = (
                    [["products", index, "framing", output.market, output.ratio, "subject_box"]]
                    if framing and framing.subject_box
                    else [["products", index, "source", "subject_box"]]
                )
            elif finding.rule == "controlled_text_color":
                fields = [["brand", "text_color"]]
            elif finding.rule == "logo_pixels":
                fields = [["brand", "logo_path"]]
            elif finding.rule == "logo_bounds" and output:
                fields = [
                    ["products", index, "framing", output.market, output.ratio, "logo_placement"]
                ]
            elif finding.item != (output.id if output else None):
                fields = [["products", index, "source"]]
                if finding.rule == "subject_crop":
                    fields[0].append("subject_box")
        elif finding.item == "logo":
            fields = [["brand", "logo_path"]]
        context.fields = [
            FieldIssue.model_validate(field_issue(path, finding.rule, finding.remedy))
            for path in fields
        ]
        finding.context = context


def make_local_plan(previous: Plan, brief: Brief, storage: Storage) -> Plan:
    """Keep immutable input identities for a local edit, including unrelated missing inputs.

    Callers verify the full affected set against these saved hashes before preview/save.
    Paid production always uses make_plan without this retained-input path.
    """
    return make_plan(
        brief, storage, _previous=previous, retained_requests=previous.generation_requests
    )


def make_plan(
    brief: Brief,
    storage: Storage,
    *,
    _previous: Plan | None = None,
    retained_requests: dict[str, GenerationRequest] | None = None,
) -> Plan:
    require_font()
    findings = []
    assets = {}
    images = {}
    project = storage.project(brief.campaign_id)
    references = {"logo": brief.brand.logo_path} if brief.brand.logo_path else {}
    if not brief.brand.logo_path:
        findings.append(
            Finding(
                item="logo",
                rule="logo_presence",
                severity="blocking" if brief.brand.require_logo else "warning",
                evidence="No logo artwork is attached.",
                remedy="Attach a logo, or turn off the logo requirement."
                if brief.brand.require_logo
                else "Attach a logo if these images need branding.",
            )
        )
    references.update(
        {
            f"products/{product.id}": product.source.path
            for product in brief.products
            if product.source.role != "concept"
        }
    )
    if brief.replay:
        references.update(
            {f"masters/{product}": source.path for product, source in brief.replay.sources.items()}
        )
    for item, reference in references.items():
        retained = _previous.assets.get(item) if _previous else None
        try:
            if retained and retained.reference == reference:
                project.require(reference, inspect_file=False)
                assets[item] = retained
                findings.extend(
                    f.model_copy(deep=True) for f in _previous.findings if f.item == item
                )
                continue
            path = storage.path(project.require(reference))
            image, _ = load_image(path)
            assets[item] = Asset(
                reference=reference,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                width=image.width,
                height=image.height,
            )
            images[item] = image
            if item.startswith("masters/"):
                master = brief.replay.sources[item.removeprefix("masters/")]
                if assets[item].sha256 != master.sha256:
                    raise ImageProblem("The saved replay master does not match its expected hash.")
                if "A" in image.getbands() and image.getchannel("A").getextrema()[0] < 255:
                    raise ImageProblem("A saved replay master must be an opaque scene.")
            if item == "logo":
                require_visible_logo(image)
        except (ImageProblem, StorageError, OSError) as error:
            findings.append(
                Finding(
                    item=item,
                    rule="asset",
                    severity="blocking",
                    evidence=str(error)
                    if isinstance(error, (ImageProblem, StorageError))
                    else "The named file could not be read.",
                    remedy="Replace the named asset; a broken file never permits generation.",
                )
            )
    outputs = []
    resolved_outputs = {output.id: output for output in expand_outputs(brief)}
    generation = {}
    transmitted = {}
    for product in brief.products:
        source = product.source
        if _previous:
            findings.extend(
                f.model_copy(deep=True)
                for f in _previous.findings
                if f.item in {product.id, *(f"{product.id}/{ratio}" for ratio in FORMATS)}
            )
        if source.role != "finished_scene" and (
            brief.replay is None or product.id not in brief.replay.sources
        ):
            # Local edits reuse the saved photograph and its original request.
            retained_request = (retained_requests or {}).get(product.id)
            generation[product.id] = retained_request or request_for(brief, product)
            transmitted[product.id] = (
                "Audience, creative direction, and a scene or concept description; "
                "no supplied product pixels."
            )
        image = images.get(f"products/{product.id}")
        if image is not None and source.role == "prepared_product":
            alpha = image.getchannel("A") if "A" in image.getbands() else None
            # Ignore only one 8-bit alpha unit (1/255 opacity) when checking the
            # product boundary. The original source pixels remain unchanged.
            bounds = (
                alpha.point(lambda value: 255 if value >= 2 else 0).getbbox() if alpha else None
            )
            if (
                alpha is None
                or alpha.getextrema() != (0, 255)
                or bounds is None
                or bounds[0] == 0
                or bounds[1] == 0
                or bounds[2] == image.width
                or bounds[3] == image.height
            ):
                findings.append(
                    Finding(
                        item=product.id,
                        rule="prepared_product",
                        severity="blocking",
                        evidence=(
                            "The PNG needs transparency and a fully visible product with margin."
                        ),
                        remedy="Supply an opaque-product cutout or select another supported route.",
                    )
                )
        if (
            image is not None
            and source.role == "finished_scene"
            and "A" in image.getbands()
            and image.getchannel("A").getextrema()[0] < 255
        ):
            findings.append(
                Finding(
                    item=product.id,
                    rule="scene_coverage",
                    severity="blocking",
                    evidence="This image has transparent areas.",
                    remedy="Select a product image without transparency.",
                )
            )
        for market in brief.markets:
            copy = brief.effective_copy(product, market)
            for ratio, (width, height) in FORMATS.items():
                output = resolved_outputs[f"{product.id}/{market}/{ratio.replace(':', 'x')}"]
                scene = (
                    images.get(f"products/{product.id}")
                    if source.role == "finished_scene"
                    else images.get(f"masters/{product.id}")
                )
                scene_asset = assets.get(
                    f"products/{product.id}"
                    if source.role == "finished_scene"
                    else f"masters/{product.id}"
                )
                scene_size = (
                    scene.size
                    if scene is not None
                    else ((scene_asset.width, scene_asset.height) if scene_asset else None)
                )
                if (
                    scene_size is not None
                    and output.framing.subject_box
                    and not protects_subject(
                        scene_size,
                        crop_box(scene_size, ratio, output.framing),
                        output.framing.subject_box,
                    )
                ):
                    findings.append(
                        Finding(
                            item=output.id,
                            rule="subject_crop",
                            severity="blocking",
                            evidence="This crop cuts into the marked product area.",
                            remedy=(
                                "Attach a photo framed to keep the product visible in every "
                                "selected format. The existing marked area is retained. "
                                "You can also revise "
                                "the imported brief’s framing settings and re-import it."
                            ),
                        )
                    )
                outputs.append(output)
                findings.extend(copy_findings(brief, output))
                if output.framing.logo_placement is not None and "logo" in assets:
                    try:
                        placed_logo_bounds(
                            (assets["logo"].width, assets["logo"].height),
                            (width, height),
                            output.framing.logo_placement,
                        )
                    except ImageProblem as error:
                        findings.append(
                            Finding(
                                item=output.id,
                                rule="logo_bounds",
                                severity="blocking",
                                evidence=str(error),
                                remedy="Move or resize the logo so it fits inside this image.",
                            )
                        )
                try:
                    text_layout(copy, ratio, output.framing)
                except ImageProblem as error:
                    findings.append(
                        Finding(
                            item=output.id,
                            rule="text_fit",
                            severity="blocking",
                            evidence=str(error),
                            remedy="Correct supplied copy or the layout before production.",
                        )
                    )
    for image in images.values():
        image.close()
    digest = canonical_hash(
        {
            "brief": brief.model_dump(mode="json"),
            "assets": {name: asset.model_dump() for name, asset in assets.items()},
            "generation_requests": {
                name: request.model_dump() for name, request in generation.items()
            },
        }
    )
    contextualize_findings(brief, outputs, findings)
    return Plan(
        digest=digest,
        brief=brief,
        assets=assets,
        outputs=outputs,
        generation_requests=generation,
        transmitted_data=transmitted,
        findings=findings,
        cost_status="unknown" if generation else "no_new_requests",
        cost_estimate=estimate_requests(generation),
    )


class Impact(Contract):
    render_outputs: list[str] = Field(default_factory=list)
    recheck_outputs: list[str] = Field(default_factory=list)
    generation_products: list[str] = Field(default_factory=list)


def compare_plans(old: Plan, new: Plan) -> Impact:
    before = {output.id: output for output in old.outputs}
    old_products = {product.id: product for product in old.brief.products}
    changed_sources = set()
    for product in new.brief.products:
        prior = old_products.get(product.id)
        if (
            prior is None
            or prior.source != product.source
            or old.assets.get(f"products/{product.id}") != new.assets.get(f"products/{product.id}")
            or old.assets.get(f"masters/{product.id}") != new.assets.get(f"masters/{product.id}")
            or old.brief.creative_direction != new.brief.creative_direction
            or old.brief.audience != new.brief.audience
            or old.generation_requests.get(product.id) != new.generation_requests.get(product.id)
        ):
            changed_sources.add(product.id)
    result = Impact(
        generation_products=[item for item in new.generation_products if item in changed_sources]
    )
    old_logo = old.assets.get("logo")
    new_logo = new.assets.get("logo")
    logo_changed = (old_logo.sha256 if old_logo else None) != (
        new_logo.sha256 if new_logo else None
    )
    default_color_changed = old.brief.brand.text_color != new.brief.brand.text_color
    for output in new.outputs:
        # Palette and brand-name changes update the brief. A default-color
        # change only paints text that actually inherits that color.
        placements = output.framing.text_placements
        recolor = default_color_changed and any(
            getattr(output.text, field)
            and (field not in placements or placements[field].color is None)
            for field in ("message", "cta")
        )
        if (
            output != before.get(output.id)
            or logo_changed
            or recolor
            or output.product_id in changed_sources
        ):
            result.render_outputs.append(output.id)
        if (
            output.id in result.render_outputs
            or old.brief.markets.get(output.market) != new.brief.markets[output.market]
        ):
            result.recheck_outputs.append(output.id)
    return result
