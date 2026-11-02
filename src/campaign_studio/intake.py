"""App-owned brief drafts and original image uploads, separate from confirmed runs."""

import copy
import io
import json
import re
import warnings
from datetime import date
from pathlib import PurePosixPath
from uuid import uuid4

from PIL import Image
from pydantic import Field

from campaign_studio.brief import FORMATS, Brief, Contract, Product
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
from campaign_studio.storage import CapacityError, StorageError
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

    def upload(self, payload, content_type, campaign_id=None, role=None):
        if role not in {None, "logo", "product"}:
            raise ActionError("upload_role", "Choose the logo or product image picker.", 422)
        with self.storage.exclusive_run():
            if campaign_id is not None:
                from campaign_studio.cleanup import require_campaign_available

                require_campaign_available(self.storage, campaign_id)
            return self._upload(payload, content_type, campaign_id, role)

    def _upload(self, payload, content_type, campaign_id, role=None):
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
            self.storage.require_capacity(len(payload) * 2)
            for _ in range(100):
                token = uuid4().hex
                reference = f"private/uploads/{token}.{'png' if kind == 'PNG' else 'jpg'}"
                # Commit ownership first. A crash can leave an owned missing file,
                # but never a new successfully written anonymous attachment.
                if campaign_id is not None:
                    with self.storage.connect() as db:
                        db.execute("INSERT INTO file_owners VALUES (?,?)", (reference, campaign_id))
                try:
                    self.storage.write_new(reference, payload)
                    break
                except FileExistsError:
                    self._forget_upload_owner(reference, campaign_id)
                    continue
                except BaseException:
                    # Publication can succeed before a directory-fsync error.
                    # Remove the file before forgetting ownership; if unlink also
                    # fails, retain its owner so campaign deletion can reclaim it.
                    self.storage.path(reference).unlink(missing_ok=True)
                    self._forget_upload_owner(reference, campaign_id)
                    raise
            else:
                raise StorageError("A new upload identity could not be reserved. Try again.")
            try:
                image, _ = load_image(self.storage.path(reference))
                try:
                    dimensions = image.size
                    opaque = (
                        "A" not in image.getbands() or image.getchannel("A").getextrema()[0] == 255
                    )
                    if role == "logo":
                        require_visible_logo(image)
                finally:
                    image.close()
            except Exception:
                self.storage.path(reference).unlink(missing_ok=True)
                self._forget_upload_owner(reference, campaign_id)
                raise
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
        return {
            "reference": reference,
            "width": dimensions[0],
            "height": dimensions[1],
            "type": kind,
            "opaque": opaque,
            "url": f"/uploads/{PurePosixPath(reference).name}",
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

    def _forget_upload_owner(self, reference, campaign_id):
        if campaign_id is not None:
            with self.storage.connect() as db:
                db.execute(
                    "DELETE FROM file_owners WHERE reference=? AND campaign_id=?",
                    (reference, campaign_id),
                )

    def uploaded_path(self, name):
        if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpg)", name):
            raise ActionError("upload", "This uploaded image is unavailable.", 404)
        reference = f"private/uploads/{name}"
        with self.storage.connect() as db:
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
        path = self.storage.path(f"private/brief-drafts/{campaign_id}.json")
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_bytes())
            if document["value"]["campaign_id"] != campaign_id:
                raise ValueError("Campaign identity does not match its saved document")
            return document
        except ValueError, KeyError, TypeError:
            raise ActionError(
                "campaign_data",
                "This campaign’s saved brief could not be read. "
                "Restore its saved data before editing it again.",
            ) from None

    def current_document(self, campaign_id, workspace):
        document = self.read(campaign_id)
        run = workspace.run_for(campaign_id)
        if not document or not run:
            return document
        current = workspace.state.plan(run["id"]).brief.model_dump(mode="json")
        if document.get("planned_run") == run["id"]:
            # Even a first partial revision uses the snapshotted source references.
            # Those path changes are not an unsaved producer edit.
            document["value"] = current
        elif (
            workspace.state.effective_revision(run["id"]) and document.get("base_run") == run["id"]
        ):
            from campaign_studio.planning import Plan

            base_revision = document.get("base_revision")
            if base_revision:
                record = workspace.state.revision(base_revision)
                baseline = Plan.model_validate_json(
                    self.storage.path(record["plan_ref"]).read_bytes()
                ).brief.model_dump(mode="json")
            else:
                baseline = workspace.state.plan(run["id"], current=False).brief.model_dump(
                    mode="json"
                )
            draft = document["value"]
            for field in ("logo_path", "logo_provenance"):
                if draft["brand"].get(field) == baseline["brand"].get(field):
                    draft["brand"][field] = current["brand"].get(field)
            for language, content in draft["markets"].items():
                if language not in baseline["markets"] or language not in current["markets"]:
                    continue
                for field in ("message", "cta"):
                    if content["copy"].get(field) == baseline["markets"][language]["copy"].get(
                        field
                    ):
                        content["copy"][field] = current["markets"][language]["copy"].get(field)
            before = {p["id"]: p for p in baseline["products"]}
            after = {p["id"]: p for p in current["products"]}
            for product in draft["products"]:
                if (
                    product["id"] in before
                    and product["id"] in after
                    and product.get("copy_overrides", {})
                    == before[product["id"]].get("copy_overrides", {})
                ):
                    product["copy_overrides"] = after[product["id"]].get("copy_overrides", {})
                if (
                    product["id"] in before
                    and product["id"] in after
                    and product.get("framing", {}) == before[product["id"]].get("framing", {})
                ):
                    product["framing"] = after[product["id"]].get("framing", {})
        output = workspace.state.current_output(campaign_id)
        output_run = workspace.state.run(output[0]) if output else run
        try:
            manifest = workspace.manifest(output_run)
        except OSError, json.JSONDecodeError:
            # A damaged image record cannot authorize a logo-field replacement.
            # Leave the draft readable for the existing local recovery path.
            manifest = None
        receipt = (manifest or {}).get("edit_receipt", {})
        if (
            "replacement_draft_version" in receipt
            and receipt["replacement_draft_version"] == document["version"]
        ):
            # Explicit campaign-wide replacement supersedes that same logo
            # choice, including a pending Brief choice. A later authored Save
            # has a different token and must keep its newer fields untouched.
            brand = workspace.state.plan(output_run["id"]).brief.model_dump(mode="json")["brand"]
            for field in ("logo_path", "logo_provenance"):
                document["value"]["brand"][field] = copy.deepcopy(brand[field])
                if isinstance(document.get("authored", {}).get("brief", {}).get("brand"), dict):
                    document["authored"]["brief"]["brand"][field] = copy.deepcopy(brand[field])
        return document

    def persist_logo_revision(self, campaign_id, workspace):
        with self.storage.exclusive_draft():
            self._persist_logo_revision(campaign_id, workspace)

    def _persist_logo_revision(self, campaign_id, workspace):
        from campaign_studio.planning import canonical_hash

        # Revision publication is authoritative. The same baseline reconciliation
        # repairs a lost refresh after a crash without discarding unrelated fields.
        previous = self.read(campaign_id)
        current = self.current_document(campaign_id, workspace)
        if current and (
            current["value"] != previous["value"]
            or current.get("authored") != previous.get("authored")
        ):
            previous_authored = previous.get("authored")
            if (
                previous_authored
                and previous.get("planned_run")
                and canonical_hash(previous_authored["brief"])
                == previous.get("planned_authored_hash", canonical_hash(previous["value"]))
            ):
                # A confirmed local replacement changes the admitted logo, not
                # its pinned generation request. Never admit an unrelated edit.
                current["planned_authored_hash"] = canonical_hash(current["authored"]["brief"])
            current["version"] = uuid4().hex
            current["saved_at"] = timestamp()
            run = workspace.run_for(campaign_id)
            current["base_run"] = run["id"]
            current["base_revision"] = workspace.state.effective_revision(run["id"])
            self.storage.write(
                f"private/brief-drafts/{campaign_id}.json",
                json.dumps(current, ensure_ascii=False).encode(),
            )

    def write(self, campaign_id, value, expected, *, attachment_hints=None):
        with self.storage.exclusive_draft():
            return self._write(campaign_id, value, expected, attachment_hints=attachment_hints)

    def create(self, campaign_id, value, *, attachment_hints=None):
        """Publish one new draft and its database identity, without replacing work."""
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id, registered=False)
        if value.get("campaign_id") != campaign_id:
            raise ActionError("campaign_identity", "A campaign’s identity cannot be changed.", 422)
        reference = f"private/brief-drafts/{campaign_id}.json"
        result = {
            "value": value,
            "version": uuid4().hex,
            "saved_at": timestamp(),
        }
        if attachment_hints:
            result["attachment_hints"] = attachment_hints
        published = False
        with self.storage.exclusive_draft():
            try:
                with self.storage.connect() as db:
                    db.execute("INSERT INTO campaigns VALUES (?)", (campaign_id,))
                    self.storage.write_new(
                        reference, json.dumps(result, ensure_ascii=False).encode()
                    )
                    published = True
            except BaseException:
                # Only this call’s newly published file can be rolled back.
                # Pre-existing drafts are protected by write_new's exclusive link.
                if published:
                    self.storage.path(reference).unlink()
                raise
        return result

    def _write(self, campaign_id, value, expected, *, attachment_hints=None):
        previous = self.read(campaign_id)
        if previous and previous["version"] != expected:
            raise ActionError(
                "stale_brief", "This brief changed in another window. Reopen it before saving."
            )
        if value.get("campaign_id") != campaign_id:
            raise ActionError("campaign_identity", "A campaign’s identity cannot be changed.", 422)
        if previous and previous["value"] == value:
            return previous
        result = {"value": value, "version": uuid4().hex, "saved_at": timestamp()}
        # Validated Brief writes must preserve independently saved unfinished
        # form work. The common project Save owns that envelope and its receipt.
        for key in ("authored", "authored_base", "authored_receipt"):
            if previous and key in previous:
                result[key] = copy.deepcopy(previous[key])
        hints = (
            attachment_hints
            if attachment_hints is not None
            else (previous or {}).get("attachment_hints", {})
        )
        if hints:
            result["attachment_hints"] = hints
        writer = self.storage.write if previous else self.storage.write_new
        try:
            writer(
                f"private/brief-drafts/{campaign_id}.json",
                json.dumps(result, ensure_ascii=False).encode(),
            )
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
            if (
                not known
                and not self.storage.path(f"private/brief-drafts/{identity}.json").exists()
            ):
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
            if reference and reference not in allowed:
                if not reference.startswith("private/uploads/"):
                    message = "Choose missing images using the file pickers."
                    raise ActionError(
                        "image", message, 422, field_errors=[field_issue(path, "image", message)]
                    )
                try:
                    with self.storage.connect() as db:
                        owners = {
                            row[0]
                            for row in db.execute(
                                "SELECT campaign_id FROM file_owners WHERE reference=?",
                                (reference,),
                            )
                        }
                    if owners and campaign_id not in owners:
                        raise ActionError(
                            "image",
                            "Attach this image to the selected campaign using its file picker.",
                            422,
                        )
                    self.uploaded_path(reference.removeprefix("private/uploads/"))
                except ActionError as error:
                    raise ActionError(
                        error.code,
                        error.message,
                        error.status,
                        field_errors=[field_issue(path, "image", error.message)],
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
        self.storage.write(
            f"private/brief-drafts/{campaign_id}.json",
            json.dumps(result, ensure_ascii=False).encode(),
        )
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
        fill(candidate["brand"], ("name", "logo_path"))
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
        from pydantic import ValidationError

        from campaign_studio.planning import canonical_hash, make_plan
        from campaign_studio.workspace import validation_messages

        with self.storage.exclusive_run(), self.storage.exclusive_draft():
            document = self.current_document(campaign_id, workspace)
            if not document or document["version"] != expected:
                raise ActionError("stale_brief", "Save the latest brief before reviewing its plan.")
            # Validate the current authored input without publishing a new draft.
            value = document["value"]
            if "authored" in document:
                from campaign_studio.authored import AuthoredProjects

                project = AuthoredProjects(workspace).read(campaign_id)
                document["authored"] = project["authored"]
                authored_hash = canonical_hash(document["authored"]["brief"])
                if (
                    document.get("planned_run")
                    and document.get("planned_authored_hash") == authored_hash
                    and all(
                        document.get("authored_base", {}).get(field) == project["base"][field]
                        for field in ("run_id", "revision_id")
                    )
                ):
                    return workspace.state.plan(document["planned_run"])
                try:
                    Brief.model_validate(document["authored"]["brief"])
                except ValidationError as error:
                    raise ActionError(
                        "brief_fields",
                        "Check the brief: " + "; ".join(validation_messages(error)),
                        422,
                        field_errors=validation_issues(error),
                    ) from error
                run = workspace.run_for(campaign_id)
                value = self._save(
                    campaign_id,
                    BriefDraft(
                        value=document["authored"]["brief"],
                        version=document["version"],
                        base_revision=workspace.state.effective_revision(run["id"])
                        if run
                        else None,
                    ),
                    workspace,
                    persist=False,
                    preserve_ids=True,
                )
            if (
                "authored" not in document
                and document.get("planned_run")
                and value == document["value"]
            ):
                return workspace.state.plan(document["planned_run"])
            try:
                brief = Brief.model_validate(value)
            except ValidationError as error:
                raise ActionError(
                    "brief_fields",
                    "Check the brief: " + "; ".join(validation_messages(error)),
                    422,
                    field_errors=validation_issues(error),
                ) from error
            old_run = workspace.run_for(campaign_id)
            previous_logo = (
                workspace.state.plan(old_run["id"]).brief.brand.logo_path if old_run else None
            )
            brief, fitted = self.fit_replacement_logo(brief, previous_logo)
            plan = make_plan(brief, self.storage)
            if old_run and plan.generation_products:
                plan = self.reuse_unchanged(plan, old_run, workspace)
            if plan.blocked:
                reasons = list(
                    dict.fromkeys(f.evidence for f in plan.findings if f.severity == "blocking")
                )
                raise ActionError(
                    "brief_images",
                    "Check the images or wording: " + " ".join(reasons),
                    422,
                    field_errors=list(
                        {
                            (tuple(field.path), field.code): field.model_dump()
                            for finding in plan.findings
                            if finding.severity == "blocking" and finding.context
                            for field in finding.context.fields
                        }.values()
                    ),
                    proposal={
                        "campaign_id": campaign_id,
                        "version": document["version"],
                        "outputs": [
                            {
                                "id": output.id,
                                "product_id": output.product_id,
                                "market": output.market,
                                "ratio": output.ratio,
                            }
                            for output in plan.outputs
                        ],
                        "findings": [
                            finding.model_dump(mode="json")
                            for finding in plan.findings
                            if finding.severity == "blocking"
                        ],
                    },
                )
            # Reserve ownership and create the run's immutable input snapshot first.
            document["planned_run"] = workspace.state.create(plan, len(plan.generation_products))
            # Store the run's actual input snapshot references, so reads before
            # its first completed revision agree with reads after production.
            plan = workspace.state.plan(document["planned_run"])
            document["value"] = plan.brief.model_dump(mode="json")
            if "authored" in document:
                # Planning snapshots attachments into this run. Update only
                # those owned references; raw fields and pending edits stay exact.
                authored = document["authored"]["brief"]
                authored["brand"]["logo_path"] = plan.brief.brand.logo_path
                if authored.get("replay") is not None and plan.brief.replay is not None:
                    # Explicit saved masters are snapshotted just like supplied
                    # images. Keep their verified path and authored provenance
                    # together, without introducing automatic replay metadata.
                    for product_id, source in authored["replay"]["sources"].items():
                        if product_id in plan.brief.replay.sources:
                            source["path"] = plan.brief.replay.sources[product_id].path
                snapshots = {p.id: p for p in plan.brief.products}
                for product in authored["products"]:
                    source = snapshots[product["id"]].source
                    if source.role != "concept":
                        product["source"]["path"] = source.path
                    for item in fitted:
                        if item["product_id"] == product["id"]:
                            product["framing"][item["market"]][item["ratio"]]["logo_placement"] = (
                                item["placement"]
                            )
                document["authored_base"] = AuthoredProjects(workspace).base(campaign_id)
                document["planned_authored_hash"] = canonical_hash(document["authored"]["brief"])
            # Publish matching draft pointers only after preparation succeeds.
            self.storage.write(
                f"private/brief-drafts/{campaign_id}.json",
                json.dumps(document, ensure_ascii=False).encode(),
            )
        return plan

    def fit_replacement_logo(self, brief, previous_reference):
        """A changed Brief attachment uses the same proportional fit as Studio."""
        if brief.brand.logo_path == previous_reference:
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
        from pydantic import ValidationError

        from campaign_studio.planning import canonical_hash, compare_plans, make_plan
        from campaign_studio.rendering import render_revision

        with self.storage.exclusive_run():
            run = workspace.run_for(campaign_id, run_id)
            if not run or not run["current_revision"]:
                raise ActionError(
                    "recheck", "Create a complete image version before rechecking rules."
                )
            if run["current_revision"] != document.base_revision:
                raise ActionError(
                    "stale_brief", "The images changed. Reopen this brief before rechecking."
                )
            previous = self.current_document(campaign_id, workspace)
            if document.version != (previous["version"] if previous else None):
                raise ActionError(
                    "stale_brief", "This brief changed in another tab. Reopen it before rechecking."
                )
            old = workspace.state.plan(run_id)
            value = copy.deepcopy(document.value)
            prior = old.brief.model_dump(mode="json")
            automatic_replay = False
            if value.get("replay") is None and self._automatic_replay(
                prior, previous, run, workspace
            ):
                value["replay"] = prior["replay"]
                automatic_replay = True
            languages = value.get("markets")
            for language in languages.values() if isinstance(languages, dict) else []:
                if isinstance(language, dict):
                    rules = language.get("phrase_rules")
                    for rule in rules if isinstance(rules, list) else []:
                        if isinstance(rule, dict) and not rule.get("id"):
                            rule["id"] = f"rule-{uuid4().hex}"
            try:
                brief = Brief.model_validate(value)
            except ValidationError as error:
                raise ActionError(
                    "brief_fields",
                    "Check the rule and required wording fields. Your draft is unchanged.",
                    422,
                    field_errors=validation_issues(error),
                ) from error
            candidate = brief.model_dump(mode="json")
            for language in candidate["markets"]:
                if language in prior["markets"]:
                    for field in ("phrase_rules",):
                        candidate["markets"][language][field] = prior["markets"][language][field]
            if candidate != prior:
                raise ActionError(
                    "rule_changes_only",
                    "Recheck applies only rule changes. Your wording, product, or brand edits "
                    "are still here; save and review their production plan first.",
                )
            new = make_plan(brief, self.storage)
            impact = compare_plans(old, new)
            if impact.render_outputs or new.assets != old.assets:
                raise ActionError(
                    "recheck", "A source changed. Restore it before rechecking rules."
                )
            if not impact.recheck_outputs:
                raise ActionError("recheck", "No rule changes to recheck.")
            manifest = render_revision(
                workspace.state,
                run_id,
                new,
                reuse_revision=document.base_revision,
                rule_recheck=True,
            )
            # Authored work may be saved during rendering. Preserve that latest
            # envelope; the runner lock still excludes another canonical apply.
            with self.storage.exclusive_draft():
                latest = self.read(campaign_id)
                result = self._write(
                    campaign_id,
                    new.brief.model_dump(mode="json"),
                    latest["version"] if latest else None,
                )
                result["base_run"] = run_id
                result["base_revision"] = workspace.state.run(run_id)["current_revision"]
                result["planned_run"] = run_id
                if result.get("authored"):
                    from campaign_studio.authored import AuthoredProjects

                    AuthoredProjects(workspace).reconcile_applied_fields(result, result["authored"])
                    captured = (
                        (previous or {}).get("authored", {}).get("brief", {}).get("markets", {})
                    )
                    latest_markets = (
                        (latest or {}).get("authored", {}).get("brief", {}).get("markets", {})
                    )
                    # This command explicitly applies changed rule fields. Its
                    # acknowledgement must advance unchanged saved intent, while
                    # a newer rule edit made during local work remains pending.
                    for market, content in new.brief.markets.items():
                        if content.phrase_rules == old.brief.markets[market].phrase_rules:
                            continue
                        before = captured.get(market)
                        current = latest_markets.get(market)
                        if not isinstance(before, dict) or not isinstance(current, dict):
                            continue
                        target = result["authored"]["brief"]["markets"][market]
                        if current.get("phrase_rules") == before.get("phrase_rules"):
                            target["phrase_rules"] = [
                                r.model_dump(mode="json") for r in content.phrase_rules or []
                            ]
                        elif "phrase_rules" in current:
                            target["phrase_rules"] = copy.deepcopy(current["phrase_rules"])
                        else:
                            target.pop("phrase_rules", None)
                # The same imagery plan remains admitted under its new rules.
                # Distinct authored edits retain a different fingerprint and stay pending.
                result["planned_authored_hash"] = canonical_hash(new.brief.model_dump(mode="json"))
                if automatic_replay and result.get("authored"):
                    raw = result["authored"]["brief"]
                    admitted = new.brief.model_dump(mode="json")
                    admitted["replay"] = None
                    try:
                        matches = Brief.model_validate(raw).model_dump(mode="json") == admitted
                    except ValidationError:
                        matches = False
                    if matches:
                        # Keep the admitted producer representation, not the
                        # internal reuse receipt, as the existing dispatch guard.
                        result["planned_authored_hash"] = canonical_hash(raw)
                self.storage.write(
                    f"private/brief-drafts/{campaign_id}.json",
                    json.dumps(result, ensure_ascii=False).encode(),
                )
            return manifest

    def _automatic_replay(self, prior, document, run, workspace):
        """Recognize omitted local reuse bookkeeping, not an edited source mapping."""
        from campaign_studio.generation import request_for

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
                source_plan = workspace.state.plan(source_run["id"], current=False)
                request = source_plan.generation_requests.get(product_id)
                if (
                    request == request_for(brief, products[product_id])
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
            if plan.generation_requests[product_id] != old_request:
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
