"""Curated first-start campaigns are ordinary owned work, never paid startup work."""

import asyncio
import copy
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.bundled import RECEIPT, install_campaigns, load_campaigns
from campaign_studio.cleanup import Cleanup
from campaign_studio.config import Settings
from campaign_studio.intake import Intake
from campaign_studio.runner import Runner
from campaign_studio.sample import write_sample
from campaign_studio.state import State
from campaign_studio.storage import Storage, StorageError
from campaign_studio.workspace import Workspace


@pytest.fixture
def bundle(tmp_path):
    fixture_storage = Storage(tmp_path / "synthetic-inputs")
    _, example = write_sample(fixture_storage, 3)
    source = tmp_path / "bundle"
    manifest = {"version": 1, "campaigns": []}
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
        manifest["campaigns"].append(
            {
                "slug": slug,
                "brief": f"{slug}/brief.json",
                "compose": compose,
                "files": {p: hashlib.sha256(b).hexdigest() for p, b in payloads.items()},
            }
        )
    (source / "manifest.json").write_text(json.dumps(manifest))
    return source


def test_install_claim_local_composition_missing_plan_and_deleted_examples_stay_deleted(
    tmp_path, bundle, monkeypatch
):
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    runner = Runner(workspace.state)
    original_execute = runner.execute

    async def simultaneous_install():
        reached, release = asyncio.Event(), asyncio.Event()

        async def hold_local(run_id, provider=None):
            assert provider is None
            reached.set()
            await release.wait()
            return await original_execute(run_id)

        monkeypatch.setattr(runner, "execute", hold_local)
        first = asyncio.create_task(install_campaigns(workspace, runner, source=bundle))
        arrival = asyncio.create_task(reached.wait())
        done, _ = await asyncio.wait({first, arrival}, return_when=asyncio.FIRST_COMPLETED)
        if first in done:
            arrival.cancel()
            return await first
        assert await install_campaigns(workspace, runner, source=bundle) is None
        release.set()
        return await first

    receipt = asyncio.run(simultaneous_install())
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
    assert asyncio.run(install_campaigns(workspace, runner, source=bundle)) is None
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

    async def record(workspace, runner, **options):
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
        asyncio.run(install_campaigns(workspace, Runner(workspace.state), source=bundle))
    assert not workspace.storage.path(RECEIPT).exists()
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0


def test_compose_flag_cannot_authorize_missing_imagery(tmp_path, bundle, monkeypatch):
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    manifest["campaigns"].reverse()
    manifest["campaigns"][0]["compose"] = True
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    workspace = Workspace(State(Storage(tmp_path / "workspace")))
    runner = Runner(workspace.state)

    async def unexpected_execution(*args, **kwargs):
        pytest.fail("A missing source must be refused before execution or confirmation")

    monkeypatch.setattr(runner, "execute", unexpected_execution)
    with pytest.raises(StorageError, match="requires new imagery"):
        asyncio.run(install_campaigns(workspace, runner, source=bundle))
    with workspace.storage.connect() as db:
        assert [r[0] for r in db.execute("SELECT state FROM runs")] == ["planned"]
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
