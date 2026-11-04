"""Optional artwork preserves truthful findings, editable pixels, and strict delivery checks."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_prohibited_exports import export_urls, save

from campaign_studio.app import create_app
from campaign_studio.brief import Brand, Brief
from campaign_studio.config import Settings
from campaign_studio.planning import make_plan
from campaign_studio.sample import write_sample
from campaign_studio.state import StateError


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
        brief = Brief.model_validate(value)
        plan = make_plan(brief, ws.storage)
        assert not plan.blocked
        assert "logo" not in plan.assets
        run = ws.state.create(plan, 0)
        ws.state.confirm(run, ws.state.plan(run).digest)
        manifest = asyncio.run(app.state.runner.execute(run))
        assert manifest["complete"]
        assert manifest["metrics"]["completed_images"] == 3
        cid = brief.campaign_id
        url = f"/api/motif/campaigns/{cid}"
        save(client, url)
        yield client, ws, url, original


def test_brand_logo_requirement_is_explicit_and_preserves_old_serialization():
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
    assert Brand.model_validate(value | {"require_logo": True}).model_dump()["require_logo"]
    for bad in (None, 1, "false"):
        with pytest.raises(ValidationError):
            Brand.model_validate(value | {"require_logo": bad})


def test_unbranded_production_edit_preview_revision_and_report(unbranded):
    client, ws, url, _ = unbranded
    current = client.get(url).json()
    manifest = current["manifest"]
    cid, run, revision = (
        current["value"]["campaign_id"],
        current["output_run_id"],
        manifest["revision_id"],
    )
    warning = [f for f in manifest["findings"] if f["rule"] == "logo_presence"]
    assert len(warning) == 1 and warning[0]["severity"] == "warning"
    assert not any(f["rule"] == "logo_pixels" for f in manifest["findings"])
    assert all(current["editable_products"].values())
    edit = {
        "run_id": run,
        "revision_id": revision,
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
    }
    # All three editor services can preview an actual unbranded accepted image.
    logo = client.post(f"/api/campaigns/{cid}/logo/preview", json=edit)
    assert logo.status_code == 200, logo.text
    assert logo.json()["logo_asset"] is None
    assert logo.json()["images"][0]["layers"]["logo_url"] is None
    text = edit | {
        "values": {"message": "Fresh wording.", "cta": "See more."},
        "field_scopes": {"message": "product", "cta": "product"},
    }
    preview = client.post(f"/api/campaigns/{cid}/text/preview", json=text)
    assert preview.status_code == 200, preview.text
    applied = client.post(
        f"/api/campaigns/{cid}/text/apply", json=text | {"preview_digest": preview.json()["digest"]}
    )
    assert applied.status_code == 200, applied.text
    current = client.get(url).json()
    frame = edit | {
        "revision_id": current["manifest"]["revision_id"],
        "framing": {"zoom": 1.1},
    }
    preview = client.post(f"/api/campaigns/{cid}/framing/preview", json=frame)
    assert preview.status_code == 200, preview.text
    applied = client.post(
        f"/api/campaigns/{cid}/framing/apply",
        json=frame | {"preview_digest": preview.json()["digest"]},
    )
    assert applied.status_code == 200, applied.text
    current = client.get(url).json()
    paths = export_urls(current)
    report = client.post(paths["folder"], json=paths["body"])
    assert report.status_code == 200, report.text
    html = client.get(report.json()["url"])
    assert html.status_code == 200
    assert "These images have no logo." in html.text
    assert "<h3>Logo origin</h3>" not in html.text
    assert ws.state.attempts(run) == []


def test_strict_toggle_blocks_old_downloads_but_keeps_preview_and_can_attach_later(unbranded):
    client, ws, url, original = unbranded
    before = client.get(url).json()
    cid, run = before["value"]["campaign_id"], before["output_run_id"]
    paths = export_urls(before)
    zipped = client.post(
        paths["zip"], json={k: paths["body"][k] for k in ("snapshot_id", "allow_partial")}
    )
    report = client.post(paths["folder"], json=paths["body"])
    assert zipped.status_code == report.status_code == 200
    old_image = before["manifest"]["outputs"][0]["url"]
    original_hashes = [o["sha256"] for o in before["manifest"]["outputs"]]
    save(client, url, lambda a: a["brief"]["brand"].update(require_logo=True))
    project = client.get(url + "/authored").json()
    assert client.get(url).json()["logo_issue"]["code"] == "logo_presence"
    for destination in (zipped.json()["url"], report.json()["url"], old_image + "?download=true"):
        assert client.get(destination).status_code == 409
    assert client.get(old_image).status_code == 200
    for partial in (False, True):
        response = client.post(paths["folder"], json=paths["body"] | {"allow_partial": partial})
        assert response.status_code == 409 and response.json()["code"] == "logo_presence"
    request = {
        "version": project["version"],
        "brief": project["authored"]["brief"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
    }
    preview = client.post(url + "/brief-preview", json=request)
    assert preview.status_code == 200 and preview.json()["status"] == "ready", preview.text
    assert preview.json()["logo_missing"]
    assert [o["sha256"] for o in client.get(url).json()["manifest"]["outputs"]] == original_hashes
    save(client, url, lambda a: a["brief"]["brand"].update(require_logo=False))
    assert client.get(zipped.json()["url"]).status_code == 200
    save(client, url, lambda a: a["brief"]["brand"].update(require_logo=True))
    project = client.get(url + "/authored").json()
    upload = client.post(
        "/api/uploads",
        content=ws.storage.path(original.brand.logo_path).read_bytes(),
        headers={"X-Campaign-Id": cid, "X-Image-Role": "logo", "Content-Type": "image/png"},
    )
    assert upload.status_code == 200, upload.text
    current = client.get(url).json()
    edit = {
        "run_id": run,
        "revision_id": current["manifest"]["revision_id"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": "1:1",
        "replacement": upload.json()["reference"],
        "draft_version": project["version"],
    }
    preview = client.post(f"/api/campaigns/{cid}/logo/preview", json=edit)
    assert preview.status_code == 200, preview.text
    result = client.post(
        f"/api/campaigns/{cid}/logo/apply", json=edit | {"preview_digest": preview.json()["digest"]}
    )
    assert result.status_code == 200, result.text
    current = client.get(url).json()
    assert current["logo_issue"] is None
    assert all(
        o["sha256"] != old
        for o, old in zip(current["manifest"]["outputs"], original_hashes, strict=True)
    )
    assert not any(f["rule"] == "logo_presence" for f in current["manifest"]["findings"])
    # An older unbranded report is still subject to its own pixels and current requirement.
    assert client.get(report.json()["url"]).status_code == 409
    paths = export_urls(current)
    assert client.post(paths["folder"], json=paths["body"]).status_code == 200
    assert ws.state.attempts(run) == []


@pytest.mark.parametrize("required", [False, True])
def test_missing_logo_policy_does_not_mask_invalid_attached_artwork(unbranded, required):
    _, ws, url, original = unbranded
    value = original.model_dump(mode="json")
    value["brand"].update(require_logo=required, logo_path="")
    plan = make_plan(Brief.model_validate(value), ws.storage)
    assert plan.blocked == required
    assert next(f for f in plan.findings if f.rule == "logo_presence").severity == (
        "blocking" if required else "warning"
    )
    if required:
        with pytest.raises(StateError):
            ws.state.create(plan, 0)
    value["brand"]["logo_path"] = original.brand.logo_path
    ws.storage.path(original.brand.logo_path).write_bytes(b"broken image")
    plan = make_plan(Brief.model_validate(value), ws.storage)
    assert plan.blocked
    assert any(
        f.rule == "asset" and f.item == "logo" and f.severity == "blocking" for f in plan.findings
    )


def test_removing_and_restoring_logo_reuses_saved_generated_source(tmp_path):
    import httpx
    from test_provider_runner import generated_case

    from campaign_studio.provider import OpenAIImages

    state, runner, run, response = generated_case(tmp_path)
    calls = []
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json=response)
        ),
    )
    branded = asyncio.run(runner.execute(run, provider))
    before = state.plan(run)
    source = state.attempts(run)[0]
    source_bytes = state.storage.path(source["source_ref"]).read_bytes()
    value = before.brief.model_dump(mode="json")
    value["brand"]["logo_path"] = ""
    unbranded = Brief.model_validate(value)
    plan = make_plan(unbranded, state.storage)
    missing = runner.revise(run, unbranded, branded["revision_id"], plan.digest)
    assert missing["complete"]
    assert not any(f["rule"] == "logo_pixels" for f in missing["findings"])
    restore = make_plan(before.brief, state.storage)
    restored = runner.revise(run, before.brief, missing["revision_id"], restore.digest)
    assert [o["pixel_sha256"] for o in restored["outputs"]] == [
        o["pixel_sha256"] for o in branded["outputs"]
    ]
    assert state.storage.path(source["source_ref"]).read_bytes() == source_bytes
    assert len(calls) == len(state.attempts(run)) == 1


def test_official_bundled_inputs_and_prepared_pixels_still_install_exactly(tmp_path, monkeypatch):
    from campaign_studio.bundled import load_campaigns
    from campaign_studio.runner import Runner

    def forbidden(*args, **kwargs):
        pytest.fail("Prepared examples must copy their verified pixels")

    monkeypatch.setattr(Runner, "execute", forbidden)
    monkeypatch.setattr("campaign_studio.rendering.compose", forbidden)
    _, definitions = load_campaigns()
    _, original, _, prepared, _ = next(item for item in definitions if item[3])
    assert Brand.model_validate(original["brand"]).model_dump(mode="json") == original["brand"]
    expected = sorted(o["sha256"] for o in prepared.descriptor["outputs"])
    with TestClient(
        create_app(
            Settings(data_dir=tmp_path / "fresh"),
            seed_examples=True,
            key_available=lambda: False,
            provider_factory=forbidden,
        ),
        base_url="http://127.0.0.1",
    ) as client:
        campaigns = client.get("/api/motif/workspace").json()["campaigns"]
        assert len(campaigns) == 2
        autumn = next(c for c in campaigns if c["brand"] == "Autumn Winds")
        current = client.get(f"/api/motif/campaigns/{autumn['id']}").json()
        assert sorted(o["sha256"] for o in current["manifest"]["outputs"]) == expected
        assert current["manifest"]["metrics"]["completed_images"] == 30
        assert current["logo_issue"] is None
        assert not any(f["rule"] == "logo_presence" for f in current["manifest"]["findings"])
