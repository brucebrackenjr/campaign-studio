"""Workbench projects the saved composition and actual source-request outcomes."""

import asyncio

import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient
from test_provider_runner import generated_case

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.provider import ProviderFailure


@pytest.mark.parametrize("replay", [False, True])
def test_home_readiness_follows_current_saved_fields_without_publishing_images(tmp_path, replay):
    app = create_app(Settings(data_dir=tmp_path / "runtime"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        cid = seed_campaign(ws, replay=replay)

        def card():
            return next(
                c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
            )

        before = card()
        assert before["available_images"] == before["planned_images"] == 12
        current = client.get(f"/api/motif/campaigns/{cid}/authored").json()
        current["authored"]["brief"]["products"] = []
        response = client.post(
            f"/api/motif/campaigns/{cid}/authored",
            json={k: current[k] for k in ("version", "base", "authored")},
        )
        assert response.status_code == 200, response.text
        after = card()
        assert after["products"] == after["planned_images"] == after["available_images"] == 0
        assert after["status"] == "draft"
        assert ws.runs() == []
        assert not list(ws.storage.root.glob("Campaigns/*/Outputs"))


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_workbench_retains_request_outcomes_and_does_not_borrow_other_product_thumbnail(
    tmp_path, outcome
):
    state, runner, run, _ = generated_case(tmp_path)
    cid = state.run(run)["campaign_id"]
    calls = []

    class Failed:
        async def generate(self, *args):
            calls.append(outcome)
            raise ProviderFailure(
                outcome, "overall_deadline" if outcome == "unknown" else "http_401"
            )

    asyncio.run(runner.execute(run, Failed()))
    app = create_app(Settings(data_dir=state.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf

        def card():
            return next(
                c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
            )

        before = card()
        assert before["available_images"] == 6 and before["planned_images"] == 12
        assert before["outcome_unknown"] == (outcome == "unknown")
        assert before["thumbnail"] is None
        current = client.get(f"/api/motif/campaigns/{cid}/authored").json()
        current["authored"]["brief"]["name"] = "Renamed after request"
        response = client.post(
            f"/api/motif/campaigns/{cid}/authored",
            json={k: current[k] for k in ("version", "base", "authored")},
        )
        assert response.status_code == 200, response.text
        reopened = client.get(f"/api/motif/campaigns/{cid}/authored").json()
        assert reopened["authored"]["brief"]["name"] == "Renamed after request"
        after = card()
        assert after["outcome_unknown"] == before["outcome_unknown"]
        assert after["available_images"] == 6 and calls == [outcome]
        assert len(state.attempts(run)) == 1


def test_workbench_same_length_source_damage_changes_availability_and_recovers(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "runtime"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        cid = seed_campaign(ws, replay=True)

        def card():
            return next(
                c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
            )

        before = card()
        assert before["available_images"] == 12
        composition = ws.current_composition(cid)
        source = composition["sources"][composition["plan"].brief.products[0].id]
        path = ws.storage.path(source.reference)
        data = path.read_bytes()
        path.write_bytes(b"X" + data[1:])
        damaged = card()
        assert damaged["available_images"] == 6 and damaged["thumbnail"] is None
        path.write_bytes(data)
        restored = card()
        assert restored["available_images"] == 12
        assert restored["thumbnail"] == before["thumbnail"]
        assert ws.runs() == []
