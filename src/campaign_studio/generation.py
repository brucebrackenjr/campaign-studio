"""The exact paid request belongs to the saved confirmation snapshot."""

from typing import Literal

from pydantic import Field

from campaign_studio.brief import Brief, Contract, Product

MODEL = "gpt-image-2.5-flare-2026-09-08"


class GenerationRequest(Contract):
    method_version: Literal[1, 2] = 2
    model: str = MODEL
    size: Literal["1920x1920"] = "1920x1920"
    quality: Literal["high"] = "high"
    background: Literal["opaque"] = "opaque"
    output_format: Literal["png"] = "png"
    n: Literal[1] = 1
    max_retries: Literal[0] = 0
    deadline_seconds: float = Field(default=120, gt=0)
    prompt: str


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
        "Keep important content within the central 1080-square region, "
        "with room above for supplied copy."
    )
    return GenerationRequest(prompt=prompt)
