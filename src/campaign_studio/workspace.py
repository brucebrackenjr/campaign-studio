"""Small application operations shared by the four task pages."""

import hashlib
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
from campaign_studio.planning import Plan
from campaign_studio.state import State
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
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
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
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
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
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
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
        self.display_inputs = (None, {})
        self.display_damage = {}

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
                from campaign_studio.campaign_drafts import read as read_draft

                document = read_draft(self.storage, identity)
                if document is not None:
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

    def current_composition(self, campaign_id, expected_version=None):
        from campaign_studio.compositions import snapshot

        return snapshot(self, campaign_id, expected_version)

    def preview_composition(self, campaign_id, expected_version=None):
        from campaign_studio.compositions import snapshot

        return snapshot(self, campaign_id, expected_version, preview=True)

    def save_composition(self, campaign_id, edit):
        from campaign_studio.compositions import save

        return save(self, campaign_id, edit)

    def plan(self, campaign_id: str) -> Plan:
        return self.current_composition(campaign_id)["plan"]

    def retained_product_source(
        self, campaign_id: str, product_id: str, *, verify_bytes=True
    ) -> dict | None:
        """The saved attachment choice stays separate from active concept input."""
        from campaign_studio.campaign_drafts import read

        document = read(self.storage, campaign_id) or {}
        authored = document.get("authored", {})
        product = next(
            (
                p
                for p in authored.get("brief", document.get("value", {})).get("products", [])
                if isinstance(p, dict) and p.get("id") == product_id
            ),
            None,
        )
        if not product:
            return None
        source = authored.get("form_drafts", {}).get("product_images", {}).get(product_id)
        if not isinstance(source, dict) or not source.get("path"):
            source = product.get("source", {})
        if source.get("role") == "concept" or not source.get("path"):
            return None
        self.storage.project(campaign_id).require(source["path"], inspect_file=verify_bytes)
        return source

    def source_preview_reference(
        self, campaign_id: str, product_id: str, *, verify_bytes=True
    ) -> str | None:
        """Resolve current supplied pixels or an intact master for the same imagery intent."""
        current = self.preview_composition(campaign_id)
        asset = current["sources"].get(product_id)
        return asset.reference if asset else None

    def manifest(self, run: dict, revision_id: str | None = None) -> dict | None:
        selected = revision_id or self.state.effective_revision(run["id"])
        if selected is None:
            return None
        self.state.require_images(selected)
        revision = self.state.revision(selected)
        if revision["run_id"] != run["id"]:
            raise ActionError("unknown_revision", "This saved revision is unavailable.", 404)
        return self.state.revision_manifest(revision)

    def require_saved_outputs(self, revision_id, manifest, output_ids):
        """An edit may replace its own verified files. Other files belong to other edits."""
        selected = set(output_ids)
        prefix = self.state.revision(revision_id)["manifest_ref"].rsplit("/", 1)[0]
        for output in manifest["outputs"]:
            if output["id"] not in selected or not output["file"]:
                continue
            try:
                payload = self.storage.path(f"{prefix}/{output['file']}").read_bytes()
                intact = hashlib.sha256(payload).hexdigest() == output["sha256"]
            except OSError:
                intact = False
            if not intact:
                raise ActionError(
                    "saved_images_unavailable",
                    f"The saved image for {output['product_id']} · {output['market']} · "
                    f"{output['ratio']} is missing or changed. "
                    "Use local recovery in Production before editing it.",
                )

    def logo(self, campaign_id: str, edit: LogoEdit, *, allow_unchanged=False):
        from campaign_studio.compositions import candidate

        return candidate(self, campaign_id, edit, preview=allow_unchanged)

    def text(self, campaign_id: str, edit: TextEdit, *, allow_unchanged=False):
        from campaign_studio.compositions import candidate

        return candidate(self, campaign_id, edit, preview=allow_unchanged)

    def frame(self, campaign_id: str, edit: FrameEdit, *, allow_unchanged=False):
        from campaign_studio.compositions import candidate

        return candidate(self, campaign_id, edit, preview=allow_unchanged)


def image_url(campaign_id: str, revision_id: str, item: dict) -> str | None:
    if not item["file"]:
        return None
    return (
        f"/campaigns/{campaign_id}/images/{revision_id}/{item['product_id']}/"
        f"{item['market']}/{item['ratio'].replace(':', 'x')}"
    )
