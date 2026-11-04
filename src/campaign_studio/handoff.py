"""Selected-revision metadata and registered ZIP artifacts; no production dispatch."""

import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile

from pydantic import Field

from campaign_studio.brief import Contract
from campaign_studio.exporting import (
    dated_payloads,
    export_package,
    export_zip,
    folder_matches,
    package_payloads,
    require_export_rules,
    zipped_payloads,
)
from campaign_studio.local_files import display_path
from campaign_studio.review import CreativeUpdate, RevisionTarget
from campaign_studio.state import State, StateError


class MetadataAction(CreativeUpdate):
    mode: Literal["current", "saved_version"]


class PackageAction(Contract):
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_partial: bool = Field(default=False, strict=True)


class Handoff:
    def __init__(self, state: State):
        self.state = state

    def metadata(self, target: RevisionTarget) -> dict:
        return self.state.metadata_snapshot(target.revision_id, target=target)

    def update(self, target: RevisionTarget, creative_id: str, action: MetadataAction) -> dict:
        update = CreativeUpdate.model_validate(
            action.model_dump(exclude={"mode"}, exclude_unset=True)
        )
        return self.state.update_creative(
            target.revision_id, creative_id, update, target=target, include_snapshot=True
        )

    @staticmethod
    def package_summary(target, action, manifest):
        return {
            "revision_id": target.revision_id,
            "snapshot_id": action.snapshot_id,
            "complete": manifest["complete"],
            "included": sum(bool(item["file"]) for item in manifest["outputs"]),
            "omitted": sum(not item["file"] for item in manifest["outputs"]),
        }

    def require_snapshot(self, target, action):
        if self.metadata(target)["snapshot_id"] != action.snapshot_id:
            raise StateError("This version changed in another tab. Reload it before exporting.")

    def create_zip(self, target: RevisionTarget, action: PackageAction) -> dict:
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            self.state.require_current_output(target)
            require_export_rules(self.state, target.campaign_id, target.revision_id)
            # Publication cannot race this lock. The selected output may predate
            # an unstarted plan, while its metadata snapshot remains authoritative.
            target = target.model_copy(update={"mode": "saved_version"})
            self.state.require_images(target.revision_id)
            self.metadata(target)
            payloads = package_payloads(
                self.state,
                target.revision_id,
                allow_partial=action.allow_partial,
                expected_snapshot=action.snapshot_id,
            )
            manifest = json.loads(payloads["manifest.json"])
            result = {
                **self.package_summary(target, action, manifest),
                "filename": manifest["package_name"] + ".zip",
            }
            # A snapshot alone is insufficient: the report format can change.
            # Compare the exact safe entries, including the newly rendered HTML.
            for package in self.state.packages_for_snapshot(target, action.snapshot_id):
                try:
                    path = self.state.storage.path(package["reference"])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != package["sha256"]:
                        continue
                    with ZipFile(path) as archive:
                        root = manifest["package_name"]
                        created_at = json.loads(archive.read(f"{root}/manifest.json"))[
                            "report_created_at"
                        ]
                        expected = dated_payloads(
                            self.state, target.revision_id, payloads, created_at
                        )
                        expected = zipped_payloads(expected)
                        intact = sorted(archive.namelist()) == sorted(expected) and all(
                            archive.read(name) == data for name, data in expected.items()
                        )
                    if intact:
                        self.require_snapshot(target, action)
                        return {**result, "artifact_id": package["id"]}
                except OSError, ValueError, TypeError, KeyError, BadZipFile:
                    continue
            for _ in range(100):
                reference = self.state.storage.project(target.campaign_id).ref(
                    f"Exports/archives/{uuid4().hex}.zip"
                )
                if not self.state.storage.path(reference).exists():
                    break
            else:
                raise StateError("A new package identity could not be reserved. Try again.")
            published = False
            self.require_snapshot(target, action)
            try:
                export_zip(
                    self.state,
                    target.revision_id,
                    reference,
                    payloads=dated_payloads(self.state, target.revision_id, payloads),
                )
                published = True
                path = self.state.storage.path(reference)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                artifact_id = self.state.register_package(
                    target, reference, digest, action.snapshot_id
                )
            except BaseException:
                if published:
                    self.state.storage.path(reference).unlink(missing_ok=True)
                raise
            return {**result, "artifact_id": artifact_id}

    def create_folder(self, target: RevisionTarget, action: PackageAction) -> dict:
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            self.state.require_current_output(target)
            require_export_rules(self.state, target.campaign_id, target.revision_id)
            target = target.model_copy(update={"mode": "saved_version"})
            self.state.require_images(target.revision_id)
            self.metadata(target)
            payloads = package_payloads(
                self.state,
                target.revision_id,
                allow_partial=action.allow_partial,
                expected_snapshot=action.snapshot_id,
            )
            # This identifies the complete sanitized report, not just its pixels.
            name = json.loads(payloads["manifest.json"])["package_name"]
            base = self.state.storage.project(target.campaign_id).ref(f"Exports/{name}")
            for suffix in range(100):
                destination = base if suffix == 0 else f"{base}-{suffix}"
                try:
                    saved = json.loads(
                        self.state.storage.path(f"{destination}/manifest.json").read_bytes()
                    )
                    expected = dated_payloads(
                        self.state, target.revision_id, payloads, saved["report_created_at"]
                    )
                    intact = folder_matches(self.state, destination, expected)
                except OSError, ValueError, TypeError, KeyError:
                    intact = False
                self.require_snapshot(target, action)
                if intact:
                    break
                if not self.state.storage.path(destination).exists():
                    export_package(self.state, target.revision_id, destination, payloads=payloads)
                    break
            else:
                raise StateError("Existing package copies need attention before creating another.")
            reference = f"{destination}/index.html"
            digest = hashlib.sha256(
                self.state.storage.path(f"{destination}/manifest.json").read_bytes()
                + self.state.storage.path(reference).read_bytes()
            ).hexdigest()
            report_id = self.state.report_for_reference(reference, digest)
            if report_id is None:
                report_id = self.state.register_package(
                    target, reference, digest, action.snapshot_id
                )
            return {
                **self.package_summary(target, action, json.loads(payloads["manifest.json"])),
                "location": display_path(self.state.storage.path(destination)),
                "url": f"/reports/{report_id}/index.html",
            }

    def report_file(self, report_id: str, reference: str) -> bytes:
        missing = StateError(
            "This report is unavailable. Open Handoff to create a report.",
            code="report_not_found",
        )
        try:
            if str(UUID(report_id)) != report_id:
                raise missing
        except ValueError:
            raise missing from None
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            report = self.state.report(report_id)
            if report is None:
                raise missing
            self.state.run(report["run_id"])
            require_export_rules(self.state, report["campaign_id"], report["revision_id"])
            prefix = report["reference"].rsplit("/", 1)[0]
            try:
                raw = self.state.storage.path(f"{prefix}/manifest.json").read_bytes()
                html = self.state.storage.path(report["reference"]).read_bytes()
                manifest = json.loads(raw)
                intact = (
                    hashlib.sha256(raw + html).hexdigest() == report["sha256"]
                    and manifest["campaign_id"] == report["campaign_id"]
                    and manifest["revision_id"] == report["revision_id"]
                    and manifest["producer_metadata_snapshot"]["snapshot_id"]
                    == report["snapshot_id"]
                )
            except OSError, ValueError, KeyError, TypeError:
                intact = False
            if not intact:
                raise StateError(
                    "This saved report is missing or changed. Create it again in Handoff."
                )
            if reference == "index.html":
                return html
            if reference == "manifest.json":
                return raw
            image = next((item for item in manifest["outputs"] if item["file"] == reference), None)
            if image is None:
                raise missing
            try:
                payload = self.state.storage.path(f"{prefix}/{reference}").read_bytes()
                intact = hashlib.sha256(payload).hexdigest() == image["sha256"]
            except OSError:
                intact = False
            if not intact:
                raise StateError(
                    "A report image is missing or changed. Create the report again in Handoff."
                )
            return payload

    def download(self, target: RevisionTarget, artifact_id: str):
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            require_export_rules(self.state, target.campaign_id, target.revision_id)
            return self._download(target, artifact_id)

    def _download(self, target: RevisionTarget, artifact_id: str):
        package = self.state.package(target, artifact_id)
        path = self.state.storage.path(package["reference"])
        try:
            payload = path.read_bytes()
            intact = hashlib.sha256(payload).hexdigest() == package["sha256"]
        except OSError:
            intact = False
        if not intact:
            raise StateError(
                "This ZIP package is missing or changed. "
                "Create a new package from the saved version."
            )
        # Serve the very bytes verified above, without reopening a mutable path later.
        return payload
