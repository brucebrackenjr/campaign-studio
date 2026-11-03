"""Lossless current project work, separate from validated execution inputs.

Manual Save and autosave use this same compare-and-write operation. The current
draft file holds one authored envelope and one last acknowledgement, not history.
"""

import copy
import json
from uuid import uuid4

from pydantic import Field

from campaign_studio.brief import Contract
from campaign_studio.intake import Intake
from campaign_studio.planning import canonical_hash
from campaign_studio.state import timestamp
from campaign_studio.workspace import ActionError

AUTHORED_LIMIT = 1_048_576
PENDING_FIELDS = (
    "form_drafts",
    "review_drafts",
    "ui_context",
    "feedback",
)


def attachment_paths(value):
    # Cleanup once deleted another campaign's upload when its path appeared
    # in the audience description of the campaign being deleted.
    brief = value.get("brief", {})
    brand = brief.get("brand")
    if isinstance(brand, dict):
        yield brand.get("logo_path")
    products = brief.get("products")
    for product in products if isinstance(products, list) else []:
        if isinstance(product, dict) and isinstance(product.get("source"), dict):
            yield product["source"].get("path")
    photos = value.get("form_drafts", {}).get("product_images", {})
    for photo in photos.values() if isinstance(photos, dict) else []:
        if isinstance(photo, dict):
            yield photo.get("path")


class AuthoredBase(Contract):
    run_id: str | None = None
    revision_id: str | None = None
    metadata_snapshot_id: str | None = None


class AuthoredSave(Contract):
    version: str | None = Field(max_length=64)
    base: AuthoredBase
    authored: dict


class AuthoredProjects:
    def __init__(self, workspace):
        self.workspace = workspace
        self.storage = workspace.storage
        self.intake = Intake(self.storage)

    def base(self, campaign_id):
        run = self.workspace.run_for(campaign_id)
        output = self.workspace.state.current_output(campaign_id)
        if output:
            run = self.workspace.state.run(output[0])
        revision = output[1] if output else None
        return {
            "run_id": run["id"] if run else None,
            "revision_id": revision,
            "metadata_snapshot_id": (
                self.workspace.state.metadata_snapshot(revision)["snapshot_id"]
                if revision
                else None
            ),
        }

    def document(self, campaign_id):
        # run_for also validates campaign identity and pending-deletion state.
        run = self.workspace.run_for(campaign_id)
        document = self.intake.current_document(campaign_id, self.workspace)
        if document is None and run:
            document = {
                "value": self.workspace.state.plan(run["id"]).brief.model_dump(mode="json"),
                "version": None,
                "planned_run": run["id"],
                "base_run": run["id"],
                "base_revision": self.workspace.state.effective_revision(run["id"]),
            }
        if document is None:
            raise ActionError(
                "campaign_data",
                "This campaign’s saved data is unavailable. "
                "Restore it before saving the campaign again.",
            )
        return document

    def read(self, campaign_id):
        document = self.document(campaign_id)
        authored = copy.deepcopy(document.get("authored"))
        if authored is None:
            authored = {
                "brief": copy.deepcopy(document["value"]),
                **{key: {} for key in PENDING_FIELDS},
            }
        else:
            self.reconcile_applied_fields(document, authored)
        base = self.base(campaign_id)
        return {
            "version": document["version"],
            "saved_at": document.get("saved_at"),
            "authored_hash": canonical_hash(authored),
            "authored": copy.deepcopy(authored),
            "base": base,
            "authored_base": document.get("authored_base", base),
        }

    def edit_authored_base(self, campaign_id, revision_id):
        """Bind an explicit Apply to its saved work and comparison image."""
        document = self.intake.read(campaign_id)
        if not document or "authored" not in document:
            return None
        revision = self.workspace.state.revision(revision_id)
        manifest = json.loads(self.storage.path(revision["manifest_ref"]).read_bytes())
        prior = manifest.get("edit_receipt", {}).get("authored_base", {})
        # Several Applies can precede the next Save. Keep their first comparison
        # revision only while the exact authored document remains unchanged.
        if prior and prior.get("version") == document["version"]:
            return prior
        return {"version": document["version"], "revision_id": revision_id}

    def reconcile_applied_fields(self, document, authored):
        """Carry explicit image applications into untouched authored fields.

        Matching raw Brief controls follow the applied copy; distinct unfinished
        values stay exact. The recorded authored base is the comparison boundary.
        """
        base = document.get("authored_base", {})
        if not base.get("run_id"):
            return
        current = self.base(authored["brief"]["campaign_id"])
        if (base.get("run_id"), base.get("revision_id")) == (
            current["run_id"],
            current["revision_id"],
        ):
            # No image Apply occurred. Preserve the raw authored representation,
            # including numbers the browser serializes without a decimal point.
            return
        applied = {}
        if current["revision_id"]:
            record = self.workspace.state.revision(current["revision_id"])
            manifest = json.loads(self.storage.path(record["manifest_ref"]).read_bytes())
            applied = manifest.get("edit_receipt", {}).get("authored_base", {})
        if applied and applied.get("version") == document["version"]:
            # Explicit Apply compares against the image that was actually edited,
            # even when Save still names an earlier run or a newer unstarted plan.
            base = {"run_id": current["run_id"], "revision_id": applied["revision_id"]}
        elif not base.get("revision_id") and base["run_id"] != current["run_id"]:
            # Merely viewing older results must not overwrite a newer plan's draft.
            return
        if base.get("revision_id"):
            record = self.workspace.state.revision(base["revision_id"])
            before = self.workspace.state.revision_plan(record).brief.model_dump(mode="json")
        else:
            before = self.workspace.state.plan(base["run_id"], current=False).brief.model_dump(
                mode="json"
            )
        after = self.workspace.state.plan(current["run_id"]).brief.model_dump(mode="json")
        draft = authored["brief"]
        brand = draft.get("brand")
        if isinstance(brand, dict):
            for field in ("logo_path", "logo_provenance"):
                if field in brand and brand[field] == before["brand"].get(field):
                    brand[field] = copy.deepcopy(after["brand"].get(field))
        markets = draft.get("markets")
        for language, content in markets.items() if isinstance(markets, dict) else []:
            if language not in before["markets"] or language not in after["markets"]:
                continue
            if isinstance(content, dict):
                for field in ("phrase_rules",):
                    if content.get(field) == before["markets"][language].get(field):
                        content[field] = copy.deepcopy(after["markets"][language].get(field))
            wording = content.get("copy") if isinstance(content, dict) else None
            if isinstance(wording, dict):
                for field in ("message", "cta"):
                    if field in wording and wording[field] == before["markets"][language][
                        "copy"
                    ].get(field):
                        wording[field] = after["markets"][language]["copy"].get(field)
                        fields = authored.get("form_drafts", {}).get("fields", {})
                        control = "copy-headline" if field == "message" else "copy-cta"
                        old = before["markets"][language]["copy"].get(field) or ""
                        new = wording[field] or ""
                        for name, values in fields.items() if isinstance(fields, dict) else []:
                            if (
                                name.startswith("brief/")
                                and name.rsplit("/", 1)[-1] == language.split("-", 1)[0]
                                and isinstance(values, dict)
                                and values.get(control) == old
                            ):
                                values[control] = new
        old_products = {p["id"]: p for p in before["products"]}
        new_products = {p["id"]: p for p in after["products"]}
        products = draft.get("products")
        for product in products if isinstance(products, list) else []:
            if not isinstance(product, dict) or not isinstance(product.get("id"), str):
                continue
            identifier = product["id"]
            if identifier not in old_products or identifier not in new_products:
                continue
            old, new = old_products[identifier], new_products[identifier]
            if product.get("framing", {}) == old.get("framing", {}):
                product["framing"] = copy.deepcopy(new.get("framing", {}))
            overrides = product.get("copy_overrides", {})
            if not isinstance(overrides, dict):
                continue
            for language in old.get("copy_overrides", {}) | new.get("copy_overrides", {}):
                prior = old.get("copy_overrides", {}).get(language, {})
                current = new.get("copy_overrides", {}).get(language, {})
                local = overrides.get(language, {})
                if not isinstance(local, dict):
                    continue
                for field in ("message", "cta"):
                    if (field in local, local.get(field)) == (
                        field in prior,
                        prior.get(field),
                    ) and (field in prior, prior.get(field)) != (
                        field in current,
                        current.get(field),
                    ):
                        if field in current:
                            local[field] = current[field]
                        else:
                            local.pop(field, None)
                if local:
                    overrides[language] = local
                else:
                    overrides.pop(language, None)
            product["copy_overrides"] = overrides

    @staticmethod
    def check_envelope(authored):
        # These are transport containers, not production fields. Text, numeric
        # strings, incomplete values, and validation feedback remain untouched.
        if (
            not isinstance(authored.get("brief"), dict)
            or set(authored) - {"brief", *PENDING_FIELDS}
            or any(not isinstance(authored[key], dict) for key in PENDING_FIELDS if key in authored)
        ):
            raise ActionError("authored_shape", "This project state could not be saved.", 422)
        try:
            encoded = json.dumps(authored, ensure_ascii=False, allow_nan=False).encode()
        except (ValueError, TypeError, RecursionError) as error:
            raise ActionError(
                "authored_shape", "This project state could not be saved.", 422
            ) from error
        if len(encoded) > AUTHORED_LIMIT:
            raise ActionError("authored_size", "This project state is too large to save.", 413)

    def check_attachments(self, campaign_id, authored, previous, reconciled):
        allowed = {r for r in attachment_paths({"brief": previous["value"]}) if isinstance(r, str)}
        allowed.update(
            r for r in attachment_paths(previous.get("authored", {})) if isinstance(r, str)
        )
        # read() already exposes these server-reconciled saved references. A prior
        # interrupted plan must not make the application's own response unsaveable.
        allowed.update(r for r in attachment_paths(reconciled) if isinstance(r, str))
        for product in authored.get("brief", {}).get("products", []):
            if isinstance(product, dict) and isinstance(product.get("id"), str):
                photo = self.workspace.retained_product_source(campaign_id, product["id"])
                if photo:
                    allowed.add(photo["path"])
        requested = {r for r in attachment_paths(authored) if isinstance(r, str)}
        project = self.storage.project(campaign_id)
        replay = authored["brief"].get("replay")
        if isinstance(replay, dict) and isinstance(replay.get("sources"), dict):
            for master in replay["sources"].values():
                if (
                    isinstance(master, dict)
                    and isinstance(master.get("path"), str)
                    and master["path"].casefold().startswith("projects/")
                ):
                    try:
                        project.require(master["path"])
                    except ValueError as error:
                        raise ActionError(
                            "authored_attachment", "Choose saved imagery from this campaign.", 422
                        ) from error
        for reference in requested:
            if not reference.casefold().startswith("projects/"):
                continue
            try:
                project.require(reference)
                if reference not in allowed and any(
                    part in reference for part in ("/Assets/Products/", "/Assets/Brand/")
                ):
                    self.intake.uploaded_path(project.upload_name(reference))
                elif reference not in allowed and not reference.lower().endswith(
                    (".png", ".jpg", ".jpeg")
                ):
                    raise ValueError("Choose an image.")
            except ValueError as error:
                raise ActionError(
                    "authored_attachment",
                    "Attach this image with this campaign’s file picker.",
                    422,
                ) from error

    def save(self, campaign_id, action):
        self.check_envelope(action.authored)
        if action.authored["brief"].get("campaign_id") != campaign_id:
            raise ActionError("campaign_identity", "A campaign’s identity cannot be changed.", 422)
        with self.storage.exclusive_draft():
            previous = self.document(campaign_id)
            base = action.base.model_dump()
            current_base = self.base(campaign_id)
            digest = canonical_hash({"authored": action.authored, "base": base})
            receipt = previous.get("authored_receipt", {})
            # A lost response can repeat exactly the last request. A different
            # edit, base, or intervening write still needs its current token.
            if (
                receipt.get("request_hash") == digest
                and receipt.get("request_version") == action.version
                and receipt.get("version") == previous["version"]
                and base == current_base
            ):
                return {key: receipt[key] for key in ("version", "saved_at", "authored_hash")}
            if action.version != previous["version"] or base != current_base:
                raise ActionError(
                    "authored_conflict",
                    "This project changed in another tab. Your work is still here.",
                    409,
                    project_state={
                        "version": previous["version"],
                        "saved_at": previous.get("saved_at"),
                    },
                )
            result = copy.deepcopy(previous)
            old_authored = previous.get("authored", {"brief": previous["value"]})
            admitted = previous.get("planned_authored_hash", canonical_hash(previous["value"]))
            # Explicit local Apply may change canonical copy/logo/framing while
            # the same pinned generation plan remains the retry authority.
            reconciled = copy.deepcopy(old_authored)
            self.reconcile_applied_fields(previous, reconciled)
            self.check_attachments(campaign_id, action.authored, previous, reconciled)
            if (
                previous.get("planned_run") == current_base["run_id"]
                and canonical_hash(old_authored["brief"]) == admitted
                and action.authored["brief"] == reconciled["brief"]
            ):
                result["planned_authored_hash"] = canonical_hash(action.authored["brief"])
            result["authored"] = copy.deepcopy(action.authored)
            result["authored_base"] = base
            result["version"] = uuid4().hex
            result["saved_at"] = timestamp()
            acknowledgement = {
                "version": result["version"],
                "saved_at": result["saved_at"],
                "authored_hash": canonical_hash(action.authored),
            }
            result["authored_receipt"] = {
                **acknowledgement,
                "request_hash": digest,
                "request_version": action.version,
            }
            project = self.storage.project(campaign_id)
            project.write(
                project.draft,
                json.dumps(result, ensure_ascii=False, allow_nan=False).encode(),
            )
            return acknowledgement
