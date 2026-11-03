"""Maintainer-only refresh of fixed example PNGs; never called during app startup.

From the source directory: uv run --locked python scripts/build_bundled_outputs.py
This composes the supplied Autumn sources locally and updates their resource hashes.
"""

import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import PIL

from campaign_studio import __version__
from campaign_studio.brief import Brief
from campaign_studio.bundled_outputs import PIXEL_RULES, prepared_font_hashes
from campaign_studio.planning import canonical_hash, make_plan
from campaign_studio.runner import Runner
from campaign_studio.state import State
from campaign_studio.storage import Storage


def build_prepared_outputs(resources, entry, storage):
    original = json.loads((resources / entry["brief"]).read_bytes())
    from copy import deepcopy

    state = State(storage)
    value = deepcopy(original)
    state.register_campaign(value["campaign_id"], value["brand"]["name"])
    project = storage.project(value["campaign_id"])
    copied = {}
    for reference, expected in entry["files"].items():
        payload = (resources / entry["slug"] / reference).read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError("An example source does not match the resource manifest.")
        target = project.ref("Assets/Prepared/" + reference)
        project.write(target, payload)
        copied[reference] = target
    value["brand"]["logo_path"] = copied[value["brand"]["logo_path"]]
    for product in value["products"]:
        product["source"]["path"] = copied[product["source"]["path"]]
    plan = make_plan(Brief.model_validate(value), storage)
    if plan.blocked or plan.generation_products:
        raise ValueError("Prepared examples must use complete supplied sources and valid copy.")
    run_id = state.create(plan, 0)
    state.confirm(run_id, state.plan(run_id).digest)
    manifest = asyncio.run(Runner(state).execute(run_id))
    if not manifest["complete"] or state.attempts(run_id):
        raise ValueError("The example must finish without provider requests.")
    plan = state.plan(run_id)
    prefix = storage.path(state.revision(manifest["revision_id"])["manifest_ref"]).parent
    records = []
    output_root = resources / entry["slug"] / "outputs"
    if output_root.exists():
        shutil.rmtree(output_root)  # This fixed resource folder contains only derived PNGs.
    for item, output in zip(manifest["outputs"], plan.outputs, strict=True):
        relative = (
            f"{entry['slug']}/outputs/{output.product_id}/"
            f"{output.ratio.replace(':', 'x')}/{output.market}.png"
        )
        payload = (prefix / item["file"]).read_bytes()
        target = resources / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        checks = [
            f for f in manifest["findings"] if f["item"] == output.id and f["rule"] in PIXEL_RULES
        ]
        if hashlib.sha256(payload).hexdigest() != item["sha256"] or any(
            f["severity"] != "pass" for f in checks
        ):
            raise ValueError("An example output failed its recorded integrity checks.")
        records.append(
            {
                "output": output.model_dump(mode="json"),
                "file": relative,
                "bytes": len(payload),
                "sha256": item["sha256"],
                "pixel_sha256": item["pixel_sha256"],
                "findings": checks,
            }
        )
    descriptor = {
        "version": 1,
        "brief_sha256": canonical_hash(original),
        "assets": entry["files"],
        "fonts": prepared_font_hashes(),
        "text_color": plan.brief.brand.text_color,
        "rendered_with": {"application": __version__, "pillow": PIL.__version__},
        "outputs": records,
    }
    path = resources / entry["slug"] / "prepared-outputs.json"
    path.write_bytes((json.dumps(descriptor, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    entry["prepared_outputs"] = {
        "path": path.relative_to(resources).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    resources = Path(__file__).resolve().parents[1] / "src/campaign_studio/resources/campaigns"
    manifest_path = resources / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    autumn = next(e for e in manifest["campaigns"] if e["slug"] == "autumn-winds")
    with tempfile.TemporaryDirectory(prefix="motif-example-build-") as temporary:
        build_prepared_outputs(resources, autumn, Storage(Path(temporary).resolve() / "workspace"))
    manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    print("Prepared Autumn Winds outputs and updated the resource manifest. No AI requests.")


if __name__ == "__main__":
    main()
