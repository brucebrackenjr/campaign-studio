"""Curated saved-source replay and clearly labeled synthetic installation cases."""

import asyncio
from importlib.resources import files
from uuid import uuid4

from PIL import Image, ImageDraw

from campaign_studio.brief import Brief
from campaign_studio.imaging import save_srgb
from campaign_studio.storage import Storage


def write_sample(
    storage: Storage,
    product_count: int = 2,
    one_market: bool = False,
    *,
    campaign_id: str | None = None,
) -> tuple[str, Brief]:
    if product_count not in (2, 3):
        raise ValueError("Synthetic examples support two or three products.")
    from campaign_studio.state import State

    state = State(storage)
    campaign_id = (
        campaign_id
        or {
            2: "80d58cbe-5e57-5544-9b07-a4f018714dde",
            3: "10a02c18-9114-53ce-beb7-b41293746c84",
        }[product_count]
    )
    state.register_campaign(campaign_id, "Synthetic sample")
    project = storage.project(campaign_id)
    storage.require_capacity(product_count * 1920 * 1920 * 4 + 1_000_000)
    prefix = (
        project.ref("Assets/Samples")
        + "/"
        + state.reserve_owned_namespaces(campaign_id, (project.ref("Assets/Samples/{id}"),))
    )
    provenance = {
        "label": "Synthetic geometric fixture",
        "rights": "Locally authored test graphics",
        "synthetic": True,
    }
    with Image.new("RGBA", (200, 80)) as logo:
        ImageDraw.Draw(logo).rectangle((10, 10, 190, 70), fill="#FFFFFF")
        save_srgb(logo, storage.path(project.claim(f"{prefix}/logo.png")))
    products = []
    for index in range(product_count):
        identifier = f"product-{index + 1}"
        reference = f"{prefix}/{identifier}.png"
        with Image.new("RGB", (1920, 1920), (25 + 15 * index, 55, 65)) as scene:
            draw = ImageDraw.Draw(scene)
            draw.ellipse((750, 750, 1170, 1170), fill=(180, 100 + 20 * index, 65))
            save_srgb(scene, storage.path(project.claim(reference)))
        products.append(
            {
                "id": identifier,
                "name": f"Synthetic product {index + 1}",
                "description": "A geometric test subject, not a real product photograph.",
                "source": {
                    "role": "finished_scene",
                    "path": reference,
                    "provenance": provenance,
                    "subject_box": {"left": 0.39, "top": 0.39, "right": 0.61, "bottom": 0.61},
                },
            }
        )
    markets = {
        "en-US": {
            "copy": {"message": "Made for everyday moments.", "cta": "Explore the collection."},
            "phrase_rules": [
                {
                    "id": "test-block",
                    "phrase": "TESTBLOCK",
                }
            ],
        }
    }
    if not one_market:
        markets["es-MX"] = {
            "copy": {"message": "Siempre contigo.", "cta": "Explora la colección."},
            "phrase_rules": [],
        }
    brief = Brief.model_validate(
        {
            "campaign_id": campaign_id,
            "name": f"Synthetic sample {product_count}" + (" – US" if one_market else ""),
            "audience": "People evaluating the local workflow using synthetic fixtures.",
            "creative_direction": "Geometric shapes for behavior checks. Not photography.",
            "brand": {
                "id": "synthetic-brand",
                "name": "Synthetic brand",
                "logo_path": f"{prefix}/logo.png",
                "logo_provenance": provenance,
                "palette": ["#FFFFFF"],
                "text_color": "#FFFFFF",
            },
            "copy_provenance": {
                "label": "Illustrative supplied copy",
                "rights": "Authored for synthetic checks",
                "synthetic": True,
            },
            "markets": markets,
            "products": products,
        }
    )
    reference = f"{prefix}/brief.json"
    storage.path(project.claim(reference)).write_text(
        brief.model_dump_json(indent=2, exclude_unset=True) + "\n", encoding="utf-8"
    )
    return reference, brief


def _exercise_delivery(storage, brief, *, evidence):
    """Prepare sources, autosave a local correction, reopen, and download a ZIP."""
    from campaign_studio.delivery import delivery_manifest, prepare_current_zip
    from campaign_studio.planning import compare_plans, make_plan
    from campaign_studio.runner import Runner
    from campaign_studio.state import State
    from campaign_studio.workspace import TextEdit, Workspace

    state = State(storage)
    run_id = state.create(make_plan(brief, storage), allowance=0)
    state.confirm(run_id, state.plan(run_id).digest)
    asyncio.run(Runner(state).execute(run_id))
    workspace = Workspace(state)
    current = workspace.current_composition(brief.campaign_id)
    corrected = 0
    if "es-MX" in brief.markets:
        workspace.save_composition(
            brief.campaign_id,
            TextEdit(
                revision_id=current["version"],
                product_id=brief.products[0].id,
                market="es-MX",
                ratio="1:1",
                values={"message": "Para cada movimiento."},
                field_scopes={"message": "all_products"},
            ),
        )
        changed = workspace.current_composition(brief.campaign_id)
        corrected = len(compare_plans(current["plan"], changed["plan"]).render_outputs)
    # A fresh service reads the same saved instructions without rendering images.
    current = Workspace(State(storage)).current_composition(brief.campaign_id)
    manifest = delivery_manifest(current)
    with prepare_current_zip(state, current) as download:
        handoff_bytes = download.size
    result = {
        "evidence": evidence,
        "run_id": run_id,
        "composition_version": current["version"],
        "complete": manifest["complete"],
        "expected_images": manifest["metrics"]["requested_images"],
        "completed_images": manifest["metrics"]["completed_images"],
        "corrected_images": corrected,
        "provider_requests": len(state.attempts(run_id)),
        "handoff_zip_bytes": handoff_bytes,
    }
    if brief.replay:
        result["historical_requests"] = brief.replay.historical_requests
    return result


def run_sample(storage: Storage, product_count: int = 2, one_market: bool = False) -> dict:
    """Exercise real local services with synthetic inputs and no provider object."""
    _, brief = write_sample(storage, product_count, one_market, campaign_id=str(uuid4()))
    return _exercise_delivery(storage, brief, evidence="synthetic-key-free-workflow")


def write_replay(storage: Storage, *, campaign_id: str | None = None) -> Brief:
    """Copy curated genuine saved sources; never import private execution records."""
    from campaign_studio.state import State

    state = State(storage)
    package = files("campaign_studio").joinpath("resources/replay")
    brief = Brief.model_validate_json(package.joinpath("brief.json").read_bytes())
    value = brief.model_dump()
    if campaign_id is not None:
        value["campaign_id"] = campaign_id
        brief = Brief.model_validate(value)
    holders = [
        value["brand"],
        *[p["source"] for p in value["products"] if p["source"]["role"] != "concept"],
    ]
    holders.extend(value["replay"]["sources"].values())
    sizes = [
        len(
            package.joinpath(
                holder["logo_path"] if holder is value["brand"] else holder["path"]
            ).read_bytes()
        )
        for holder in holders
    ]
    storage.require_capacity(sum(sizes) + max(sizes, default=0))
    state.register_campaign(brief.campaign_id, brief.brand.name)
    project = storage.project(brief.campaign_id)
    prefix = (
        project.ref("Assets/Replays")
        + "/"
        + state.reserve_owned_namespaces(brief.campaign_id, (project.ref("Assets/Replays/{id}"),))
    )
    for holder in holders:
        field = "logo_path" if holder is value["brand"] else "path"
        reference = holder[field]
        target = f"{prefix}/{reference}"
        storage.path(target)  # Validate before resolving the packaged resource.
        project.write(target, package.joinpath(reference).read_bytes())
        holder[field] = target
    return Brief.model_validate(value)


def run_replay(storage: Storage) -> dict:
    """Reuse genuine sources, autosave copy, reopen, and export without a key."""
    return _exercise_delivery(
        storage,
        write_replay(storage, campaign_id=str(uuid4())),
        evidence="genuine_saved_source_replay",
    )
