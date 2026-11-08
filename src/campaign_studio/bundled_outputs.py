"""Validate bundled showcase resources before importing their editable source inputs."""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

from campaign_studio.brief import FORMATS, Brief
from campaign_studio.imaging import font_path
from campaign_studio.planning import Finding, canonical_hash, expand_outputs
from campaign_studio.storage import StorageError

PIXEL_RULES = {"image_integrity", "logo_pixels", "controlled_text_color"}


def prepared_font_hashes():
    return {
        name: hashlib.sha256(Path(font_path(name)).read_bytes()).hexdigest()
        for name in ("noto_sans", "barlow_condensed")
    }


@dataclass(frozen=True)
class PreparedOutputs:
    source: Traversable
    descriptor: dict


def load_prepared_outputs(source, entry, original):
    """Validate the complete resource set before setup claims or creates a campaign."""
    prepared = entry["prepared_outputs"]
    if prepared is None:
        return None
    if prepared.get("path") != f"{entry['slug']}/prepared-outputs.json":
        raise StorageError("The bundled output descriptor has an invalid path.")
    payload = source.joinpath(prepared["path"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != prepared.get("sha256"):
        raise StorageError("The bundled output descriptor does not match its package.")
    descriptor = json.loads(payload)
    if (
        descriptor.get("version") != 1
        or descriptor.get("brief_sha256") != canonical_hash(original)
        or descriptor.get("assets") != entry["files"]
        or descriptor.get("fonts") != prepared_font_hashes()
    ):
        raise StorageError("The bundled outputs do not match their editable campaign inputs.")
    expected = {
        (product["id"], market, ratio)
        for product in original["products"]
        for market in original["markets"]
        for ratio in FORMATS
    }
    expected_outputs = {
        output.id: output.model_dump(mode="json")
        for output in expand_outputs(Brief.model_validate(original))
    }
    observed = set()
    for record in descriptor["outputs"]:
        output = record["output"]
        if output != expected_outputs.get(output.get("id")):
            raise StorageError(
                "The bundled output wording or layout does not match its editable brief."
            )
        key = (output["product_id"], output["market"], output["ratio"])
        if key not in expected or key in observed:
            raise StorageError("The bundled output list does not match the campaign scope.")
        observed.add(key)
        product, market, ratio = key
        reference = f"{entry['slug']}/outputs/{product}/{ratio.replace(':', 'x')}/{market}.png"
        if record["file"] != reference:
            raise StorageError("A bundled output has an invalid file path.")
        checks = [Finding.model_validate(finding) for finding in record["findings"]]
        if not any(f.rule == "image_integrity" for f in checks) or any(
            f.rule not in PIXEL_RULES or f.severity != "pass" or f.item != output["id"]
            for f in checks
        ):
            raise StorageError("A bundled output is missing its verified pixel checks.")
        _read_output(source, record)
    if observed != expected:
        raise StorageError("The bundled output set is incomplete.")
    return PreparedOutputs(source, descriptor)


def _read_output(source, record):
    try:
        payload = source.joinpath(record["file"]).read_bytes()
    except OSError as error:
        raise StorageError("A bundled output is missing or unreadable.") from error
    if len(payload) != record["bytes"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise StorageError("A bundled output does not match its verified package.")
    return payload
