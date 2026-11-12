"""The exact paid request belongs to the saved confirmation snapshot."""

from typing import Literal

from pydantic import Field

from campaign_studio.brief import Brief, Contract, Identifier, Product

MODEL = "gpt-image-2.5-flare-2026-09-08"

# A centered portrait and landscape crop share only the middle of the square.
CONCEPT_FRAMING = (
    "Framing for delivery: this 1920 by 1920 photograph will be cropped into a tall 9:16 "
    "portrait and a wide 16:9 landscape as well as a square. Use a wide establishing "
    "photograph, with the camera pulled back. Keep the ENTIRE product, including every end, "
    "buckle, rope, fringe, and other attached part, inside the central 40 percent of the "
    "image width AND the central 40 percent of its height. Center the product horizontally "
    "and vertically. Surround it with generous continuous scenery on all four sides; do not "
    "enlarge the product to fill the square. Leave quiet space above the product for later "
    "lettering. These framing requirements take precedence over any conflicting close-up or "
    "upper/lower-half placement instruction above. Do not draw crop guides, borders, text, "
    "or logos."
)


class GenerationRequest(Contract):
    method_version: Literal[1, 2] = 2
    model: str = MODEL
    size: Literal["1920x1920"] = "1920x1920"
    quality: Literal["high"] = "high"
    background: Literal["opaque"] = "opaque"
    output_format: Literal["png"] = "png"
    n: Literal[1] = 1
    max_retries: Literal[0] = 0
    deadline_seconds: float = Field(default=120.0, gt=0)
    prompt: str


class ReplacementIntent(Contract):
    """An explicit one-product request, separate from its unchanged image prompt."""

    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    product_id: Identifier
    document_version: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_reference: str
    confirmation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def request_for(brief: Brief, product: Product) -> GenerationRequest:
    if product.source.role == "concept":
        treatment = (
            f"A concept image of the described subject.\nSubject: {product.source.description}"
        )
    else:
        treatment = (
            "An empty campaign environment only. No products, foreground subjects, equipment, "
            "people, text, or logos. Leave the foreground surface empty for local composition."
        )
    prompt = (
        f"{treatment}\nAudience: {brief.audience}\n"
        f"Creative direction: {brief.creative_direction}\n"
        "Photographic treatment, no text, no logo, and an opaque square canvas. "
    )
    prompt += (
        CONCEPT_FRAMING
        if product.source.role == "concept"
        else "Keep important content within the central 1080-square region, "
        "with room above for supplied copy."
    )
    return GenerationRequest(prompt=prompt)
