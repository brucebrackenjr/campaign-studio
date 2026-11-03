"""Small application operations shared by the four task pages."""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError

from campaign_studio.brief import (
    Brief,
    Contract,
    Framing,
    LogoPlacement,
    Market,
    Provenance,
    Ratio,
    TextPlacement,
)
from campaign_studio.planning import Plan, compare_plans, make_local_plan
from campaign_studio.rendering import blocks_output
from campaign_studio.state import State, StateError
from campaign_studio.validation import issue_messages, validation_issues


class ActionError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        *,
        field_errors: list[dict] | None = None,
        proposal: dict | None = None,
        input_image: dict | None = None,
        revision_number: int | None = None,
        project_state: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field_errors = field_errors or []
        self.proposal = proposal
        self.input_image = input_image
        self.revision_number = revision_number
        self.project_state = project_state


@dataclass(frozen=True)
class Notice:
    code: str
    message: str


MISSING_KEY = Notice(
    "missing_key",
    "Add an OpenAI API key in Production to generate new images. If OPENAI_API_KEY is "
    "managed by your launch environment, update it there and restart the app. "
    "Saved campaigns and local reuse are still available.",
)


def provider_notice(attempts: list[dict]) -> Notice | None:
    # A later receipt resolves that product's generation failure. Keep old
    # attempts for allowance/charge history, but never present them as new errors.
    latest = {}
    for attempt in reversed(attempts):
        latest.setdefault(attempt["product_id"], attempt)
    failed = next(
        (item for item in latest.values() if item["error_kind"] and item["state"] != "decoded"),
        None,
    )
    if failed is None:
        return None
    kind = failed["error_kind"]
    if kind == "http_401":
        return Notice(
            "rejected_key",
            "OpenAI rejected the API key. It may have expired or been revoked. Update your local "
            "key to generate new images. Saved campaigns are still available.",
        )
    if kind == "http_403":
        return Notice(
            "access_denied",
            "This key or account cannot access the image operation. Check its image permissions "
            "and model access. Saved campaigns are still available.",
        )
    billing_remedies = {
        "project_spend_limit_exceeded": (
            "The OpenAI project spending limit was reached. "
            "Review that project's spending limit in OpenAI."
        ),
        "organization_spend_limit_exceeded": (
            "The OpenAI organization spending limit was reached. "
            "Review the organization's spending limit in OpenAI."
        ),
        "organization_usage_limit_exceeded": (
            "The OpenAI organization usage limit was reached. "
            "Review its approved usage limit in OpenAI."
        ),
        "credit_balance_exhausted": (
            "The OpenAI credit balance is exhausted. "
            "Review the account's billing and credits in OpenAI."
        ),
        "insufficient_quota": (
            "OpenAI declined this request because its billing quota is unavailable. "
            "Review the account's billing, credits, and usage limits in OpenAI."
        ),
    }
    if kind in billing_remedies:
        return Notice(
            "billing_limit",
            billing_remedies[kind]
            + " Approving another app request allowance does not resolve this block. "
            "Saved work remains available; nothing is retried automatically.",
        )
    if kind in {"rate_limit_error", "rate_limit_exceeded"}:
        return Notice(
            "rate_limited",
            "OpenAI temporarily limited the request rate. Wait before deliberately reviewing "
            "another attempt. Saved work remains available; nothing is retried automatically.",
        )
    if kind == "http_429":
        return Notice(
            "provider_failure",
            "OpenAI declined the request, but did not provide a recognized limit reason. "
            "Check its billing, usage, and rate limits before reviewing another attempt. "
            "Saved work remains available; nothing is retried automatically.",
        )
    if kind == "saved_response_decode":
        return Notice(
            "local_processing",
            "The response is saved, but it does not contain a usable image. Review another "
            "generation attempt or attach a replacement. Nothing is retried automatically.",
        )
    if kind == "saved_response_storage":
        return Notice(
            "local_processing",
            "The response is saved, but local storage prevented restoring its image. Check "
            "available space and write access, then try local recovery. "
            "No new image request will be sent.",
        )
    if failed["state"] in {"unknown", "dispatching"}:
        return Notice(
            "unknown_request",
            "The request result was not confirmed locally. A charge may still apply. "
            "Saved work is preserved; nothing is resent automatically.",
        )
    return Notice(
        "provider_failure",
        "The image service did not complete the request. Saved work is preserved, and no "
        "automatic retry was made. Review the request allowance before trying again.",
    )


def validation_messages(error: ValidationError) -> list[str]:
    return issue_messages(validation_issues(error))


class RunAction(Contract):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    allowance: int = Field(ge=0, strict=True)
    retry: bool = False


class FrameEdit(Contract):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    product_id: str
    market: Market
    ratio: Ratio
    framing: Framing
    preview_digest: str | None = None

    @property
    def preview_products(self):
        return [self.product_id]


class TextEdit(Contract):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    product_id: str
    market: Market
    ratio: Ratio
    # Omission preserves a block; null explicitly restores its inherited layout.
    # Stored Framing values remain concrete placements, never reset commands.
    placements: dict[Literal["message", "cta"], TextPlacement | None] = Field(default_factory=dict)
    values: dict[Literal["message", "cta"], str | None] = Field(default_factory=dict)
    field_scopes: dict[Literal["message", "cta"], Literal["product", "all_products"]] = Field(
        default_factory=dict
    )
    preview_digest: str | None = None

    @property
    def preview_products(self):
        return [self.product_id]

    @property
    def output_id(self):
        return f"{self.product_id}/{self.market}/{self.ratio.replace(':', 'x')}"


class LogoEdit(Contract):
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    product_id: str
    market: Market
    ratio: Ratio
    placement: LogoPlacement | None = None
    pre_fit_placement: LogoPlacement | None = None
    replacement: str | None = None
    provenance: Provenance | None = None
    draft_version: str | None = None
    preview_digest: str | None = None

    @property
    def preview_products(self):
        return [self.product_id]

    @property
    def output_id(self):
        return f"{self.product_id}/{self.market}/{self.ratio.replace(':', 'x')}"


def apply_wording(value: dict, previous: Brief, edit: TextEdit) -> None:
    """Change only explicitly scoped wording fields in a candidate brief."""
    if set(edit.values) != set(edit.field_scopes):
        raise ActionError("copy_fields", "Choose a scope for each intended wording field.", 422)
    selected = next((p for p in value["products"] if p["id"] == edit.product_id), None)
    if selected is None:
        raise ActionError("product", "Choose a product in this campaign.", 422)
    # Checkbox selection is explicit intent even without typing. Only the
    # named fields participate; other product overrides remain untouched.
    for field, text in edit.values.items():
        targets = (
            previous.products
            if edit.field_scopes[field] == "all_products"
            else [p for p in previous.products if p.id == edit.product_id]
        )
        if all(
            getattr(previous.effective_copy(product, edit.market), field) == text
            for product in targets
        ):
            # Equal-value overrides remain authored data. Do not publish an
            # unchanged correction just to normalize them away.
            continue
        if edit.field_scopes[field] == "all_products":
            value["markets"][edit.market]["copy"][field] = text
            for product in value["products"]:
                override = product["copy_overrides"].get(edit.market)
                if override is not None:
                    override.pop(field, None)
                    if not override:
                        product["copy_overrides"].pop(edit.market)
        else:
            selected["copy_overrides"].setdefault(edit.market, {})[field] = text


class Workspace:
    def __init__(self, state: State):
        self.state = state
        self.storage = state.storage
        self.storage.path("incoming").mkdir(exist_ok=True, mode=0o700)

    def runs(self, campaign_id: str | None = None) -> list[dict]:
        import sqlite3

        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            if campaign_id:
                rows = db.execute(
                    "SELECT * FROM runs WHERE campaign_id=? AND NOT EXISTS (SELECT 1 FROM "
                    "cleanup_intents c WHERE c.campaign_id=runs.campaign_id AND "
                    "c.kind='campaign') ORDER BY rowid DESC",
                    (campaign_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM runs WHERE NOT EXISTS (SELECT 1 FROM cleanup_intents c "
                    "WHERE c.campaign_id=runs.campaign_id AND c.kind='campaign') ORDER BY "
                    "rowid DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def revisions(self, run_id: str) -> list[dict]:
        import sqlite3

        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM revisions WHERE run_id=? AND images_retired=0 "
                "AND (image_count>0 OR id=(SELECT current_revision FROM runs WHERE id=?)) "
                "ORDER BY rowid DESC",
                (run_id, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_for(self, campaign_id: str, run_id: str | None = None) -> dict | None:
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id)
        runs = self.runs(campaign_id)
        if run_id and (not runs or runs[0]["id"] != run_id):
            raise ActionError("stale", "The campaign changed. Review the updated plan.")
        return runs[0] if runs else None

    def edit_run(self, campaign_id: str, run_id: str, revision_id: str, product_id: str) -> dict:
        """Edit the current accepted images, independently of an unstarted plan."""
        self.run_for(campaign_id)  # Identity and pending-deletion admission.
        run = self.state.run(run_id)
        if run["campaign_id"] != campaign_id:
            raise ActionError("stale", "Reopen this campaign’s current saved images.")
        self.state.require_current_revision(run_id, revision_id)
        from campaign_studio.authored import AuthoredProjects

        products = AuthoredProjects(self).read(campaign_id)["authored"]["brief"].get("products", [])
        if not any(
            isinstance(product, dict) and product.get("id") == product_id for product in products
        ):
            raise ActionError(
                "product_removed",
                "This product is no longer in the campaign. Choose one of its current products.",
            )
        return run

    def catalog(self) -> list[dict]:
        with self.storage.connect() as db:
            identities = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM campaigns WHERE NOT EXISTS (SELECT 1 FROM cleanup_intents c "
                    "WHERE c.campaign_id=campaigns.id AND c.kind='campaign') ORDER BY rowid DESC"
                )
            ]
        latest = {}
        for run in self.runs():
            latest.setdefault(run["campaign_id"], run)
        entries = []
        for identity in identities:
            run = latest.get(identity)
            entry = {
                "id": identity,
                "name": "Campaign unavailable",
                "status": "damaged",
                "page": "campaign",
            }
            try:
                path = self.storage.path(self.storage.project(identity).draft)
                if path.is_file():
                    document = json.loads(path.read_bytes())
                    value = document.get("authored", {}).get("brief", document["value"])
                    entry.update(
                        name=value.get("name") or "Untitled campaign",
                        status=run["state"] if run else "draft",
                    )
                elif run:
                    entry.update(name=self.state.plan(run["id"]).brief.name, status=run["state"])
            except ValueError, OSError, KeyError, TypeError:
                # Keep registered damage visible for deliberate recovery or deletion.
                pass
            entries.append(entry)
        for entry in entries:
            entry["detail"] = (
                entry["id"] if sum(e["name"] == entry["name"] for e in entries) > 1 else ""
            )
        return entries

    def plan(self, campaign_id: str) -> Plan:
        run = self.run_for(campaign_id)
        if run is None:
            raise ActionError("no_plan", "This campaign has no saved production plan.", 404)
        return self.state.plan(run["id"])

    def retained_product_source(self, campaign_id: str, product_id: str) -> dict | None:
        """The saved attachment choice stays separate from active concept input."""
        from campaign_studio.brief import FinishedScene, PreparedProduct
        from campaign_studio.intake import Intake

        self.run_for(campaign_id)
        document = Intake(self.storage).read(campaign_id) or {}
        authored = document.get("authored", {})
        if not any(
            p.get("id") == product_id
            for p in authored.get("brief", document.get("value", {})).get("products", [])
            if isinstance(p, dict)
        ):
            return None
        source = authored.get("form_drafts", {}).get("product_images", {}).get(product_id)
        if isinstance(source, dict):
            try:
                schema = (
                    PreparedProduct if source.get("role") == "prepared_product" else FinishedScene
                )
                source = schema.model_validate(source).model_dump(mode="json")
                if self.storage.path(source["path"]).is_file():
                    return source
            except ValueError, OSError:
                pass
        output = self.state.current_output(campaign_id)
        runs = [output[0]] if output else []
        latest = self.run_for(campaign_id)
        if latest and latest["id"] not in runs:
            runs.append(latest["id"])
        for run_id in runs:
            plan = self.state.plan(run_id)
            product = next((p for p in plan.brief.products if p.id == product_id), None)
            asset = plan.assets.get(f"products/{product_id}")
            if product and product.source.role != "concept" and asset:
                try:
                    if (
                        hashlib.sha256(self.storage.path(asset.reference).read_bytes()).hexdigest()
                        == asset.sha256
                    ):
                        return product.source.model_dump(mode="json")
                except OSError:
                    pass
        return None

    def source_preview_reference(self, campaign_id: str, product_id: str) -> str | None:
        """Resolve current supplied pixels or an intact master for the same imagery intent."""
        from campaign_studio.authored import AuthoredProjects
        from campaign_studio.brief_preview import same_imagery_intent

        run = self.run_for(campaign_id)
        plan = self.plan(campaign_id)
        product = next((p for p in plan.brief.products if p.id == product_id), None)
        if product is None:
            return None
        asset = plan.assets.get(f"products/{product_id}")
        if product.source.role == "concept":
            raw = AuthoredProjects(self).read(campaign_id)["authored"]["brief"]
            if not same_imagery_intent(plan, raw, product_id):
                return None
            asset = plan.assets.get(f"masters/{product_id}")
        if asset:
            try:
                if (
                    hashlib.sha256(self.storage.path(asset.reference).read_bytes()).hexdigest()
                    == asset.sha256
                ):
                    return asset.reference
            except OSError:
                pass
            return None
        if run and product.source.role == "concept":
            return next(
                (
                    a["source_ref"]
                    for a in reversed(self.state.attempts(run["id"]))
                    if a["product_id"] == product_id
                    and a["state"] == "decoded"
                    and self.state.source_is_intact(a)
                ),
                None,
            )
        return None

    def manifest(self, run: dict, revision_id: str | None = None) -> dict | None:
        selected = revision_id or self.state.effective_revision(run["id"])
        if selected is None:
            return None
        self.state.require_images(selected)
        revision = self.state.revision(selected)
        if revision["run_id"] != run["id"]:
            raise ActionError("unknown_revision", "This saved revision is unavailable.", 404)
        return json.loads(self.storage.path(revision["manifest_ref"]).read_bytes())

    def logo(self, campaign_id: str, edit: LogoEdit, *, allow_unchanged: bool = False):
        from PIL import Image

        from campaign_studio.brief import FORMATS
        from campaign_studio.imaging import (
            fit_logo_placement,
            load_image,
            logo_placement,
            placed_logo_bounds,
        )
        from campaign_studio.intake import Intake

        try:
            run = self.edit_run(campaign_id, edit.run_id, edit.revision_id, edit.product_id)
        except StateError as error:
            raise ActionError("logo_stale_revision", str(error)) from error
        old = self.state.plan(run["id"])
        manifest = self.manifest(run, edit.revision_id)
        available = {o["id"] for o in manifest["outputs"] if o["file"]}
        if edit.output_id not in available:
            raise ActionError("creative", "Choose an available saved image to edit its logo.", 422)
        revision = self.state.revision(edit.revision_id)
        prefix = revision["manifest_ref"].rsplit("/", 1)[0]
        damaged = []
        for output in manifest["outputs"]:
            if not output["file"]:
                continue
            try:
                intact = (
                    hashlib.sha256(
                        self.storage.path(f"{prefix}/{output['file']}").read_bytes()
                    ).hexdigest()
                    == output["sha256"]
                )
            except OSError:
                intact = False
            if not intact:
                product = next(p for p in old.brief.products if p.id == output["product_id"])
                damaged.append(f"{product.name} · {output['market']} · {output['ratio']}")
        # Missing recorded outputs are supported partial work. Damaged formerly
        # saved PNGs need explicit recovery; a logo edit must not repair them or
        # publish an unintended partial behind a protected complete pointer.
        if damaged and (edit.replacement or "placement" in edit.model_fields_set):
            raise ActionError(
                "saved_images_unavailable",
                "Saved images are missing or changed: "
                + "; ".join(damaged)
                + ". Use local recovery on Production before applying logo edits.",
            )
        if edit.pre_fit_placement is not None and (not edit.replacement or edit.placement is None):
            raise ActionError("logo_fit", "Review the replacement logo preview again.", 422)
        value = old.brief.model_dump(mode="json")
        fitted = []
        logo_size = None
        same_artwork = False
        same_placement = False
        if edit.replacement:
            document = Intake(self.storage).current_document(campaign_id, self)
            if document and document["version"] != edit.draft_version:
                raise ActionError("stale_brief", "The brief changed. Reopen the logo editor.")
            try:
                self.storage.project(campaign_id).upload_name(edit.replacement)
            except ValueError as error:
                raise ActionError(
                    "logo", "Choose a PNG or JPEG logo using this campaign's file picker.", 422
                ) from error
            value["brand"]["logo_path"] = edit.replacement
            value["brand"]["logo_provenance"] = (
                edit.provenance.model_dump()
                if edit.provenance
                else {
                    "label": "User-supplied logo",
                    "rights": "Not provided",
                    "synthetic": False,
                }
            )
            logo, _ = load_image(self.storage.path(edit.replacement))
            logo_size = logo.size
            old_asset = old.assets.get("logo")
            if old_asset:
                try:
                    same_artwork = (
                        hashlib.sha256(
                            self.storage.path(old_asset.reference).read_bytes()
                        ).hexdigest()
                        == old_asset.sha256
                        == hashlib.sha256(
                            self.storage.path(edit.replacement).read_bytes()
                        ).hexdigest()
                    )
                except OSError:
                    same_artwork = False
            if same_artwork:
                value["brand"] = old.brief.brand.model_dump(mode="json")
            # Saved dimensions remain authoritative when the former file is missing.
            previous_logo = (
                Image.new("RGBA", (old_asset.width, old_asset.height)) if old_asset else logo
            )
            try:
                if same_artwork and edit.placement is not None:
                    selected = next(o for o in old.outputs if o.id == edit.output_id)
                    size = FORMATS[selected.ratio]
                    if selected.framing.logo_placement is not None:
                        before_bounds = placed_logo_bounds(
                            logo_size, size, selected.framing.logo_placement
                        )
                    else:
                        rendered, position = logo_placement(previous_logo, size)
                        before_bounds = (
                            *position,
                            position[0] + rendered.width,
                            position[1] + rendered.height,
                        )
                        rendered.close()
                    try:
                        same_placement = (
                            placed_logo_bounds(logo_size, size, edit.placement) == before_bounds
                        )
                    except ValueError:
                        same_placement = (
                            False  # The ordinary fit/validation path handles invalid bounds.
                        )
                # Preserve each available image’s current position and size when
                # new artwork replaces its logo; omitted outputs remain untouched.
                for output in old.outputs:
                    if (
                        same_artwork
                        or output.id not in available
                        or output.framing.logo_placement is not None
                    ):
                        continue
                    size = FORMATS[output.ratio]
                    rendered, position = logo_placement(previous_logo, size)
                    placement = dict(
                        left=position[0] / size[0],
                        top=position[1] / size[1],
                        width=rendered.width / size[0],
                    )
                    rendered.close()
                    product = next(p for p in value["products"] if p["id"] == output.product_id)
                    framing = (
                        product.setdefault("framing", {})
                        .setdefault(output.market, {})
                        .setdefault(output.ratio, {})
                    )
                    framing["logo_placement"] = placement
            finally:
                if previous_logo is not logo:
                    previous_logo.close()
                logo.close()
        if (edit.placement is not None and not same_placement) or (
            not edit.replacement and "placement" in edit.model_fields_set
        ):
            product = next(p for p in value["products"] if p["id"] == edit.product_id)
            framing = (
                product.setdefault("framing", {})
                .setdefault(edit.market, {})
                .setdefault(edit.ratio, {})
            )
            framing["logo_placement"] = edit.placement.model_dump() if edit.placement else None
        if edit.pre_fit_placement is not None:
            # The editor fits the selected image before preview. Verify that fact
            # against the owned artwork, rather than infer it from a small logo.
            size = FORMATS[edit.ratio]
            requested = edit.pre_fit_placement
            resolved = fit_logo_placement(logo_size, size, requested)
            try:
                bounds = placed_logo_bounds(logo_size, size, resolved)
                valid_fit = resolved.width < requested.width and bounds == placed_logo_bounds(
                    logo_size, size, edit.placement
                )
            except ValueError:
                valid_fit = False
            if not valid_fit:
                raise ActionError("logo_fit", "Review the replacement logo preview again.", 422)
            fitted.append(
                {
                    "id": edit.output_id,
                    "requested": requested.model_dump(),
                    "placement": edit.placement.model_dump(),
                    "bounds": list(bounds),
                }
            )
        if edit.replacement:
            for output in old.outputs:
                if output.id not in available:
                    continue
                product = next(p for p in value["products"] if p["id"] == output.product_id)
                framing = product.get("framing", {}).get(output.market, {}).get(output.ratio, {})
                if framing.get("logo_placement") is None:
                    continue  # Identical artwork keeps the existing composition.
                requested = LogoPlacement.model_validate(framing["logo_placement"])
                size = FORMATS[output.ratio]
                try:
                    placed_logo_bounds(logo_size, size, requested)
                except ValueError:
                    resolved = fit_logo_placement(logo_size, size, requested)
                    framing["logo_placement"] = resolved.model_dump()
                    fitted = [item for item in fitted if item["id"] != output.id]
                    fitted.append(
                        {
                            "id": output.id,
                            "requested": requested.model_dump(),
                            "placement": resolved.model_dump(),
                            "bounds": list(placed_logo_bounds(logo_size, size, resolved)),
                        }
                    )
        brief = Brief.model_validate(value)
        plan = make_local_plan(old, brief, self.storage)
        impact = compare_plans(old, plan)
        # The replacement promise covers the available set, never an implicit repair.
        impact.render_outputs = [o for o in impact.render_outputs if o in available]
        impact.recheck_outputs = [o for o in impact.recheck_outputs if o in available]
        required = available if edit.replacement else {edit.output_id}
        logo_available = True
        if allow_unchanged and not edit.replacement:
            asset = plan.assets.get("logo")
            try:
                logo_available = bool(
                    asset
                    and hashlib.sha256(self.storage.path(asset.reference).read_bytes()).hexdigest()
                    == asset.sha256
                )
            except OSError:
                logo_available = False
        self.state.require_edit_inputs(run["id"], plan, required, include_logo=logo_available)
        if not allow_unchanged and not impact.render_outputs:
            raise ActionError("no_changes", "No logo changes to save.")
        return (
            brief,
            plan,
            {
                **impact.model_dump(),
                "fitted_outputs": fitted,
                "fitted_count": len(fitted),
                "logo_available": logo_available,
                "available_outputs": len(available),
                "expected_outputs": len(plan.outputs),
            },
        )

    def text(self, campaign_id: str, edit: TextEdit, *, allow_unchanged: bool = False):
        """Preview wording and selected-format typography as one local correction."""
        run = self.edit_run(campaign_id, edit.run_id, edit.revision_id, edit.product_id)
        old = self.state.plan(run["id"])
        output = next((o for o in old.outputs if o.id == edit.output_id), None)
        manifest = self.manifest(run, edit.revision_id)
        available = any(o["id"] == edit.output_id and o["file"] for o in manifest["outputs"])
        withheld = output is not None and any(
            finding.context and finding.context.phrase and blocks_output(finding, output)
            for finding in old.findings
        )
        if output is None or not (available or withheld):
            raise ActionError(
                "creative", "Choose an available saved image to arrange its text.", 422
            )
        value = old.brief.model_dump(mode="json")
        apply_wording(value, old.brief, edit)
        candidate = Brief.model_validate(value)
        candidate_product = next(p for p in candidate.products if p.id == edit.product_id)
        wording = candidate.effective_copy(candidate_product, edit.market)
        cleared = {field for field, text in edit.values.items() if text is None}
        selected = next(p for p in value["products"] if p["id"] == edit.product_id)
        framing = output.framing.model_dump(mode="json")
        for field, placement in edit.placements.items():
            # Clearing optional wording also makes its in-session gesture obsolete.
            # Keep the accepted layout for a later reintroduction of that wording.
            if field in cleared:
                continue
            if placement is not None and getattr(wording, field) is None:
                raise ActionError(
                    "text_block", "This image has no supplied text for that block.", 422
                )
            if placement is None:
                framing["text_placements"].pop(field, None)
            else:
                framing["text_placements"][field] = placement.model_dump()
        if framing != output.framing.model_dump(mode="json"):
            selected["framing"].setdefault(edit.market, {})[edit.ratio] = framing
        brief = Brief.model_validate(value)
        if brief == old.brief and not allow_unchanged:
            raise ActionError("no_changes", "No changes to apply. Your saved image is unchanged.")
        plan = make_local_plan(old, brief, self.storage)
        impact = compare_plans(old, plan)
        self.state.require_edit_inputs(run["id"], plan, impact.render_outputs or [edit.output_id])
        # A local text edit cannot silently recover a damaged saved set. Recovery
        # remains explicit. A wording correction may replace an intentional rule
        # omission, but it cannot repair another output's missing saved file.
        prefix = self.state.revision(edit.revision_id)["manifest_ref"].rsplit("/", 1)[0]
        for saved in manifest["outputs"]:
            if not saved["file"]:
                continue
            try:
                intact = (
                    hashlib.sha256(
                        self.storage.path(f"{prefix}/{saved['file']}").read_bytes()
                    ).hexdigest()
                    == saved["sha256"]
                )
            except OSError:
                intact = False
            if not intact:
                raise ActionError(
                    "saved_images_unavailable",
                    "A saved image needs local recovery before applying text changes.",
                )
        return brief, plan, impact.model_dump()

    def frame(self, campaign_id: str, edit: FrameEdit, *, allow_unchanged: bool = False):
        from campaign_studio.imaging import crop_box, load_image, protects_subject
        from campaign_studio.planning import Finding, contextualize_findings

        run = self.edit_run(campaign_id, edit.run_id, edit.revision_id, edit.product_id)
        old = self.state.plan(run["id"])
        product = next((p for p in old.brief.products if p.id == edit.product_id), None)
        if product is None or edit.market not in old.brief.markets:
            raise ActionError("creative", "Choose an image from this campaign.", 422)
        if product.source.role == "prepared_product":
            raise ActionError(
                "framing", "Prepared-product placement is not adjustable in this version.", 422
            )
        previous = next(
            o
            for o in old.outputs
            if o.product_id == edit.product_id and o.market == edit.market and o.ratio == edit.ratio
        )
        # A crop-only command must not erase independently applied artwork/text.
        edit.framing = edit.framing.model_copy(
            update={
                field: getattr(previous.framing, field)
                for field in ("logo_placement", "text_placements")
                if field not in edit.framing.model_fields_set
            }
        )
        value = old.brief.model_dump(mode="json")
        selected = next(p for p in value["products"] if p["id"] == edit.product_id)
        selected.setdefault("framing", {}).setdefault(edit.market, {})[edit.ratio] = (
            edit.framing.model_dump(mode="json")
        )
        brief = Brief.model_validate(value)
        plan = make_local_plan(old, brief, self.storage)
        impact = compare_plans(old, plan)
        self.state.require_edit_inputs(
            run["id"], plan, [f"{edit.product_id}/{edit.market}/{edit.ratio.replace(':', 'x')}"]
        )
        if not allow_unchanged and not impact.render_outputs and not impact.recheck_outputs:
            raise ActionError("no_changes", "No framing changes to save.")
        # Generated masters become known after the request. Validate these actual
        # retained pixels as well as the dimensions available during original preflight.
        reference = (
            old.assets.get(f"products/{product.id}")
            if product.source.role == "finished_scene"
            else old.assets.get(f"masters/{product.id}")
        )
        source_ref = (
            reference.reference
            if reference
            else next(
                (
                    a["source_ref"]
                    for a in self.state.attempts(run["id"])
                    if a["product_id"] == product.id
                    and a["state"] == "decoded"
                    and self.state.source_is_intact(a)
                ),
                None,
            )
        )
        if not source_ref:
            raise ActionError(
                "source", "The saved source is unavailable. Recover it before adjusting framing."
            )
        scene, _ = load_image(self.storage.path(source_ref))
        try:
            if edit.framing.subject_box and not protects_subject(
                scene.size, crop_box(scene.size, edit.ratio, edit.framing), edit.framing.subject_box
            ):
                identifier = f"{product.id}/{edit.market}/{edit.ratio.replace(':', 'x')}"
                if not any(
                    f.item == identifier and f.rule == "subject_crop" for f in plan.findings
                ):
                    plan.findings.append(
                        Finding(
                            item=identifier,
                            rule="subject_crop",
                            severity="blocking",
                            evidence="This crop cuts protected subject detail.",
                            remedy="Move the focal point or correct the bounds before applying.",
                        )
                    )
                    contextualize_findings(brief, plan.outputs, plan.findings)
        finally:
            scene.close()
        return brief, plan, impact.model_dump()


def image_url(campaign_id: str, revision_id: str, item: dict) -> str | None:
    if not item["file"]:
        return None
    return (
        f"/campaigns/{campaign_id}/images/{revision_id}/{item['product_id']}/"
        f"{item['market']}/{item['ratio'].replace(':', 'x')}"
    )
