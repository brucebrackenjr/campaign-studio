"""Selected-language intake stays coherent through local production and handoff."""

import copy
import io
import json
from zipfile import ZipFile

import pytest
from campaign_fixtures import authored_edit, zip_root
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.sample import write_sample


@pytest.mark.parametrize("markets", [("en-US",), ("es-MX",), ("en-US", "es-MX")])
def test_selected_languages_import_save_produce_correct_export_and_reopen(tmp_path, markets):
    settings = Settings(data_dir=tmp_path / "runtime")

    def application():
        return create_app(
            settings,
            key_available=lambda: False,
            provider_factory=lambda: pytest.fail("Selected-language work must stay local"),
        )

    app = application()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        _, fixture = write_sample(ws.storage)
        value = fixture.model_dump(mode="json")
        value["markets"] = {key: value["markets"][key] for key in markets}
        imported = client.post("/api/motif/import", json=value)
        assert imported.status_code == 200, imported.text
        campaign = imported.json()["campaign_id"]
        base = f"/api/motif/campaigns/{campaign}"
        document = client.get(base).json()
        assert set(document["value"]["markets"]) == set(markets)

        def upload(reference):
            result = client.post(
                "/api/uploads",
                content=ws.storage.path(reference).read_bytes(),
                headers={"Content-Type": "image/png", "X-Campaign-Id": campaign},
            )
            assert result.status_code == 200, result.text
            return result.json()["reference"]

        draft = document["value"]
        draft["audience"] = "Saved selected-language audience"
        draft["brand"]["logo_path"] = upload(fixture.brand.logo_path)
        for product, original in zip(draft["products"], fixture.products, strict=True):
            product["source"]["path"] = upload(original.source.path)
        action = authored_edit(client, campaign)
        action["authored"]["brief"] = draft
        saved = client.post(base + "/authored", json=action)
        assert saved.status_code == 200, saved.text
        assert client.get(base).json()["project"]["authored"]["brief"] == draft
        expected = 2 * len(markets) * 3
        before = client.get(base).json()
        assert before["manifest"]["complete"] and before["can_edit"]
        assert before["run"] is None
        assert before["manifest"]["preview_only"]
        market = markets[-1]
        edit = {
            "ratio": "1:1",
            "revision_id": before["version"],
            "market": market,
            "product_id": before["output_brief"]["products"][0]["id"],
            "field_scopes": {"message": "all_products", "cta": "all_products"},
            "values": before["output_brief"]["markets"][market]["copy"]
            | {"message": "Para cada salida." if market == "es-MX" else "Made for the next trip."},
        }
        preview = client.post(f"/api/campaigns/{campaign}/text/preview", json=edit)
        assert preview.status_code == 200 and not preview.json()["blocked"], preview.text
        assert len(preview.json()["impact"]["render_outputs"]) == 6
        assert preview.json()["impact"]["generation_products"] == []
        saved = client.post(f"/api/campaigns/{campaign}/text/save", json=edit)
        assert saved.status_code == 200, saved.text
        after = client.get(base).json()
        outputs = after["manifest"]["outputs"]
        assert len(outputs) == expected and {item["market"] for item in outputs} == set(markets)
        for output in outputs:
            if output["market"] == market:
                assert output["copy"]["message"] == edit["values"]["message"]
            else:
                previous = next(o for o in before["manifest"]["outputs"] if o["id"] == output["id"])
                assert output["copy"] == previous["copy"]
        package = client.post(
            f"/api/campaigns/{campaign}/delivery/zip", json={"version": after["version"]}
        )
        assert package.status_code == 200, package.text
        with ZipFile(io.BytesIO(package.content)) as archive:
            assert sum(name.endswith(".png") for name in archive.namelist()) == expected
            assert archive.testzip() is None
            report = json.loads(archive.read(f"{zip_root(archive)}/manifest.json"))
            assert {item["market"] for item in report["outputs"]} == set(markets)
            assert {o["id"]: o["copy"] for o in report["outputs"]} == {
                o["id"]: o["copy"] for o in outputs
            }
        with ws.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert not list(ws.storage.root.glob("Campaigns/*/Outputs"))
        assert not list(ws.storage.root.glob("Campaigns/*/Exports"))

    # Reopen through a fresh application owner, with no in-memory draft or plan.
    with TestClient(application(), base_url="http://127.0.0.1") as client:
        reopened = client.get(base).json()
        assert set(reopened["value"]["markets"]) == set(markets)
        assert reopened["value"]["audience"] == "Saved selected-language audience"
        assert reopened["manifest"]["revision_id"] == after["manifest"]["revision_id"]
        assert len(reopened["manifest"]["outputs"]) == expected
        assert reopened["manifest"]["preview_only"]


def test_invalid_selected_languages_preserve_drafts_and_five_product_limit(tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "runtime"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        created = client.post("/api/motif/campaigns", json={"name": "Untouched draft"}).json()
        base = f"/api/motif/campaigns/{created['campaign_id']}"
        original = client.get(base).json()
        assert set(original["value"]["markets"]) == {"en-US", "es-MX"}
        _, fixture = write_sample(app.state.workspace.storage)
        imported = fixture.model_dump(mode="json")
        for selection in [{}, {"fr-FR": imported["markets"]["en-US"]}, [], None]:
            invalid = copy.deepcopy(imported)
            invalid["markets"] = selection
            assert client.post("/api/motif/import", json=invalid).status_code == 422
            invalid_draft = copy.deepcopy(original["value"])
            invalid_draft["markets"] = selection
            invalid_draft["products"] = [copy.deepcopy(imported["products"][0])]
            invalid_draft["products"][0]["source"]["path"] = ""
            action = authored_edit(client, created["campaign_id"])
            action["authored"]["brief"] = invalid_draft
            saved = client.post(base + "/authored", json=action)
            assert saved.status_code == 200, saved.text
            assert client.get(base + "/authored").json()["authored"]["brief"] == invalid_draft
            refused = client.post(base + "/plan", json={"version": saved.json()["version"]})
            assert refused.status_code == 422, refused.text
            assert any(field["path"] == ["markets"] for field in refused.json()["field_errors"])
            assert client.get(base + "/authored").json()["authored"]["brief"] == invalid_draft
            assert app.state.workspace.runs() == []
            restored = authored_edit(client, created["campaign_id"])
            restored["authored"]["brief"] = original["value"]
            assert client.post(base + "/authored", json=restored).status_code == 200
        imported["products"] = [copy.deepcopy(imported["products"][0]) for _ in range(6)]
        for index, product in enumerate(imported["products"]):
            product["id"] = f"product-{index}"
        refused = client.post("/api/motif/import", json=imported)
        assert refused.status_code == 422
        assert refused.json()["field_errors"][0]["path"] == ["products"]
        catalog = client.get("/api/motif/workspace").json()["campaigns"]
        assert {item["id"] for item in catalog} == {created["campaign_id"], fixture.campaign_id}
        assert app.state.workspace.runs() == []
