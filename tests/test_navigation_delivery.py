"""Current Handoff descriptions, language-specific filenames, and source truth."""

import asyncio
import copy

import httpx
from test_prohibited_exports import campaign
from test_provider_runner import generated_case

from campaign_studio import delivery
from campaign_studio.compositions import snapshot
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.workspace import Workspace


def test_handoff_description_current_patch_persists_and_enters_report(tmp_path):
    with campaign(tmp_path, products=1) as (client, ws, base):
        cid = base.rsplit("/", 1)[1]
        current = snapshot(ws, cid)
        output = current["plan"].outputs[0]
        description = "A brass lamp beside an open book."
        response = client.post(
            f"/api/campaigns/{cid}/composition/{current['version']}/metadata/{output.id}",
            json={"mode": "current", "expected_version": 0, "description": description},
            headers={"X-Campaign-Token": client.app.state.csrf},
        )
        assert response.status_code == 200, response.text
        reopened = snapshot(ws, cid)
        assert reopened["metadata"][output.id]["description"] == description
        manifest = delivery.delivery_manifest(reopened)
        assert manifest["outputs"][0]["accessible_description"] == description
        html = delivery.document(reopened, manifest, "index.html").decode()
        assert description in html


def test_individual_png_names_distinguish_markets_without_changing_dimensions(tmp_path):
    with campaign(tmp_path, products=1) as (_, ws, base):
        current = snapshot(ws, base.rsplit("/", 1)[1])
        brief = current["plan"].brief.model_dump(mode="json")
        brief["markets"]["es-MX"] = copy.deepcopy(brief["markets"]["en-US"])
        current["plan"] = make_plan(type(current["plan"].brief).model_validate(brief), ws.storage)
        names = []
        for market in ["en-US", "es-MX"]:
            output = next(
                o for o in current["plan"].outputs if o.market == market and o.ratio == "1:1"
            )
            payload, name = delivery.current_image(ws.state, current, output.id)
            assert payload.startswith(b"\x89PNG")
            assert market in name
            assert "1080x1080" in name
            names.append(name)
        assert names[0] != names[1]


def test_report_generated_origin_follows_verified_decoded_source_not_producer_text(tmp_path):
    state, runner, run_id, body = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    assert asyncio.run(runner.execute(run_id, provider))["complete"]
    ws = Workspace(state)
    cid = state.run(run_id)["campaign_id"]
    current = snapshot(ws, cid)
    pid = current["plan"].brief.products[0].id
    assert current["source_origins"][pid] == "AI-generated source photograph"
    manifest = delivery.delivery_manifest(current)
    assert manifest["sources"][0]["imagery_origin"] == "AI-generated source photograph"
    assert (
        "AI-generated source photograph"
        in delivery.document(current, manifest, "index.html").decode()
    )
    assert len(state.attempts(run_id)) == 1


def test_supplied_source_is_not_classified_as_application_generation(tmp_path):
    with campaign(tmp_path, products=1) as (_, ws, base):
        current = snapshot(ws, base.rsplit("/", 1)[1])
        assert set(current["source_origins"].values()) == {"Supplied source photograph"}
