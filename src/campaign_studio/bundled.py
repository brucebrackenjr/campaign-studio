"""Install the two curated campaigns once, through the ordinary local lifecycle."""

import hashlib
import json
import re
import time
from importlib.resources import files
from pathlib import PurePosixPath

from campaign_studio.brief import Brief
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.storage import StorageError

RECEIPT = "private/bundled-campaigns.json"


def load_campaigns(source=None, *, product_limit=5):
    """Read only explicitly listed public resources, never paths from an imported brief."""
    source = source or files("campaign_studio").joinpath("resources/campaigns")
    manifest = json.loads(source.joinpath("manifest.json").read_bytes())
    if manifest.get("version") != 1 or len(manifest.get("campaigns", [])) != 2:
        raise StorageError("The bundled campaign package is incomplete.")
    campaigns = []
    for entry in manifest["campaigns"]:
        slug = entry["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise StorageError("The bundled campaign package has an invalid folder.")
        if entry["brief"] != f"{slug}/brief.json" or type(entry["compose"]) is not bool:
            raise StorageError("The bundled campaign package has an invalid brief entry.")
        raw = json.loads(source.joinpath(entry["brief"]).read_bytes())
        brief = Brief.model_validate(raw)
        if len(brief.products) > product_limit:
            raise StorageError("The bundled campaigns exceed the configured product limit.")
        references = {brief.brand.logo_path}
        references.update(p.source.path for p in brief.products if p.source.role != "concept")
        if references != entry["files"].keys():
            raise StorageError("The bundled campaign attachments do not match its brief.")
        assets = {}
        for reference, expected in entry["files"].items():
            path = PurePosixPath(reference)
            if (
                len(path.parts) != 2
                or path.parts[0] != "assets"
                or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name)
            ):
                raise StorageError("The bundled campaign package has an invalid attachment.")
            payload = source.joinpath(slug, reference).read_bytes()
            if hashlib.sha256(payload).hexdigest() != expected:
                raise StorageError("A bundled campaign attachment does not match its package.")
            assets[reference] = payload
        campaigns.append((entry, raw, assets))
    return manifest, campaigns


async def install_campaigns(workspace, runner, *, source=None, product_limit=5):
    """Claim setup once; interrupted ordinary campaigns remain available on the next launch.

    The receipt is not a campaign-count policy or a resume queue. It prevents a
    second setup call from recreating deleted examples or replaying partial work.
    """
    storage = workspace.storage
    with storage.exclusive_run():
        if storage.path(RECEIPT).exists():
            return None
        manifest, campaigns = load_campaigns(source, product_limit=product_limit)
        receipt = {"version": manifest["version"], "state": "started", "campaigns": []}
        storage.write_new(RECEIPT, json.dumps(receipt).encode())
    started = time.monotonic()
    intake = Intake(storage)
    for entry, original, assets in campaigns:
        campaign_id = intake.new_campaign_id()
        value = intake.import_value(campaign_id, original)
        document = intake.create(
            campaign_id, value, attachment_hints=intake.attachment_hints(original, value)
        )
        receipt["campaigns"].append({"slug": entry["slug"], "id": campaign_id})
        storage.write(RECEIPT, json.dumps(receipt).encode())
        value["brand"]["logo_path"] = intake.upload(
            assets[original["brand"]["logo_path"]], None, campaign_id, "logo"
        )["reference"]
        for old, product in zip(original["products"], value["products"], strict=True):
            if product["source"]["role"] != "concept":
                product["source"]["path"] = intake.upload(
                    assets[old["source"]["path"]], None, campaign_id, "product"
                )["reference"]
        document = intake.save(
            campaign_id, BriefDraft(value=value, version=document["version"]), workspace
        )
        plan = intake.prepare(campaign_id, document["version"], workspace)
        run = workspace.run_for(campaign_id)
        if entry["compose"]:
            # Built-in supplied-image composition is local setup, never authority
            # to dispatch a request if the curated brief unexpectedly changes.
            if plan.generation_products:
                raise StorageError("A bundled local campaign unexpectedly requires new imagery.")
            workspace.state.confirm(run["id"], plan.digest)
            result = await runner.execute(run["id"])
            if not result["complete"]:
                raise StorageError("The bundled campaign could not finish local composition.")
    receipt.update(state="complete", elapsed_seconds=round(time.monotonic() - started, 3))
    storage.write(RECEIPT, json.dumps(receipt).encode())
    return receipt
