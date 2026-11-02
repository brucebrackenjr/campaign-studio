"""SQLite owns campaign identity, runs, attempts, and revision pointers."""

import hashlib
import json
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from campaign_studio.brief import Brief
from campaign_studio.capacity import DOCUMENT_BYTES
from campaign_studio.costing import estimate_campaign_usage
from campaign_studio.imaging import image_suffix
from campaign_studio.planning import Plan, canonical_hash, make_plan
from campaign_studio.review import (
    CreativeUpdate,
    RevisionTarget,
    inherit_metadata,
    initial_metadata,
    public_metadata,
)
from campaign_studio.storage import Storage, StorageError


class StateError(ValueError):
    def __init__(self, message, *, code="state"):
        super().__init__(message)
        self.code = code


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class State:
    def __init__(self, storage: Storage):
        self.storage = storage
        # Active-operation progress is process-local; durable attempts remain in SQLite.
        self.live_progress = {}
        storage.initialize()
        with storage.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    plan_ref TEXT NOT NULL,
                    digest TEXT NOT NULL, state TEXT NOT NULL,
                    allowance INTEGER NOT NULL CHECK(allowance >= 0),
                    stop_requested INTEGER NOT NULL DEFAULT 0, current_revision TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    product_id TEXT NOT NULL, state TEXT NOT NULL, response_ref TEXT NOT NULL,
                    source_ref TEXT NOT NULL, source_sha256 TEXT, request_id TEXT,
                    usage_json TEXT, error_kind TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    plan_ref TEXT NOT NULL, manifest_ref TEXT NOT NULL, complete INTEGER NOT NULL,
                    images_retired INTEGER NOT NULL DEFAULT 0, image_count INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creative_metadata (
                    revision_id TEXT NOT NULL REFERENCES revisions(id),
                    creative_id TEXT NOT NULL, value_json TEXT NOT NULL,
                    PRIMARY KEY (revision_id, creative_id)
                );
                CREATE TABLE IF NOT EXISTS package_exports (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    revision_id TEXT NOT NULL REFERENCES revisions(id),
                    snapshot_id TEXT NOT NULL, reference TEXT NOT NULL,
                    sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_owners (
                    reference TEXT NOT NULL, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    PRIMARY KEY (reference,campaign_id)
                );
                CREATE TABLE IF NOT EXISTS cleanup_intents (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    value_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            db.executescript("""
                CREATE INDEX IF NOT EXISTS runs_campaign ON runs(campaign_id);
                CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_id);
                CREATE INDEX IF NOT EXISTS revisions_run ON revisions(run_id);
                CREATE INDEX IF NOT EXISTS packages_snapshot
                    ON package_exports(revision_id,snapshot_id);
                CREATE INDEX IF NOT EXISTS uploads_campaign ON file_owners(campaign_id);
                CREATE INDEX IF NOT EXISTS cleanup_campaign ON cleanup_intents(campaign_id);
            """)
            db.execute("PRAGMA user_version = 6")

    def run(self, run_id: str) -> dict:
        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            result = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if result is None:
            raise StateError("Run not found.")
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, result["campaign_id"])
        return dict(result)

    def require_image_cleanup(self, run_id):
        campaign_id = self.run(run_id)["campaign_id"]
        with self.storage.connect() as db:
            pending = db.execute(
                "SELECT 1 FROM cleanup_intents WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        if pending:
            raise StateError(
                "Finish pending image cleanup before creating new images.",
                code="image_cleanup_pending",
            )

    def require_images(self, revision_id):
        if self.revision(revision_id)["images_retired"]:
            raise StateError(
                "These image files are no longer available. Open the current images; "
                "previously exported packages remain available."
            )

    def saved_activity(self, campaign_id: str, draft_saved_at: str | None = None) -> str | None:
        """Derive recency from committed work, without a second activity ledger.

        Draft timestamps are stored in the same atomic document as their values.
        Revisions and human metadata already carry timestamps in their commit.
        Plan creation, browsing, previews, and package downloads are not saves.
        """
        with self.storage.connect() as db:
            rows = db.execute(
                "SELECT r.created_at FROM revisions r JOIN runs ON runs.id=r.run_id "
                "WHERE runs.campaign_id=? UNION ALL "
                "SELECT json_extract(m.value_json, '$.updated_at') "
                "FROM creative_metadata m JOIN revisions r ON r.id=m.revision_id "
                "JOIN runs ON runs.id=r.run_id WHERE runs.campaign_id=?",
                (campaign_id, campaign_id),
            ).fetchall()
        candidates = [draft_saved_at, *(row[0] for row in rows)]
        valid = []
        for value in candidates:
            if not isinstance(value, str):
                continue
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is not None:
                    valid.append((parsed, value))
            except ValueError:
                continue
        return max(valid, default=(None, None), key=lambda item: item[0])[1]

    @staticmethod
    def _effective_revision(db, run_id: str) -> str | None:
        """Honor accepted output/rule decisions; otherwise retain first useful partial work."""
        run = db.execute("SELECT current_revision FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise StateError("Run not found.")
        if run[0]:
            return run[0]
        latest = db.execute(
            "SELECT id FROM revisions WHERE run_id=? AND images_retired=0 "
            "ORDER BY CASE WHEN image_count>0 THEN 0 ELSE 1 END,rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return latest[0] if latest else None

    def effective_revision(self, run_id: str) -> str | None:
        with self.storage.connect() as db:
            return self._effective_revision(db, run_id)

    @staticmethod
    def _current_output(db, campaign_id: str) -> tuple[str, str] | None:
        """A new plan is not a new result; accepted zero-output decisions still count."""
        runs = db.execute(
            "SELECT id,current_revision FROM runs WHERE campaign_id=? ORDER BY rowid DESC",
            (campaign_id,),
        ).fetchall()
        for run_id, accepted in runs:
            if accepted:
                return run_id, accepted
            usable = db.execute(
                "SELECT id FROM revisions WHERE run_id=? AND images_retired=0 "
                "AND image_count>0 ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if usable:
                return run_id, usable[0]
        return None

    def current_output(self, campaign_id: str) -> tuple[str, str] | None:
        with self.storage.connect() as db:
            db.execute("BEGIN")
            return self._current_output(db, campaign_id)

    def require_current_output(self, target: RevisionTarget) -> None:
        """A new plan cannot replace the accepted images or admit stale exports."""
        with self.storage.connect() as db:
            db.execute("BEGIN")
            self._select_revision(db, target.model_copy(update={"mode": "saved_version"}))
            if self._current_output(db, target.campaign_id) != (
                target.run_id,
                target.revision_id,
            ):
                raise StateError(
                    "The current campaign images changed. Reload them before exporting."
                )

    def require_current_revision(self, run_id: str, revision_id: str) -> None:
        run = self.run(run_id)
        with self.storage.connect() as db:
            self._select_revision(
                db,
                RevisionTarget(
                    campaign_id=run["campaign_id"],
                    run_id=run_id,
                    revision_id=revision_id,
                    mode="current",
                ),
            )

    def plan(self, run_id: str, current: bool = True) -> Plan:
        # Current follows effective output corrections; False reads the original run snapshot.
        run = self.run(run_id)
        reference = run["plan_ref"]
        revision = self.effective_revision(run_id) if current else None
        if revision:
            reference = self.revision(revision)["plan_ref"]
        return Plan.model_validate_json(self.storage.path(reference).read_bytes())

    def identity_exists(self, table: str, identity: str) -> bool:
        if table not in {"runs", "attempts", "revisions", "package_exports"}:
            raise ValueError("Unknown identity namespace.")
        with self.storage.connect() as db:
            return (
                db.execute(f"SELECT 1 FROM {table} WHERE id=?", (identity,)).fetchone() is not None
            )

    def reserve_revision(self, run_id: str) -> str:
        return self.storage.reserve_namespaces(
            (f"campaigns/{run_id}/revisions/{{id}}",),
            occupied=lambda identity: (
                self.identity_exists("revisions", identity)
                or self.storage.path(f"private/runs/{run_id}/revisions/{identity}.json").exists()
            ),
        )

    def reserve_owned_namespaces(self, campaign_id, patterns, *, occupied=None) -> str:
        """Own fresh internal directories before copying content into them."""
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id)
        identity = self.storage.reserve_namespaces(patterns, occupied=occupied)
        namespaces = tuple(pattern.format(id=identity) for pattern in patterns)
        try:
            with self.storage.connect() as db:
                db.executemany(
                    "INSERT INTO file_owners VALUES (?,?)",
                    [(reference, campaign_id) for reference in namespaces],
                )
        except BaseException:
            # No content exists yet. Never remove anything but these empty reservations.
            for reference in namespaces:
                with suppress(OSError):
                    self.storage.path(reference).rmdir()
            raise
        # Own the fresh namespaces before the first copy, including temporary files.
        # If preparation fails or the process exits before runs INSERT, confirmed
        # campaign deletion can still inventory its exact leaves. Startup never sweeps.
        return identity

    def register_campaign(self, campaign_id: str) -> None:
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id, registered=False)
        with self.storage.connect() as db:
            db.execute("INSERT OR IGNORE INTO campaigns VALUES (?)", (campaign_id,))

    def create(self, plan: Plan, allowance: int) -> str:
        if plan.blocked:
            raise StateError("Correct the campaign details before creating a production plan.")
        if type(allowance) is not int or allowance < len(plan.generation_products):
            raise StateError("The allowance does not cover the displayed initial requests.")
        sizes = [self.storage.path(a.reference).stat().st_size for a in plan.assets.values()]
        self.storage.require_capacity(sum(sizes) + max(sizes, default=0) + DOCUMENT_BYTES)
        # Direct CLI preparation is also explicit campaign creation. Register
        # before owning files, so a failed preparation remains safely deletable.
        self.register_campaign(plan.brief.campaign_id)
        run_id = self.reserve_owned_namespaces(
            plan.brief.campaign_id,
            ("private/runs/{id}", "campaigns/{id}"),
            occupied=lambda identity: self.identity_exists("runs", identity),
        )
        brief = plan.brief.model_dump(exclude_unset=True)
        for item, asset in plan.assets.items():
            source = self.storage.path(asset.reference)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset.sha256:
                raise StateError("An input changed after preflight; rebuild the plan.")
            reference = f"campaigns/{run_id}/inputs/{item}{image_suffix(data)}"
            self.storage.write(reference, data)
            if item == "logo":
                brief["brand"]["logo_path"] = reference
            elif item.startswith("masters/"):
                brief["replay"]["sources"][item.removeprefix("masters/")]["path"] = reference
            else:
                next(
                    product
                    for product in brief["products"]
                    if product["id"] == item.removeprefix("products/")
                )["source"]["path"] = reference
        snapshot = make_plan(Brief.model_validate(brief), self.storage)
        if snapshot.blocked:
            raise StateError("The saved input snapshot failed preflight.")
        reference = f"private/runs/{run_id}/plan.json"
        self.storage.write(reference, snapshot.model_dump_json(indent=2).encode())
        with self.storage.connect() as db:
            db.execute(
                "INSERT INTO runs (id,campaign_id,plan_ref,digest,state,allowance,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    snapshot.brief.campaign_id,
                    reference,
                    snapshot.digest,
                    "planned",
                    allowance,
                    timestamp(),
                ),
            )
        return run_id

    def require_authored_plan(self, run_id: str) -> None:
        run = self.run(run_id)
        path = self.storage.path(f"private/brief-drafts/{run['campaign_id']}.json")
        if not path.is_file():
            return
        document = json.loads(path.read_bytes())
        authored = document.get("authored")
        changed = authored is not None and canonical_hash(authored["brief"]) != document.get(
            "planned_authored_hash", canonical_hash(document["value"])
        )
        output = self.current_output(run["campaign_id"])
        base = document.get(
            "authored_base",
            {
                "run_id": document.get("base_run"),
                "revision_id": document.get("base_revision"),
            },
        )
        images_changed = bool(
            output
            and output[0] != run_id
            and output != (base.get("run_id"), base.get("revision_id"))
        )
        if document.get("planned_run") != run_id or changed or images_changed:
            raise StateError(
                "The authored brief changed. Review a fresh production plan before starting.",
                code="stale_brief",
            )

    def confirm(self, run_id: str, digest: str, *, retry_allowance: int | None = None) -> None:
        with self.storage.exclusive_draft():
            self.require_authored_plan(run_id)
            self._confirm(run_id, digest, retry_allowance=retry_allowance)

    def _confirm(self, run_id: str, digest: str, *, retry_allowance: int | None = None) -> None:
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT plan_ref,state,allowance,current_revision FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StateError("The displayed plan is stale or unknown.")
            revision = self._effective_revision(db, run_id)
            reference = self.revision(revision)["plan_ref"] if revision else row[0]
            plan = Plan.model_validate_json(self.storage.path(reference).read_bytes())
            if plan.digest != digest:
                raise StateError("The displayed plan is stale or unknown.")
            if retry_allowance is None:
                if row[1] not in {"planned", "confirmed"}:
                    raise StateError("This run needs a deliberate retry plan or local recovery.")
                allowance = row[2]
            else:
                if type(retry_allowance) is not int:
                    raise StateError("The request allowance must be a whole number.")
                if row[1] not in {"paused", "stopped", "partial"}:
                    raise StateError("Only paused work can receive a revised request allowance.")
                if not self.pending_generation(run_id, plan):
                    raise StateError(
                        "No imagery is pending. Use local recovery to rebuild the last complete "
                        "revision, or correct the blocked draft in Review."
                    )
                used = db.execute(
                    "SELECT COUNT(*) FROM attempts WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
                if retry_allowance <= used or retry_allowance < row[2]:
                    raise StateError(
                        "The revised allowance must retain used attempts and allow another."
                    )
                allowance = retry_allowance
            db.execute(
                "UPDATE runs SET state='confirmed',allowance=?,stop_requested=0,"
                "plan_ref=?,digest=? WHERE id=?",
                (allowance, reference, plan.digest, run_id),
            )

    def require_local_recovery(self, run_id: str) -> None:
        """Under the runner lock, distinguish recovery from an unstarted plan."""
        run = self.run(run_id)
        # A retry may be confirmed with recorded work. An interrupted first
        # execution may have saved provider material but no revision yet.
        if run["state"] == "planned" or (
            run["state"] == "confirmed"
            and not self.attempts(run_id)
            and not self.revision_count(run_id)
        ):
            raise StateError(
                "This plan has not started. Review and confirm it in Production before starting.",
                code="recovery_not_started",
            )

    def pending_generation(self, run_id: str, plan: Plan | None = None) -> list[str]:
        plan = plan or self.plan(run_id)
        ready = {
            item["product_id"]
            for item in self.attempts(run_id)
            if item["state"] == "decoded" and self.source_is_intact(item)
        }
        return [product for product in plan.generation_products if product not in ready]

    def product_inputs_ready(
        self, run_id: str, plan: Plan, product_id: str, *, include_logo=True
    ) -> bool:
        """Check a local composition bundle without recovering, reserving, or dispatching."""
        product = next((p for p in plan.brief.products if p.id == product_id), None)
        if product is None or (include_logo and "logo" not in plan.assets):
            return False
        keys = ["logo"] if include_logo else []
        if product.source.role != "concept":
            keys.append(f"products/{product_id}")
        if product.source.role != "finished_scene":
            master = f"masters/{product_id}"
            if master in plan.assets:
                keys.append(master)
            elif not any(
                a["product_id"] == product_id
                and a["state"] == "decoded"
                and self.source_is_intact(a)
                for a in self.attempts(run_id)
            ):
                return False
        for key in keys:
            asset = plan.assets.get(key)
            if asset is None:
                return False
            try:
                payload = self.storage.path(asset.reference).read_bytes()
            except OSError:
                return False
            if hashlib.sha256(payload).hexdigest() != asset.sha256:
                return False
        return True

    def require_edit_inputs(
        self, run_id: str, plan: Plan, output_ids, *, include_logo=True
    ) -> None:
        affected = {o.product_id for o in plan.outputs if o.id in output_ids}
        missing = [
            p.name
            for p in plan.brief.products
            if p.id in affected
            and not self.product_inputs_ready(run_id, plan, p.id, include_logo=include_logo)
        ]
        if missing:
            raise StateError(
                "Saved inputs are missing or changed for: "
                + "; ".join(missing)
                + ". Choose an available product, or recover/replace these inputs before "
                "changing this scope. No image request was made."
            )

    def set_status(self, run_id: str, status: str) -> None:
        with self.storage.connect() as db:
            db.execute("UPDATE runs SET state=? WHERE id=?", (status, run_id))

    def stop(self, run_id: str) -> None:
        self.run(run_id)
        with self.storage.connect() as db:
            db.execute("UPDATE runs SET stop_requested=1 WHERE id=?", (run_id,))

    def attempts(self, run_id: str) -> list[dict]:
        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def campaign_metrics(self, campaign_id: str) -> dict:
        """Project durable attempts once per ID, independent of repeated image revisions."""
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id)
        with self.storage.connect() as db:
            # Attempts and their latest publication references must describe one
            # instant, even while another operation records a received result.
            db.execute("BEGIN")
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT a.*,r.plan_ref FROM attempts a JOIN runs r ON r.id=a.run_id "
                "WHERE r.campaign_id=? ORDER BY a.rowid",
                (campaign_id,),
            ).fetchall()
            runs = db.execute(
                "SELECT r.id,r.state,(SELECT v.manifest_ref FROM revisions v "
                "WHERE v.run_id=r.id ORDER BY v.rowid DESC LIMIT 1) AS manifest_ref "
                "FROM runs r WHERE r.campaign_id=?",
                (campaign_id,),
            ).fetchall()
        attempts = list({row["id"]: dict(row) for row in rows}.values())
        coverage_complete = all(
            a["state"] in {"dispatching", "received", "decoded", "failed", "unknown"}
            for a in attempts
        )
        for run in runs:
            if run["state"] not in {
                "planned",
                "confirmed",
                "running",
                "complete",
                "partial",
                "paused",
                "stopped",
            }:
                coverage_complete = False
            if not run["manifest_ref"]:
                coverage_complete &= run["state"] not in {"complete", "partial"}
                continue
            try:
                record = json.loads(self.storage.path(run["manifest_ref"]).read_bytes())
                recorded = record["metrics"]["attempted_requests"]
                known = sum(a["run_id"] == run["id"] for a in attempts)
                coverage_complete &= (
                    record.get("schema_version") == 1
                    and record.get("campaign_id") == campaign_id
                    and type(recorded) is int
                    and 0 <= recorded <= known
                )
            except OSError, ValueError, TypeError, KeyError:
                coverage_complete = False
        requests_by_run = {}
        for attempt in attempts:
            if attempt["run_id"] in requests_by_run:
                continue
            try:
                plan = Plan.model_validate_json(self.storage.path(attempt["plan_ref"]).read_bytes())
                requests_by_run[attempt["run_id"]] = plan.generation_requests
            except OSError, ValueError:
                # The attempt remains counted; unreadable historical pricing is unknown.
                requests_by_run[attempt["run_id"]] = {}
        cost = estimate_campaign_usage(attempts, requests_by_run)
        if not coverage_complete:
            cost["estimated_usd"] = None
        summary = {
            "schema_version": 1,
            "campaign_id": campaign_id,
            "coverage": "complete"
            if coverage_complete
            else "incomplete"
            if attempts
            else "unknown",
            "attempted_requests": len(attempts),
            "successful_requests": sum(a["state"] == "decoded" for a in attempts),
            "failed_requests": sum(a["state"] == "failed" for a in attempts),
            "received_pending_local": sum(a["state"] == "received" for a in attempts),
            "unknown_requests": sum(
                a["state"] not in {"decoded", "failed", "received"} for a in attempts
            ),
            "last_attempt_at": max((a["created_at"] for a in attempts), default=None),
            "usage_cost_estimate": cost,
        }
        return {**summary, "snapshot_id": canonical_hash(summary)}

    def reserve(self, run_id: str, product_id: str) -> dict:
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,allowance,stop_requested FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            used = db.execute("SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,)).fetchone()[
                0
            ]
            if row is None or row[0] != "running" or row[2] or used >= row[1]:
                raise StateError(
                    "Dispatch is stopped, unconfirmed, or beyond its request allowance."
                )
            attempt_id = self.storage.reserve_namespaces(
                (f"private/runs/{run_id}/attempts/{{id}}",),
                occupied=lambda identity: (
                    db.execute("SELECT 1 FROM attempts WHERE id=?", (identity,)).fetchone()
                    is not None
                ),
            )
            root = f"private/runs/{run_id}/attempts/{attempt_id}"
            db.execute(
                "INSERT INTO attempts "
                "(id,run_id,product_id,state,response_ref,source_ref,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    run_id,
                    product_id,
                    "dispatching",
                    f"{root}/response.json",
                    f"{root}/source.png",
                    timestamp(),
                ),
            )
        return next(item for item in self.attempts(run_id) if item["id"] == attempt_id)

    def finish_attempt(
        self,
        attempt_id: str,
        status: str,
        *,
        request_id: str | None = None,
        usage: dict | None = None,
        error: str | None = None,
    ) -> None:
        with self.storage.connect() as db:
            source_sha256 = None
            if status == "decoded":
                row = db.execute(
                    "SELECT source_ref FROM attempts WHERE id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise StateError("Attempt not found.")
                source_sha256 = hashlib.sha256(self.storage.path(row[0]).read_bytes()).hexdigest()
            db.execute(
                "UPDATE attempts SET state=?,request_id=COALESCE(?,request_id),"
                "usage_json=COALESCE(?,usage_json),error_kind=?,"
                "source_sha256=COALESCE(?,source_sha256) WHERE id=?",
                (
                    status,
                    request_id,
                    json.dumps(usage) if usage is not None else None,
                    error,
                    source_sha256,
                    attempt_id,
                ),
            )

    def source_is_intact(self, attempt: dict) -> bool:
        if not attempt["source_sha256"]:
            return False
        try:
            payload = self.storage.path(attempt["source_ref"]).read_bytes()
            return hashlib.sha256(payload).hexdigest() == attempt["source_sha256"]
        except OSError:
            return False

    def reconcile_interrupted(self) -> None:
        """Never infer success or retry a dispatch merely because the process restarted."""
        try:
            with self.storage.exclusive_run(), self.storage.connect() as db:
                db.row_factory = sqlite3.Row
                for row in db.execute(
                    "SELECT * FROM attempts WHERE state='dispatching'"
                ).fetchall():
                    status = (
                        "received"
                        if self.storage.path(row["response_ref"]).is_file()
                        else "unknown"
                    )
                    db.execute(
                        "UPDATE attempts SET state=?,error_kind='interrupted' WHERE id=?",
                        (status, row["id"]),
                    )
                db.execute("UPDATE runs SET state='paused' WHERE state='running'")
        except StorageError:
            # An existing runner still owns the lock; do not alter its records.
            return

    def revision(self, revision_id: str) -> dict:
        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise StateError("Revision not found.")
        return dict(row)

    def publish_revision(
        self,
        run_id: str,
        revision_id: str,
        plan_ref: str,
        manifest_ref: str,
        complete: bool,
        *,
        parent_revision: str | None = None,
        retirement_files: dict | None = None,
        accept_partial: bool = False,
        preserve_current: bool = False,
        rule_withheld_outputs: frozenset[str] = frozenset(),
    ) -> None:
        from campaign_studio.cleanup import Cleanup

        # Authored Save can run during rendering. Final dependency admission,
        # publication, and cleanup selection share its short file guard so a
        # newly restored input cannot turn a committed Apply into a refusal.
        # The caller's runner lock still excludes another render or export.
        with self.storage.exclusive_draft():
            if not self.storage.path(manifest_ref).is_file():
                raise StateError("A revision cannot be committed before its manifest exists.")
            manifest = json.loads(self.storage.path(manifest_ref).read_bytes())
            if any(item["file"] for item in manifest["outputs"]):
                campaign_id = self.run(run_id)["campaign_id"]
                cleanup = Cleanup(self)
                if retirement_files is None:
                    retirement_files = cleanup.prepare_retirement(campaign_id)
                else:
                    cleanup._require_unshared(
                        campaign_id,
                        [item for files in retirement_files.values() for item in files],
                    )
            self._publish_revision(
                run_id,
                revision_id,
                plan_ref,
                manifest_ref,
                complete,
                parent_revision=parent_revision,
                retirement_files=retirement_files,
                accept_partial=accept_partial,
                preserve_current=preserve_current,
                rule_withheld_outputs=rule_withheld_outputs,
            )

    def _publish_revision(
        self,
        run_id: str,
        revision_id: str,
        plan_ref: str,
        manifest_ref: str,
        complete: bool,
        *,
        parent_revision: str | None,
        retirement_files: dict | None,
        accept_partial: bool,
        preserve_current: bool,
        rule_withheld_outputs: frozenset[str],
    ) -> None:
        # Caller has published files and validated the final input dependencies.
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            campaign = db.execute("SELECT campaign_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if campaign is None:
                raise StateError("Run not found.")
            if parent_revision is None:
                # Recovery renders the effective plan. A later failed correction
                # must not replace the description ancestry of the retained complete one.
                parent_revision = self._effective_revision(db, run_id)
            if parent_revision is None:
                prior = db.execute(
                    "SELECT r.id FROM revisions r JOIN runs ON runs.id=r.run_id "
                    "WHERE runs.campaign_id=? AND r.complete=1 "
                    "ORDER BY r.rowid DESC LIMIT 1",
                    (campaign[0],),
                ).fetchone()
                parent_revision = prior[0] if prior else None
            previous = {}
            if parent_revision:
                parent_campaign = db.execute(
                    "SELECT runs.campaign_id FROM revisions r JOIN runs ON runs.id=r.run_id "
                    "WHERE r.id=?",
                    (parent_revision,),
                ).fetchone()
                if parent_campaign is None or parent_campaign[0] != campaign[0]:
                    raise StateError(
                        "Image descriptions can inherit only within the same campaign."
                    )
                previous, _ = self._metadata(db, parent_revision)
            db.execute(
                "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (revision_id, run_id, plan_ref, manifest_ref, int(complete), timestamp()),
            )
            manifest = json.loads(self.storage.path(manifest_ref).read_bytes())
            image_count = sum(bool(item["file"]) for item in manifest["outputs"])
            db.execute("UPDATE revisions SET image_count=? WHERE id=?", (image_count, revision_id))
            current, _ = self._metadata(db, revision_id)
            for creative_id, value in current.items():
                if creative_id in previous:
                    value = inherit_metadata(
                        value,
                        previous[creative_id],
                        parent_revision,
                        rule_withheld=creative_id in rule_withheld_outputs,
                    )
                db.execute(
                    "INSERT INTO creative_metadata VALUES (?,?,?)",
                    (revision_id, creative_id, json.dumps(value)),
                )
            if complete or accept_partial:
                db.execute(
                    "UPDATE runs SET current_revision=?,state=? WHERE id=?",
                    (revision_id, "complete" if complete else "partial", run_id),
                )
            else:
                # The first useful partial can precede a pinned pointer. A failed
                # explicit edit must retain that exact base before cleanup chooses
                # files; its newer diagnostic is not an accepted result.
                db.execute(
                    "UPDATE runs SET state='partial',current_revision="
                    "COALESCE(current_revision,?) WHERE id=?",
                    (parent_revision if preserve_current else None, run_id),
                )
        from campaign_studio.cleanup import Cleanup

        # Only a committed nonempty result can supersede older output files.
        # Startup resumes accepted cleanup lists; it never selects new files.
        if image_count:
            Cleanup(self).retire_after_publish(campaign[0], retirement_files)

    def _metadata(self, db: sqlite3.Connection, revision_id: str) -> tuple[dict, dict]:
        """Read all rows under the caller's transaction; old revisions default honestly."""
        from campaign_studio.cleanup import pending_campaign

        owner = db.execute(
            "SELECT runs.campaign_id FROM revisions r JOIN runs ON runs.id=r.run_id WHERE r.id=?",
            (revision_id,),
        ).fetchone()
        if owner and pending_campaign(db, owner[0]):
            raise StateError("This campaign is being deleted. Retry its pending cleanup.")
        revision = db.execute(
            "SELECT manifest_ref FROM revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        if revision is None:
            raise StateError("Revision not found.")
        manifest = json.loads(self.storage.path(revision[0]).read_bytes())
        values = {item["id"]: initial_metadata(item) for item in manifest["outputs"]}
        values.update(
            {
                row[0]: json.loads(row[1])
                for row in db.execute(
                    "SELECT creative_id,value_json FROM creative_metadata WHERE revision_id=?",
                    (revision_id,),
                )
            }
        )
        return values, manifest

    def _select_revision(self, db, target: RevisionTarget) -> None:
        target = RevisionTarget.model_validate(target)
        row = db.execute(
            "SELECT runs.campaign_id,r.run_id FROM revisions r JOIN runs ON runs.id=r.run_id "
            "WHERE r.id=?",
            (target.revision_id,),
        ).fetchone()
        if row is None or row != (target.campaign_id, target.run_id):
            raise StateError("This saved version is unavailable in the selected campaign and run.")
        from campaign_studio.cleanup import pending_campaign

        if pending_campaign(db, target.campaign_id):
            raise StateError("This campaign is being deleted. Retry its pending cleanup.")
        if target.mode == "current":
            current = db.execute(
                "SELECT id,state FROM runs WHERE campaign_id=? ORDER BY rowid DESC LIMIT 1",
                (target.campaign_id,),
            ).fetchone()
            if (
                current is None
                or current[1] == "running"
                or self._current_output(db, target.campaign_id)
                != (target.run_id, target.revision_id)
            ):
                raise StateError(
                    "The current campaign version changed. "
                    "Reload the current images before continuing."
                )

    def metadata_snapshot(self, revision_id: str, *, target: RevisionTarget | None = None) -> dict:
        """One atomic, versioned export/read snapshot, independent of immutable pixels."""
        with self.storage.connect() as db:
            db.execute("BEGIN")
            if target:
                if target.revision_id != revision_id:
                    raise StateError("The selected revision does not match this action.")
                self._select_revision(db, target)
            values, _ = self._metadata(db, revision_id)
        return self._metadata_payload(revision_id, values)

    @staticmethod
    def _metadata_payload(revision_id, values):
        snapshot = {
            "schema_version": 1,
            "revision_id": revision_id,
            "creatives": {key: public_metadata(value) for key, value in values.items()},
        }
        return {**snapshot, "snapshot_id": canonical_hash(snapshot)}

    def update_creative(
        self,
        revision_id: str,
        creative_id: str,
        update: CreativeUpdate,
        *,
        target: RevisionTarget | None = None,
        include_snapshot: bool = False,
    ) -> dict:
        """Explicit revision edits use optimistic concurrency; never compose or dispatch."""
        self.require_images(revision_id)
        update = CreativeUpdate.model_validate(update)
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if target:
                if target.revision_id != revision_id:
                    raise StateError("The selected revision does not match this action.")
                self._select_revision(db, target)
            if db.execute(
                "SELECT images_retired FROM revisions WHERE id=?", (revision_id,)
            ).fetchone()[0]:
                raise StateError(
                    "These image files are no longer available. Open the current images."
                )
            values, _ = self._metadata(db, revision_id)
            if creative_id not in values:
                raise StateError("Creative not found in this revision.")
            value = values[creative_id]
            if value["version"] != update.expected_version:
                raise StateError("This creative's metadata changed; reload it before saving.")
            changes = update.model_dump(exclude_unset=True, exclude={"expected_version"})
            if all(value[key] == item for key, item in changes.items()):
                return (
                    self._metadata_payload(revision_id, values)
                    if include_snapshot
                    else public_metadata(value)
                )
            value.update(changes)
            value["version"] += 1
            value["updated_at"] = timestamp()
            db.execute(
                "INSERT INTO creative_metadata VALUES (?,?,?) "
                "ON CONFLICT(revision_id,creative_id) DO UPDATE SET value_json=excluded.value_json",
                (revision_id, creative_id, json.dumps(value)),
            )
            result = (
                self._metadata_payload(revision_id, values)
                if include_snapshot
                else public_metadata(value)
            )
        return result

    def packages_for_snapshot(self, target: RevisionTarget, snapshot_id: str) -> list[dict]:
        with self.storage.connect() as db:
            self._select_revision(db, target.model_copy(update={"mode": "saved_version"}))
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM package_exports WHERE run_id=? "
                    "AND revision_id=? AND snapshot_id=? AND reference LIKE '%.zip' "
                    "ORDER BY rowid DESC",
                    (target.run_id, target.revision_id, snapshot_id),
                )
            ]

    def register_package(
        self, target: RevisionTarget, reference: str, sha256: str, snapshot_id: str
    ) -> str:
        for _ in range(100):
            artifact_id = str(uuid4())
            if not self.identity_exists("package_exports", artifact_id):
                break
        else:
            raise StateError("A new package identity could not be reserved. Try again.")
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._select_revision(db, target.model_copy(update={"mode": "saved_version"}))
            db.execute(
                "INSERT INTO package_exports VALUES (?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    target.run_id,
                    target.revision_id,
                    snapshot_id,
                    reference,
                    sha256,
                    timestamp(),
                ),
            )
        return artifact_id

    def package(self, target: RevisionTarget, artifact_id: str) -> dict:
        with self.storage.connect() as db:
            db.execute("BEGIN")
            self._select_revision(db, target.model_copy(update={"mode": "saved_version"}))
            row = db.execute(
                "SELECT reference,sha256,snapshot_id FROM package_exports "
                "WHERE id=? AND run_id=? AND revision_id=? AND reference LIKE '%.zip'",
                (artifact_id, target.run_id, target.revision_id),
            ).fetchone()
        if row is None:
            raise StateError("This ZIP package is unavailable for the selected saved version.")
        return dict(zip(("reference", "sha256", "snapshot_id"), row, strict=True))

    def report_for_reference(self, reference: str, sha256: str) -> str | None:
        with self.storage.connect() as db:
            row = db.execute(
                "SELECT id FROM package_exports WHERE reference=? AND sha256=?",
                (reference, sha256),
            ).fetchone()
        return row[0] if row else None

    def report(self, report_id: str) -> dict | None:
        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT p.*, runs.campaign_id FROM package_exports p "
                "JOIN runs ON runs.id=p.run_id JOIN revisions r ON r.id=p.revision_id "
                "AND r.run_id=p.run_id WHERE p.id=? AND p.reference LIKE '%/index.html'",
                (report_id,),
            ).fetchone()
        return dict(row) if row else None

    def revision_count(self, run_id: str) -> int:
        with self.storage.connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM revisions WHERE run_id=?", (run_id,)
            ).fetchone()[0]

    def revision_number(self, revision_id: str) -> int:
        with self.storage.connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM revisions r JOIN runs ON runs.id=r.run_id "
                "WHERE runs.campaign_id=(SELECT runs.campaign_id FROM revisions selected "
                "JOIN runs ON runs.id=selected.run_id WHERE selected.id=?) "
                "AND r.rowid <= (SELECT rowid FROM revisions WHERE id=?)",
                (revision_id, revision_id),
            ).fetchone()[0]
