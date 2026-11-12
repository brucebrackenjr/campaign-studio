"""Review metadata, browser downloads, live previews, and earlier saved packages."""

import hashlib
import json
import threading
from datetime import UTC, datetime
from io import BytesIO
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from campaign_studio.brief import Contract
from campaign_studio.exporting import (
    ZipDownload,
    package_plan,
    prepare_zip_download,
    report_document,
    require_export_rules,
    verify_image,
)
from campaign_studio.planning import canonical_hash
from campaign_studio.review import CreativeUpdate, RevisionTarget
from campaign_studio.state import State, StateError


class MetadataAction(CreativeUpdate):
    mode: Literal["current", "saved_version"]


class PackageAction(Contract):
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_partial: bool = Field(default=False, strict=True)


class DeliveryAction(Contract):
    version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    operation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    allow_partial: bool = Field(default=False, strict=True)
    output_ids: list[str] | None = Field(default=None, min_length=1, max_length=30)

    @field_validator("output_ids")
    @classmethod
    def unique_selection(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Choose each image only once.")
        return sorted(value) if value is not None else None


class Handoff:
    def __init__(self, state: State):
        self.state = state

    def current_snapshot(self, campaign_id, version):
        from campaign_studio.workspace import Workspace

        return Workspace(self.state).current_composition(campaign_id, expected_version=version)

    def _jobs(self):
        return self.state.__dict__.setdefault("delivery_jobs", {})

    def _job_guard(self):
        return self.state.__dict__.setdefault("delivery_guard", threading.RLock())

    def current_progress(self, campaign_id, operation_id):
        job = self._jobs().get(operation_id)
        if job is None or job.campaign_id != campaign_id or job.closed:
            return {"stage": None, "status": "unavailable"}
        return job.view()

    def cancel_current_delivery(self, campaign_id, operation_id):
        job = self._jobs().get(operation_id)
        if job is not None and job.campaign_id == campaign_id:
            job.cancel()
        return {"status": "cancelled"}

    def create_current_zip(self, campaign_id, action: DeliveryAction):
        from campaign_studio.campaign_drafts import read as read_draft
        from campaign_studio.cleanup import require_campaign_available
        from campaign_studio.delivery_jobs import DeliveryJob

        operation_id = action.operation_id or uuid4().hex
        jobs = self._jobs()
        with self._job_guard():
            job = jobs.get(operation_id)
            try:
                require_campaign_available(self.state.storage, campaign_id)
            except StateError:
                if job is not None and job.campaign_id == campaign_id:
                    job.cancel()
                raise
            if job is not None:
                if job.campaign_id != campaign_id:
                    raise StateError("This download belongs to another campaign.")
                if job.active or job.status == "ready":
                    raise StateError(
                        "This download is already being prepared or sent.", code="busy"
                    )
                current = read_draft(self.state.storage, campaign_id)
                if (
                    not current
                    or current["version"] != job.snapshot["version"]
                    or action.version != job.snapshot["version"]
                    or action.allow_partial != job.allow_partial
                    or action.output_ids != job.output_ids
                ):
                    job.close()
                    raise StateError(
                        "This campaign changed after the download started. Start a new download "
                        "to use the current saved settings.",
                        code="composition_conflict",
                    )
            else:
                for previous in list(jobs.values()):
                    if previous.active or previous.status == "ready":
                        raise StateError(
                            "Another download is still being prepared or sent. Try again shortly.",
                            code="busy",
                        )
                    previous.close()
                snapshot = self.current_snapshot(campaign_id, action.version)
                job = DeliveryJob(
                    self.state,
                    snapshot,
                    operation_id,
                    allow_partial=action.allow_partial,
                    output_ids=action.output_ids,
                    on_close=lambda finished: jobs.pop(finished.operation_id, None),
                )
                jobs[operation_id] = job
        return job.run()

    def create_current_preview(self, campaign_id, action: DeliveryAction):
        from campaign_studio.delivery import delivery_manifest

        if action.output_ids is not None:
            raise StateError(
                "The online preview shows the whole campaign. Download selected images "
                "for a report of your selection.",
                code="export_selection",
            )
        snapshot = self.current_snapshot(campaign_id, action.version)
        manifest = delivery_manifest(snapshot, allow_partial=action.allow_partial)
        created = int(datetime.now(UTC).timestamp())
        fingerprint = self.preview_fingerprint(manifest, created)
        return {
            "url": (
                f"/reports/current/{campaign_id}/{action.version}/"
                f"{int(action.allow_partial)}/{created}/{fingerprint}/index.html"
            )
        }

    def current_preview_file(
        self, campaign_id, version, allow_partial, created, fingerprint, reference
    ):
        from campaign_studio.delivery import current_image, delivery_manifest, document

        snapshot = self.current_snapshot(campaign_id, version)
        manifest = delivery_manifest(snapshot, allow_partial=allow_partial)
        if self.preview_fingerprint(manifest, created) != fingerprint:
            raise StateError(
                "This campaign changed. Reopen the report from Handoff to see the current images.",
                code="stale_report",
            )
        manifest["preview"] = True
        if reference in {"index.html", "manifest.json"}:
            return document(snapshot, manifest, reference)
        for item in manifest["outputs"]:
            if item["file"] == reference:
                return current_image(self.state, snapshot, item["id"], preview=True)[0]
        raise StateError("This file is not part of the report.", code="report_not_found")

    def current_png(self, campaign_id, output_id, version):
        from campaign_studio.delivery import current_image

        # Detached-upload cleanup must not remove an input between this export's
        # saved-version check and its capture of the actual source bytes.
        with self._job_guard():
            payload, filename = current_image(
                self.state, self.current_snapshot(campaign_id, version), output_id
            )
        return ZipDownload(filename, BytesIO(payload), len(payload))

    def metadata(self, target: RevisionTarget) -> dict:
        return self.state.metadata_snapshot(target.revision_id, target=target)

    def update(self, target: RevisionTarget, creative_id: str, action: MetadataAction) -> dict:
        update = CreativeUpdate.model_validate(
            action.model_dump(exclude={"mode"}, exclude_unset=True)
        )
        return self.state.update_creative(
            target.revision_id, creative_id, update, target=target, include_snapshot=True
        )

    def require_snapshot(self, target, action):
        if self.metadata(target)["snapshot_id"] != action.snapshot_id:
            raise StateError("This version changed in another tab. Reload it before exporting.")

    def create_zip(self, target: RevisionTarget, action: PackageAction) -> ZipDownload:
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            self.state.require_current_output(target)
            download = prepare_zip_download(
                self.state,
                target.revision_id,
                allow_partial=action.allow_partial,
                expected_snapshot=action.snapshot_id,
            )
            # Descriptions can change while PNGs are read. Discard an out-of-date
            # package before returning an independent stream to the browser.
            try:
                self.require_snapshot(target.model_copy(update={"mode": "saved_version"}), action)
            except BaseException:
                download.close()
                raise
            return download

    def create_preview(self, target: RevisionTarget, action: PackageAction) -> dict:
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            self.state.require_current_output(target)
            _, manifest, images = package_plan(
                self.state,
                target.revision_id,
                allow_partial=action.allow_partial,
                expected_snapshot=action.snapshot_id,
            )
            # Verify all the PNGs once before offering a complete report. Each GET
            # subsequently verifies only the file it will actually serve.
            for _, reference, digest in images:
                verify_image(self.state.storage, reference, digest)
            created = int(datetime.now(UTC).timestamp())
            fingerprint = self.preview_fingerprint(manifest, created)
            self.require_snapshot(target.model_copy(update={"mode": "saved_version"}), action)
            url = (
                f"/reports/preview/{target.campaign_id}/{target.run_id}/{target.revision_id}/"
                f"{int(action.allow_partial)}/{created}/{fingerprint}/index.html"
            )
            return {"url": url}

    @staticmethod
    def preview_fingerprint(manifest: dict, created: int) -> str:
        manifest["report_created_at"] = datetime.fromtimestamp(created, UTC).isoformat()
        return canonical_hash(manifest)

    def preview_file(self, target, allow_partial, created, fingerprint, reference):
        stale = StateError(
            "This campaign changed. Reopen the report from Handoff to see the current images.",
            code="stale_report",
        )
        with self.state.storage.exclusive_run(), self.state.storage.exclusive_draft():
            if self.state.current_output(target.campaign_id) != (target.run_id, target.revision_id):
                raise stale
            revision, manifest, images = package_plan(
                self.state, target.revision_id, allow_partial=allow_partial
            )
            if self.preview_fingerprint(manifest, created) != fingerprint:
                raise stale
            if reference in {"index.html", "manifest.json"}:
                return report_document(self.state, revision, manifest, reference)
            for name, path, digest in images:
                if name == reference:
                    with BytesIO() as image:
                        verify_image(self.state.storage, path, digest, copy_to=image)
                        return image.getvalue()
            raise StateError("This file is not part of the report.", code="report_not_found")

    def report_file(self, report_id: str, reference: str) -> bytes:
        missing = StateError(
            "This report is unavailable. Open Handoff to preview the current report.",
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
                    "This saved report is missing or changed. "
                    "Open Handoff to preview the current report."
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
                    "A saved report image is missing or changed. "
                    "Open Handoff to preview the current report."
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
                "Open Handoff to download the current images."
            )
        # Serve the very bytes verified above, without reopening a mutable path later.
        return payload
