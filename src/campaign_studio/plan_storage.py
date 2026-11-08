"""Store editing instructions once, and expand their runtime output view without I/O."""

import json
from typing import Literal

from pydantic import Field

from campaign_studio.brief import Brief, CampaignID, Contract, Identifier
from campaign_studio.generation import GenerationRequest
from campaign_studio.planning import Plan, expand_outputs

STORAGE_FORMAT = "brief-only-v1"
COMPLETED_FORMAT = "completed-run-v1"


class GenerationInputs(Contract):
    audience: str
    creative_direction: str
    role: Literal["concept", "prepared_product"]
    description: str


class GenerationReceipt(Contract):
    request: GenerationRequest
    inputs: GenerationInputs


class CompletedRun(Contract):
    storage_format: Literal["completed-run-v1"] = COMPLETED_FORMAT
    campaign_id: CampaignID
    generation: dict[Identifier, GenerationReceipt] = Field(default_factory=dict)


def generation_inputs(brief, product):
    """Only producer inputs used by request_for, without any composition settings."""
    return {
        "audience": brief.audience,
        "creative_direction": brief.creative_direction,
        "role": product.source.role,
        "description": product.source.description if product.source.role == "concept" else "",
    }


def _value(payload):
    value = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    if not isinstance(value, dict):
        raise ValueError("A saved plan must be a JSON object.")
    return dict(value)


def completed_run(payload):
    """Return a validated completion receipt, never an executable approximation."""
    value = _value(payload)
    return (
        CompletedRun.model_validate(value)
        if value.get("storage_format") == COMPLETED_FORMAT
        else None
    )


def source_generation(payload):
    """Read paid-request evidence from either an unfinished plan or a completed run."""
    receipt = completed_run(payload)
    if receipt is not None:
        return {
            product: {"request": item.request, "inputs": item.inputs.model_dump()}
            for product, item in receipt.generation.items()
        }
    plan = load_plan(payload)
    products = {product.id: product for product in plan.brief.products}
    return {
        product: {"request": request, "inputs": generation_inputs(plan.brief, products[product])}
        for product, request in plan.generation_requests.items()
    }


def dump_completed(plan, attempted_products):
    """Retain actual-request evidence after its executable production input is obsolete."""
    sources = source_generation(dump_plan(plan))
    missing = set(attempted_products) - sources.keys()
    if missing:
        raise ValueError("A recorded image request has no saved generation settings.")
    receipt = CompletedRun(
        campaign_id=plan.brief.campaign_id,
        generation={product: sources[product] for product in sorted(set(attempted_products))},
    )
    return receipt.model_dump_json()


def _require_matching_outputs(plan: Plan) -> None:
    if plan.outputs != expand_outputs(plan.brief):
        raise ValueError("The saved plan's outputs do not match its brief.")


def dump_plan(plan: Plan) -> str:
    """Serialize the brief and retained execution metadata, without repeated output settings."""
    _require_matching_outputs(plan)
    value = plan.model_dump(mode="json", exclude={"outputs"})
    value["storage_format"] = STORAGE_FORMAT
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)


def load_plan(payload: str | bytes | bytearray | dict) -> Plan:
    """Read current or legacy plans without recomputing requests, findings, or their digest."""
    value = _value(payload)
    if value.get("storage_format") == COMPLETED_FORMAT:
        raise ValueError("This production is complete. Open its current accepted image settings.")
    if "storage_format" not in value:
        plan = Plan.model_validate(value)
        _require_matching_outputs(plan)
        return plan
    if value.pop("storage_format") != STORAGE_FORMAT:
        raise ValueError("The saved plan uses an unsupported storage format.")
    if "outputs" in value:
        raise ValueError("A brief-only saved plan cannot also contain output settings.")
    brief = Brief.model_validate(value.get("brief"))
    value["brief"] = brief
    value["outputs"] = expand_outputs(brief)
    return Plan.model_validate(value)
