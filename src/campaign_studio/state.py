"""SQLite owns campaign identity, runs, attempts, and revision pointers."""

import hashlib
import json
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime

from campaign_studio.brief import Brief
from campaign_studio.capacity import DOCUMENT_BYTES
from campaign_studio.costing import estimate_campaign_usage
from campaign_studio.imaging import image_suffix
from campaign_studio.plan_storage import (
    completed_run,
    dump_completed,
    dump_plan,
    load_plan,
    source_generation,
)
from campaign_studio.planning import Plan, canonical_hash
from campaign_studio.review import (
    CreativeUpdate,
    RevisionTarget,
    inherit_metadata,
    initial_metadata,
    public_metadata,
)
from campaign_studio.storage import WORKSPACE_SCHEMA, Storage, StorageError, asset_component


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
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, folder TEXT UNIQUE COLLATE NOCASE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_drafts (
                    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id) ON DELETE CASCADE,
                    document_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    plan_ref TEXT NOT NULL, plan_json TEXT,
                    digest TEXT NOT NULL, state TEXT NOT NULL,
                    allowance INTEGER NOT NULL CHECK(allowance >= 0),
                    stop_requested INTEGER NOT NULL DEFAULT 0, current_revision TEXT,
                    created_at TEXT NOT NULL,
                    save_count INTEGER NOT NULL DEFAULT 0,
                    saved_attempts INTEGER, draft_comparisons TEXT,
                    confirmed_revision TEXT REFERENCES revisions(id) DEFERRABLE INITIALLY DEFERRED,
                    FOREIGN KEY(id,current_revision) REFERENCES revisions(run_id,id)
                        DEFERRABLE INITIALLY DEFERRED
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
                    plan_json TEXT, manifest_json TEXT, campaign_number INTEGER,
                    created_at TEXT NOT NULL, UNIQUE(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS creative_metadata (
                    revision_id TEXT NOT NULL REFERENCES revisions(id),
                    creative_id TEXT NOT NULL, value_json TEXT NOT NULL,
                    PRIMARY KEY (revision_id, creative_id)
                );
                CREATE TABLE IF NOT EXISTS package_exports (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    revision_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, reference TEXT NOT NULL,
                    sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id,revision_id) REFERENCES revisions(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS file_owners (
                    reference TEXT NOT NULL, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    PRIMARY KEY (reference)
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    name TEXT PRIMARY KEY, reference TEXT NOT NULL UNIQUE
                    REFERENCES file_owners(reference) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS cleanup_intents (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    value_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS withheld_output_sources (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                    creative_id TEXT NOT NULL, revision_id TEXT NOT NULL REFERENCES revisions(id),
                    file TEXT NOT NULL, PRIMARY KEY(campaign_id,creative_id)
                );
                CREATE TABLE IF NOT EXISTS workspace_migrations (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS staging_output_files (
                    reference TEXT PRIMARY KEY REFERENCES file_owners(reference) ON DELETE CASCADE,
                    revision_id TEXT NOT NULL
                );
                DROP TABLE IF EXISTS output_mirrors;
                CREATE INDEX IF NOT EXISTS runs_campaign ON runs(campaign_id);
                CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_id);
                CREATE INDEX IF NOT EXISTS packages_snapshot
                    ON package_exports(revision_id,snapshot_id);
                CREATE INDEX IF NOT EXISTS uploads_campaign ON file_owners(campaign_id);
                CREATE INDEX IF NOT EXISTS cleanup_campaign ON cleanup_intents(campaign_id);
            """)
            if "plan_json" not in {row[1] for row in db.execute("PRAGMA table_info(revisions)")}:
                db.execute("ALTER TABLE revisions ADD COLUMN plan_json TEXT")
                db.execute("ALTER TABLE revisions ADD COLUMN manifest_json TEXT")
            if "plan_json" not in {row[1] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN plan_json TEXT")
            if "campaign_number" not in {
                row[1] for row in db.execute("PRAGMA table_info(revisions)")
            }:
                # Assign the public save numbers before pruning rows from older workspaces.
                db.execute("ALTER TABLE revisions ADD COLUMN campaign_number INTEGER")
                db.execute("ALTER TABLE runs ADD COLUMN save_count INTEGER NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE runs ADD COLUMN saved_attempts INTEGER")
                db.execute("ALTER TABLE runs ADD COLUMN draft_comparisons TEXT")
                db.execute(
                    "WITH numbers AS (SELECT r.id,row_number() OVER "
                    "(PARTITION BY runs.campaign_id ORDER BY r.rowid) AS n FROM revisions r "
                    "JOIN runs ON runs.id=r.run_id) UPDATE revisions SET campaign_number="
                    "(SELECT n FROM numbers WHERE numbers.id=revisions.id)"
                )
                db.execute(
                    "UPDATE runs SET save_count=(SELECT COUNT(*) FROM revisions r WHERE "
                    "r.run_id=runs.id)"
                )
                db.execute("INSERT OR IGNORE INTO workspace_migrations VALUES ('retire_edits')")
            if "confirmed_revision" not in {
                row[1] for row in db.execute("PRAGMA table_info(runs)")
            }:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN confirmed_revision TEXT "
                    "REFERENCES revisions(id) DEFERRABLE INITIALLY DEFERRED"
                )
            # Mark the new layout before moving files so the old application cannot
            # reopen a half-converted workspace. The marker survives interrupted upgrades.
            previous_schema = db.execute("PRAGMA user_version").fetchone()[0]
            if previous_schema in (9, 10):
                db.execute(
                    "INSERT OR IGNORE INTO workspace_migrations VALUES ('empty_export_dirs')"
                )
            if previous_schema in (9, 10, 11, 12, 13):
                db.execute("INSERT OR IGNORE INTO workspace_migrations VALUES ('campaign_outputs')")
            if previous_schema < WORKSPACE_SCHEMA:
                db.execute("INSERT OR IGNORE INTO workspace_migrations VALUES ('database_plans')")
                db.execute("INSERT OR IGNORE INTO workspace_migrations VALUES ('completed_runs')")
            db.execute(f"PRAGMA user_version = {WORKSPACE_SCHEMA}")
            pending = {row[0] for row in db.execute("SELECT name FROM workspace_migrations")}
        if pending:
            from campaign_studio.output_files import prune_retired_edits
            from campaign_studio.output_migration import migrate_outputs, prune_empty_export_dirs
            from campaign_studio.plan_migration import compact_completed_runs, migrate_plans

            with self.storage.exclusive_run(), self.storage.exclusive_draft():
                if "empty_export_dirs" in pending:
                    prune_empty_export_dirs(storage)
                    with storage.connect() as db:
                        db.execute(
                            "DELETE FROM workspace_migrations WHERE name='empty_export_dirs'"
                        )
                    pending.remove("empty_export_dirs")
                upgraded = (
                    set(migrate_outputs(self))
                    if any(name.startswith("campaign_outputs") for name in pending)
                    else set()
                )
                upgraded.update(migrate_plans(self))
                from campaign_studio.campaign_drafts import migrate_drafts

                migrate_drafts(storage)
                upgraded.update(compact_completed_runs(self))
                with storage.connect() as db:
                    if "retire_edits" in pending:
                        upgraded.update(c for (c,) in db.execute("SELECT id FROM campaigns"))
                        db.execute(
                            "UPDATE runs SET saved_attempts=(SELECT CASE WHEN "
                            "json_valid(manifest_json) "
                            "THEN json_extract(manifest_json,'$.metrics.attempted_requests') END "
                            "FROM revisions r WHERE r.run_id=runs.id ORDER BY r.rowid DESC LIMIT 1)"
                        )
                    for campaign_id in upgraded:
                        if not db.execute(
                            "SELECT 1 FROM workspace_migrations WHERE name=?",
                            (f"campaign_outputs:{campaign_id}",),
                        ).fetchone():
                            prune_retired_edits(self, db, storage.project(campaign_id))
                    db.execute(
                        "DELETE FROM workspace_migrations WHERE name IN "
                        "('campaign_outputs','retire_edits','database_plans','completed_runs')"
                    )

        from campaign_studio.composition_migration import migrate_compositions

        migrate_compositions(self)

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
        # A confirmed retry may pin a corrected plan while later Studio work keeps advancing.
        run = self.run(run_id)
        if not current and run["state"] == "complete":
            raise StateError(
                "This production is complete. Open its current accepted image settings.",
                code="production_complete",
            )
        if current and completed_run(run["plan_json"]) is not None:
            from campaign_studio.workspace import Workspace

            return Workspace(self).current_composition(run["campaign_id"])["plan"]
        revision = self.effective_revision(run_id) if current else run["confirmed_revision"]
        if revision:
            record = self.revision(revision)
            if record["run_id"] != run_id:
                raise StateError("The saved production plan belongs to another run.")
            return self.revision_plan(record)
        return self._checked_plan(self.storage.project(run["campaign_id"]), run["plan_json"])

    def source_generation(self, run_id):
        """The original request evidence does not carry obsolete image editing settings."""
        run = self.run(run_id)
        receipt = completed_run(run["plan_json"])
        if receipt is not None:
            if receipt.campaign_id != run["campaign_id"]:
                raise StateError("The generation receipt belongs to another campaign.")
        else:
            self._checked_plan(self.storage.project(run["campaign_id"]), run["plan_json"])
        return source_generation(run["plan_json"])

    def read_plan(self, campaign_id, reference):
        # plan_ref remains a compatibility identifier, not a file read in normal operation.
        with self.storage.connect() as db:
            row = db.execute(
                "SELECT plan_json FROM runs WHERE campaign_id=? AND plan_ref=?",
                (campaign_id, reference),
            ).fetchone()
        if row is None or row[0] is None:
            raise StateError("The saved production plan is unavailable. Finish its upgrade first.")
        project = self.storage.project(campaign_id)
        return self._checked_plan(project, row[0])

    @staticmethod
    def _checked_plan(project, payload):
        plan = load_plan(payload)
        campaign_id = project.campaign_id
        if plan.brief.campaign_id != campaign_id:
            raise StateError("The saved plan belongs to a different campaign.")
        project.check_brief(
            plan.brief.model_dump(mode="json"),
            (a.reference for a in plan.assets.values()),
            inspect_files=False,
        )
        return plan

    def _legacy_document(self, revision, kind):
        # An exported v0.2.0 set with damaged history can still have one intact
        # document. Read only its own registered, original path, if available.
        run = self.run(revision["run_id"])
        project = self.storage.project(run["campaign_id"])
        reference = f"{project.run(run['id'])}/Revisions/{revision['id']}/{kind}.json"
        try:
            if revision[f"{kind}_ref"] != reference:
                raise ValueError("Unexpected saved document path")
            data = self.storage.path(project.require(reference)).read_bytes()
            if kind == "plan":
                return self._checked_plan(project, data)
            manifest = json.loads(data)
            if (
                manifest["campaign_id"] != project.campaign_id
                or manifest["revision_id"] != revision["id"]
                or not isinstance(manifest["outputs"], list)
            ):
                raise ValueError("Invalid saved manifest")
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise StateError(
                f"This earlier image set’s saved {kind} is unavailable. Restore its files "
                "from your Workspace backup to use this historical export in Motif."
            ) from error

    def comparison_brief(self, campaign_id, revision_id):
        """Studio and a separate unsaved Brand draft may still compare a few old values."""
        with self.storage.connect() as db:
            for (payload,) in db.execute(
                "SELECT draft_comparisons FROM runs WHERE campaign_id=? AND draft_comparisons "
                "IS NOT NULL",
                (campaign_id,),
            ):
                if brief := json.loads(payload).get(revision_id):
                    return brief
        row = self.revision(revision_id)
        if self.run(row["run_id"])["campaign_id"] != campaign_id:
            raise StateError("The saved comparison belongs to another campaign.")
        return self.revision_plan(row).brief.model_dump(mode="json")

    def revision_plan(self, revision):
        if revision["plan_json"] is None and "/Revisions/" in revision["plan_ref"]:
            return self._legacy_document(revision, "plan")
        project = self.storage.project(self.run(revision["run_id"])["campaign_id"])
        return self._checked_plan(project, revision["plan_json"])

    def revision_manifest(self, revision):
        if isinstance(revision, str):
            revision = self.revision(revision)
        if revision["manifest_json"] is None and "/Revisions/" in revision["manifest_ref"]:
            return self._legacy_document(revision, "manifest")
        return json.loads(revision["manifest_json"])

    def identity_exists(self, table: str, identity: str) -> bool:
        if table not in {"runs", "attempts", "revisions"}:
            raise ValueError("Unknown identity namespace.")
        with self.storage.connect() as db:
            return (
                db.execute(f"SELECT 1 FROM {table} WHERE id=?", (identity,)).fetchone() is not None
            )

    def reserve_revision(self, run_id: str) -> str:
        self.run(run_id)
        return self.storage.reserve_namespaces((), occupied=self._revision_in_use)

    def _revision_in_use(self, identity):
        with self.storage.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM revisions WHERE id=? UNION ALL "
                    "SELECT 1 FROM staging_output_files WHERE revision_id=? LIMIT 1",
                    (identity, identity),
                ).fetchone()
                is not None
            )

    def write_output(self, run_id, revision_id, output, payload):
        from campaign_studio.projects import output_file, product_folders

        project = self.storage.project(self.run(run_id)["campaign_id"])
        current = self.current_output(project.campaign_id)
        folders = product_folders(
            self.plan(run_id).brief.products,
            self.revision_manifest(current[1]) if current else None,
        )
        relative = output_file(
            output.product_id,
            output.ratio,
            output.market,
            revision_id,
            folder=folders[output.product_id],
        )
        reference = project.ref(f"Outputs/{relative}")
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM file_owners WHERE reference=?", (reference,)).fetchone():
                raise StorageError("This output destination is already reserved.")
            project.claim(reference, db)
            db.execute(
                "INSERT INTO staging_output_files(reference,revision_id) VALUES (?,?)",
                (reference, revision_id),
            )
        self.storage.write_new(reference, payload)
        return relative

    def discard_unpublished_outputs(self, revision_id=None):
        """The runner lock protects these explicit pre-publication reservations."""
        from campaign_studio.output_files import discard_unpublished_outputs

        discard_unpublished_outputs(self.storage, revision_id)

    def reserve_owned_namespaces(self, campaign_id, patterns, *, occupied=None) -> str:
        """Own fresh internal directories before copying content into them."""
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id)
        project = self.storage.project(campaign_id)
        for pattern in patterns:
            project.check(pattern.format(id="reserved"))
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

    def register_campaign(self, campaign_id: str, label="Campaign") -> None:
        from campaign_studio.cleanup import require_campaign_available

        require_campaign_available(self.storage, campaign_id, registered=False)
        with self.storage.connect() as db:
            from campaign_studio.projects import create_project

            if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
                create_project(self.storage, db, campaign_id, label)

    def create(self, plan: Plan, allowance: int) -> str:
        if plan.source_blocked:
            raise StateError("Correct the campaign details before creating a production plan.")
        if type(allowance) is not int or allowance < len(plan.generation_products):
            raise StateError("The allowance does not cover the displayed initial requests.")
        project = self.storage.project(plan.brief.campaign_id)
        project.check_brief(plan.brief.model_dump(mode="json"))
        source_assets = {
            key: asset
            for key, asset in plan.assets.items()
            if key != "logo"
            or not any(f.item == "logo" and f.severity == "blocking" for f in plan.findings)
        }
        sizes = [
            self.storage.path(project.require(a.reference)).stat().st_size
            for a in source_assets.values()
        ]
        self.storage.require_capacity(sum(sizes) + max(sizes, default=0) + DOCUMENT_BYTES)
        run_id = self.reserve_owned_namespaces(
            plan.brief.campaign_id,
            (project.ref("Runs/{id}"),),
            occupied=lambda identity: self.identity_exists("runs", identity),
        )
        from campaign_studio.projects import product_folders

        brief = plan.brief.model_dump(exclude_unset=True)
        product_names = product_folders(plan.brief.products)
        brand_name = asset_component(plan.brief.brand.name + " logo", "Brand logo")
        brand_identity = hashlib.sha256(plan.brief.brand.id.encode()).hexdigest()[:12]
        assets = {}
        if "logo" not in source_assets:
            brief["brand"]["logo_path"] = ""
        for item, asset in source_assets.items():
            source = self.storage.path(asset.reference)
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != asset.sha256:
                raise StateError("An input changed after preflight; rebuild the plan.")
            if item == "logo":
                input_name = f"{brand_name}-{brand_identity}"
            else:
                group, _, product_id = item.partition("/")
                readable, _, identity = product_names[product_id].rpartition("-")
                input_name = f"{group}/{asset_component(readable)}-{identity}"
            reference = f"{project.run(run_id)}/Inputs/{input_name}{image_suffix(data)}"
            project.write(reference, data)
            assets[item] = asset.model_copy(update={"reference": reference})
            if item == "logo":
                brief["brand"]["logo_path"] = reference
            elif item.startswith("masters/"):
                replay = brief.get("replay")
                product_id = item.removeprefix("masters/")
                if replay and product_id in replay["sources"]:
                    replay["sources"][product_id]["path"] = reference
            else:
                next(
                    product
                    for product in brief["products"]
                    if product["id"] == item.removeprefix("products/")
                )["source"]["path"] = reference
        # Freeze the reviewed requests along with their verified inputs. Replanning
        # here would mistake a retained generated master for a missing source and
        # add a new paid request for it. Imported replay and generated sources have
        # different provenance, but both retain their exact validated source bytes.
        snapshot = plan.model_copy(deep=True)
        snapshot.brief = Brief.model_validate(brief)
        snapshot.assets = assets
        snapshot.digest = canonical_hash(
            {
                "brief": snapshot.brief.model_dump(mode="json"),
                **(
                    {"replacement": snapshot.replacement.model_dump()}
                    if snapshot.replacement
                    else {}
                ),
                "assets": {name: asset.model_dump() for name, asset in assets.items()},
                "generation_requests": {
                    name: request.model_dump()
                    for name, request in snapshot.generation_requests.items()
                },
            }
        )
        reference = f"{project.run(run_id)}/plan.json"
        with self.storage.connect() as db:
            db.execute(
                "INSERT INTO runs (id,campaign_id,plan_ref,plan_json,digest,state,allowance,"
                "created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    snapshot.brief.campaign_id,
                    reference,
                    dump_plan(snapshot),
                    snapshot.digest,
                    "planned",
                    allowance,
                    timestamp(),
                ),
            )
        return run_id

    def require_authored_plan(self, run_id: str) -> None:
        from campaign_studio.campaign_drafts import read as read_draft

        run = self.run(run_id)
        document = read_draft(self.storage, run["campaign_id"])
        if document is None:
            return
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
                "SELECT plan_ref,state,allowance,current_revision,campaign_id FROM runs "
                "WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StateError("The displayed plan is stale or unknown.")
            revision = self._effective_revision(db, run_id)
            plan = (
                self.revision_plan(self.revision(revision))
                if revision
                else self.read_plan(row[4], row[0])
            )
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
                "digest=?,confirmed_revision=? WHERE id=?",
                (allowance, plan.digest, revision, run_id),
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
        self, run_id: str, plan: Plan, product_id: str, *, include_logo=True, verify_bytes=True
    ) -> bool:
        """Check a local composition bundle without recovering, reserving, or dispatching."""
        product = next((p for p in plan.brief.products if p.id == product_id), None)
        if product is None:
            return False
        keys = ["logo"] if include_logo and plan.brief.brand.logo_path else []
        if product.source.role != "concept":
            keys.append(f"products/{product_id}")
        if product.source.role != "finished_scene":
            master = f"masters/{product_id}"
            if master in plan.assets:
                keys.append(master)
            elif not any(
                a["product_id"] == product_id
                and a["state"] == "decoded"
                and (self.source_is_intact(a) if verify_bytes else bool(a["source_sha256"]))
                for a in self.attempts(run_id)
            ):
                return False
        for key in keys:
            asset = plan.assets.get(key)
            if asset is None:
                return False
            if not verify_bytes:
                continue
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
                "SELECT a.*,r.plan_json FROM attempts a JOIN runs r ON r.id=a.run_id "
                "WHERE r.campaign_id=? ORDER BY a.rowid",
                (campaign_id,),
            ).fetchall()
            runs = db.execute(
                "SELECT r.id,r.state,r.saved_attempts,EXISTS(SELECT 1 FROM revisions x WHERE "
                "x.run_id=r.id) AS has_saved_record,(SELECT v.manifest_json FROM revisions v "
                "WHERE v.run_id=r.id ORDER BY v.rowid DESC LIMIT 1) AS manifest_json "
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
            if not run["manifest_json"]:
                if run["has_saved_record"]:
                    coverage_complete = False
                elif run["saved_attempts"] is None:
                    coverage_complete &= run["state"] not in {"complete", "partial"}
                else:
                    known = sum(a["run_id"] == run["id"] for a in attempts)
                    coverage_complete &= 0 <= run["saved_attempts"] <= known
                continue
            try:
                record = json.loads(run["manifest_json"])
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
                requests_by_run[attempt["run_id"]] = {
                    product: item["request"]
                    for product, item in source_generation(attempt["plan_json"]).items()
                }
            except OSError, ValueError, KeyError, TypeError:
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
        product = next(
            (p for p in self.plan(run_id, current=False).brief.products if p.id == product_id), None
        )
        if product is None:
            raise StateError("Choose a product in this production plan.")
        source_name = asset_component(product.name, "product-image")
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
            project = self.storage.project(self.run(run_id)["campaign_id"])
            attempt_id = self.storage.reserve_namespaces(
                (f"{project.run(run_id)}/Requests/{{id}}",),
                occupied=lambda identity: (
                    db.execute("SELECT 1 FROM attempts WHERE id=?", (identity,)).fetchone()
                    is not None
                ),
            )
            root = f"{project.run(run_id)}/Requests/{attempt_id}"
            db.execute("INSERT INTO file_owners VALUES (?,?)", (root, project.campaign_id))
            source_ref = f"{root}/{source_name}-{attempt_id}.png"
            for reference in (source_ref, f"{root}/response.json"):
                project.claim(reference, db)
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
                    source_ref,
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
                from campaign_studio.campaign_drafts import read as read_draft
                from campaign_studio.cleanup import Cleanup
                from campaign_studio.output_files import sync_draft_comparisons

                with self.storage.exclusive_draft():
                    self.discard_unpublished_outputs()
                    for (campaign,) in db.execute(
                        "SELECT DISTINCT campaign_id FROM runs WHERE draft_comparisons IS NOT NULL"
                    ).fetchall():
                        project = self.storage.project(campaign)
                        try:
                            draft = read_draft(self.storage, campaign, db=db)
                        except OSError, ValueError:
                            # Keep a damaged draft's comparison for restore and continue startup.
                            continue
                        with self.storage.connect() as cleanup_db:
                            sync_draft_comparisons(self, cleanup_db, project, draft)
                    retiring = db.execute(
                        "SELECT id FROM cleanup_intents WHERE kind IN ('retirement','migration')"
                    ).fetchall()
                    for (intent,) in retiring:
                        Cleanup(self)._resume(intent)
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
        except StorageError as error:
            if error.code != "busy":
                raise
            # Another runner owns the lock and will finish its publication.
            return

    def revision(self, revision_id: str) -> dict:
        with self.storage.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise StateError(
                "Revision not found. It is no longer available. Open the current images.",
                code="retired_revision",
            )
        return dict(row)

    def publish_output_revision(
        self,
        run_id,
        revision_id,
        plan,
        manifest,
        *,
        parent_revision=None,
        accept_partial=False,
        rule_withheld_outputs=frozenset(),
    ):
        from campaign_studio.brand_updates import BrandUpdate, BrandUpdates
        from campaign_studio.workspace import Workspace

        project = self.storage.project(self.run(run_id)["campaign_id"])
        if plan.brief.campaign_id != project.campaign_id or (
            manifest["campaign_id"],
            manifest["revision_id"],
        ) != (project.campaign_id, revision_id):
            raise StateError("The revised images do not belong to this campaign.")
        with self.storage.exclusive_draft():
            brand_update = manifest.get("edit_receipt", {}).get("brand_update")
            if brand_update:
                BrandUpdates(Workspace(self)).require_base(
                    project.campaign_id, BrandUpdate.model_validate(brand_update)
                )
            self._publish_revision(
                project,
                run_id,
                revision_id,
                plan,
                manifest,
                parent_revision,
                accept_partial,
                rule_withheld_outputs,
            )

    def _publish_revision(
        self,
        project,
        run_id,
        revision_id,
        plan,
        manifest,
        parent_revision,
        accept_partial,
        rule_withheld_outputs,
    ):
        from campaign_studio.campaign_drafts import strip_accepted_settings
        from campaign_studio.cleanup import Cleanup
        from campaign_studio.output_files import prune_retired_edits

        cleanup = Cleanup(self)
        complete = manifest["complete"]
        plan_ref = project.ref("Outputs/plan.json")
        manifest_ref = project.ref("Outputs/manifest.json")
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            campaign = db.execute("SELECT campaign_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if campaign is None:
                raise StateError("Run not found.")
            if manifest.get("edit_receipt") and self._current_output(db, project.campaign_id) != (
                run_id,
                parent_revision,
            ):
                raise StateError(
                    "The saved images changed. Reopen the current image and try again."
                )
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
                    "SELECT runs.campaign_id FROM revisions r "
                    "JOIN runs ON runs.id=r.run_id "
                    "WHERE r.id=?",
                    (parent_revision,),
                ).fetchone()
                if parent_campaign is None or parent_campaign[0] != campaign[0]:
                    raise StateError(
                        "Image descriptions can inherit only within the same campaign."
                    )
                previous, _ = self._metadata(db, parent_revision)
            db.execute(
                "UPDATE runs SET save_count=save_count+1,saved_attempts=? WHERE id=?",
                (manifest["metrics"]["attempted_requests"], run_id),
            )
            number = db.execute(
                "SELECT SUM(save_count) FROM runs WHERE campaign_id=?", (project.campaign_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO revisions (id,run_id,plan_ref,manifest_ref,complete,created_at,"
                "plan_json,manifest_json,campaign_number) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    run_id,
                    plan_ref,
                    manifest_ref,
                    int(complete),
                    timestamp(),
                    dump_plan(plan),
                    json.dumps(manifest, indent=2, ensure_ascii=False),
                    number,
                ),
            )
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
                db.execute("UPDATE runs SET state='partial' WHERE id=?", (run_id,))
            if complete:
                self._complete_run(db, run_id, revision_id)
            # Only completed output writes enter the accepted set. Reservations for
            # interrupted or rejected files stay recoverable until removed.
            published = [
                (project.ref(f"Outputs/{item['file']}"), revision_id)
                for item in manifest["outputs"]
                if item["file"] and item["file"].endswith(f"-{revision_id}.png")
            ]
            db.executemany(
                "DELETE FROM staging_output_files WHERE reference=? AND revision_id=?", published
            )
            intent = cleanup.plan_retirement(db, project.campaign_id)
            strip_accepted_settings(self.storage, project.campaign_id, db=db)
            prune_retired_edits(self, db, project)
        if intent:
            cleanup._resume(intent)

    def _complete_run(self, db, run_id, revision_id=None):
        """Retire execution-only inputs with the accepted result and its draft baseline."""
        from campaign_studio.campaign_drafts import complete_base

        row = db.execute(
            "SELECT campaign_id,plan_json,state,current_revision FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None or row[2] != "complete":
            return
        campaign_id, payload, _, current = row
        receipt = completed_run(payload)
        if receipt is not None:
            if receipt.campaign_id != campaign_id:
                raise StateError("The generation receipt belongs to another campaign.")
            db.execute("UPDATE runs SET confirmed_revision=NULL WHERE id=?", (run_id,))
            return
        project = self.storage.project(campaign_id)
        original = self._checked_plan(project, payload)
        revision_id = revision_id or current
        accepted_run_id = run_id
        if revision_id is None:
            # A later production can have retired this run's output. Rebase only
            # a draft still naming its pre-production input onto the actual
            # campaign result before removing the obsolete comparison plan.
            active = self._current_output(db, campaign_id)
            if active is not None:
                accepted_run_id, revision_id = active
        if revision_id:
            accepted = db.execute(
                "SELECT plan_json FROM revisions WHERE id=? AND run_id=?",
                (revision_id, accepted_run_id),
            ).fetchone()
            if accepted is None:
                raise StateError("The completed production has no accepted image settings.")
            plan = self._checked_plan(project, accepted[0])
            values, _ = self._metadata(db, revision_id)
            complete_base(
                self.storage,
                campaign_id,
                run_id,
                revision_id,
                original,
                plan,
                db=db,
                metadata_snapshot_id=self._metadata_payload(revision_id, values)["snapshot_id"],
                accepted_run_id=accepted_run_id,
            )
        attempted = {
            product
            for (product,) in db.execute(
                "SELECT DISTINCT product_id FROM attempts WHERE run_id=?", (run_id,)
            )
        }
        db.execute(
            "UPDATE runs SET plan_json=?,confirmed_revision=NULL WHERE id=?",
            (dump_completed(original, attempted), run_id),
        )

    def _metadata(self, db: sqlite3.Connection, revision_id: str) -> tuple[dict, dict | None]:
        """Read all rows under the caller's transaction; old revisions default honestly."""
        from campaign_studio.cleanup import pending_campaign

        owner = db.execute(
            "SELECT runs.campaign_id FROM revisions r JOIN runs ON runs.id=r.run_id WHERE r.id=?",
            (revision_id,),
        ).fetchone()
        if owner and pending_campaign(db, owner[0]):
            raise StateError("This campaign is being deleted. Retry its pending cleanup.")
        revision = db.execute(
            "SELECT manifest_json FROM revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        if revision is None:
            raise StateError(
                "Revision not found. It is no longer available. Open the current images.",
                code="retired_revision",
            )
        manifest = (
            self.revision_manifest(revision_id) if revision[0] is None else json.loads(revision[0])
        )
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
            "SELECT runs.campaign_id,r.run_id FROM revisions r "
            "JOIN runs ON runs.id=r.run_id "
            "WHERE r.id=?",
            (target.revision_id,),
        ).fetchone()
        if row is None and target.mode == "current":
            raise StateError("The current campaign version changed. Reload the current images.")
        if row != (target.campaign_id, target.run_id):
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

    def package(self, target: RevisionTarget, artifact_id: str) -> dict:
        """Look up a managed ZIP issued by a previous Motif installation."""
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

    def report(self, report_id: str) -> dict | None:
        """Look up an offline report saved by a previous Motif installation."""
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
        return self.run(run_id)["save_count"]

    def revision_number(self, revision_id: str) -> int:
        return self.revision(revision_id)["campaign_number"]
