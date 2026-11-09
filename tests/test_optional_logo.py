"""Optional logos preserve editing, source reuse, and strict current delivery checks."""

import asyncio
import io
import json
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_prohibited_exports import export_urls, save

from campaign_studio.app import create_app
from campaign_studio.brief import Brand, Brief
from campaign_studio.campaign_drafts import write
from campaign_studio.config import Settings
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_sample


@pytest.fixture
def unbranded(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Logo changes must stay local"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        _, original = write_sample(ws.storage, 2, True)
        value = original.model_dump(mode="json")
        value["products"] = value["products"][:1]
        value["brand"].pop("logo_path")
        value["brand"].pop("logo_provenance")
        write(
            ws.storage,
            original.campaign_id,
            {"authored": {"brief": value}, "version": uuid4().hex, "composition_schema": 1},
        )
        yield client, ws, f"/api/motif/campaigns/{original.campaign_id}", original


def exported(client, current):
    paths = export_urls(current)
    result = client.post(paths["zip"], json=paths["body"])
    assert result.status_code == 200, result.text
    with ZipFile(io.BytesIO(result.content)) as archive:
        name = next(n for n in archive.namelist() if n.endswith("manifest.json"))
        prefix = name.removesuffix("manifest.json")
        return json.loads(archive.read(name)), archive.read(prefix + "index.html").decode()


def test_legacy_logo_requirement_is_accepted_but_no_longer_published():
    value = {
        "id": "brand",
        "name": "Brand",
        "logo_path": "old/logo.png",
        "logo_provenance": {"label": "Supplied", "rights": "Owned", "synthetic": False},
        "palette": ["#FFFFFF"],
        "text_color": "#FFFFFF",
    }
    assert Brand.model_validate(value).model_dump() == value
    assert Brand.model_validate(value | {"require_logo": False}).model_dump() == value
    assert Brand.model_validate(value | {"require_logo": True}).model_dump() == value
    for bad in (None, 1, "false"):
        with pytest.raises(ValidationError):
            Brand.model_validate(value | {"require_logo": bad})


def test_unbranded_autosaved_edit_preview_and_report(unbranded):
    client, ws, url, _ = unbranded
    current = client.get(url).json()
    manifest = current["manifest"]
    cid = current["value"]["campaign_id"]
    warning = [f for f in manifest["findings"] if f["rule"] == "logo_presence"]
    assert len(warning) == 1 and warning[0]["severity"] == "warning"
    assert all(current["editable_products"].values())
    edit = {
        "revision_id": current["version"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
    }
    logo = client.post(f"/api/campaigns/{cid}/logo/preview", json=edit)
    assert logo.status_code == 200, logo.text
    assert logo.json()["logo_asset"] is None
    text = edit | {
        "values": {"message": "Fresh wording.", "cta": "See more."},
        "field_scopes": {"message": "product", "cta": "product"},
    }
    saved = client.post(f"/api/campaigns/{cid}/text/save", json=text)
    assert saved.status_code == 200, saved.text
    frame = edit | {"revision_id": saved.json()["version"], "framing": {"zoom": 1.1}}
    saved = client.post(f"/api/campaigns/{cid}/framing/save", json=frame)
    assert saved.status_code == 200, saved.text
    current = client.get(url).json()
    assert client.get(current["manifest"]["outputs"][0]["url"]).status_code == 200
    result, html = exported(client, current)
    assert "These images have no logo." in html and "<h3>Logo origin</h3>" not in html
    assert all(o["copy"]["message"] == "Fresh wording." for o in result["outputs"])
    assert not any(f["rule"] == "logo_pixels" for f in result["findings"])
    assert not list(ws.storage.root.rglob("Outputs"))
    with ws.storage.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_legacy_required_logo_stays_optional_and_new_artwork_updates_export(unbranded):
    client, ws, url, original = unbranded
    before = client.get(url).json()
    old_paths = export_urls(before)
    report = client.post(old_paths["preview"], json=old_paths["body"])
    assert report.status_code == 200
    old_manifest, _ = exported(client, before)
    save(client, url, lambda a: a["brief"]["brand"].update(require_logo=True))
    current = client.get(url).json()
    assert current["logo_issue"] is None
    assert all(current["editable_products"].values())
    for partial in (False, True):
        paths = export_urls(current)
        for kind in ("zip", "preview"):
            response = client.post(paths[kind], json=paths["body"] | {"allow_partial": partial})
            assert response.status_code == 200, response.text
    # Adding optional artwork remains a normal campaign edit.
    assert client.get(url + "/authored").status_code == 200
    save(
        client,
        url,
        lambda a: a["brief"]["brand"].update(
            logo_path=original.brand.logo_path,
            logo_provenance=original.brand.logo_provenance.model_dump(mode="json"),
        ),
    )
    current = client.get(url).json()
    assert current["logo_issue"] is None
    restored, html = exported(client, current)
    assert all(
        a["pixel_sha256"] != b["pixel_sha256"]
        for a, b in zip(restored["outputs"], old_manifest["outputs"], strict=True)
    )
    assert "<h3>Logo origin</h3>" in html
    assert not list(ws.storage.root.rglob("Outputs"))


@pytest.mark.parametrize("required", [False, True])
def test_missing_logo_policy_does_not_mask_invalid_attached_artwork(unbranded, required):
    _, ws, url, original = unbranded
    value = original.model_dump(mode="json")
    value["brand"].update(require_logo=required, logo_path="")
    plan = make_plan(Brief.model_validate(value), ws.storage)
    assert not plan.blocked
    assert next(f for f in plan.findings if f.rule == "logo_presence").severity == "warning"
    value["brand"]["logo_path"] = original.brand.logo_path
    ws.storage.path(original.brand.logo_path).write_bytes(b"broken image")
    plan = make_plan(Brief.model_validate(value), ws.storage)
    assert plan.blocked
    assert any(
        f.rule == "asset" and f.item == "logo" and f.severity == "blocking" for f in plan.findings
    )


def test_removing_and_restoring_logo_reuses_saved_generated_source(tmp_path):
    import httpx
    from test_current_composition import save_campaign
    from test_offline_report import current_payloads
    from test_provider_runner import generated_case

    from campaign_studio.provider import OpenAIImages
    from campaign_studio.workspace import Workspace

    state, runner, run, response = generated_case(tmp_path)
    calls = []
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=response)
        ),
    )
    asyncio.run(runner.execute(run, provider))
    cid = state.run(run)["campaign_id"]
    ws = Workspace(state)
    original = ws.current_composition(cid)["plan"].brief
    source = state.attempts(run)[0]
    source_bytes = state.storage.path(source["source_ref"]).read_bytes()
    branded = json.loads(current_payloads(state, cid)["manifest.json"])
    save_campaign(ws, cid, lambda b: b["brand"].update(logo_path=""))
    missing = json.loads(current_payloads(state, cid)["manifest.json"])
    assert not any(f["rule"] == "logo_pixels" for f in missing["findings"])
    save_campaign(ws, cid, lambda b: b["brand"].update(logo_path=original.brand.logo_path))
    restored = json.loads(current_payloads(state, cid)["manifest.json"])
    assert [o["pixel_sha256"] for o in restored["outputs"]] == [
        o["pixel_sha256"] for o in branded["outputs"]
    ]
    assert state.storage.path(source["source_ref"]).read_bytes() == source_bytes
    assert len(calls) == len(state.attempts(run)) == 1
    assert not list(state.storage.root.rglob("Outputs"))


def test_official_bundled_inputs_install_sources_without_rendering_finished_images(
    tmp_path, monkeypatch
):
    from campaign_studio.bundled import load_campaigns
    from campaign_studio.runner import Runner

    def forbidden(*args, **kwargs):
        pytest.fail("Installing examples must not compose final images or request AI")

    _, definitions = load_campaigns()
    _, original, _, prepared, _ = next(item for item in definitions if item[3])
    assert Brand.model_validate(original["brand"]).model_dump(mode="json") == original["brand"]
    app = create_app(
        Settings(data_dir=tmp_path / "fresh"),
        seed_examples=True,
        key_available=lambda: False,
        provider_factory=forbidden,
    )
    with monkeypatch.context() as patch:
        patch.setattr(Runner, "execute", forbidden)
        patch.setattr("campaign_studio.rendering.compose", forbidden)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.headers["X-Campaign-Token"] = app.state.csrf
            campaigns = client.get("/api/motif/workspace").json()["campaigns"]
            assert len(campaigns) == 2
            autumn = next(c for c in campaigns if c["brand"] == "Autumn Winds")
            current = client.get(f"/api/motif/campaigns/{autumn['id']}").json()
            assert current["manifest"]["preview_only"]
            assert len(current["manifest"]["outputs"]) == len(prepared.descriptor["outputs"]) == 30
            assert current["logo_issue"] is None
            assert not any(f["rule"] == "logo_presence" for f in current["manifest"]["findings"])
            assert not list(app.state.workspace.storage.root.rglob("Outputs"))
