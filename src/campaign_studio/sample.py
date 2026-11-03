"""Curated saved-source replay and clearly labeled synthetic installation cases."""

import asyncio
from importlib.resources import files

from PIL import Image, ImageDraw

from campaign_studio.brief import Brief
from campaign_studio.imaging import save_srgb
from campaign_studio.storage import Storage


def write_sample(
    storage: Storage, product_count: int = 2, one_market: bool = False
) -> tuple[str, Brief]:
    if product_count not in (2, 3):
        raise ValueError("Synthetic examples support two or three products.")
    from campaign_studio.state import State

    state = State(storage)
    campaign_id = {
        2: "80d58cbe-5e57-5544-9b07-a4f018714dde",
        3: "10a02c18-9114-53ce-beb7-b41293746c84",
    }[product_count]
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
            "name": f"Synthetic sample – {product_count} products"
            + (" – US only" if one_market else ""),
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


def run_sample(storage: Storage, product_count: int = 2, one_market: bool = False) -> dict:
    """Exercise real local services with synthetic inputs and no provider object."""
    from campaign_studio.exporting import export_package
    from campaign_studio.planning import compare_plans, make_plan
    from campaign_studio.runner import Runner
    from campaign_studio.state import State

    _, brief = write_sample(storage, product_count, one_market)
    state = State(storage)
    run_id = state.create(make_plan(brief, storage), allowance=0)
    state.confirm(run_id, state.plan(run_id).digest)
    runner = Runner(state)
    result = asyncio.run(runner.execute(run_id))
    corrected = 0
    if "es-MX" in brief.markets:
        value = state.plan(run_id).brief.model_dump()
        value["markets"]["es-MX"]["copy"]["message"] = "Para cada movimiento."
        edited = Brief.model_validate(value)
        preview = make_plan(edited, storage)
        corrected = len(compare_plans(state.plan(run_id), preview).render_outputs)
        result = runner.revise(run_id, edited, result["revision_id"], preview.digest)
    index = export_package(
        state, result["revision_id"], storage.project(brief.campaign_id).ref(f"Exports/{run_id}")
    )
    return {
        "evidence": "synthetic-key-free-workflow",
        "run_id": run_id,
        "revision_id": result["revision_id"],
        "complete": result["complete"],
        "expected_images": len(result["outputs"]),
        "completed_images": result["metrics"]["completed_images"],
        "corrected_images": corrected,
        "provider_requests": len(state.attempts(run_id)),
        "offline_index": index,
    }


def write_replay(storage: Storage) -> Brief:
    """Copy curated genuine saved sources; never import private execution records."""
    from campaign_studio.state import State

    state = State(storage)
    package = files("campaign_studio").joinpath("resources/replay")
    brief = Brief.model_validate_json(package.joinpath("brief.json").read_bytes())
    value = brief.model_dump()
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
    """Recompose genuine saved sources, correct copy, reopen, and export without a key."""
    from campaign_studio.exporting import export_package
    from campaign_studio.planning import compare_plans, make_plan
    from campaign_studio.runner import Runner
    from campaign_studio.state import State

    state = State(storage)
    brief = write_replay(storage)
    run_id = state.create(make_plan(brief, storage), allowance=0)
    state.confirm(run_id, state.plan(run_id).digest)
    runner = Runner(state)
    first = asyncio.run(runner.execute(run_id))
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["es-MX"]["copy"]["message"] = "Para cada movimiento."
    edited = make_plan(Brief.model_validate(value), storage)
    affected = len(compare_plans(state.plan(run_id), edited).render_outputs)
    runner.revise(run_id, edited.brief, first["revision_id"], edited.digest)
    result = Runner(State(storage)).recover_local(run_id)
    index = export_package(
        state, result["revision_id"], storage.project(brief.campaign_id).ref(f"Exports/{run_id}")
    )
    return {
        "evidence": result["evidence"],
        "run_id": run_id,
        "revision_id": result["revision_id"],
        "complete": result["complete"],
        "expected_images": len(result["outputs"]),
        "completed_images": result["metrics"]["completed_images"],
        "corrected_images": affected,
        "historical_requests": result["replay"]["historical_requests"],
        "provider_requests": len(state.attempts(run_id)),
        "offline_index": index,
    }
