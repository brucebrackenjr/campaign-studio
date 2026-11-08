"""Current product readiness must not inherit an obsolete paid request plan."""

import base64
import copy
import io
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from campaign_fixtures import authored_edit, zip_root
from fastapi.testclient import TestClient
from test_web_workflow import enable_actions, wait_for_work

from campaign_studio.app import create_app
from campaign_studio.campaign_drafts import write as write_draft
from campaign_studio.config import Settings
from campaign_studio.sample import write_sample


def save_products(client, cid, products):
    action = authored_edit(client, cid)
    action["authored"]["brief"]["products"] = copy.deepcopy(products)
    result = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
    assert result.status_code == 200, result.text
    return result.json()


def prepare(client, cid, version):
    response = client.post(f"/api/motif/campaigns/{cid}/plan", json={"version": version})
    assert response.status_code == 200, response.text
    return response.json()


def generate(client, cid, data):
    response = client.post(
        f"/api/campaigns/{cid}/start",
        json={
            "run_id": data["run"]["id"],
            "digest": data["plan"]["digest"],
            "allowance": data["run"]["allowance"],
        },
    )
    assert response.status_code == 202, response.text
    wait_for_work(client, cid)
    return client.get(f"/api/motif/campaigns/{cid}").json()


@pytest.mark.parametrize("generated_count", [0, 1, 3])
def test_three_to_four_to_five_to_four_products_reuses_ready_sources(tmp_path, generated_count):
    calls = []
    body = {}

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    from campaign_studio.provider import OpenAIImages

    app = create_app(
        Settings(data_dir=tmp_path / "workspace"),
        key_available=lambda: True,
        provider_factory=lambda: OpenAIImages("mock-key", transport=httpx.MockTransport(transport)),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        enable_actions(client)
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        value = brief.model_dump(mode="json")
        cid = value["campaign_id"]
        photo = copy.deepcopy(value["products"][0])
        body.update(
            data=[
                {
                    "b64_json": base64.b64encode(
                        ws.storage.path(photo["source"]["path"]).read_bytes()
                    ).decode()
                }
            ]
        )
        value["products"].append(copy.deepcopy(photo) | {"id": "third", "name": "Third product"})
        for product in value["products"][:generated_count]:
            product["source"] = {
                "role": "concept",
                "imagery_absent": True,
                "description": "Synthetic scene for controlled tests.",
                "provenance": {"label": "Test", "rights": "Test", "synthetic": True},
            }
        write_draft(
            ws.storage,
            cid,
            {"composition_schema": 1, "version": uuid4().hex, "authored": {"brief": value}},
        )
        data = client.get(f"/api/motif/campaigns/{cid}").json()
        if generated_count:
            data = generate(client, cid, prepare(client, cid, data["version"]))
        assert len(calls) == generated_count
        products = copy.deepcopy(data["value"]["products"])
        products.append(photo | {"id": "fourth", "name": "Fourth supplied product"})
        data = save_products(client, cid, products)
        assert data["plan"]["generation_products"] == []
        assert data["unplanned_draft"] is False
        assert data["manifest"]["complete"]
        assert len(data["manifest"]["outputs"]) == 24

        products.append(
            copy.deepcopy(products[0])
            | {
                "id": "fifth",
                "name": "Fifth concept",
                "source": {
                    "role": "concept",
                    "imagery_absent": True,
                    "description": "Another synthetic test scene.",
                    "provenance": {"label": "Test", "rights": "Test", "synthetic": True},
                },
            }
        )
        data = save_products(client, cid, products)
        reopened = client.get(f"/api/motif/campaigns/{cid}").json()
        assert reopened["plan"]["generation_products"] == ["fifth"]
        assert reopened["unplanned_draft"] is True
        data = prepare(client, cid, data["version"])
        assert data["unplanned_draft"] is False
        assert data["plan"]["generation_products"] == ["fifth"]
        abandoned = data["run"]["id"]
        # Equivalent raw form representations must re-admit the same plan,
        # rather than keep its obsolete save guard or allocate another run.
        equivalent = copy.deepcopy(data["value"]["products"])
        if "framing" in equivalent[-1]:
            del equivalent[-1]["framing"]
        else:
            equivalent[-1]["framing"] = {}
        data = save_products(client, cid, equivalent)
        run_count = len(ws.runs(cid))
        data = prepare(client, cid, data["version"])
        assert data["unplanned_draft"] is False
        assert data["run"]["id"] == abandoned
        assert len(ws.runs(cid)) == run_count
        data = save_products(client, cid, products[:-1])
        assert data["plan"]["generation_products"] == []
        assert data["unplanned_draft"] is False
        assert data["manifest"]["complete"]
        assert data["can_edit"]
        original_version = data["version"]
        runs = len(ws.runs(cid))
        for _ in range(3):
            data = prepare(client, cid, data["version"])
            assert not data["unplanned_draft"]
            assert data["plan"]["generation_products"] == []
            assert data["version"] == original_version
        assert len(ws.runs(cid)) == runs
        assert len(calls) == generated_count
        # An old confirmation cannot start the deleted product's request.
        response = client.post(
            f"/api/campaigns/{cid}/start",
            json={"run_id": abandoned, "digest": ws.state.plan(abandoned).digest, "allowance": 1},
        )
        assert response.status_code == 409
        assert len(calls) == generated_count
        delivery = client.post(
            f"/api/campaigns/{cid}/delivery/zip", json={"version": data["version"]}
        )
        assert delivery.status_code == 200, delivery.text[:300]
        with ZipFile(io.BytesIO(delivery.content)) as archive:
            zip_root(archive)
            assert len([name for name in archive.namelist() if name.endswith(".png")]) == 24
            assert archive.testzip() is None
        with ws.storage.connect() as db:
            assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        if generated_count:
            action = authored_edit(client, cid)
            audience = action["authored"]["brief"]["audience"]
            action["authored"]["brief"]["audience"] += " A different audience."
            response = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
            assert response.status_code == 200, response.text
            changed = response.json()
            assert len(changed["plan"]["generation_products"]) == generated_count
            assert len(changed["plan"]["changed_source_products"]) == generated_count
            assert changed["unplanned_draft"]
            assert len(calls) == generated_count
            action = authored_edit(client, cid)
            action["authored"]["brief"]["audience"] = audience
            restored = client.post(f"/api/motif/campaigns/{cid}/authored", json=action).json()
            assert restored["plan"]["generation_products"] == []
            assert restored["plan"]["changed_source_products"] == []
            assert not restored["unplanned_draft"]
            assert restored["manifest"]["complete"]
