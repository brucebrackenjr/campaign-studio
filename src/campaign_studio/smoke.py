"""A synthetic, entirely local installation check, not campaign generation."""

import hashlib
import json
import sqlite3
from importlib.resources import files
from typing import Literal
from uuid import uuid4

from PIL import Image, ImageDraw, PngImagePlugin
from pydantic import BaseModel

from campaign_studio.imaging import require_font
from campaign_studio.storage import Storage


class SmokeResult(BaseModel):
    evidence: Literal["synthetic-installation-check"] = "synthetic-installation-check"
    photographic_evidence: Literal[False] = False
    provider_requests: Literal[0] = 0
    width: int
    height: int
    image_sha256: str
    sqlite_version: str
    artifact_reference: str


def run_smoke(storage: Storage) -> SmokeResult:
    require_font()
    storage.initialize()
    if "Campaign Studio" not in (
        files("campaign_studio").joinpath("templates/motif.html").read_text(encoding="utf-8")
    ):
        raise RuntimeError("The installed application template is missing.")
    reference = f"smoke/{uuid4().hex}"
    directory = storage.path(reference)
    directory.mkdir(parents=True, mode=0o700)
    image_path = storage.path(f"{reference}/synthetic.png")
    with Image.new("RGB", (128, 128), "#f4f0e8") as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((16, 16, 111, 111), fill="#173b45")
        draw.ellipse((40, 40, 87, 87), fill="#e28b58")
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Evidence", "Synthetic installation fixture; not photographic evidence.")
        image.save(image_path, format="PNG", pnginfo=metadata)
    with Image.open(image_path) as reopened:
        reopened.load()
        if reopened.format != "PNG" or reopened.size != (128, 128):
            raise RuntimeError("The synthetic PNG failed its round-trip check.")
    result = SmokeResult(
        width=128,
        height=128,
        image_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
        sqlite_version=sqlite3.sqlite_version,
        artifact_reference=reference,
    )
    storage.path(f"{reference}/manifest.json").write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    # Read back through JSON and Pydantic to check the installed serialization path.
    saved = json.loads(storage.path(f"{reference}/manifest.json").read_text(encoding="utf-8"))
    if SmokeResult.model_validate(saved) != result:
        raise RuntimeError("The synthetic manifest failed its round-trip check.")
    return result
