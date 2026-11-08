"""Legacy readable folders migrate safely; new deliveries use readable product paths."""

import hashlib
import json
import ntpath
from types import SimpleNamespace

import pytest
from test_workflow import setup_case

from campaign_studio.cleanup import Cleanup
from campaign_studio.projects import matches_output_file, output_file, product_folders
from campaign_studio.runner import Runner
from campaign_studio.state import State, StateError
from campaign_studio.storage import WORKSPACE_SCHEMA, delivery_component


def test_readable_folders_are_safe_distinct_and_stable_after_name_changes():
    names = ["Leaflight lantern", "Leaflight lantern", "CON", '../bells: <>"/\\|?*#% ', "灯" * 100]
    products = [SimpleNamespace(id=f"product-{i}", name=name) for i, name in enumerate(names)]
    folders = product_folders(products)
    assert len({folder.casefold() for folder in folders.values()}) == len(products)
    manifest = {"outputs": []}
    for product in products:
        folder = folders[product.id]
        assert folder.startswith(delivery_component(product.name, "Product") + "-")
        assert not ntpath.isreserved(folder)
        assert len(folder.encode()) <= 93
        relative = output_file(product.id, "1:1", "en-US", "1" * 32, folder=folder)
        assert matches_output_file(relative, product.id, "1:1", "en-US")
        assert not matches_output_file(relative, "different-product", "1:1", "en-US")
        assert not matches_output_file(
            relative.replace("images/", "images/../"), product.id, "1:1", "en-US"
        )
        manifest["outputs"].append(
            {"product_id": product.id, "ratio": "1:1", "market": "en-US", "file": relative}
        )
        product.name = "Renamed product"
    assert product_folders(products, manifest) == folders
    assert matches_output_file("images/product-1/1x1/en-US.png", "product-1", "1:1", "en-US")


def test_same_name_hash_prefix_collision_extends_only_colliding_suffix(monkeypatch):
    import campaign_studio.projects as projects

    monkeypatch.setattr(
        projects,
        "_product_suffix",
        lambda identity: "a" * 12 + hashlib.sha256(identity.encode()).hexdigest()[12:],
    )
    products = [
        SimpleNamespace(id="one", name="Lantern"),
        SimpleNamespace(id="two", name="Lantern"),
    ]
    folders = product_folders(products)
    assert len(set(folders.values())) == 2
    assert sorted(len(name.rsplit("-", 1)[1]) for name in folders.values()) == [12, 16]


@pytest.fixture
def rc2_workspace(tmp_path):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    from campaign_studio.rendering import render_revision

    manifest = render_revision(state, run, state.plan(run, current=False))
    revision = manifest["revision_id"]
    with state.storage.connect() as db:
        payload = json.loads(
            db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?",
                (brief.campaign_id,),
            ).fetchone()[0]
        )
        payload.pop("composition_schema", None)
        db.execute(
            "UPDATE campaign_drafts SET document_json=? WHERE campaign_id=?",
            (json.dumps(payload), brief.campaign_id),
        )
    project = state.storage.project(brief.campaign_id)
    root = state.storage.path(project.ref("Outputs"))
    original = {}
    old_refs = []
    for item in manifest["outputs"]:
        source_ref = project.ref(f"Outputs/{item['file']}")
        source = state.storage.path(source_ref)
        original[item["id"]] = source.read_bytes()
        old = (
            f"images/{item['product_id']}/{item['ratio'].replace(':', 'x')}/"
            f"{item['market']}-{revision}.png"
        )
        destination = project.ref(f"Outputs/{old}")
        project.write(destination, original[item["id"]])
        source.unlink()
        source.parent.rmdir()
        if not list(source.parent.parent.iterdir()):
            source.parent.parent.rmdir()
        old_refs.append(destination)
        item["file"] = old
        with state.storage.connect() as db:
            db.execute("DELETE FROM file_owners WHERE reference=?", (source_ref,))
    project.write(project.ref("Outputs/manifest.json"), json.dumps(manifest).encode())
    with state.storage.connect() as db:
        db.execute(
            "UPDATE revisions SET manifest_json=? WHERE id=?", (json.dumps(manifest), revision)
        )
        db.execute("PRAGMA user_version=11")
    keep = root / "images" / "product-1" / "producer notes.txt"
    keep.write_text("Keep my notes.")
    return SimpleNamespace(
        state=state,
        storage=state.storage,
        run=run,
        brief=brief,
        revision=revision,
        original=original,
        root=root,
        keep=keep,
        old_refs=old_refs,
    )


def assert_migrated(case, state):
    from campaign_studio.workspace import Workspace

    snapshot = Workspace(state).current_composition(case.brief.campaign_id)
    assert len(snapshot["plan"].outputs) == len(case.original)
    assert len(snapshot["sources"]) == len(case.brief.products)
    assert not state.identity_exists("revisions", case.revision)
    assert not (case.root / "manifest.json").exists()
    assert not (case.root / "plan.json").exists()
    assert not list(case.root.rglob("*.png"))
    assert case.keep.read_text() == "Keep my notes."
    assert set(case.keep.parent.iterdir()) == {case.keep}
    assert not (case.root / "images" / "product-2").exists()
    with case.storage.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone() == (WORKSPACE_SCHEMA,)
        assert not db.execute("SELECT 1 FROM workspace_migrations").fetchone()
        assert not db.execute("SELECT 1 FROM cleanup_intents").fetchone()
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='output_mirrors'").fetchone()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for reference in case.old_refs:
            assert not db.execute(
                "SELECT 1 FROM file_owners WHERE reference=?", (reference,)
            ).fetchone()


def test_rc2_upgrade_preserves_sources_and_only_retires_owned_derived_files(
    rc2_workspace, monkeypatch
):
    case = rc2_workspace
    reopened = State(case.storage)
    assert_migrated(case, reopened)
    mtimes = {path: path.stat().st_mtime_ns for path in case.root.rglob("*.png")}
    import campaign_studio.output_migration as migration

    monkeypatch.setattr(
        migration, "migrate_outputs", lambda *_: pytest.fail("Repeated one-time upgrade")
    )
    Runner(State(case.storage))
    assert mtimes == {path: path.stat().st_mtime_ns for path in case.root.rglob("*.png")}


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_rc2_folder_upgrade_recovers_an_interruption(rc2_workspace, monkeypatch, stage):

    case = rc2_workspace

    def interrupt(*args, **kwargs):
        raise SystemExit("Interrupted directory upgrade")

    with monkeypatch.context() as patch:
        if stage == "before_commit":
            patch.setattr(Cleanup, "record_intent", staticmethod(interrupt))
        else:
            patch.setattr(Cleanup, "_resume", interrupt)
        with pytest.raises(SystemExit):
            State(case.storage)
    assert all(case.storage.path(reference).is_file() for reference in case.old_refs)
    assert_migrated(case, State(case.storage))


def test_failed_folder_upgrade_blocks_saves_and_retries_after_restore(rc2_workspace, monkeypatch):
    import campaign_studio.output_migration as migration

    case = rc2_workspace
    with monkeypatch.context() as patch:
        patch.setattr(
            migration,
            "_copy_once",
            lambda *args: (_ for _ in ()).throw(PermissionError("locked destination")),
        )
        state = State(case.storage)
        with pytest.raises(StateError, match="could not finish upgrading"):
            state.require_image_cleanup(case.run)
    assert_migrated(case, State(case.storage))


def test_migrated_campaign_can_block_and_restore_exports_without_saved_png_history(rc2_workspace):
    from zipfile import ZipFile

    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.delivery import prepare_current_zip
    from campaign_studio.workspace import ActionError, Workspace

    case = rc2_workspace
    state = State(case.storage)
    workspace = Workspace(state)
    project = AuthoredProjects(workspace)

    def save_rules(rules):
        current = project.read(case.brief.campaign_id)
        current["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = rules
        return project.save(
            case.brief.campaign_id,
            AuthoredSave(
                version=current["version"],
                base=current["base"],
                authored=current["authored"],
            ),
        )

    phrase = case.brief.markets["en-US"].text.message
    save_rules(
        [{"id": "blocked-headline", "phrase": phrase, "match": "literal", "case_sensitive": False}]
    )
    with pytest.raises((ActionError, StateError)):
        prepare_current_zip(state, workspace.current_composition(case.brief.campaign_id))
    save_rules([])
    with (
        prepare_current_zip(
            state, workspace.current_composition(case.brief.campaign_id)
        ) as download,
        ZipFile(download.stream) as archive,
    ):
        manifest = json.loads(
            archive.read(next(n for n in archive.namelist() if n.endswith("/manifest.json")))
        )
        assert manifest["complete"] and len(manifest["outputs"]) == 6
        assert len([n for n in archive.namelist() if n.endswith(".png")]) == 6
        names = {p.id: p.name for p in case.brief.products}
        for item in manifest["outputs"]:
            assert names[item["product_id"]] in item["file"].split("/")
    assert_migrated(case, state)


def test_upgrade_removes_only_owned_output_sidecars_and_pending_mirror_records(rc2_workspace):
    case = rc2_workspace
    project = case.storage.project(case.brief.campaign_id)
    row = case.state.revision(case.revision)
    project.write(project.ref("Outputs/plan.json"), row["plan_json"].encode())
    temporary = project.ref("Outputs/manifest.json." + "a" * 32 + ".tmp")
    project.write(temporary, b"interrupted mirror write")
    original = project.ref("Brief/original.json")
    project.write(original, b'{"original_upload": true}')
    user_notes = case.root / "producer.json"
    user_notes.write_text('{"keep": "producer notes"}')
    with case.storage.connect() as db:
        db.execute("CREATE TABLE output_mirrors(campaign_id TEXT PRIMARY KEY, revision_id TEXT)")
        db.execute(
            "INSERT INTO output_mirrors VALUES (?,?)", (case.brief.campaign_id, case.revision)
        )
    assert_migrated(case, State(case.storage))
    assert not case.storage.path(temporary).exists()
    assert case.storage.path(original).read_bytes() == b'{"original_upload": true}'
    assert json.loads(user_notes.read_text()) == {"keep": "producer notes"}
    with case.storage.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM file_owners WHERE reference=?", (temporary,)
        ).fetchone()


def test_upgrade_keeps_output_sidecars_if_sqlite_settings_are_damaged(rc2_workspace):
    case = rc2_workspace
    project = case.storage.project(case.brief.campaign_id)
    sidecar = case.root / "manifest.json"
    original = sidecar.read_bytes()
    with case.storage.connect() as db:
        db.execute("UPDATE revisions SET plan_json='{}' WHERE id=?", (case.revision,))
    State(case.storage)
    assert sidecar.read_bytes() == original
    assert case.storage.path(project.ref("Outputs")).is_dir()
    with case.storage.connect() as db:
        assert db.execute("SELECT name FROM workspace_migrations").fetchall() == [
            (f"campaign_outputs:{case.brief.campaign_id}",)
        ]


def test_upgrade_preserves_unregistered_files_with_old_sidecar_names(tmp_path):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    project = state.storage.project(brief.campaign_id)
    root = state.storage.path(project.ref("Outputs"))
    root.mkdir(parents=True)
    files = [root / f"{name}.json" for name in ("plan", "manifest")]
    for path in files:
        path.write_bytes(b"Producer-created file, outside Motif's ownership records.")
    with state.storage.connect() as db:
        db.execute("PRAGMA user_version=12")
    reopened = State(state.storage)
    from campaign_studio.workspace import Workspace

    assert len(Workspace(reopened).current_composition(brief.campaign_id)["sources"]) == 2
    assert all(
        path.read_bytes() == b"Producer-created file, outside Motif's ownership records."
        for path in files
    )
