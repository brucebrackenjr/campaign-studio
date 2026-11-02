"""The strict JSON brief and exact supplied-copy contract."""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
CAMPAIGN_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
CampaignID = Annotated[str, StringConstraints(pattern=CAMPAIGN_ID_PATTERN)]


def valid_campaign_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(CAMPAIGN_ID_PATTERN, value) is not None


Text = Annotated[str, StringConstraints(min_length=1, max_length=5000, pattern=r"\S")]


def normalize_color(value):
    """Normalize completed hex only; unfinished authored text remains untouched."""
    if isinstance(value, str) and re.fullmatch(r"#?[0-9A-Fa-f]{6}", value.strip()):
        return "#" + value.strip().removeprefix("#").upper()
    return value


Color = Annotated[
    str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$"), BeforeValidator(normalize_color)
]
Market = Literal["en-US", "es-MX"]
Ratio = Literal["1:1", "9:16", "16:9"]
FORMATS: dict[Ratio, tuple[int, int]] = {
    "1:1": (1080, 1080),
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)


class Provenance(Contract):
    label: Text
    rights: Text
    synthetic: bool = False


class Box(Contract):
    """Optional subject bounds in oriented image coordinates, from zero to one."""

    left: float = Field(ge=0, lt=1)
    top: float = Field(ge=0, lt=1)
    right: float = Field(gt=0, le=1)
    bottom: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.left >= self.right or self.top >= self.bottom:
            raise ValueError("subject bounds must have positive width and height")
        return self


class LogoPlacement(Contract):
    """Proportional logo bounds in output-canvas coordinates, from zero to one."""

    left: float = Field(ge=0, le=1, allow_inf_nan=False)
    top: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)


class TextPlacement(Contract):
    """Canvas position, independent wrapping measure, and actual output font size."""

    left: float = Field(ge=0, le=1, allow_inf_nan=False)
    top: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    # FreeType's pixel-size representation ends here. Actual glyph containment,
    # rather than an editorial size maximum, decides whether text fits a canvas.
    font_size: int = Field(ge=1, le=65535, strict=True)
    font_id: Literal["noto_sans", "barlow_condensed"] = "noto_sans"
    color: Color | None = None


class Framing(Contract):
    """Local output choices; these never alter the provider request or source image."""

    focal_x: float = Field(default=0.5, ge=0, le=1)
    focal_y: float = Field(default=0.5, ge=0, le=1)
    zoom: float = Field(default=1, ge=1, le=2)
    copy_placement: Literal["top", "bottom"] = "top"
    subject_box: Box | None = None
    # Old records inherited source bounds when their per-format box was absent.
    # An explicit clear must survive serialization without reactivating inheritance.
    inherit_subject: bool = True
    logo_placement: LogoPlacement | None = None
    text_placements: dict[Literal["message", "cta"], TextPlacement] = Field(default_factory=dict)


class FinishedScene(Contract):
    role: Literal["finished_scene"]
    path: Text
    provenance: Provenance
    subject_box: Box | None = None


class PreparedProduct(Contract):
    role: Literal["prepared_product"]
    path: Text
    provenance: Provenance
    opaque_nonreflective_product_confirmed: Literal[True]


class Concept(Contract):
    role: Literal["concept"]
    imagery_absent: Literal[True]
    description: Text
    provenance: Provenance


Source = Annotated[FinishedScene | PreparedProduct | Concept, Field(discriminator="role")]


class Copy(Contract):
    message: Text
    cta: Text | None = None

    @model_validator(mode="after")
    def visible_text(self):
        for value in (self.message, self.cta):
            if value is not None and (not value.strip() or "\r" in value or "\t" in value):
                raise ValueError(
                    "copy must contain visible text; use ordinary spaces and LF breaks"
                )
        return self


class Override(Contract):
    message: Text | None = None
    cta: Text | None = None

    @model_serializer(mode="plain")
    def supplied_fields(self):
        # Omission means inheritance, so serializing defaults would change the brief.
        return {field: getattr(self, field) for field in self.model_fields_set}

    @model_validator(mode="after")
    def cannot_clear_message(self):
        if "message" in self.model_fields_set and self.message is None:
            raise ValueError("a required message cannot be cleared")
        return self


class PhraseRule(Contract):
    id: Identifier
    phrase: Text
    match: Literal["literal", "whole_word"] = "literal"
    case_sensitive: bool = False


class MarketContent(Contract):
    text: Copy = Field(alias="copy")
    phrase_rules: list[PhraseRule] | None = None

    @model_validator(mode="after")
    def unique_rules(self):
        if self.phrase_rules:
            ids = [rule.id for rule in self.phrase_rules]
            if len(set(ids)) != len(ids):
                raise ValueError("phrase-rule IDs must be unique within a market")
        return self


class Product(Contract):
    id: Identifier
    name: Text
    description: Text
    source: Source
    copy_overrides: dict[Market, Override] = Field(default_factory=dict)
    framing: dict[Market, dict[Ratio, Framing]] = Field(default_factory=dict)


class Brand(Contract):
    id: Identifier
    name: Text
    logo_path: Text
    logo_provenance: Provenance
    palette: list[Color] = Field(min_length=1, max_length=8)
    text_color: Color

    @field_validator("palette", mode="before")
    @classmethod
    def completed_palette(cls, value):
        if not isinstance(value, list):
            return value
        result = []
        for item in value:
            normalized = normalize_color(item)
            if (
                not isinstance(normalized, str)
                or not re.fullmatch(r"#[0-9A-F]{6}", normalized)
                or normalized not in result
            ):
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def controlled_text_color(self):
        if self.text_color.upper() not in {color.upper() for color in self.palette}:
            raise ValueError("text_color must be one of the controlled palette colors")
        return self


class ReplayMaster(Contract):
    path: Text
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: Text
    generated_on: date
    provenance: Provenance


class Replay(Contract):
    partial: bool = False
    sources: dict[Identifier, ReplayMaster] = Field(min_length=1, max_length=5)
    historical_requests: int = Field(ge=1, strict=True)
    history: Text


class Brief(Contract):
    schema_version: Literal[1] = 1
    campaign_id: CampaignID
    name: Text
    audience: Text
    creative_direction: Text
    brand: Brand
    copy_provenance: Provenance
    markets: dict[Market, MarketContent] = Field(min_length=1, max_length=2)
    products: list[Product] = Field(min_length=1, max_length=5)
    replay: Replay | None = None

    @model_validator(mode="after")
    def coherent(self):
        for value in (self.name, self.audience, self.creative_direction):
            if not value.strip():
                raise ValueError(
                    "campaign name, audience, and creative direction must be meaningful"
                )
        ids = [product.id for product in self.products]
        if len(set(ids)) != len(ids):
            raise ValueError("product IDs must be unique")
        required_masters = {
            product.id for product in self.products if product.source.role != "finished_scene"
        }
        if self.replay and (
            not set(self.replay.sources) <= required_masters
            or (not self.replay.partial and set(self.replay.sources) != required_masters)
        ):
            raise ValueError(
                "replay must name exactly every prepared-product or concept master unless partial; "
                "unknown products and finished scenes are not allowed"
            )
        for product in self.products:
            if set(product.copy_overrides) - set(self.markets):
                raise ValueError("copy overrides must use a selected market")
            for market in self.markets:
                self.effective_copy(product, market)
            for formats in product.framing.values():
                for framing in formats.values():
                    for placement in framing.text_placements.values():
                        if placement.color and placement.color.upper() not in {
                            color.upper() for color in self.brand.palette
                        }:
                            raise ValueError(
                                "text placement color must be one of the controlled palette colors"
                            )
        return self

    def effective_copy(self, product: Product, market: Market) -> Copy:
        content = self.markets[market]
        values = content.text.model_dump()
        if override := product.copy_overrides.get(market):
            values.update(override.model_dump(exclude_unset=True))
        return Copy.model_validate(values)
