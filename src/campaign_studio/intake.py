"""App-owned brief drafts and original image uploads, separate from confirmed runs."""

import copy
import io
import re
import warnings
from contextlib import suppress
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from PIL import Image
from pydantic import Field

from campaign_studio.brief import FORMATS, Brief, Contract, Product
from campaign_studio.campaign_drafts import read as read_draft
from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.imaging import (
    ColorProfileProblem,
    EmptyLogoProblem,
    ImageProblem,
    fit_logo_placement,
    load_image,
    placed_logo_bounds,
    require_visible_logo,
)
from campaign_studio.state import timestamp
from campaign_studio.storage import CapacityError, StorageError, asset_component
from campaign_studio.validation import field_issue, validation_issues
from campaign_studio.workspace import ActionError

UPLOAD_LIMIT = 20 * 1024 * 1024
PIXEL_LIMIT = 40_000_000
REUSE_HISTORY = (
    "Verified sources retained from earlier plans in this campaign. "
    "New requests are listed separately in this plan."
)


class BriefDraft(Contract):
    value: dict
    version: str | None = None
    base_revision: str | None = None


class EditableBrief(Brief):
    """An empty product list is a saved draft, never a production-ready brief."""

    products: list[Product] = Field(max_length=5)


class Intake:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def validate_upload(payload, role=None, source_role=None):
        if role not in {None, "logo", "product"}:
            raise ActionError("upload_role", "Choose the logo or product image picker.", 422)
        if source_role not in {None, "finished_scene", "prepared_product"}:
            raise ActionError("upload_role", "Choose a supported product image type.", 422)
        if len(payload) > UPLOAD_LIMIT:
            raise ActionError(
                "upload_size",
                "Choose a PNG or JPEG image up to 20 MiB.",
                413,
                input_image={
                    "kind": "encoded_bytes",
                    "encoded_bytes": len(payload),
                    "limit_bytes": UPLOAD_LIMIT,
                },
            )
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                # Read supported headers without decoding pixels. Their format
                # readers let our stricter 40 MP limit report exact dimensions
                # even above Pillow's generic decompression-bomb threshold.
                from PIL import JpegImagePlugin, PngImagePlugin

                reader = (
                    PngImagePlugin.PngImageFile
                    if payload.startswith(b"\x89PNG\r\n\x1a\n")
                    else JpegImagePlugin.JpegImageFile
                    if payload.startswith(b"\xff\xd8\xff")
                    else Image.open
                )
                try:
                    with reader(io.BytesIO(payload)) as source:
                        kind = source.format
                        dimensions = source.size
                except SyntaxError as error:
                    # Pillow's direct image readers use SyntaxError for malformed headers.
                    raise ImageProblem(
                        "This image could not be opened. Choose a valid PNG or JPEG."
                    ) from error
            if kind not in {"PNG", "JPEG"}:
                raise ActionError(
                    "upload_image",
                    "Choose a PNG or JPEG image.",
                    422,
                    input_image={"kind": "unsupported_type", "detected_type": str(kind)[:24]},
                )
            if dimensions[0] * dimensions[1] > PIXEL_LIMIT:
                raise ActionError(
                    "upload_image",
                    "Choose an image up to 40 megapixels.",
                    422,
                    input_image={
                        "kind": "pixel_count",
                        "width": dimensions[0],
                        "height": dimensions[1],
                        "limit_pixels": PIXEL_LIMIT,
                    },
                )
            image, _ = load_image(io.BytesIO(payload))
            try:
                dimensions = image.size
                opaque = "A" not in image.getbands() or image.getchannel("A").getextrema()[0] == 255
                if role == "logo":
                    require_visible_logo(image)
                if source_role == "finished_scene" and not opaque:
                    raise ActionError(
                        "upload_scene",
                        "This image has transparent areas. "
                        "Select a product image without transparency.",
                        422,
                        input_image={"kind": "transparent_scene"},
                    )
            finally:
                image.close()
        except CapacityError, ActionError:
            raise
        except (
            OSError,
            ValueError,
            Image.DecompressionBombWarning,
            Image.DecompressionBombError,
        ) as error:
            raise ActionError(
                "upload_image",
                str(error)
                if isinstance(error, ImageProblem)
                else "This image could not be opened. Choose a valid PNG or JPEG.",
                422,
                input_image={
                    "kind": "color_profile"
                    if isinstance(error, ColorProfileProblem)
                    else "no_visible_pixels"
                    if isinstance(error, EmptyLogoProblem)
                    else "decode_failure"
                },
            ) from error
        return {"width": dimensions[0], "height": dimensions[1], "type": kind, "opaque": opaque}

    def upload(
        self, payload, content_type, campaign_id=None, role=None, label=None, source_role=None
    ):
        with self.storage.exclusive_run():
            from campaign_studio.cleanup import require_campaign_available

            require_campaign_available(self.storage, campaign_id)
            return self._upload(payload, content_type, campaign_id, role, label, source_role)

    def _upload(self, payload, content_type, campaign_id, role=None, label=None, source_role=None):
        details = self.validate_upload(payload, role, source_role)
        kind = details["type"]
        self.storage.require_capacity(len(payload) * 2)
        for _ in range(100):
            token = uuid4().hex
            project = self.storage.project(campaign_id)
            group = "Brand" if role == "logo" else "Products"
            readable = asset_component(
                label if isinstance(label, str) else "",
                "Brand logo" if role == "logo" else "Product image",
            )
            name = f"{readable}-{token}.{'png' if kind == 'PNG' else 'jpg'}"
            reference = project.ref(f"Assets/{group}/{name}")
            # Commit ownership first. A crash can leave an owned missing file,
            # but never a new successfully written anonymous attachment.
            with self.storage.connect() as db:
                if db.execute("SELECT 1 FROM uploads WHERE name=?", (name,)).fetchone():
                    continue
                project.claim(reference, db)
                db.execute("INSERT INTO uploads(name,reference) VALUES (?,?)", (name, reference))
            try:
                self.storage.write_new(reference, payload)
                break
            except FileExistsError:
                self._forget_upload_owner(reference, campaign_id)
                continue
            except BaseException as error:
                # Publication can succeed before a directory-fsync error.
                # Remove the file before forgetting ownership; if unlink also
                # fails, retain its owner so campaign deletion can reclaim it.
                self.storage.unlink(self.storage.path(reference), missing_ok=True)
                self._forget_upload_owner(reference, campaign_id)
                if isinstance(error, OSError):
                    raise ActionError(
                        "upload_write", "This image could not be saved. Try again.", 500
                    ) from error
                raise
        else:
            raise StorageError("A new upload identity could not be reserved. Try again.")
        return {
            **details,
            "reference": reference,
            "url": f"/uploads/{quote(PurePosixPath(reference).name, safe='')}",
            **(
                {
                    "provenance": {
                        "label": "User-supplied logo",
                        "rights": "Not provided",
                        "synthetic": False,
                    }
                }
                if role == "logo"
                else {}
            ),
        }

    def add_product(self, workspace, campaign_id, version, base, product, payload):
        """Add a photo and its new product in one request; failed saves release only this upload."""
        from campaign_studio.authored import AuthoredProjects, AuthoredSave, attachment_paths
        from campaign_studio.cleanup import require_campaign_available

        value = copy.deepcopy(product)
        source = value.get("source", {})
        if (
            not isinstance(source, dict)
            or source.get("role") not in {"finished_scene", "prepared_product"}
            or source.get("path")
        ):
            raise ActionError("product_image", "Select an image for this new product.", 422)
        source["path"] = "new-product.png"
        value = Product.model_validate(value).model_dump(mode="json")
        self.validate_upload(payload, "product", source["role"])
        projects = AuthoredProjects(workspace)
        with self.storage.exclusive_run():
            require_campaign_available(self.storage, campaign_id)
            current = projects.read(campaign_id)
            products = current["authored"]["brief"].get("products", [])
            existing = next((p for p in products if p.get("id") == value["id"]), None)
            if existing:
                # A lost response may repeat Add. Return the saved result only
                # when the current product and original bytes still match.
                expected = copy.deepcopy(value)
                reference = existing.get("source", {}).get("path")
                expected["source"]["path"] = reference
                if expected == Product.model_validate(existing).model_dump(mode="json"):
                    self.storage.project(campaign_id).require(reference)
                    if self.storage.path(reference).read_bytes() == payload:
                        return current
                raise ActionError(
                    "authored_conflict",
                    "This product has already been added. "
                    "Refresh the page to see the latest changes.",
                    409,
                )
            if version != current["version"] or base != current["base"]:
                raise ActionError(
                    "authored_conflict",
                    "This campaign has been updated elsewhere. "
                    "Refresh the page to see the latest changes.",
                    409,
                )
            if len(products) >= 5:
                raise ActionError("product_limit", "This workspace supports up to 5 products.", 422)
            project = self.storage.project(campaign_id)
            new_directories = [
                self.storage.path(project.ref(part)) for part in ("Assets/Products", "Assets")
            ]
            new_directories = [path for path in new_directories if not path.exists()]
            try:
                uploaded = self._upload(
                    payload, "", campaign_id, "product", value["name"], source["role"]
                )
                reference = uploaded["reference"]
                value["source"]["path"] = reference
                authored = copy.deepcopy(current["authored"])
                authored["brief"].setdefault("products", []).append(value)
                try:
                    projects.save(
                        campaign_id, AuthoredSave(version=version, base=base, authored=authored)
                    )
                except BaseException:
                    # A response can fail after the save committed. Never delete
                    # bytes now referenced by the current document.
                    saved = projects.read(campaign_id)
                    if reference not in set(attachment_paths(saved["authored"])):
                        project.require(reference)
                        self.storage.unlink(self.storage.path(reference), missing_ok=True)
                        self._forget_upload_owner(reference, campaign_id)
                    raise
                return projects.read(campaign_id)
            finally:
                # Only remove directories created by this request, and only
                # while empty. Existing folders and adopted files stay intact.
                for directory in new_directories:
                    with suppress(OSError):
                        directory.rmdir()

    def _forget_upload_owner(self, reference, campaign_id):
        if campaign_id is not None:
            with self.storage.connect() as db:
                db.execute(
                    "DELETE FROM file_owners WHERE reference=? AND campaign_id=?",
                    (reference, campaign_id),
                )

    def uploaded_path(self, name):
        from campaign_studio.projects import valid_upload_name

        if not valid_upload_name(name):
            raise ActionError("upload", "This uploaded image is unavailable.", 404)
        with self.storage.connect() as db:
            row = db.execute("SELECT reference FROM uploads WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ActionError("upload", "This uploaded image is unavailable.", 404)
            reference = row[0]
            pending = db.execute(
                "SELECT 1 FROM cleanup_intents, json_each(value_json, '$.files') AS f "
                "WHERE json_extract(f.value, '$.reference')=? LIMIT 1",
                (reference,),
            ).fetchone()
        # Shared leaves are excluded from the committed removal list and remain usable.
        path = self.storage.path(reference)
        if pending or not path.is_file():
            raise ActionError("upload", "This uploaded image is unavailable.", 404)
        return path

    def read(self, campaign_id):
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id)
        try:
            return read_draft(self.storage, campaign_id)
        except ValueError, KeyError, TypeError:
            raise ActionError(
                "campaign_data",
                "This campaign’s saved brief could not be read. "
                "Restore its saved data before editing it again.",
            ) from None

    def current_document(self, campaign_id, workspace):
        from campaign_studio.compositions import migrate_current

        return migrate_current(workspace.state, campaign_id)

    def persist_applied_revision(self, campaign_id, workspace, *, only_superseded_colors=False):
        with self.storage.exclusive_draft():
            self._persist_applied_revision(
                campaign_id, workspace, only_superseded_colors=only_superseded_colors
            )

    def _persist_applied_revision(self, campaign_id, workspace, *, only_superseded_colors=False):
        from campaign_studio.planning import canonical_hash

        # Revision publication is authoritative. The same baseline reconciliation
        # repairs a lost refresh after a crash without discarding unrelated fields.
        previous = self.read(campaign_id)
        current = self.current_document(campaign_id, workspace)
        if only_superseded_colors:
            prior_targets = (
                (previous or {})
                .get("authored", {})
                .get("form_drafts", {})
                .get("brand_color_defaults")
            )
            current_targets = (
                (current or {})
                .get("authored", {})
                .get("form_drafts", {})
                .get("brand_color_defaults")
            )
            if prior_targets == current_targets:
                return
        if not current:
            return
        from campaign_studio.authored import AuthoredProjects

        projects = AuthoredProjects(workspace)
        previous_authored = previous.get("authored")
        admitted_before = False
        if previous_authored and previous.get("planned_run"):
            admitted = previous.get("planned_authored_hash", canonical_hash(previous["value"]))
            before = copy.deepcopy(previous_authored["brief"])
            # The read view already has current image settings. Reconstruct the
            # prior comparison in memory when checking an existing plan admission.
            base = previous.get("authored_base", {})
            if base.get("revision_id"):
                baseline = workspace.state.comparison_brief(campaign_id, base["revision_id"])
                old = {product["id"]: product for product in baseline["products"]}
                products = before.get("products", [])
                for product in products if isinstance(products, list) else []:
                    if isinstance(product, dict) and product.get("id") in old:
                        product["framing"] = copy.deepcopy(old[product["id"]].get("framing", {}))
            admitted_before = admitted in {
                canonical_hash(previous_authored["brief"]),
                canonical_hash(before),
            }
        if current.get("authored"):
            projects.reconcile_applied_fields(current, current["authored"])
            current["authored_base"] = projects.base(campaign_id)
            if admitted_before and current.get("planned_run") == current["authored_base"]["run_id"]:
                current["planned_authored_hash"] = canonical_hash(current["authored"]["brief"])
        if current == previous:
            from campaign_studio.campaign_drafts import strip_accepted_settings

            strip_accepted_settings(self.storage, campaign_id)
            return
        current["version"] = uuid4().hex
        current["saved_at"] = timestamp()
        run = workspace.run_for(campaign_id)
        current["base_run"] = run["id"]
        current["base_revision"] = workspace.state.effective_revision(run["id"])
        project = self.storage.project(campaign_id)
        write_draft(self.storage, campaign_id, current)
        from campaign_studio.output_files import sync_draft_comparisons

        with self.storage.connect() as db:
            sync_draft_comparisons(workspace.state, db, project, current)

    def write(self, campaign_id, value, expected, *, attachment_hints=None):
        with self.storage.exclusive_draft():
            return self._write(campaign_id, value, expected, attachment_hints=attachment_hints)

    def create(self, campaign_id, value, *, attachment_hints=None, imported_bytes=None):
        """Publish one new draft and its database identity, without replacing work."""
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id, registered=False)
        if value.get("campaign_id") != campaign_id:
            raise ActionError("campaign_identity", "A campaign’s identity cannot be changed.", 422)
        result = {
            "value": value,
            "authored": {"brief": copy.deepcopy(value)},
            "composition_schema": 1,
            "version": uuid4().hex,
            "saved_at": timestamp(),
        }
        if attachment_hints:
            result["attachment_hints"] = attachment_hints
        from campaign_studio.projects import create_project

        with self.storage.exclusive_draft():
            label = value.get("brand", {}).get("name") or value.get("name")
            with self.storage.connect() as db:
                project = create_project(self.storage, db, campaign_id, label)
                files = []
                if imported_bytes is not None:
                    files.append((project.ref("Brief/Imports/original.json"), imported_bytes))
                for reference, _ in files:
                    project.claim(reference, db)
            # The original import remains an owned source file. Publish the
            # editable database record only after that input has been written.
            published = []
            try:
                for reference, payload in files:
                    self.storage.write_new(reference, payload)
                    published.append(reference)
                write_draft(self.storage, campaign_id, result, create=True)
            except BaseException:
                for reference in published:
                    self.storage.unlink(self.storage.path(reference), missing_ok=True)
                with self.storage.connect() as db:
                    db.execute("DELETE FROM file_owners WHERE campaign_id=?", (campaign_id,))
                    db.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
                root = self.storage.path(f"Campaigns/{project.folder}")
                for relative in ("Brief/Imports", "Brief", ""):
                    with suppress(OSError):
                        (root / relative).rmdir()
                raise
        return result

    def _write(self, campaign_id, value, expected, *, attachment_hints=None):
        project = self.storage.project(campaign_id)
        project.check_brief(value)
        previous = self.read(campaign_id)
        if previous and previous["version"] != expected:
            raise ActionError(
                "stale_brief", "This brief changed in another window. Reopen it before saving."
            )
        if value.get("campaign_id") != campaign_id:
            raise ActionError("campaign_identity", "A campaign’s identity cannot be changed.", 422)
        if previous and previous["value"] == value:
            return previous
        result = copy.deepcopy(previous) if previous else {}
        result.update(value=value, version=uuid4().hex, saved_at=timestamp(), composition_schema=1)
        # Validated Brief writes must preserve independently saved unfinished
        # form work. The common project Save owns that envelope and its receipt.
        for key in ("authored", "authored_base", "authored_receipt"):
            if previous and key in previous:
                result[key] = copy.deepcopy(previous[key])
        result.setdefault("authored", {})["brief"] = copy.deepcopy(value)
        hints = (
            attachment_hints
            if attachment_hints is not None
            else (previous or {}).get("attachment_hints", {})
        )
        if hints:
            result["attachment_hints"] = hints
        try:
            write_draft(self.storage, campaign_id, result, create=previous is None)
        except FileExistsError as error:
            raise ActionError(
                "stale_brief", "This campaign was saved in another window. Reopen it before saving."
            ) from error
        return result

    def new_campaign_id(self):
        for _ in range(100):
            identity = str(uuid4())
            with self.storage.connect() as db:
                known = db.execute("SELECT 1 FROM campaigns WHERE id=?", (identity,)).fetchone()
            if not known:
                return identity
        raise StorageError("A new campaign identity could not be reserved. Try again.")

    def new(self, *, name="Untitled campaign", audience="", brand=""):
        campaign_id = self.new_campaign_id()
        value = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "name": name,
            "audience": audience,
            "creative_direction": "",
            "brand": {
                "id": f"brand-{uuid4().hex}",
                "name": brand,
                "logo_path": "",
                "logo_provenance": {
                    "label": "Not provided",
                    "rights": "Not provided",
                    "synthetic": False,
                },
                "palette": ["#FFFFFF"],
                "text_color": "#FFFFFF",
            },
            "copy_provenance": {
                "label": "User-supplied wording",
                "rights": "Not provided",
                "synthetic": False,
            },
            "markets": {
                market: {
                    "copy": {"message": "", "cta": None},
                    "phrase_rules": None,
                }
                for market in ("en-US", "es-MX")
            },
            "products": [],
            "replay": None,
        }
        return campaign_id, self.create(campaign_id, value)

    def save(self, campaign_id, document, workspace):
        with self.storage.exclusive_run(), self.storage.exclusive_draft():
            return self._save(campaign_id, document, workspace)

    def _save(self, campaign_id, document, workspace, *, persist=True, preserve_ids=False):
        """Validate and normalize a brief without using the lossless authored Save.

        persist=False returns only the prepared value. The default publishes it
        and returns the draft document; callers already hold runner, then draft.
        """
        # Only previously selected assets or this app's validated uploads may be referenced.
        previous = self.current_document(campaign_id, workspace)
        run = workspace.run_for(campaign_id)
        if document.base_revision != (
            workspace.state.effective_revision(run["id"]) if run else None
        ):
            raise ActionError(
                "stale_brief",
                "The saved images changed. Reopen the brief before saving; "
                "your typed draft is still here.",
            )
        old = (
            previous["value"]
            if previous
            else (workspace.state.plan(run["id"]).brief.model_dump(mode="json") if run else None)
        )
        if old is None:
            raise ActionError("brief", "Create a campaign before saving its brief.", 404)
        value = copy.deepcopy(document.value)
        if not isinstance(value.get("products"), list) or len(value["products"]) > 5:
            message = "Save up to 5 products in a draft; add a product before production."
            raise ActionError(
                "products",
                message,
                422,
                field_errors=[field_issue(["products"], "product_count", message)],
            )
        if (
            not isinstance(value.get("markets"), dict)
            or not value["markets"]
            or set(value["markets"]) - {"en-US", "es-MX"}
        ):
            message = "Choose English (United States), Spanish (Mexico), or both."
            raise ActionError(
                "languages",
                message,
                422,
                field_errors=[field_issue(["markets"], "languages", message)],
            )
        if (
            not isinstance(value.get("brand"), dict)
            or any(
                not isinstance(p, dict) or not isinstance(p.get("source"), dict)
                for p in value["products"]
            )
            or any(
                not isinstance(m, dict)
                or not isinstance(m.get("copy"), dict)
                or (m.get("phrase_rules") is not None and not isinstance(m["phrase_rules"], list))
                for m in value["markets"].values()
            )
        ):
            raise ActionError(
                "brief_fields",
                "Check the campaign, product, brand, and wording fields.",
                422,
                field_errors=self.shape_errors(value),
            )
        allowed = {old.get("brand", {}).get("logo_path", "")}
        allowed.update(p.get("source", {}).get("path", "") for p in old.get("products", []))
        # A concept plan does not retire the same product's retained photo.
        for product in value["products"]:
            if isinstance(product.get("id"), str):
                retained = workspace.retained_product_source(campaign_id, product["id"])
                if retained:
                    allowed.add(retained["path"])
        references = [(value["brand"].get("logo_path", ""), ["brand", "logo_path"])]
        references.extend(
            (p["source"].get("path", ""), ["products", i, "source", "path"])
            for i, p in enumerate(value["products"])
        )
        for reference, path in references:
            if not isinstance(reference, str):
                message = "Choose an image using its file picker."
                raise ActionError(
                    "image", message, 422, field_errors=[field_issue(path, "image", message)]
                )
            if reference:
                try:
                    project = self.storage.project(campaign_id)
                    project.require(reference)
                    if reference not in allowed:
                        self.uploaded_path(project.upload_name(reference))
                except (ActionError, StorageError) as error:
                    raise ActionError(
                        "image",
                        "Attach this image to the selected campaign using its file picker.",
                        422,
                        field_errors=[
                            field_issue(path, "image", "Choose an image for this campaign.")
                        ],
                    ) from error
        ids = {p["id"] for p in old["products"]}
        seen = set()
        for product in value["products"] if not preserve_ids else []:
            if (
                not isinstance(product.get("id"), str)
                or product["id"] not in ids
                or product["id"] in seen
            ):
                product["id"] = f"product-{uuid4().hex}"
            seen.add(product["id"])
        value["brand"]["id"] = old["brand"]["id"]
        for market, language in value["markets"].items():
            for index, rule in enumerate(language.get("phrase_rules") or []):
                if not isinstance(rule, dict):
                    raise ActionError(
                        "phrase_rule",
                        "Each phrase rule needs a word or phrase.",
                        422,
                        field_errors=[
                            field_issue(
                                ["markets", market, "phrase_rules", index],
                                "phrase_rule",
                                "Supply a phrase rule with a word or phrase.",
                            )
                        ],
                    )
                if not rule.get("id"):
                    rule["id"] = f"rule-{uuid4().hex}"
        # Retain only the same product's verified master while authoring its next plan.
        value["replay"] = copy.deepcopy(old.get("replay"))
        if value["replay"]:
            old_products = {p["id"]: p for p in old["products"]}
            unchanged = {
                p["id"]
                for p in value["products"]
                if p["id"] in old_products
                and p["source"] == old_products[p["id"]]["source"]
                and p.get("description") == old_products[p["id"]].get("description")
            }
            if value.get("creative_direction") != old.get("creative_direction") or value.get(
                "audience"
            ) != old.get("audience"):
                unchanged = set()
            value["replay"]["sources"] = {
                key: master
                for key, master in value["replay"]["sources"].items()
                if key in unchanged
            }
            if not value["replay"]["sources"]:
                value["replay"] = None
            else:
                value["replay"]["partial"] = set(value["replay"]["sources"]) != {
                    p["id"] for p in value["products"] if p["source"]["role"] != "finished_scene"
                }
        self.validate_draft(value)
        if not persist:
            return value
        result = self._write(campaign_id, value, document.version)
        result["base_run"] = run["id"] if run else None
        result["base_revision"] = workspace.state.effective_revision(run["id"]) if run else None
        write_draft(self.storage, campaign_id, result)
        return result

    @staticmethod
    def shape_errors(value):
        paths = []
        if not isinstance(value.get("brand"), dict):
            paths.append(["brand"])
        for index, product in enumerate(value["products"]):
            if not isinstance(product, dict):
                paths.append(["products", index])
            elif not isinstance(product.get("source"), dict):
                paths.append(["products", index, "source"])
        for market, content in value["markets"].items():
            if not isinstance(content, dict):
                paths.append(["markets", market])
            elif not isinstance(content.get("copy"), dict):
                paths.append(["markets", market, "copy"])
            elif content.get("phrase_rules") is not None and not isinstance(
                content["phrase_rules"], list
            ):
                paths.append(["markets", market, "phrase_rules"])
        return [
            field_issue(path, "invalid_type", "Use the expected fields for this part of the brief.")
            for path in paths
        ]

    @staticmethod
    def validate_draft(value):
        # Incomplete text and unselected files may be saved, but malformed structures,
        # unknown fields, invalid types, and unsupported source methods may not.
        from pydantic import ValidationError

        candidate = copy.deepcopy(value)

        def fill(record, keys):
            for key in keys:
                if record.get(key) == "":
                    record[key] = "Not provided"

        fill(candidate, ("name", "audience", "creative_direction"))
        fill(candidate["brand"], ("name",))
        for product in candidate["products"]:
            fill(product, ("name", "description"))
            fill(product["source"], ("path", "description"))
            if product["source"].get("opaque_nonreflective_product_confirmed") is False:
                product["source"]["opaque_nonreflective_product_confirmed"] = True
        for market in candidate["markets"].values():
            fill(market["copy"], ("message",))
            for rule in market.get("phrase_rules") or []:
                fill(rule, ("phrase",))
        try:
            EditableBrief.model_validate(candidate)
        except ValidationError as error:
            raise ActionError(
                "brief_fields",
                "Some brief fields are invalid. "
                "Check product methods, colors, and language wording.",
                422,
                field_errors=validation_issues(error),
            ) from error

    def import_value(self, campaign_id, value):
        from pydantic import ValidationError

        try:
            validated = Brief.model_validate(value).model_dump(mode="json")
        except ValidationError as error:
            raise ActionError(
                "import_brief",
                "This JSON brief is incomplete or invalid. Check its campaign, brand palette, "
                "products, and language wording. Your current draft is unchanged.",
                422,
                field_errors=validation_issues(error, imported_fields=True),
            ) from error
        # Paths are input descriptions only. Import never opens a referenced file.
        validated["campaign_id"] = campaign_id
        validated["brand"]["id"] = f"brand-{uuid4().hex}"
        validated["brand"]["logo_path"] = ""
        validated["replay"] = None
        for product in validated["products"]:
            product["id"] = f"product-{uuid4().hex}"
            if product["source"]["role"] != "concept":
                product["source"]["path"] = ""
        for market in validated["markets"].values():
            for rule in market["phrase_rules"] or []:
                rule["id"] = f"rule-{uuid4().hex}"
        return validated

    @staticmethod
    def attachment_hints(original, imported):
        """Display-only basenames; never restore imported paths as trusted asset references."""

        def filename(reference):
            name = PurePosixPath(str(reference or "").replace("\\", "/")).name
            return re.sub(r"[^\w .()-]", "_", name)[:128] if name not in {"", ".", ".."} else None

        hints = {}
        logo = filename(original["brand"].get("logo_path"))
        if logo:
            hints["logo"] = logo
        for source, product in zip(original["products"], imported["products"], strict=True):
            name = filename(source["source"].get("path"))
            if name and product["source"]["role"] != "concept":
                hints[product["id"]] = name
        return hints

    def prepare(self, campaign_id, expected, workspace):
        from campaign_studio.authored import AuthoredProjects
        from campaign_studio.planning import canonical_hash

        document = self.read(campaign_id)
        receipt = (document or {}).get("production_receipt", {})
        if (
            receipt.get("request_version") == expected
            and receipt.get("version") == document["version"]
        ):
            return workspace.state.plan(receipt["run_id"], current=False)
        from pydantic import ValidationError

        try:
            current = workspace.current_composition(campaign_id, expected)
        except ValidationError as error:
            raise ActionError(
                "brief_fields",
                "Some campaign fields need attention before preparing sources.",
                422,
                field_errors=validation_issues(error),
            ) from error
        plan = current["plan"]
        if plan.blocked:
            raise ActionError(
                "brief_images",
                "Correct the listed campaign findings before preparing sources.",
                422,
                field_errors=list(
                    {
                        (tuple(field.path), field.code): field.model_dump(mode="json")
                        for finding in plan.findings
                        if finding.severity == "blocking" and finding.context
                        for field in finding.context.fields
                    }.values()
                ),
                proposal={
                    "outputs": [o.model_dump(mode="json") for o in plan.outputs],
                    "campaign_id": campaign_id,
                    "version": expected,
                    "findings": [
                        f.model_dump(mode="json") for f in plan.findings if f.severity == "blocking"
                    ],
                },
            )
        # Supplied and previously generated sources are already editable. Reviewing
        # their readiness does not create a run, copy files, or render images.
        if not plan.generation_products:
            return plan
        with self.storage.exclusive_run(), self.storage.exclusive_draft():
            document = self.read(campaign_id)
            if not document or document["version"] != expected:
                raise ActionError(
                    "stale_brief", "This campaign changed. Review its current source plan."
                )
            old = workspace.run_for(campaign_id)
            frozen = None
            if old and old["state"] == "planned":
                existing = workspace.state.plan(old["id"], current=False)
                if self.same_source_plan(existing, plan):
                    frozen = existing
            if frozen is None:
                run_id = workspace.state.create(plan, len(plan.generation_products))
                frozen = workspace.state.plan(run_id, current=False)
            else:
                run_id = old["id"]
                # The current brief may have returned to an earlier plan after
                # an edit was undone. Re-admit that reviewed version without
                # creating another run or retaining its stale authored guard.
                if document.get("planned_run") == run_id and document.get(
                    "planned_authored_hash"
                ) == canonical_hash(document["authored"]["brief"]):
                    return frozen
            authored = document["authored"]["brief"]
            authored["brand"]["logo_path"] = frozen.brief.brand.logo_path
            products = {p.id: p for p in frozen.brief.products}
            for product in authored["products"]:
                if products[product["id"]].source.role != "concept":
                    product["source"]["path"] = products[product["id"]].source.path
            if authored.get("replay") and frozen.brief.replay:
                for pid, source in authored["replay"]["sources"].items():
                    source["path"] = frozen.brief.replay.sources[pid].path
            document["planned_run"] = run_id
            document["planned_authored_hash"] = canonical_hash(authored)
            document["authored_base"] = AuthoredProjects(workspace).base(campaign_id)
            document["version"] = uuid4().hex
            document["production_receipt"] = {
                "request_version": expected,
                "version": document["version"],
                "run_id": run_id,
            }
            write_draft(self.storage, campaign_id, document)
            return frozen

    @staticmethod
    def same_source_plan(first, second):
        """Frozen input copies keep the same meaning when their verified bytes match."""
        from campaign_studio.planning import canonical_hash

        def identity(plan):
            brief = plan.brief.model_dump(mode="json")
            assets = plan.assets
            if "logo" in assets:
                brief["brand"]["logo_path"] = assets["logo"].sha256
            for product in brief["products"]:
                asset = assets.get(f"products/{product['id']}")
                if asset and "path" in product["source"]:
                    product["source"]["path"] = asset.sha256
            for pid, source in (brief.get("replay") or {}).get("sources", {}).items():
                asset = assets.get(f"masters/{pid}")
                if asset:
                    source["path"] = asset.sha256
            return canonical_hash(
                {
                    "brief": brief,
                    "assets": {
                        key: asset.model_dump(exclude={"reference"})
                        for key, asset in assets.items()
                    },
                    "generation_requests": {
                        key: request.model_dump()
                        for key, request in plan.generation_requests.items()
                    },
                }
            )

        return identity(first) == identity(second)

    def fit_replacement_logo(self, brief, previous_reference):
        """A changed Brief attachment uses the same proportional fit as Studio."""
        if not brief.brand.logo_path or brief.brand.logo_path == previous_reference:
            return brief, []
        try:
            image, _ = load_image(self.storage.path(brief.brand.logo_path))
            try:
                require_visible_logo(image)
                size = image.size
            finally:
                image.close()
        except ValueError, OSError:
            return brief, []  # Ordinary plan preflight reports the actual asset failure.
        fitted = []
        resolved = brief.model_copy(deep=True)
        for product in resolved.products:
            for market, formats in product.framing.items():
                for ratio, framing in formats.items():
                    requested = framing.logo_placement
                    if requested is None:
                        continue
                    try:
                        placed_logo_bounds(size, FORMATS[ratio], requested)
                    except ImageProblem:
                        framing.logo_placement = fit_logo_placement(size, FORMATS[ratio], requested)
                        fitted.append(
                            {
                                "id": f"{product.id}/{market}/{ratio.replace(':', 'x')}",
                                "product_id": product.id,
                                "market": market,
                                "ratio": ratio,
                                "requested": requested.model_dump(),
                                "placement": framing.logo_placement.model_dump(),
                                "bounds": list(
                                    placed_logo_bounds(size, FORMATS[ratio], framing.logo_placement)
                                ),
                            }
                        )
        return resolved, fitted

    def recheck_rules(self, campaign_id, document, run_id, workspace):
        """Recheck only supplied rules against intact current images, under one write lock."""
        raise ActionError(
            "autosave",
            "Phrase rules save automatically. Refresh the current campaign preview.",
            410,
        )

    def _automatic_replay(self, prior, document, run, workspace):
        """Recognize omitted local reuse bookkeeping, not an edited source mapping."""
        from campaign_studio.plan_storage import generation_inputs

        replay = prior.get("replay")
        if (
            not replay
            or document.get("planned_run") != run["id"]
            or document.get("authored", {}).get("brief", {}).get("replay") is not None
        ):
            return False
        brief = Brief.model_validate({**prior, "replay": None})
        required = {p.id for p in brief.products if p.source.role != "finished_scene"}
        if replay["history"] != REUSE_HISTORY or replay["partial"] != (
            set(replay["sources"]) != required
        ):
            return False
        # Imported replay declarations alone are not evidence of automatic reuse.
        # Each omitted master must have an actual same-campaign generation receipt.
        remaining = set(replay["sources"])
        products = {product.id: product for product in brief.products}
        for source_run in workspace.runs(brief.campaign_id):
            for attempt in workspace.state.attempts(source_run["id"]):
                product_id = attempt["product_id"]
                if product_id not in remaining or attempt["state"] != "decoded":
                    continue
                master = replay["sources"][product_id]
                source = workspace.state.source_generation(source_run["id"]).get(product_id)
                request = source["request"] if source else None
                if (
                    request is not None
                    and source["inputs"] == generation_inputs(brief, products[product_id])
                    and request.model == master["model"]
                    and attempt["source_sha256"] == master["sha256"]
                    and attempt["created_at"][:10] == master["generated_on"]
                    and master["provenance"]
                    == {
                        "label": "Verified image retained from an earlier plan in this campaign",
                        "rights": "Not provided",
                        "synthetic": products[product_id].source.provenance.synthetic,
                    }
                ):
                    remaining.remove(product_id)
            if not remaining:
                return True
        return False

    def reuse_unchanged(self, plan, old_run, workspace):
        from campaign_studio.generation import request_for
        from campaign_studio.planning import make_plan

        old = workspace.state.plan(old_run["id"])
        old_products = {product.id: product for product in old.brief.products}
        sources = plan.brief.replay.model_dump(mode="json")["sources"] if plan.brief.replay else {}
        attempts = workspace.state.attempts(old_run["id"])
        for product_id in plan.generation_products:
            prior = old_products.get(product_id)
            if prior is None or prior.source.role == "finished_scene":
                continue
            old_request = old.generation_requests.get(product_id) or request_for(old.brief, prior)
            # Compare the producer's inputs under the same request builder.
            # Revised app instructions alone do not require a new photograph.
            if plan.generation_requests[product_id] != request_for(old.brief, prior):
                continue
            asset = plan.assets.get(f"products/{product_id}")
            old_asset = old.assets.get(f"products/{product_id}")
            if bool(asset) != bool(old_asset) or (asset and asset.sha256 != old_asset.sha256):
                continue
            if old.brief.replay and product_id in old.brief.replay.sources:
                master = old.brief.replay.sources[product_id]
                if master.model == old_request.model:
                    sources[product_id] = master.model_dump(mode="json")
                continue
            attempt = next(
                (
                    a
                    for a in reversed(attempts)
                    if a["product_id"] == product_id
                    and a["state"] == "decoded"
                    and workspace.state.source_is_intact(a)
                ),
                None,
            )
            if attempt:
                sources[product_id] = {
                    "path": attempt["source_ref"],
                    "sha256": attempt["source_sha256"],
                    "model": old_request.model,
                    "generated_on": date.fromisoformat(attempt["created_at"][:10]).isoformat(),
                    "provenance": {
                        "label": "Verified image retained from an earlier plan in this campaign",
                        "rights": "Not provided",
                        "synthetic": prior.source.provenance.synthetic,
                    },
                }
        if not sources:
            return plan
        required = {p.id for p in plan.brief.products if p.source.role != "finished_scene"}
        value = plan.brief.model_dump(mode="json")
        value["replay"] = {
            "partial": set(sources) != required,
            "sources": sources,
            "historical_requests": len(attempts)
            + (old.brief.replay.historical_requests if old.brief.replay else 0),
            "history": REUSE_HISTORY,
        }
        return make_plan(Brief.model_validate(value), self.storage)
