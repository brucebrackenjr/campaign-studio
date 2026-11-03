"""Portable offline review packages with only named, verified, relative files."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from jinja2 import Environment, select_autoescape
from pydantic import ValidationError

from campaign_studio.brief import Copy, PhraseRule
from campaign_studio.costing import campaign_metric_display, compact_usd, incomplete_reason
from campaign_studio.local_files import is_link, publish_file
from campaign_studio.planning import contextualize_findings, copy_findings
from campaign_studio.state import State, StateError, timestamp
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


def zipped_payloads(payloads: dict) -> dict:
    root = json.loads(payloads["manifest.json"])["package_name"]
    return {f"{root}/{name}": data for name, data in payloads.items()}


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
    manifest = json.loads(state.storage.path(revision["manifest_ref"]).read_bytes())
    accepted = {
        market: list(content.phrase_rules or []) for market, content in plan.brief.markets.items()
    }
    pending = []
    path = state.storage.path(state.storage.project(campaign_id).draft)
    if path.is_file():
        document = json.loads(path.read_bytes())
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


def require_export_wording(state: State, campaign_id: str) -> None:
    issue = wording_export_issue(state, campaign_id)
    if issue:
        raise StateError(issue["message"], code=issue["code"])


def public_manifest(state: State, revision_id: str) -> dict:
    revision = state.revision(revision_id)
    manifest = json.loads(state.storage.path(revision["manifest_ref"]).read_bytes())
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


def folder_matches(state, destination, payloads):
    try:
        root = state.storage.path(destination)
        actual = set()

        def unreadable(error):
            raise error

        for directory, names, filenames in os.walk(root, onerror=unreadable):
            base = Path(directory)
            if any(is_link((base / name).lstat()) for name in (*names, *filenames)):
                return False
            actual.update((base / name).relative_to(root).as_posix() for name in filenames)
        return actual == set(payloads) and all(
            state.storage.path(f"{destination}/{name}").read_bytes() == data
            for name, data in payloads.items()
        )
    except OSError, ValueError:
        return False


def export_package(
    state: State,
    revision_id: str,
    destination: str,
    *,
    allow_partial: bool = False,
    expected_snapshot: str | None = None,
    payloads: dict | None = None,
) -> str:
    project = state.storage.project(state.run(state.revision(revision_id)["run_id"])["campaign_id"])
    if not destination.startswith(project.ref("Exports") + "/"):
        raise StateError("Choose an export location inside this campaign’s Exports folder.")
    target = state.storage.path(project.check(destination))
    if target.exists():
        raise StateError("Choose a new export directory; existing packages are preserved.")
    payloads = (
        payloads
        if payloads is not None
        else package_payloads(
            state, revision_id, allow_partial=allow_partial, expected_snapshot=expected_snapshot
        )
    )
    if "report_created_at" not in json.loads(payloads["manifest.json"]):
        payloads = dated_payloads(state, revision_id, payloads)
    state.storage.require_capacity(
        sum(map(len, payloads.values())) + max(map(len, payloads.values()), default=0) + 1_000_000
    )
    project.claim(destination)
    target.mkdir(parents=True, mode=0o700)
    for reference, payload in payloads.items():
        project.write(f"{destination}/{reference}", payload)
    return f"{destination}/index.html"


def require_snapshot(manifest: dict, expected_snapshot: str | None) -> None:
    if (
        expected_snapshot is not None
        and manifest["producer_metadata_snapshot"]["snapshot_id"] != expected_snapshot
    ):
        raise StateError("This version changed in another tab. Reload it before exporting.")


def package_payloads(
    state: State,
    revision_id: str,
    *,
    allow_partial: bool = False,
    expected_snapshot: str | None = None,
) -> dict:
    """One snapshot and one allowlisted representation shared by folder and ZIP exports."""
    state.require_images(revision_id)
    revision = state.revision(revision_id)
    require_export_wording(state, state.run(revision["run_id"])["campaign_id"])
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
    expected = {
        item.id: f"images/{item.product_id}/{item.ratio.replace(':', 'x')}/{item.market}.png"
        for item in plan.outputs
    }
    if sorted(item["id"] for item in manifest["outputs"]) != sorted(expected):
        raise StateError("The saved manifest does not match its planned creative set.")
    payloads = {}
    delivered = delivery_paths(manifest)
    for item in manifest["outputs"]:
        if item["file"]:
            if item["file"] != expected[item["id"]]:
                raise StateError("A saved image has an unsafe or unexpected package path.")
            payload = state.storage.path(f"{source_prefix}/{item['file']}").read_bytes()
            if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                raise StateError("A saved output changed; recover the revision before exporting.")
            item["file"] = delivered[item["id"]]
            payloads[item["file"]] = payload
    signature = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]
    manifest["package_name"] = f"{delivery_component(plan.brief.brand.name, 'Brand')}-{signature}"
    payloads["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    # Publishing index last avoids a complete-looking overview after a partial local write.
    payloads["index.html"] = render_report(state, revision, manifest)
    return payloads


def render_report(state, revision, manifest):
    """Render the same safe report for fresh exports and exact snapshot reuse."""
    revision_id = manifest["revision_id"]
    plan = state.revision_plan(revision)
    template = (
        files("campaign_studio").joinpath("templates/offline.html").read_text(encoding="utf-8")
    )
    with state.storage.connect() as db:
        revision_number = db.execute(
            "SELECT COUNT(*) FROM revisions r JOIN runs ON runs.id=r.run_id "
            "WHERE runs.campaign_id=? AND r.rowid <= "
            "(SELECT rowid FROM revisions WHERE id=?)",
            (manifest["campaign_id"], revision_id),
        ).fetchone()[0]
    html = (
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
            brand_name=plan.brief.brand.name,
            revision_number=revision_number,
            languages={"en-US": "English (United States)", "es-MX": "Spanish (Mexico)"},
            sizes={"1:1": "Instagram post", "9:16": "Story", "16:9": "Facebook post"},
        )
    )
    return html.encode()


def dated_payloads(state, revision_id, payloads, created_at=None):
    """Reuse verified image bytes; only the report and manifest carry creation time."""
    created_at = created_at or timestamp()
    if datetime.fromisoformat(created_at).tzinfo is None:
        raise ValueError("A report creation time must include its timezone.")
    manifest = json.loads(payloads["manifest.json"])
    manifest["report_created_at"] = created_at
    return {
        **payloads,
        "manifest.json": json.dumps(manifest, indent=2, ensure_ascii=False).encode(),
        "index.html": render_report(state, state.revision(revision_id), manifest),
    }


def export_zip(
    state: State,
    revision_id: str,
    destination: str,
    *,
    allow_partial: bool = False,
    expected_snapshot: str | None = None,
    payloads: dict | None = None,
) -> str:
    """Publish a new ZIP atomically. No directory traversal or recursive collection."""
    project = state.storage.project(state.run(state.revision(revision_id)["run_id"])["campaign_id"])
    if not destination.startswith(project.ref("Exports") + "/"):
        raise StateError("Choose an export location inside this campaign’s Exports folder.")
    target = state.storage.path(project.check(destination))
    if target.suffix != ".zip":
        raise StateError("Choose a ZIP filename.")
    if target.exists():
        raise StateError("Choose a new ZIP filename; existing packages are preserved.")
    payloads = (
        payloads
        if payloads is not None
        else package_payloads(
            state, revision_id, allow_partial=allow_partial, expected_snapshot=expected_snapshot
        )
    )
    if "report_created_at" not in json.loads(payloads["manifest.json"]):
        payloads = dated_payloads(state, revision_id, payloads)
    state.storage.require_capacity(
        sum(map(len, payloads.values())) + max(map(len, payloads.values()), default=0) + 1_000_000
    )
    project.claim(destination)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary, registered = state.storage.temporary_file(destination)
    created = False
    try:
        with temporary.open("xb") as stream:
            created = True
            os.chmod(temporary, 0o600)
            with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
                for reference, payload in zipped_payloads(payloads).items():
                    archive.writestr(reference, payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            publish_file(temporary, target, replace=False)
        except FileExistsError as error:
            raise StateError(
                "Choose a new ZIP filename; existing packages are preserved."
            ) from error
    finally:
        if created:
            temporary.unlink(missing_ok=True)
        state.storage.finish_temporary(temporary, registered)
    return destination
