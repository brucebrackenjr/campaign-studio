"""Curated first-start campaigns are ordinary owned work, never paid startup work."""

import asyncio
import copy
import hashlib
import importlib.util
import json
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.bundled import RECEIPT, install_campaigns, load_campaigns
from campaign_studio.bundled_outputs import install_prepared_outputs
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State
from campaign_studio.storage import Storage, StorageError
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
        raw["name"] = f"Synthetic {slug} campaign"
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
    original_install = install_prepared_outputs

    async def simultaneous_install():
        reached, release = threading.Event(), threading.Event()

        def hold_local(*args):
            reached.set()
            assert release.wait(10)
            return original_install(*args)

        monkeypatch.setattr("campaign_studio.bundled.install_prepared_outputs", hold_local)
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
    supplied, missing = [workspace.run_for(identity) for identity in identities]
    assert supplied["state"] == "complete"
    revision = workspace.state.revision(supplied["current_revision"])
    assert revision["image_count"] == 30
    assert missing["state"] == "planned" and missing["current_revision"] is None
    assert len(workspace.state.plan(missing["id"]).generation_products) == 1
    assert workspace.state.attempts(supplied["id"]) == []
    assert workspace.state.attempts(missing["id"]) == []
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
def test_official_first_start_uses_workspace_freshness_not_campaign_count(
    tmp_path, monkeypatch, existing
):
    root = tmp_path / "workspace"
    if existing == "workspace":
        State(Storage(root))  # Existing but empty is not a first installation.
    elif existing in {"empty", "unrelated-file"}:
        root.mkdir()
        if existing == "unrelated-file":
            (root / "leave-me.txt").write_text("unrelated content")
    calls = []

    async def record(workspace, **options):
        calls.append(workspace.storage.root)

    monkeypatch.setattr("campaign_studio.bundled.install_campaigns", record)
    for _ in range(2):
        with TestClient(
            create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
        ) as client:
            assert client.get("/health").status_code == 200
    assert calls == ([root] if existing in {"absent", "empty"} else [])
    if existing == "unrelated-file":
        assert (root / "leave-me.txt").read_text() == "unrelated content"


def test_interrupted_setup_preserves_owned_draft_and_next_start_never_reinserts(
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
    assert receipt["state"] == "started" and len(receipt["campaigns"]) == 1
    identity = receipt["campaigns"][0]["id"]
    before = Intake(state.storage).read(identity)
    assert before["attachment_hints"]["logo"] == "logo.png"
    with TestClient(
        create_app(Settings(data_dir=root), seed_examples=True), base_url="http://localhost"
    ) as client:
        assert client.get(f"/api/motif/campaigns/{identity}").status_code == 200
    assert Intake(state.storage).read(identity) == before
    with state.storage.connect() as db:
        assert [r[0] for r in db.execute("SELECT id FROM campaigns")] == [identity]
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
    _, campaigns = load_campaigns(bundle)
    _, original, _, prepared, _ = campaigns[0]
    with pytest.raises(StorageError, match="complete supplied-image plan"):
        install_prepared_outputs(workspace.state, missing["id"], original, prepared)
    with workspace.storage.connect() as db:
        assert workspace.state.run(missing["id"])["state"] == "planned"
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0


def test_prepared_install_never_renders_and_rebinds_all_outputs_and_metadata(
    tmp_path, bundle, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("Installing prepared examples must not render or execute production")

    monkeypatch.setattr(Runner, "execute", forbidden)
    monkeypatch.setattr("campaign_studio.rendering.compose", forbidden)
    _, definitions = load_campaigns(bundle)
    original = definitions[0][1]
    prepared = definitions[0][3].descriptor
    identities = []
    for folder in ["first", "second"]:
        workspace = Workspace(State(Storage(tmp_path / folder)))
        receipt = asyncio.run(install_campaigns(workspace, source=bundle))
        run = workspace.run_for(receipt["campaigns"][0]["id"])
        plan = workspace.state.plan(run["id"])
        revision = workspace.state.revision(run["current_revision"])
        manifest = json.loads(workspace.storage.path(revision["manifest_ref"]).read_bytes())
        assert run["state"] == "complete" and run["digest"] == plan.digest
        assert manifest["evidence"] == "bundled_pre_rendered"
        assert manifest["metrics"]["pre_rendered_images"] == 30
        assert manifest["metrics"]["local_render_seconds"] == 0
        assert manifest["metrics"]["attempted_requests"] == 0
        assert manifest["metrics"]["cost_status"] == "no_requests"
        assert manifest["metrics"]["completed_images"] == 30
        assert len(manifest["outputs"]) == 30
        old_ids = {p["id"] for p in original["products"]}
        assert old_ids.isdisjoint(p.id for p in plan.brief.products)
        assert all(old not in json.dumps(manifest) for old in old_ids)
        for item, record in zip(manifest["outputs"], prepared["outputs"], strict=True):
            path = workspace.storage.path(revision["manifest_ref"]).parent / item["file"]
            assert path.read_bytes() == (bundle / record["file"]).read_bytes()
            assert item["sha256"] == record["sha256"]
            assert item["pixel_sha256"] == record["pixel_sha256"]
        for asset in plan.assets.values():
            assert asset.reference.startswith(
                workspace.storage.project(run["campaign_id"]).run(run["id"]) + "/Inputs/"
            )
            assert (
                hashlib.sha256(workspace.storage.path(asset.reference).read_bytes()).hexdigest()
                == asset.sha256
            )
        with workspace.storage.connect() as db:
            metadata = db.execute("SELECT creative_id,value_json FROM creative_metadata").fetchall()
            assert len(metadata) == 30
            assert {row[0] for row in metadata} == {o.id for o in plan.outputs}
            assert all(json.loads(row[1])["_context"]["content"] for row in metadata)
            assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM package_exports").fetchone()[0] == 0
            assert (
                db.execute(
                    "SELECT count(*) FROM file_owners WHERE reference=?",
                    (workspace.storage.project(run["campaign_id"]).run(run["id"]),),
                ).fetchone()[0]
                == 1
            )
        identities.append(
            {run["campaign_id"], run["id"], revision["id"], *(p.id for p in plan.brief.products)}
        )
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
        assert db.execute("SELECT state,current_revision FROM runs").fetchall() == [
            ("planned", None)
        ]


def test_interrupted_prepared_copy_stays_owned_and_is_not_reseeded(tmp_path, bundle, monkeypatch):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    write = workspace.storage.write
    copied = []

    def interrupted(reference, payload):
        if "/Revisions/" in reference and "/images/" in reference:
            if copied:
                raise OSError("controlled copy interruption")
            copied.append(reference)
        return write(reference, payload)

    with monkeypatch.context() as patch:
        patch.setattr(workspace.storage, "write", interrupted)
        with pytest.raises(OSError, match="controlled copy interruption"):
            asyncio.run(install_campaigns(workspace, source=bundle))
    receipt = json.loads(workspace.storage.path(RECEIPT).read_bytes())
    campaign = receipt["campaigns"][0]["id"]
    run = workspace.run_for(campaign)
    assert run["state"] == "planned" and run["current_revision"] is None
    assert workspace.storage.path(copied[0]).exists()
    assert asyncio.run(install_campaigns(workspace, source=bundle)) is None
    cleanup = Cleanup(workspace.state)
    preview = cleanup.preview_campaign(campaign)
    cleanup.delete_campaign(campaign, preview["token"])
    assert not workspace.storage.path(copied[0]).exists()
    assert (bundle / "supplied/prepared-outputs.json").exists()
