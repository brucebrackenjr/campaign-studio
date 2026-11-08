"""Curated first-start campaigns are ordinary owned work, never paid startup work."""

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.bundled import RECEIPT, install_campaigns, load_campaigns, verify_ready_sources
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State
from campaign_studio.storage import CapacityError, Storage, StorageError
from campaign_studio.workspace import Workspace

# The maintainer script is a source tool, not an installed application module.
_builder_spec = importlib.util.spec_from_file_location(
    "bundled_output_builder",
    Path(__file__).resolve().parents[1] / "scripts/build_bundled_outputs.py",
)
_builder = importlib.util.module_from_spec(_builder_spec)
_builder_spec.loader.exec_module(_builder)
build_prepared_outputs = _builder.build_prepared_outputs


@pytest.fixture
def bundle(tmp_path):
    fixture_storage = Storage(tmp_path / "synthetic-inputs")
    _, example = write_sample(fixture_storage, 3)
    source = tmp_path / "bundle"
    manifest = {"version": 2, "campaigns": []}
    for slug, count, compose in [("supplied", 5, True), ("missing", 3, False)]:
        folder = source / slug
        (folder / "assets").mkdir(parents=True)
        raw = example.model_dump(mode="json")
        raw["campaign_id"] = str(uuid4())
        raw["name"] = f"Synthetic {slug}"
        raw["products"] = [copy.deepcopy(raw["products"][i % 3]) for i in range(count)]
        payloads = {"assets/logo.png": fixture_storage.path(raw["brand"]["logo_path"]).read_bytes()}
        raw["brand"]["logo_path"] = "assets/logo.png"
        for i, product in enumerate(raw["products"]):
            product["id"] = f"fixture-product-{i}"
            if not compose and i == count - 1:
                product["source"] = {
                    "role": "concept",
                    "imagery_absent": True,
                    "description": "A clearly invented geometric test subject.",
                    "provenance": product["source"]["provenance"],
                }
            else:
                path = f"assets/product-{i}.png"
                payloads[path] = fixture_storage.path(product["source"]["path"]).read_bytes()
                product["source"]["path"] = path
        (folder / "brief.json").write_text(json.dumps(raw))
        for path, payload in payloads.items():
            (folder / path).write_bytes(payload)
        entry = {
            "slug": slug,
            "brief": f"{slug}/brief.json",
            "prepared_outputs": None,
            "files": {p: hashlib.sha256(b).hexdigest() for p, b in payloads.items()},
        }
        if compose:
            build_prepared_outputs(source, entry, Storage(tmp_path / f"build-{slug}"))
        manifest["campaigns"].append(entry)
    (source / "manifest.json").write_text(json.dumps(manifest))
    return source


def test_install_claim_prepared_images_missing_plan_and_deleted_examples_stay_deleted(
    tmp_path, bundle, monkeypatch
):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    original_install = verify_ready_sources

    async def simultaneous_install():
        reached, release = threading.Event(), threading.Event()

        def hold_local(*args):
            reached.set()
            assert release.wait(10)
            return original_install(*args)

        monkeypatch.setattr("campaign_studio.bundled.verify_ready_sources", hold_local)
        first = asyncio.create_task(install_campaigns(workspace, source=bundle))
        arrival = asyncio.create_task(asyncio.to_thread(reached.wait, 10))
        done, _ = await asyncio.wait({first, arrival}, return_when=asyncio.FIRST_COMPLETED)
        if first in done:
            arrival.cancel()
            return await first
        assert await install_campaigns(workspace, source=bundle) is None
        release.set()
        return await first

    receipt = asyncio.run(simultaneous_install())
    for entry in receipt["campaigns"]:
        original = (bundle / entry["slug"] / "brief.json").read_bytes()
        imported = workspace.storage.project(entry["id"]).ref("Brief/Imports/original.json")
        assert workspace.storage.path(imported).read_bytes() == original

    identities = [item["id"] for item in receipt["campaigns"]]
    assert len(set(identities)) == 2
    assert all(str(UUID(identity)) == identity for identity in identities)
    supplied = workspace.current_composition(identities[0])
    missing = workspace.run_for(identities[1])
    assert workspace.run_for(identities[0]) is None
    assert len(supplied["plan"].outputs) == 30 and len(supplied["sources"]) == 5
    assert missing["state"] == "planned" and missing["current_revision"] is None
    assert len(workspace.state.plan(missing["id"]).generation_products) == 1
    assert workspace.state.attempts(missing["id"]) == []
    assert not list(workspace.storage.root.glob("Campaigns/*/Outputs"))
    assert receipt["state"] == "complete" and receipt["elapsed_seconds"] > 0
    cleanup = Cleanup(workspace.state)
    for identity in identities:
        preview = cleanup.preview_campaign(identity)
        cleanup.delete_campaign(identity, preview["token"])
    before = workspace.storage.path(RECEIPT).read_bytes()
    assert asyncio.run(install_campaigns(workspace, source=bundle)) is None
    assert workspace.storage.path(RECEIPT).read_bytes() == before
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


@pytest.mark.parametrize("existing", ["absent", "empty", "workspace", "unrelated-file"])
def test_official_first_start_recognizes_initialized_empty_workspace(
    tmp_path, monkeypatch, existing
):
    root = tmp_path / "workspace"
    if existing == "workspace":
        State(Storage(root))  # Diagnostics can initialize the database before first serve.
    elif existing in {"empty", "unrelated-file"}:
        root.mkdir()
        if existing == "unrelated-file":
            (root / "leave-me.txt").write_text("unrelated content")
    calls = []

    async def record(workspace, **options):
        calls.append((workspace.storage.root, options["allow_new"]))

    monkeypatch.setattr("campaign_studio.bundled.install_campaigns", record)
    for _ in range(2):
        with TestClient(
            create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
        ) as client:
            assert client.get("/health").status_code == 200
    assert len(calls) == 2
    assert calls[0][1] is (existing != "unrelated-file")
    assert all(path.samefile(root) for path, _ in calls)
    if existing == "unrelated-file":
        assert (root / "leave-me.txt").read_text() == "unrelated content"


def test_interrupted_setup_resumes_untouched_owned_draft_on_next_start(
    tmp_path, bundle, monkeypatch
):
    root = tmp_path / "workspace"
    original_load = load_campaigns
    monkeypatch.setattr(
        "campaign_studio.bundled.load_campaigns",
        lambda source, **options: original_load(bundle, **options),
    )

    def fail_upload(*args, **kwargs):
        raise OSError("controlled")

    with monkeypatch.context() as patch:
        patch.setattr(Intake, "upload", fail_upload)
        with (
            pytest.raises(OSError, match="controlled"),
            TestClient(
                create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
            ),
        ):
            pytest.fail("An incomplete first setup must not be reported as ready")
    state = State(Storage(root))
    receipt = json.loads(state.storage.path(RECEIPT).read_bytes())
    assert receipt["state"] == "started" and len(receipt["campaigns"]) == 2
    identity = receipt["campaigns"][0]["id"]
    before = Intake(state.storage).read(identity)
    assert before["attachment_hints"]["logo"] == "logo.png"
    with TestClient(
        create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
    ) as client:
        assert client.get(f"/api/motif/campaigns/{identity}").status_code == 200
    after = Intake(state.storage).read(identity)
    assert after["value"]["name"] == before["value"]["name"]
    assert Workspace(state).run_for(identity) is None
    assert len(Workspace(state).current_composition(identity)["sources"]) == 5
    with state.storage.connect() as db:
        assert len(db.execute("SELECT id FROM campaigns").fetchall()) == 2
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_curated_hashes_and_paths_are_verified_before_any_campaign_is_created(tmp_path, bundle):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    (bundle / "supplied/assets/logo.png").write_bytes(b"changed")
    with pytest.raises(StorageError, match="does not match"):
        asyncio.run(install_campaigns(workspace, source=bundle))
    assert not workspace.storage.path(RECEIPT).exists()
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


def test_prepared_import_cannot_authorize_missing_imagery(tmp_path, bundle, monkeypatch):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    receipt = asyncio.run(install_campaigns(workspace, source=bundle))
    missing = workspace.run_for(receipt["campaigns"][1]["id"])
    with pytest.raises(StorageError, match="complete supplied-image plan"):
        verify_ready_sources(workspace, missing["campaign_id"], True)
    with workspace.storage.connect() as db:
        assert workspace.state.run(missing["id"])["state"] == "planned"
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_prepared_install_keeps_sources_and_current_metadata_without_finished_pngs(
    tmp_path, bundle, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Installing examples must not render or execute production")

    monkeypatch.setattr(Runner, "execute", forbidden)
    monkeypatch.setattr("campaign_studio.rendering.compose", forbidden)
    _, definitions = load_campaigns(bundle)
    original = definitions[0][1]
    identities = []
    for folder in ["first", "second"]:
        workspace = Workspace(State(Storage(tmp_path / folder)))
        receipt = asyncio.run(install_campaigns(workspace, source=bundle))
        cid = receipt["campaigns"][0]["id"]
        current = workspace.current_composition(cid)
        plan = current["plan"]
        assert workspace.run_for(cid) is None
        assert len(plan.outputs) == 30 and len(current["sources"]) == 5
        old_ids = {p["id"] for p in original["products"]}
        assert old_ids.isdisjoint(p.id for p in plan.brief.products)
        for asset in plan.assets.values():
            assert "/Assets/" in asset.reference
            assert (
                hashlib.sha256(workspace.storage.path(asset.reference).read_bytes()).hexdigest()
                == asset.sha256
            )
        with workspace.storage.connect() as db:
            assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM creative_metadata").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM package_exports").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone()[0] == 2
        assert not list(workspace.storage.root.glob("Campaigns/*/Outputs"))
        identities.append({cid, *(p.id for p in plan.brief.products)})
    assert identities[0].isdisjoint(identities[1])


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_prepared_png_damage_refused_before_any_campaign_exists(tmp_path, bundle, damage):
    descriptor = json.loads((bundle / "supplied/prepared-outputs.json").read_bytes())
    target = bundle / descriptor["outputs"][0]["file"]
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(b"changed")
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    with pytest.raises(StorageError, match="bundled output"):
        asyncio.run(install_campaigns(workspace, source=bundle))
    assert not workspace.storage.path(RECEIPT).exists()
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


def test_prepared_contract_mismatch_cannot_publish_complete(tmp_path, bundle):
    descriptor_path = bundle / "supplied/prepared-outputs.json"
    descriptor = json.loads(descriptor_path.read_bytes())
    descriptor["outputs"][0]["output"]["copy"]["message"] = "Unrelated output wording"
    descriptor_path.write_text(json.dumps(descriptor))
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["campaigns"][0]["prepared_outputs"]["sha256"] = hashlib.sha256(
        descriptor_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    with pytest.raises(StorageError, match="wording or layout"):
        asyncio.run(install_campaigns(workspace, source=bundle))
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT state,current_revision FROM runs").fetchall() == []
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


def test_interrupted_source_verification_keeps_owned_inputs_and_setup_resumes(
    tmp_path, bundle, monkeypatch
):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    with monkeypatch.context() as patch:
        patch.setattr(
            "campaign_studio.bundled.verify_ready_sources",
            lambda *args: (_ for _ in ()).throw(OSError("controlled source check interruption")),
        )
        with pytest.raises(OSError, match="controlled source check interruption"):
            asyncio.run(install_campaigns(workspace, source=bundle))
    receipt = json.loads(workspace.storage.path(RECEIPT).read_bytes())
    campaign = receipt["campaigns"][0]["id"]
    source_refs = [
        asset.reference for asset in workspace.current_composition(campaign)["sources"].values()
    ]
    assert workspace.run_for(campaign) is None
    resumed = asyncio.run(install_campaigns(workspace, source=bundle))
    assert resumed["state"] == "complete"
    assert len(workspace.current_composition(campaign)["sources"]) == 5
    cleanup = Cleanup(workspace.state)
    cleanup.delete_campaign(campaign, cleanup.preview_campaign(campaign)["token"])
    assert all(not workspace.storage.path(ref).exists() for ref in source_refs)
    assert (bundle / "supplied/prepared-outputs.json").exists()


@pytest.mark.parametrize("failure", ["capacity", "product_limit"])
def test_first_start_failure_retries_after_environment_is_corrected(tmp_path, failure):
    root = tmp_path / "workspace"
    settings = Settings(
        data_dir=root,
        storage_budget_bytes=100_000 if failure == "capacity" else 10_000_000_000,
        product_limit=2 if failure == "product_limit" else 5,
    )
    with (
        pytest.raises(CapacityError if failure == "capacity" else StorageError),
        TestClient(create_app(settings, seed_examples=True), base_url="http://localhost"),
    ):
        pytest.fail("Setup must report its capacity or product-limit failure")
    state = State(Storage(root))
    with state.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 0
    assert not state.storage.path(RECEIPT).exists()
    with TestClient(
        create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
    ) as client:
        cards = client.get("/api/motif/workspace").json()["campaigns"]
        assert len(cards) == 2
        assert max(card["available_images"] for card in cards) == 30
        assert json.loads(state.storage.path(RECEIPT).read_bytes())["state"] == "complete"


@pytest.mark.parametrize("after", ["create", "upload", "save", "prepare", "outputs"])
def test_seed_retries_each_interrupted_stage_without_duplicate_uploads_or_runs(
    tmp_path, bundle, monkeypatch, after
):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    import campaign_studio.bundled as bundled

    owner, method = (bundled, "verify_ready_sources") if after == "outputs" else (Intake, after)
    original = getattr(owner, method)
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            raise OSError("Controlled interruption after committing a stage")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, interrupted)
        with pytest.raises(OSError, match="Controlled interruption"):
            asyncio.run(install_campaigns(workspace, source=bundle))
    first = json.loads(workspace.storage.path(RECEIPT).read_bytes())
    first_ids = [item["id"] for item in first["campaigns"]]
    result = asyncio.run(install_campaigns(workspace, source=bundle))
    assert result["state"] == "complete"
    assert [item["id"] for item in result["campaigns"]] == first_ids
    assert all(item["stage"] == "complete" for item in result["campaigns"])
    with workspace.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        uploads = [row[0] for row in db.execute("SELECT reference FROM uploads")]
    signatures = [
        (
            ref.split("/Assets/")[0],
            hashlib.sha256(workspace.storage.path(ref).read_bytes()).hexdigest(),
        )
        for ref in uploads
    ]
    assert len(signatures) == len(set(signatures))
    assert Workspace(workspace.state).run_for(first_ids[0]) is None
    assert len(workspace.current_composition(first_ids[0])["sources"]) == 5


@pytest.mark.parametrize("change", ["authored", "deleted", "damaged"])
@pytest.mark.parametrize("stage", ["attachments", "outputs"])
def test_seed_recovery_preserves_user_changes_and_deletion(
    tmp_path, bundle, monkeypatch, change, stage
):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    with monkeypatch.context() as patch:
        if stage == "attachments":
            patch.setattr(
                Intake,
                "upload",
                lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Stop setup")),
            )
        else:
            import campaign_studio.bundled as bundled

            original = bundled.verify_ready_sources

            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError("Stop setup after output publication")

            patch.setattr(bundled, "verify_ready_sources", interrupted)
        with pytest.raises(OSError, match="Stop setup"):
            asyncio.run(install_campaigns(workspace, source=bundle))
    record = json.loads(workspace.storage.path(RECEIPT).read_bytes())["campaigns"][0]
    cid = record["id"]
    path = workspace.storage.path(workspace.storage.project(cid).draft)
    if change == "authored":
        from campaign_studio.authored import AuthoredProjects, AuthoredSave

        projects = AuthoredProjects(workspace)
        current = projects.read(cid)
        current["authored"]["brief"]["name"] = "My own renamed campaign"
        current["authored"].setdefault("form_drafts", {})["fields"] = {
            "brief/name": "Unfinished work"
        }
        projects.save(
            cid, AuthoredSave(**{key: current[key] for key in ("version", "base", "authored")})
        )
    elif change == "deleted":
        cleanup = Cleanup(workspace.state)
        cleanup.delete_campaign(cid, cleanup.preview_campaign(cid)["token"])
    else:
        with workspace.storage.connect() as db:
            db.execute("UPDATE campaign_drafts SET document_json=? WHERE campaign_id=?", ("{", cid))
    with workspace.storage.connect() as db:
        before = db.execute(
            "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (cid,)
        ).fetchone()
    result = asyncio.run(install_campaigns(workspace, source=bundle))
    assert result["campaigns"][0]["stage"] == "preserved"
    assert result["campaigns"][1]["stage"] == "complete"
    with workspace.storage.connect() as db:
        assert (
            db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (cid,)
            ).fetchone()
            == before
        )
    assert not path.exists()
    from campaign_studio.bundled import setup_notice

    assert bool(setup_notice(workspace)) is (change != "deleted")


def test_diagnostics_initialized_empty_workspace_can_seed_but_unrelated_folder_is_not_adopted(
    tmp_path,
):
    root = tmp_path / "workspace"
    State(Storage(root))
    with TestClient(
        create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
    ) as client:
        assert len(client.get("/api/motif/workspace").json()["campaigns"]) == 2
    other = tmp_path / "unrelated"
    other.mkdir()
    (other / "keep.txt").write_text("Mine")
    for _ in range(2):
        with TestClient(
            create_app(Settings(data_dir=other), seed_examples=True), base_url="http://localhost"
        ) as client:
            assert client.get("/api/motif/workspace").json()["campaigns"] == []
    assert (other / "keep.txt").read_text() == "Mine"


def test_existing_campaign_workspace_does_not_gain_examples_after_later_deletion(tmp_path, bundle):
    workspace = Workspace(State(Storage(tmp_path)))
    cid, _ = Intake(workspace.storage).new(name="My original campaign")
    assert asyncio.run(install_campaigns(workspace, source=bundle)) is None
    cleanup = Cleanup(workspace.state)
    cleanup.delete_campaign(cid, cleanup.preview_campaign(cid)["token"])
    assert asyncio.run(install_campaigns(workspace, source=bundle)) is None
    assert workspace.catalog() == []


def interrupted_creation(root, checkpoint):
    environment = os.environ.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("bundled_setup_worker.py")),
            str(root),
            checkpoint,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 73, result.stdout + result.stderr
    workspace = Workspace(State(Storage(root)))
    receipt = json.loads(workspace.storage.path(RECEIPT).read_bytes())
    assert receipt["state"] == "started" and receipt["campaigns"][0]["stage"] == "create"
    return workspace, receipt


@pytest.mark.parametrize(
    "checkpoint",
    [
        "registered",
        "original-temporary",
        "original-written",
        "draft-before-commit",
        "draft-written",
    ],
)
def test_process_exit_during_initial_draft_resumes_the_same_examples(tmp_path, checkpoint):
    workspace, receipt = interrupted_creation(tmp_path / "workspace", checkpoint)
    ids = [record["id"] for record in receipt["campaigns"]]
    project = workspace.storage.project(ids[0])
    note = workspace.storage.path(project.ref("producer-note.txt"))
    note.write_bytes(b"Keep my note")
    resumed = asyncio.run(install_campaigns(workspace))
    assert [record["id"] for record in resumed["campaigns"]] == ids
    assert all(record["stage"] == "complete" for record in resumed["campaigns"])
    cards = workspace.catalog()
    assert len(cards) == 2
    assert all(card["status"] != "damaged" for card in cards)
    assert note.read_bytes() == b"Keep my note"
    source_counts = []
    for campaign in ids:
        assert Intake(workspace.storage).read(campaign)
        run = workspace.run_for(campaign)
        if run:
            assert workspace.state.attempts(run["id"]) == []
        source_counts.append(len(workspace.current_composition(campaign)["sources"]))
    assert sorted(source_counts) == [2, 5]
    assert not list(workspace.storage.root.glob("Campaigns/*/Outputs"))
    with workspace.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert asyncio.run(install_campaigns(workspace)) is None


@pytest.mark.parametrize("change", ["original", "extra-owner", "missing-owner"])
def test_initial_draft_recovery_does_not_replace_changed_campaign_work(tmp_path, change):
    workspace, receipt = interrupted_creation(tmp_path / "workspace", "registered")
    cid = receipt["campaigns"][0]["id"]
    project = workspace.storage.project(cid)
    changed = None
    if change == "original":
        changed = workspace.storage.path(project.ref("Brief/Imports/original.json"))
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_bytes(b"Producer changed this file")
    elif change == "extra-owner":
        reference = project.ref("Assets/producer.txt")
        project.write(reference, b"Producer changed this file")
        changed = workspace.storage.path(reference)
    else:
        with workspace.storage.connect() as db:
            db.execute(
                "DELETE FROM file_owners WHERE reference=?",
                (project.ref("Brief/Imports/original.json"),),
            )
    resumed = asyncio.run(install_campaigns(workspace))
    assert resumed["campaigns"][0]["stage"] == "preserved"
    assert resumed["campaigns"][1]["stage"] == "complete"
    assert not workspace.storage.path(project.draft).exists()
    if changed:
        assert changed.read_bytes() == b"Producer changed this file"
