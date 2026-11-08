"""One bounded, temporary export job with verified per-image retry.

The application owns at most one job at a time. Nothing is written to SQLite or
campaign folders. Unnamed OS temporary files close after delivery, cancellation,
expiry, or process exit. Only an interrupted download preparation is retryable;
this is not a campaign history or an image-generation job.
"""

import hashlib
import tempfile
import threading
import time
from datetime import UTC, datetime
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from campaign_studio import delivery
from campaign_studio.exporting import ZipDownload
from campaign_studio.imaging import FontProblem
from campaign_studio.state import StateError

JOB_IDLE_SECONDS = 600


class DeliveryJob:
    def __init__(self, state, snapshot, operation_id, *, allow_partial=False, on_close=None):
        self.state = state
        self.snapshot = snapshot
        self.operation_id = operation_id
        self.campaign_id = snapshot["plan"].brief.campaign_id
        self.manifest = delivery.delivery_manifest(snapshot, allow_partial=allow_partial)
        self.allow_partial = allow_partial
        self.records = {o["id"]: o for o in self.manifest["outputs"] if o["file"]}
        self.outputs = [o for o in snapshot["plan"].outputs if o.id in self.records]
        self.sources = {}
        self.ready = {}
        self.zip_stream = None
        self.stage = "Creating finished images"
        self.status = "waiting"
        self.failed_output = None
        self.failed_outputs = {}
        self.message = None
        self.closed = False
        self.cancelled = False
        self.active = False
        self.timer = None
        self.on_close = on_close
        self._guard = threading.RLock()
        self.render_seconds = 0.0

    def view(self):
        return {
            "operation_id": self.operation_id,
            "version": self.snapshot["version"],
            "stage": self.stage,
            "status": self.status,
            "completed": len(self.ready),
            "total": len(self.outputs),
            "failed_output": self.failed_output,
            "failed_outputs": list(self.failed_outputs.values()),
            "message": self.message,
        }

    def _expire_later(self):
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(JOB_IDLE_SECONDS, self.cancel)
        self.timer.daemon = True
        self.timer.start()

    def close(self):
        with self._guard:
            if self.closed:
                return
            self.closed = True
            if self.timer:
                self.timer.cancel()
            for stream in [*self.sources.values(), *self.ready.values(), self.zip_stream]:
                if stream is not None:
                    stream.close()
            self.sources.clear()
            self.ready.clear()
            self.zip_stream = None
            if self.on_close:
                self.on_close(self)
                self.on_close = None

    def cancel(self):
        with self._guard:
            self.cancelled = True
            if not self.active:
                self.close()

    def _require_active(self):
        if self.cancelled or self.closed:
            raise StateError(
                "The download was cancelled. Start a new download when ready.",
                code="delivery_cancelled",
            )

    def _capture_sources(self):
        if self.sources:
            return
        project = self.state.storage.project(self.campaign_id)
        try:
            for key, asset in delivery.source_assets(self.snapshot, self.outputs).items():
                self._require_active()
                project.require(asset.reference)
                stream = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
                self.sources[key] = stream
                digest = hashlib.sha256()
                with self.state.storage.path(asset.reference).open("rb") as source:
                    while chunk := source.read(256 * 1024):
                        digest.update(chunk)
                        stream.write(chunk)
                if digest.hexdigest() != asset.sha256:
                    raise StateError(
                        "A source image changed. Reopen the campaign and try again.",
                        code="source_changed",
                    )
                stream.seek(0)
        except BaseException:
            self.close()
            raise

    def _output_details(self, output):
        return {
            "id": output.id,
            "product_name": self.manifest["product_names"][output.product_id],
            "market": output.market,
            "ratio": output.ratio,
        }

    @staticmethod
    def _output_label(detail):
        ratio = {"1:1": "Square", "9:16": "Story", "16:9": "Landscape"}[detail["ratio"]]
        market = {"en-US": "English", "es-MX": "Spanish"}[detail["market"]]
        return f"{detail['product_name']} · {ratio} · {market}"

    def _try_output(self, output, *, retry=False):
        self._require_active()
        detail = self._output_details(output)
        if retry:
            self.stage = "Retrying image"
            self.status = "retrying"
            self.failed_output = detail
            self.message = f"Retrying {self._output_label(detail)}."
        stream = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115
        started = time.monotonic()
        try:
            hashes, checks = delivery.render_png(self.snapshot, self.sources, output, stream)
        except FontProblem:
            stream.close()
            raise
        except Exception:
            stream.close()
            self.failed_output = detail
            self.failed_outputs[output.id] = detail
            if not retry:
                self.message = (
                    f"There was a problem creating {self._output_label(detail)}. "
                    "Continuing with the other images, then trying this one again."
                )
            return
        except BaseException:
            stream.close()
            raise
        finally:
            self.render_seconds += time.monotonic() - started
        self.records[output.id].update(hashes)
        self.manifest["findings"].extend(f.model_dump(mode="json") for f in checks)
        self.ready[output.id] = stream
        self.failed_outputs.pop(output.id, None)

    def _render_remaining(self):
        self.stage = "Creating finished images"
        for output in self.outputs:
            if output.id not in self.ready:
                self._try_output(output)
        # A single bounded local retry follows the first pass. Verified successes
        # are never recomposed, and this path cannot dispatch an AI request.
        for output in self.outputs:
            if output.id in self.failed_outputs:
                self._try_output(output, retry=True)
        if self.failed_outputs:
            labels = "; ".join(self._output_label(item) for item in self.failed_outputs.values())
            self.message = (
                f"We couldn’t create {labels}. "
                f"Created {len(self.ready)} of {len(self.outputs)} images. "
                "Retry failed images."
            )
            raise StateError(self.message, code="delivery_retry")
        self.failed_output = None
        self.message = None
        self.status = "working"

    def _package(self):
        self.stage = "Preparing ZIP"
        self._require_active()
        created = datetime.now(UTC)
        self.manifest["report_created_at"] = created.isoformat()
        self.manifest["metrics"]["local_render_seconds"] = round(self.render_seconds, 3)
        self.manifest["metrics"]["elapsed_processing_seconds"] = round(self.render_seconds, 3)
        delivery.require_public_metadata(self.manifest)
        stamp = created.astimezone().timetuple()[:6]
        root = self.manifest["package_name"]
        stream = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")  # noqa: SIM115
        try:
            with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
                for output in self.outputs:
                    self._require_active()
                    record = self.records[output.id]
                    image = self.ready[output.id]
                    image.seek(0)
                    digest = hashlib.sha256()
                    with archive.open(ZipInfo(f"{root}/{record['file']}", stamp), "w") as entry:
                        while chunk := image.read(256 * 1024):
                            digest.update(chunk)
                            entry.write(chunk)
                    if digest.hexdigest() != record["sha256"]:
                        image.close()
                        del self.ready[output.id]
                        self.failed_output = self._output_details(output)
                        self.failed_outputs[output.id] = self.failed_output
                        raise StateError("A temporary image failed its integrity check.")
                for name in ("manifest.json", "index.html"):
                    archive.writestr(
                        ZipInfo(f"{root}/{name}", stamp),
                        delivery.document(self.snapshot, self.manifest, name),
                        compress_type=ZIP_DEFLATED,
                    )
            self.stage = "Checking download"
            self._require_active()
            stream.seek(0)
            with ZipFile(stream) as archive:
                if archive.testzip() is not None:
                    raise StateError("The ZIP failed its integrity check.")
            size = stream.seek(0, 2)
            stream.seek(0)
            self.zip_stream = stream
            self.stage = "Download ready"
            self.status = "ready"
            return ZipDownload(f"{root}.zip", stream, size, on_close=self.close)
        except BaseException:
            stream.close()
            raise

    def run(self):
        with self._guard:
            if self.active or self.status == "ready":
                raise StateError("This download is already being prepared or sent.", code="busy")
            self._require_active()
            self.active = True
            self.status = "working"
            self.failed_output = None
            self.message = None
            if self.timer:
                self.timer.cancel()
        try:
            self._capture_sources()
            self._render_remaining()
            return self._package()
        except FontProblem:
            self.close()
            raise
        except Exception as error:
            if self.cancelled or self.closed:
                raise
            self.status = "failed"
            if self.failed_outputs:
                labels = "; ".join(
                    self._output_label(item) for item in self.failed_outputs.values()
                )
                self.message = (
                    f"We couldn’t create {labels}. "
                    f"Created {len(self.ready)} of {len(self.outputs)} images. "
                    "Retry failed images."
                )
            else:
                self.message = (
                    f"Created {len(self.ready)} of {len(self.outputs)} images, "
                    "but the download could not be prepared. Try again to finish the download."
                )
            raise StateError(self.message, code="delivery_retry") from error
        except BaseException:
            self.close()
            raise
        finally:
            with self._guard:
                self.active = False
                if self.cancelled:
                    self.close()
                elif not self.closed:
                    self._expire_later()
