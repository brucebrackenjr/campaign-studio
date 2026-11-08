"""Apply saved Brand choices to existing images without admitting another draft."""

import copy

from pydantic import Field

from campaign_studio.brief import Brand, Brief, Contract, Market, Ratio
from campaign_studio.intake import Intake
from campaign_studio.planning import compare_plans, make_local_plan
from campaign_studio.rendering import blocks_output
from campaign_studio.workspace import ActionError


class DefaultColorTarget(Contract):
    product_id: str
    market: Market
    ratio: Ratio

    @property
    def output_id(self):
        return f"{self.product_id}/{self.market}/{self.ratio.replace(':', 'x')}"


class BrandUpdate(Contract):
    version: str = Field(min_length=1, max_length=64)
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    default_color_targets: list[DefaultColorTarget] = Field(default_factory=list, max_length=30)


class BrandUpdates:
    def __init__(self, workspace):
        self.workspace = workspace
        self.state = workspace.state
        self.storage = workspace.storage

    def require_base(self, campaign_id, edit):
        self.workspace.run_for(campaign_id)
        if self.state.run(edit.run_id)["campaign_id"] != campaign_id:
            raise ActionError("stale", "Choose this campaign’s current saved images.")
        if self.state.current_output(campaign_id) != (edit.run_id, edit.revision_id):
            raise ActionError("stale", "The campaign images changed. Reload before updating Brand.")
        self.state.require_current_revision(edit.run_id, edit.revision_id)
        document = Intake(self.storage).read(campaign_id)
        if not document or document["version"] != edit.version:
            raise ActionError(
                "authored_conflict", "The saved campaign changed. Save and review Brand again."
            )
        return document

    def prepare(self, campaign_id, edit):
        # This guard is short. Save remains available while the local renderer
        # works; publication checks the same token again under its own guard.
        with self.storage.exclusive_draft():
            previous = self.require_base(campaign_id, edit)
            document = Intake(self.storage).current_document(campaign_id, self.workspace)

            def pending_targets(value):
                pending = (
                    value.get("authored", {}).get("form_drafts", {}).get("brand_color_defaults", [])
                )
                return (
                    {
                        (item.get("product_id"), item.get("market"), item.get("ratio"))
                        for item in pending
                        if isinstance(item, dict)
                        and all(
                            isinstance(item.get(key), str)
                            for key in ("product_id", "market", "ratio")
                        )
                    }
                    if isinstance(pending, list)
                    else set()
                )

            superseded = pending_targets(previous) - pending_targets(document)
            if any(
                (t.product_id, t.market, t.ratio) in superseded for t in edit.default_color_targets
            ):
                raise ActionError(
                    "authored_conflict",
                    "Image colors changed. Reload Brand before updating images.",
                )
        old = self.state.plan(edit.run_id)
        saved = document.get("authored", {}).get("brief", document["value"])
        raw_brand = saved.get("brand")
        if not isinstance(raw_brand, dict):
            raise ActionError("brand", "Complete the Brand fields before updating images.", 422)
        # Brand identity and every non-Brand field come from the accepted images.
        # An incomplete product, wording, or audience draft is not an input here.
        brand = Brand.model_validate({**raw_brand, "id": old.brief.brand.id})
        if brand.logo_path:
            try:
                self.storage.project(campaign_id).require(brand.logo_path)
            except ValueError as error:
                raise ActionError(
                    "logo", "Choose a logo attached to this campaign.", 422
                ) from error
        manifest = self.workspace.manifest(self.state.run(edit.run_id), edit.revision_id)
        available = {item["id"] for item in manifest["outputs"] if item["file"]}
        if not available:
            raise ActionError("no_images", "Create campaign images before updating their Brand.")
        value = old.brief.model_dump(mode="json")
        value["brand"] = brand.model_dump(mode="json")
        for target in edit.default_color_targets:
            if target.output_id not in available:
                raise ActionError(
                    "creative", "Choose an existing saved image for its default color.", 422
                )
            # Use default saves null colors alongside its target. An explicit
            # saved color contradicts that reset, including stale form metadata
            # saved again after a later Studio Apply. Never erase that choice.
            products = saved.get("products", [])
            raw = next(
                (
                    product
                    for product in (products if isinstance(products, list) else [])
                    if isinstance(product, dict) and product.get("id") == target.product_id
                ),
                {},
            )
            for key in ("framing", target.market, target.ratio, "text_placements"):
                raw = raw.get(key, {}) if isinstance(raw, dict) else {}
            if any(
                isinstance(placement, dict) and placement.get("color") is not None
                for placement in (raw.values() if isinstance(raw, dict) else [])
            ):
                raise ActionError(
                    "authored_conflict",
                    "Image colors changed. Reload Brand before updating images.",
                )
            product = next(p for p in value["products"] if p["id"] == target.product_id)
            framing = product.get("framing", {}).get(target.market, {}).get(target.ratio, {})
            for placement in framing.get("text_placements", {}).values():
                placement["color"] = None
        brief, fitted = Intake(self.storage).fit_replacement_logo(
            Brief.model_validate(value), old.brief.brand.logo_path
        )
        plan = make_local_plan(old, brief, self.storage)
        impact = compare_plans(old, plan)
        impact.render_outputs = [o for o in impact.render_outputs if o in available]
        impact.recheck_outputs = [o for o in impact.recheck_outputs if o in available]
        self.workspace.require_saved_outputs(edit.revision_id, manifest, impact.render_outputs)
        self.state.require_edit_inputs(edit.run_id, plan, impact.render_outputs)
        if any(
            blocks_output(f, o) for f in plan.findings for o in plan.outputs if o.id in available
        ):
            raise ActionError(
                "brand", "Resolve the Brand or image problems before updating images.", 422
            )
        return brief, plan, {**impact.model_dump(), "fitted_outputs": fitted}


def reconcile_brand_update(document, brand, receipt):
    """Recover only the acknowledged Brand operation after publication or a crash."""
    action = receipt.get("brand_update")
    if not action or action.get("version") != document["version"]:
        return
    targets = {
        (target["product_id"], target["market"], target["ratio"])
        for target in action["default_color_targets"]
    }
    for brief in (document["value"], document.get("authored", {}).get("brief", {})):
        if isinstance(brief.get("brand"), dict):
            # Saved raw Brand values already express the intent. Only the owned
            # logo snapshot changes its attachment reference during publication.
            brief["brand"]["logo_path"] = brand.logo_path
        for product in brief.get("products", []):
            if not isinstance(product, dict):
                continue
            # Replacement fitting owns only the acknowledged logo rectangle.
            # Do not copy a whole framing object over other unfinished edits.
            for fitted in receipt.get("brand_fitted_outputs", []):
                if product.get("id") != fitted["product_id"]:
                    continue
                framing = product.get("framing", {})
                for key in (fitted["market"], fitted["ratio"]):
                    framing = framing.get(key, {}) if isinstance(framing, dict) else {}
                if (
                    isinstance(framing, dict)
                    and framing.get("logo_placement") == fitted["requested"]
                ):
                    framing["logo_placement"] = copy.deepcopy(fitted["placement"])
            for pid, market, ratio in targets:
                if product.get("id") != pid:
                    continue
                framing = product.get("framing", {})
                for key in (market, ratio, "text_placements"):
                    framing = framing.get(key, {}) if isinstance(framing, dict) else {}
                for placement in framing.values() if isinstance(framing, dict) else []:
                    if isinstance(placement, dict):
                        placement["color"] = None
    forms = document.get("authored", {}).get("form_drafts", {})
    pending = forms.get("brand_color_defaults")
    if isinstance(pending, list):
        forms["brand_color_defaults"] = [
            copy.deepcopy(item)
            for item in pending
            if not isinstance(item, dict)
            or (item.get("product_id"), item.get("market"), item.get("ratio")) not in targets
        ]
