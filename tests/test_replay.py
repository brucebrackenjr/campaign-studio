import asyncio
import io
import json
import re
import shutil
from zipfile import ZipFile

import pytest
from campaign_fixtures import independent_brief, seed_campaign
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from test_provider_runner import save_current

from campaign_studio.app import create_app
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.planning import make_plan
from campaign_studio.runner import Runner
from campaign_studio.sample import write_replay
from campaign_studio.state import State, StateError
from campaign_studio.storage import Storage
from campaign_studio.workspace import Workspace


@pytest.fixture
def replay(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "campaign_studio.provider.OpenAIImages.__init__",
        lambda *args, **kwargs: pytest.fail("Replay constructed a provider"),
    )
    storage = Storage(tmp_path / "runtime")
    return storage, write_replay(storage)


def test_genuine_replay_corrects_six_reopens_after_move_and_exports_without_attempts(replay):
    storage, brief = replay
    plan = make_plan(brief, storage)
    assert not plan.blocked and plan.generation_products == []
    assert plan.cost_status == "no_new_requests"
    assert plan.brief.products[0].source.role == "concept"
    state = State(storage)
    run_id = state.create(plan, 0)
    state.confirm(run_id, state.plan(run_id).digest)
    runner = Runner(state)
    first = asyncio.run(runner.execute(run_id))
    assert first["complete"] and first["sources_ready"] == 2
    assert first["outputs"] == [] and first["metrics"]["attempted_requests"] == 0
    with (
        Handoff(state).create_current_zip(
            brief.campaign_id, DeliveryAction(version=first["version"])
        ) as download,
        ZipFile(download.stream) as archive,
    ):
        initial = {
            name.split("/", 1)[1]: archive.read(name)
            for name in archive.namelist()
            if name.endswith(".png")
        }
    saved = save_current(
        state,
        brief.campaign_id,
        lambda b: b["markets"]["es-MX"]["copy"].update(message="Para cada día."),
    )
    with (
        Handoff(state).create_current_zip(
            brief.campaign_id, DeliveryAction(version=saved["version"])
        ) as download,
        ZipFile(download.stream) as archive,
    ):
        changed = {
            name.split("/", 1)[1]: archive.read(name)
            for name in archive.namelist()
            if name.endswith(".png")
        }

    def pixels(payload):
        with Image.open(io.BytesIO(payload)) as image:
            return image.convert("RGB").tobytes()

    assert len(changed) == 12
    assert sum(pixels(initial[name]) != pixels(value) for name, value in changed.items()) == 6
    assert state.attempts(run_id) == [] and state.revision_count(run_id) == 0
    moved = storage.root.with_name("moved-runtime")
    shutil.move(storage.root, moved)
    reopened = State(Storage(moved))
    version = Workspace(reopened).current_composition(brief.campaign_id)["version"]
    assert reopened.attempts(run_id) == []
    with (
        Handoff(reopened).create_current_zip(
            brief.campaign_id, DeliveryAction(version=version)
        ) as download,
        ZipFile(download.stream) as archive,
    ):
        archive.extractall(moved.parent / "delivered")
        package = moved.parent / "delivered" / download.filename.removesuffix(".zip")
    html = (package / "index.html").read_text(encoding="utf-8")
    assert "Campaign spend estimate" in html
    assert "No generation attempts in this campaign." in html
    assert (
        "Example imagery was prepared earlier: 1 historical requests. "
        "Excluded from this campaign’s totals."
    ) in html
    assert "visually unapproved" not in html
    for reference in re.findall(r'(?:href|src)="([^"]+)"', html):
        if reference.startswith("#"):
            assert f'id="{reference[1:]}"' in html
        else:
            assert (package / reference).is_file()
    public = json.loads((package / "manifest.json").read_bytes())
    assert public["metrics"]["completed_images"] == 12
    assert public["campaign_metrics"]["coverage"] == "complete"
    assert public["campaign_metrics"]["attempted_requests"] == 0
    assert public["campaign_metrics"]["usage_cost_estimate"]["estimated_usd"] == 0
    assert public["replay"]["historical_requests"] == 1
    serialized = json.dumps(public)
    assert not any(
        value in serialized
        for value in ("/Users/", "/home/", "response_ref", "source_ref", '"prompt"')
    )

    def check_paths(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "path":
                    # Exported producer context contains brief field addresses,
                    # never the string filesystem paths of original/replay inputs.
                    assert isinstance(child, list) and child
                    assert child[0] in {"markets", "products", "brand"}
                    assert all(
                        isinstance(part, (str, int)) and "/" not in str(part) for part in child
                    )
                check_paths(child)
        elif isinstance(value, list):
            for child in value:
                check_paths(child)

    check_paths(public)


@pytest.mark.parametrize("problem", ["missing", "changed", "wrong_hash"])
def test_replay_master_failure_blocks_without_generation_fallback(replay, problem):
    storage, brief = replay
    master = brief.replay.sources["product-a"]
    if problem == "missing":
        storage.path(master.path).unlink()
    elif problem == "changed":
        storage.path(master.path).write_bytes(
            storage.path(brief.products[1].source.path).read_bytes()
        )
    else:
        master.sha256 = "0" * 64
    plan = make_plan(brief, storage)
    assert plan.blocked and plan.generation_products == []
    state = State(storage)
    with pytest.raises(StateError, match="Correct the campaign details"):
        state.create(plan, 0)
    with storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_replay_source_changed_after_confirmation_cannot_render(replay):
    storage, brief = replay
    state = State(storage)
    run_id = state.create(make_plan(brief, storage), 0)
    plan = state.plan(run_id)
    state.confirm(run_id, plan.digest)
    storage.path(plan.assets["masters/product-a"].reference).write_bytes(b"changed saved master")
    with pytest.raises(StateError, match="saved source changed"):
        asyncio.run(Runner(state).execute(run_id))
    assert state.attempts(run_id) == []


@pytest.mark.parametrize("product_id", ["unknown-product", "product-b"])
def test_replay_mapping_rejects_unknown_and_finished_scene_ids(replay, product_id):
    _, brief = replay
    value = brief.model_dump()
    value["replay"]["sources"][product_id] = value["replay"]["sources"].pop("product-a")
    with pytest.raises(ValidationError, match="exactly every"):
        Brief.model_validate(value)


def test_replay_concept_preserves_original_absent_source_state(replay):
    storage, brief = replay
    value = brief.model_dump()
    value["products"][0]["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "Original conceptual input before the saved imagery existed.",
        "provenance": value["products"][0]["source"]["provenance"],
    }
    plan = make_plan(Brief.model_validate(value), storage)
    assert not plan.blocked and plan.generation_products == []
    assert plan.brief.products[0].source.imagery_absent is True


def test_replay_loads_from_installed_resources_in_key_free_http_workspace(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Replay constructed a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        home = client.get("/")
        assert home.status_code == 200 and 'src="/static/motif/presentation.js"' in home.text
        assert "Open example" not in client.get("/static/motif/presentation.js").text
        client.headers["X-Campaign-Token"] = re.search(
            r'name="campaign-token" content="([^"]+)"', home.text
        )[1]
        seed_campaign(app.state.workspace, replay=True)
        production = client.get("/campaigns/74d68837-ca7c-509b-b296-2abd7122b1b8/production")
        assert production.status_code == 200 and 'data-page="production"' in production.text
        model = client.get("/api/motif/campaigns/74d68837-ca7c-509b-b296-2abd7122b1b8").json()
        assert model["plan"]["generation_products"] == []
        assert not any(finding["severity"] == "blocking" for finding in model["plan"]["findings"])
        assert model["notice"] is None and not model["unplanned_draft"]
        assert not client.get("/api/motif/workspace").json()["key_available"]
        assert client.get(model["concept_previews"]["product-a"]).status_code == 200
        run = app.state.workspace.run_for("74d68837-ca7c-509b-b296-2abd7122b1b8")
        assert run is None
        assert (
            app.state.workspace.state.campaign_metrics(model["value"]["campaign_id"])[
                "attempted_requests"
            ]
            == 0
        )


def test_loading_curated_example_preserves_an_existing_edited_example(tmp_path):
    from campaign_studio.workspace import Workspace

    storage = Storage(tmp_path / "runtime")
    state = State(storage)
    old = write_replay(storage).model_dump(mode="json")
    old["campaign_id"] = "c0c806f2-bf09-5af8-b694-5807b0a9bf72"
    old["name"] = "My saved example edits"
    old["markets"]["en-US"]["copy"]["message"] = "My existing words"
    copied = independent_brief(state, Brief.model_validate(old), old["campaign_id"])
    original_run = state.create(make_plan(copied, storage), 0)
    original_digest = state.plan(original_run).digest
    ws = Workspace(state)
    cid = seed_campaign(ws, replay=True)
    assert cid == "74d68837-ca7c-509b-b296-2abd7122b1b8"
    assert state.plan(original_run).digest == original_digest
    assert state.plan(original_run).brief.markets["en-US"].text.message == "My existing words"
    assert ws.run_for(cid) is None
    assert ws.current_composition(cid)["plan"].brief.campaign_id == cid
    assert state.attempts(original_run) == []
    assert ws.state.campaign_metrics(cid)["attempted_requests"] == 0
