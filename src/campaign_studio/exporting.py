"""Portable offline review packages with only named, verified, relative files."""

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import BinaryIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from jinja2 import Environment, select_autoescape
from pydantic import ValidationError

from campaign_studio.brief import Copy, PhraseRule
from campaign_studio.costing import campaign_metric_display, compact_usd, incomplete_reason
from campaign_studio.planning import contextualize_findings, copy_findings
from campaign_studio.projects import matches_output_file
from campaign_studio.state import State, StateError
from campaign_studio.storage import delivery_component

PRIVATE_METADATA = re.compile(
    r"/Users/|/home/|-----BEGIN .*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{24,}"
    r"|(?i:\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/])"
    r"|\\\\[\w.-]+\\[^\s\\\"]+"
    r"|(?<![\w:/])//[\w.-]+/[^\s/\"]+"
)


def delivery_paths(manifest: dict) -> dict[str, str]:
    names = {
        identifier: delivery_component(name, "Product")
        for identifier, name in manifest["product_names"].items()
    }
    counts = {
        name.casefold(): sum(n.casefold() == name.casefold() for n in names.values())
        for name in names.values()
    }
    used = set()
    for identifier in sorted(names):
        original = names[identifier]
        collision = counts[original.casefold()] > 1
        name = original
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        length = 8
        while collision or name.casefold() in used:
            name = f"{original} - {digest[:length]}"
            length += 4
            collision = False
        names[identifier] = name
        used.add(name.casefold())
    markets = {"en-US": "English (United States)", "es-MX": "Spanish (Mexico)"}
    formats = {"1:1": "Square", "9:16": "Story", "16:9": "Landscape"}
    return {
        item["id"]: (
            f"images/{names[item['product_id']]}/{markets[item['market']]}/"
            f"{formats[item['ratio']]} - {item['width']}x{item['height']}.png"
        )
        for item in manifest["outputs"]
    }


def wording_export_issue(state: State, campaign_id: str) -> dict | None:
    """Check accepted printed text and saved rule intent, never unfinished copy.

    Action callers hold runner then draft locks through delivery. The UI may use
    the same calculation as an advisory snapshot; it cannot authorize a download.
    """
    current = state.current_output(campaign_id)
    if current is None:
        return None
    revision = state.revision(current[1])
    plan = state.revision_plan(revision)
    manifest = state.revision_manifest(revision)
    accepted = {
        market: list(content.phrase_rules or []) for market, content in plan.brief.markets.items()
    }
    pending = []
    from campaign_studio.campaign_drafts import read as read_draft

    document = read_draft(state.storage, campaign_id)
    if document is not None:
        authored = document.get("authored") or {"brief": document["value"]}
        markets = authored.get("brief", {}).get("markets")
        for market, rules in accepted.items():
            try:
                if not isinstance(markets, dict) or not isinstance(markets.get(market), dict):
                    raise ValueError("Invalid market rules")
                raw = markets[market].get("phrase_rules")
                if raw is not None and not isinstance(raw, list):
                    raise ValueError("Invalid rule list")
                if [PhraseRule.model_validate(rule) for rule in raw or []] != rules:
                    pending.append({"market": market, "rule_index": None})
            except KeyError, TypeError, ValueError, ValidationError:
                pending.append({"market": market, "rule_index": None})
        fields = authored.get("form_drafts", {}).get("fields", {})
        for name, values in fields.items() if isinstance(fields, dict) else []:
            if not isinstance(name, str) or not name.startswith("rule/"):
                continue
            market = None
            try:
                _, language, index = name.split("/")
                market = {"en": "en-US", "es": "es-MX"}[language]
                if not isinstance(values, dict):
                    raise ValueError("Invalid saved rule fields")
                target = {"market": market, "rule_index": int(index)}
                rule = accepted[market][int(index)] if int(index) >= 0 else None
                if rule is None or values.get("mw-rule-phrase") != rule.phrase:
                    pending.append(target)
            except KeyError, IndexError, TypeError, ValueError:
                pending.append({"market": market, "rule_index": None})
    # Include withheld outputs as well as available files. Exporting a subset
    # must never turn an unresolved prohibited match into an allowed campaign.
    printed = {item["id"]: item for item in manifest["outputs"]}
    matches = []
    for output in plan.outputs:
        actual = output.model_copy(update={"text": Copy.model_validate(printed[output.id]["copy"])})
        matches.extend(f for f in copy_findings(plan.brief, actual) if f.severity == "blocking")
    contextualize_findings(plan.brief, plan.outputs, matches)
    if matches:
        return {
            "code": "prohibited_phrase",
            "message": (
                "Correct the prohibited wording, or edit or remove its rule, "
                "before exporting this campaign."
            ),
            "matches": [match.model_dump(mode="json") for match in matches],
            "pending_rules": bool(pending),
            "pending_rule_targets": pending,
        }
    if pending:
        return {
            "code": "pending_rules",
            "message": (
                "Apply or correct the saved prohibited word changes before exporting this campaign."
            ),
            "matches": [],
            "pending_rules": True,
            "pending_rule_targets": pending,
        }
    return None


def logo_export_issue(state: State, campaign_id: str, revision_id: str | None = None):
    current = state.current_output(campaign_id)
    if current is None:
        return None
    accepted = state.revision_plan(state.revision(revision_id or current[1]))
    required = accepted.brief.brand.require_logo
    from campaign_studio.campaign_drafts import read as read_draft

    document = read_draft(state.storage, campaign_id)
    if document is not None:
        authored = document.get("authored") or {"brief": document["value"]}
        required = authored.get("brief", {}).get("brand", {}).get("require_logo", False)
    if required and "logo" not in accepted.assets:
        return {
            "code": "logo_presence",
            "message": "A logo is required. Attach and apply a logo, or turn off the requirement.",
        }
    return None


def require_export_rules(state: State, campaign_id: str, revision_id: str | None = None) -> None:
    issue = wording_export_issue(state, campaign_id) or logo_export_issue(
        state, campaign_id, revision_id
    )
    if issue:
        raise StateError(issue["message"], code=issue["code"])


def public_manifest(state: State, revision_id: str) -> dict:
    revision = state.revision(revision_id)
    manifest = state.revision_manifest(revision)
    # Lost-response reconciliation belongs to the local editor, not its handoff.
    manifest.pop("edit_receipt", None)
    plan = state.revision_plan(revision)
    # Old exported folders stay immutable; a new report format gets a new package.
    manifest["report_format_version"] = 9
    manifest["campaign_metrics"] = state.campaign_metrics(manifest["campaign_id"])
    manifest["product_names"] = {product.id: product.name for product in plan.brief.products}
    manifest["logo_provenance"] = plan.brief.brand.logo_provenance.model_dump()
    metadata = state.metadata_snapshot(revision_id)
    manifest["producer_metadata_snapshot"] = {
        "schema_version": metadata["schema_version"],
        "snapshot_id": metadata["snapshot_id"],
        "revision_id": revision_id,
    }
    for output in manifest["outputs"]:
        value = metadata["creatives"][output["id"]]
        output["producer_metadata"] = {
            key: value[key] for key in ("version", "updated_at", "inherited_from", "reset_reason")
        }
        output["accessible_description"] = value["description"]
        output["copy_provenance"] = {"origin": plan.brief.copy_provenance.model_dump()}
    return manifest


def require_snapshot(manifest: dict, expected_snapshot: str | None) -> None:
    if (
        expected_snapshot is not None
        and manifest["producer_metadata_snapshot"]["snapshot_id"] != expected_snapshot
    ):
        raise StateError("This version changed in another tab. Reload it before exporting.")


def package_plan(
    state: State,
    revision_id: str,
    *,
    allow_partial: bool = False,
    expected_snapshot: str | None = None,
):
    """Shared contents for the browser preview and the downloadable ZIP."""
    state.require_images(revision_id)
    revision = state.revision(revision_id)
    require_export_rules(state, state.run(revision["run_id"])["campaign_id"], revision_id)
    manifest = public_manifest(state, revision_id)
    require_snapshot(manifest, expected_snapshot)
    if not manifest["complete"] and not allow_partial:
        raise StateError("This revision is partial; explicitly choose a partial package.")
    if not any(item["file"] for item in manifest["outputs"]):
        raise StateError("There are no valid images to export.")
    serialized = json.dumps(manifest, indent=2, ensure_ascii=False)
    if PRIVATE_METADATA.search(serialized.replace("\\\\", "\\")):
        raise StateError(
            "Export metadata contains private-looking content; correct its provenance first."
        )
    source_prefix = revision["manifest_ref"].rsplit("/", 1)[0]
    plan = state.revision_plan(revision)
    expected = {item.id: item for item in plan.outputs}
    if sorted(item["id"] for item in manifest["outputs"]) != sorted(expected):
        raise StateError("The saved manifest does not match its planned creative set.")
    images = []
    delivered = delivery_paths(manifest)
    for item in manifest["outputs"]:
        if item["file"]:
            output = expected[item["id"]]
            if not matches_output_file(
                item["file"], output.product_id, output.ratio, output.market
            ):
                raise StateError("A saved image has an unsafe or unexpected package path.")
            reference = f"{source_prefix}/{item['file']}"
            item["file"] = delivered[item["id"]]
            images.append((item["file"], reference, item["sha256"]))
    signature = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]
    manifest["package_name"] = f"{delivery_component(plan.brief.brand.name, 'Brand')}-{signature}"
    return revision, manifest, images


def verify_image(storage, reference, expected_digest, *, copy_to=None):
    """Check the saved PNG in bounded chunks, optionally copying it into the handoff."""
    digest = hashlib.sha256()
    try:
        with storage.path(reference).open("rb") as source:
            while chunk := source.read(256 * 1024):
                digest.update(chunk)
                if copy_to is not None:
                    copy_to.write(chunk)
    except FileNotFoundError as error:
        raise StateError(
            "A saved output is missing; recover the revision before exporting."
        ) from error
    if digest.hexdigest() != expected_digest:
        raise StateError("A saved output changed; recover the revision before exporting.")


@dataclass
class ZipDownload:
    filename: str
    stream: BinaryIO
    size: int
    on_close: Callable[[], None] | None = None

    def close(self):
        try:
            self.stream.close()
        finally:
            if self.on_close:
                self.on_close()
                self.on_close = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def prepare_zip_download(state, revision_id, *, allow_partial=False, expected_snapshot=None):
    """Build and verify a complete attachment before any of its bytes are sent."""
    revision, manifest, images = package_plan(
        state, revision_id, allow_partial=allow_partial, expected_snapshot=expected_snapshot
    )
    root = manifest["package_name"]
    created = datetime.now(UTC)
    manifest["report_created_at"] = created.isoformat()
    zip_date = created.astimezone().timetuple()[:6]
    # The browser can take its time downloading after the save locks are released.
    # Large downloads spill to an OS temporary file, which closes with this response.
    stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
            for name, reference, digest in images:
                # PNG is already compressed. Give the entry its real local creation time.
                with archive.open(ZipInfo(f"{root}/{name}", zip_date), "w") as entry:
                    verify_image(state.storage, reference, digest, copy_to=entry)
            for name in ("manifest.json", "index.html"):
                entry = ZipInfo(f"{root}/{name}", zip_date)
                archive.writestr(
                    entry,
                    report_document(state, revision, manifest, name),
                    compress_type=ZIP_DEFLATED,
                )
        size = stream.tell()
        stream.seek(0)
        return ZipDownload(f"{root}.zip", stream, size)
    except BaseException:
        stream.close()
        raise


def report_document(state, revision, manifest, name):
    if name == "manifest.json":
        return json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    if name == "index.html":
        return render_report(state, revision, manifest)
    raise ValueError("Unknown report document")


def render_report(state, revision, manifest):
    """Render a legacy saved package with its original revision identity."""
    plan = state.revision_plan(revision)
    return render_manifest_report(
        plan.brief.brand.name, manifest, state.revision_number(manifest["revision_id"])
    )


def render_manifest_report(brand_name, manifest, revision_number=None):
    """One report layout for captured current compositions and legacy deliveries."""
    template = (
        files("campaign_studio").joinpath("templates/offline.html").read_text(encoding="utf-8")
    )
    return (
        Environment(autoescape=select_autoescape(default_for_string=True))
        .from_string(template)
        .render(
            campaign=manifest,
            campaign_display=campaign_metric_display(manifest["campaign_metrics"]),
            compact_usd=compact_usd,
            incomplete_reason=incomplete_reason,
            report_created_label=(
                datetime.fromisoformat(manifest["report_created_at"])
                .astimezone(UTC)
                .strftime("%Y-%m-%d %H:%M:%S UTC")
                if manifest.get("report_created_at")
                else None
            ),
            brand_name=brand_name,
            revision_number=revision_number,
            languages={"en-US": "English (United States)", "es-MX": "Spanish (Mexico)"},
            sizes={"1:1": "Instagram post", "9:16": "Story", "16:9": "Facebook post"},
        )
        .encode()
    )
