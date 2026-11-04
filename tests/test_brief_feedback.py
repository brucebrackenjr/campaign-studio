"""Immediate logo intake and completed-color contracts for the Brief feedback UI."""

import asyncio
import copy
import hashlib
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from test_authored import action
from test_logo_editing import apply_logo, command
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief, normalize_color
from campaign_studio.config import Settings
from campaign_studio.exporting import package_payloads
from campaign_studio.imaging import compose, load_image
from campaign_studio.intake import Intake
from campaign_studio.planning import canonical_hash, make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.text_limits import LIMITS
from campaign_studio.workspace import Workspace


def logo_payload(kind, *, empty=False):
    with Image.new("RGBA" if kind == "PNG" else "RGB", (300, 900), "#167c52") as image:
        if empty:
            image.putalpha(0)
        with io.BytesIO() as buffer:
            image.save(buffer, kind)
            return buffer.getvalue()


@pytest.mark.parametrize("kind", ["PNG", "JPEG"])
def test_opaque_logo_intake_plan_replacement_fit_and_export_keep_original_bytes(tmp_path, kind):
    state, runner, run, brief = setup_case(tmp_path, 2, True)
    first = asyncio.run(runner.execute(run))
    cid = brief.campaign_id
    intake = Intake(state.storage)
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Logo replacement is local"),
    )
    payload = logo_payload(kind)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        previous = action(client, cid)
        uploaded = client.post(
            "/api/uploads",
            content=payload,
            headers={
                # Decoded bytes, not browser MIME, decide type.
                "Content-Type": "application/octet-stream",
                "X-Campaign-Id": cid,
                "X-Image-Role": "logo",
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        asset = uploaded.json()
        assert asset["width"] == 300 and asset["height"] == 900
        assert asset["provenance"] == {
            "label": "User-supplied logo",
            "rights": "Not provided",
            "synthetic": False,
        }
        assert action(client, cid) == previous  # Upload never assigns an attachment itself.
        assert state.storage.path(asset["reference"]).read_bytes() == payload
        assert client.get(asset["url"]).content == payload
        assert (
            client.get(asset["url"])
            .headers["content-type"]
            .startswith("image/png" if kind == "PNG" else "image/jpeg")
        )

    edit = command(state, run, replacement=asset["reference"])
    saved, impact = apply_logo(state, runner, edit)
    assert saved["complete"] and impact["fitted_outputs"]
    reference = state.plan(run).brief.brand.logo_path
    assert reference.endswith(".png" if kind == "PNG" else ".jpg")
    assert state.storage.path(reference).read_bytes() == payload
    assert state.storage.path(asset["reference"]).read_bytes() == payload
    assert saved["revision_id"] != first["revision_id"]
    assert not state.attempts(run)
    package = package_payloads(state, saved["revision_id"])
    for name, content in package.items():
        if name.endswith(".png"):
            with Image.open(io.BytesIO(content)) as image:
                image.load()
                assert image.format == "PNG"
    value = brief.model_dump(mode="json")
    value["brand"]["logo_path"] = asset["reference"]
    plan = make_plan(Brief.model_validate(value), state.storage)
    assert not plan.blocked
    new_run = state.create(plan, 0)
    assert state.plan(new_run).brief.brand.logo_path.endswith(".png" if kind == "PNG" else ".jpg")
    assert state.storage.path(state.plan(new_run).brief.brand.logo_path).read_bytes() == payload
    assert not intake.read(cid)  # Neither image operation invented a Brief draft.


def test_rejected_logo_preserves_previous_attachment_raw_work_and_upload_ownership(tmp_path):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    cid = brief.campaign_id
    intake = Intake(state.storage)
    intake.write(cid, brief.model_dump(mode="json"), None)
    projects = AuthoredProjects(Workspace(state))
    project = projects.read(cid)
    project["authored"]["brief"]["brand"]["text_color"] = " .ddd "
    projects.save(cid, AuthoredSave(**{k: project[k] for k in ("version", "base", "authored")}))
    document = intake.read(cid)
    files = {
        p: p.read_bytes()
        for p in state.storage.path(state.storage.project(cid).ref("Assets")).rglob("*.png")
    }
    with state.storage.connect() as db:
        owners = db.execute("SELECT * FROM file_owners ORDER BY reference,campaign_id").fetchall()
    app = create_app(Settings(data_dir=state.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        for payload, content_type, expected in (
            (logo_payload("PNG", empty=True), "image/png", "no_visible_pixels"),
            (b"\x89PNG\r\n\x1a\nmalformed", "image/png", "decode_failure"),
            (b"not an image", "image/jpeg", "decode_failure"),
            (logo_payload("GIF"), "image/png", "unsupported_type"),
        ):
            result = client.post(
                "/api/uploads",
                content=payload,
                headers={
                    "Content-Type": content_type,
                    "X-Campaign-Id": cid,
                    "X-Image-Role": "logo",
                },
            )
            assert result.status_code == 422 and result.json()["input_image"]["kind"] == expected
            assert intake.read(cid) == document
            assert {
                p: p.read_bytes()
                for p in state.storage.path(state.storage.project(cid).ref("Assets")).rglob("*.png")
            } == files
            with state.storage.connect() as db:
                assert (
                    db.execute(
                        "SELECT * FROM file_owners ORDER BY reference,campaign_id"
                    ).fetchall()
                    == owners
                )


def test_completed_colors_normalize_without_recoloring_explicit_blocks_or_raw_saved_work(tmp_path):
    state, _, _, brief = setup_case(tmp_path, 2, True)
    value = brief.model_dump(mode="json")
    value["brand"]["palette"] = ["ffffff", " #61177c ", "#FFFFFF", "61177C"]
    value["brand"]["text_color"] = " 61177c "
    value["products"][0]["framing"] = {
        "en-US": {
            "1:1": {
                "text_placements": {
                    "message": {
                        "left": 0.06,
                        "top": 0.06,
                        "width": 0.8,
                        "font_size": 40,
                        "color": "ffffff",
                    }
                }
            }
        }
    }
    normalized = Brief.model_validate(value)
    assert normalized.brand.palette == ["#FFFFFF", "#61177C"]
    assert normalized.brand.text_color == "#61177C"
    assert (
        normalized.products[0].framing["en-US"]["1:1"].text_placements["message"].color == "#FFFFFF"
    )
    assert not make_plan(normalized, state.storage).blocked
    value["brand"]["palette"] = ["61177c"]
    with pytest.raises(ValidationError, match="text placement color"):
        Brief.model_validate(value)
    value["brand"]["palette"] = ["ffffff"]
    with pytest.raises(ValidationError, match="text_color"):
        Brief.model_validate(value)
    for invalid in (" .ddd ", "#123", "", 17):
        assert normalize_color(invalid) == invalid
    projects = AuthoredProjects(Workspace(state))
    current = projects.read(brief.campaign_id)
    current["authored"]["brief"]["brand"].update(palette=["ffffff", "ffffff"], text_color=" .ddd ")
    projects.save(
        brief.campaign_id, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")})
    )
    assert projects.read(brief.campaign_id)["authored"] == current["authored"]


@pytest.fixture
def preview_ui(tmp_path):
    state, _, run, brief = setup_case(tmp_path)
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Brief preview must never construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        yield client, state, run, brief.campaign_id


def preview_request(client, cid, ratio="1:1"):
    draft = action(client, cid)
    return {
        "version": draft["version"],
        "brief": draft["authored"]["brief"],
        "product_id": "product-1",
        "market": "en-US",
        "ratio": ratio,
    }


def test_scratch_typography_preview_accepts_valid_names_without_a_plan(preview_ui):
    client, state, _, _ = preview_ui
    cid, _ = Intake(state.storage).new(
        name="🦊" * LIMITS["campaign_name"], brand="é" * LIMITS["brand_name"]
    )
    saved = action(client, cid)
    request = preview_request(client, cid)
    request["brief"]["markets"]["en-US"]["copy"] = {
        "message": "Ready for winter.",
        "cta": "Explore the collection.",
    }
    request["brief"]["products"] = [
        {"id": "product-1", "name": "Bell strap", "source": {"role": "concept"}}
    ]
    response = client.post(f"/api/motif/campaigns/{cid}/brief-preview", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "ready" and result["kind"] == "typography", result
    assert not result["issues"] and result["logo_missing"]
    image = client.get(result["image"]["url"])
    assert image.status_code == 200
    with Image.open(io.BytesIO(image.content)) as preview:
        assert preview.size == (1080, 1080)
    assert action(client, cid) == saved
    assert Workspace(state).run_for(cid) is None


def test_representative_preview_matches_real_renderer_all_formats_without_publication(preview_ui):
    client, state, run, cid = preview_ui
    with state.storage.connect() as db:
        rows = list(db.iterdump())
    before = {
        p: p.read_bytes()
        for p in state.storage.root.rglob("*")
        if p.is_file() and not p.name.startswith("state.sqlite3")
    }
    request = preview_request(client, cid)
    request["brief"]["brand"].update(palette=["ffffff", "61177c", "#FFFFFF"], text_color="61177c")
    expected = make_plan(Brief.model_validate(request["brief"]), state.storage)
    assert not expected.blocked
    # Unrelated unfinished fields do not stop truthful visual feedback.
    request["brief"]["name"] = request["brief"]["audience"] = ""
    request["brief"]["brand"]["name"] = ""
    request["brief"]["products"][1]["name"] = ""
    for ratio in ("1:1", "9:16", "16:9"):
        request["ratio"] = ratio
        response = client.post(f"/api/motif/campaigns/{cid}/brief-preview", json=request)
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["status"] == "ready" and preview["kind"] == "composition", preview
        assert preview["request_digest"] == canonical_hash(request)
        assert preview["target"] == {
            "campaign_id": cid,
            "product_id": "product-1",
            "market": "en-US",
            "ratio": ratio,
        }
        image_response = client.get(preview["image"]["url"])
        assert image_response.status_code == 200
        assert hashlib.sha256(image_response.content).hexdigest() == preview["image"]["sha256"]
        output = next(
            o
            for o in expected.outputs
            if o.product_id == "product-1" and o.market == "en-US" and o.ratio == ratio
        )
        logo, _ = load_image(state.storage.path(expected.assets["logo"].reference))
        source, _ = load_image(state.storage.path(expected.assets["products/product-1"].reference))
        with Image.open(io.BytesIO(image_response.content)) as actual:
            rendered = compose(
                source,
                logo,
                output.text,
                ratio,
                expected.brief.brand.text_color,
                framing=output.framing,
            )
            assert actual.size == (output.width, output.height)
            assert actual.convert("RGB").tobytes() == rendered.tobytes()
            rendered.close()
        logo.close()
        source.close()
    with state.storage.connect() as db:
        assert list(db.iterdump()) == rows
    assert all(p.read_bytes() == content for p, content in before.items())
    assert state.revision_count(run) == 0 and not state.attempts(run)


def test_attached_photo_previews_without_a_logo_even_with_a_saved_wide_logo_position(preview_ui):
    client, state, run, cid = preview_ui
    request = preview_request(client, cid)
    request["brief"]["brand"]["logo_path"] = None
    # A wide, shallow logo would fit at this position; a square placeholder would not.
    request["brief"]["products"][0].setdefault("framing", {}).setdefault("en-US", {})["1:1"] = {
        "logo_placement": {"left": 0.05, "top": 0.90, "width": 0.25}
    }
    response = client.post(f"/api/motif/campaigns/{cid}/brief-preview", json=request)
    data = response.json()
    assert data["status"] == "ready" and data["kind"] == "composition", data
    assert data["logo_missing"] and not data["issues"]
    with Image.open(io.BytesIO(client.get(data["image"]["url"]).content)) as preview:
        assert preview.size == (1080, 1080) and preview.convert("RGB").getextrema()[0][0] < 255
    assert state.revision_count(run) == 0 and not state.attempts(run)


def test_invalid_raw_preview_replaces_feedback_and_cache_is_version_and_owner_bound(preview_ui):
    client, state, _, cid = preview_ui
    request = preview_request(client, cid)
    url = f"/api/motif/campaigns/{cid}/brief-preview"
    ready = client.post(url, json=request).json()
    assert ready["status"] == "ready"
    for mutate in (
        lambda raw: raw["brand"].update(text_color=" .ddd "),
        lambda raw: raw["markets"]["en-US"]["copy"].update(message=""),
        lambda raw: raw["products"][0].update(framing=[]),
        lambda raw: raw["products"][0]["source"].update(path="private/runs/unrelated.png"),
    ):
        changed = copy.deepcopy(request)
        mutate(changed["brief"])
        response = client.post(url, json=changed)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "invalid" and result["image"] is None and result["issues"]
    other, _ = Intake(state.storage).new()
    assert client.get(ready["image"]["url"].replace(cid, other)).status_code == 404
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=action(client, cid))
    assert saved.status_code == 200
    assert client.get(ready["image"]["url"]).status_code == 404
    stale = client.post(url, json=request)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_brief"


def test_missing_concept_uses_actual_typography_without_a_generation_or_old_photo(preview_ui):
    client, state, run, cid = preview_ui
    request = preview_request(client, cid)
    request["brief"]["products"][0]["source"] = {
        "role": "concept",
        "imagery_absent": True,
        "description": "A future concept",
        "provenance": {"label": "Typed concept", "rights": "Not provided"},
    }
    result = client.post(f"/api/motif/campaigns/{cid}/brief-preview", json=request).json()
    assert result["status"] == "ready" and result["kind"] == "typography", result
    with Image.open(io.BytesIO(client.get(result["image"]["url"]).content)) as image:
        assert image.mode == "RGBA" and image.getchannel("A").getextrema() == (0, 255)
    assert state.revision_count(run) == 0 and not state.attempts(run)


def test_brief_logo_change_fits_saved_geometry_before_plan_and_preserves_other_raw_work(preview_ui):
    client, state, run, cid = preview_ui
    current = action(client, cid)
    raw = current["authored"]["brief"]
    raw["products"][0]["framing"] = {
        "en-US": {
            ratio: {"logo_placement": {"left": 0.7, "top": 0.8, "width": 0.2}}
            for ratio in ("1:1", "9:16", "16:9")
        }
    }
    upload = client.post(
        "/api/uploads",
        content=logo_payload("JPEG"),
        headers={
            "Content-Type": "image/jpeg",
            "X-Campaign-Id": cid,
            "X-Image-Role": "logo",
        },
    ).json()
    raw["brand"]["logo_path"] = upload["reference"]
    raw["audience"] = "  Exact audience spacing  "
    other_language = copy.deepcopy(raw["markets"]["es-MX"])
    old_plan = state.plan(run).model_dump()
    saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=current)
    assert saved.status_code == 200, saved.text
    request = preview_request(client, cid)
    preview = client.post(f"/api/motif/campaigns/{cid}/brief-preview", json=request).json()
    assert preview["status"] == "ready" and len(preview["fitted_outputs"]) == 1, preview
    planned = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
    )
    assert planned.status_code == 200, planned.text
    fits = planned.json()["fitted_outputs"]
    assert len(fits) == 3 and all(item["placement"]["width"] < 0.2 for item in fits)
    reopened = action(client, cid)["authored"]["brief"]
    assert (
        reopened["audience"] == raw["audience"] and reopened["markets"]["es-MX"] == other_language
    )
    assert reopened["brand"]["logo_path"].endswith(".jpg")
    assert state.storage.path(reopened["brand"]["logo_path"]).read_bytes() == logo_payload("JPEG")
    for item in fits:
        assert (
            reopened["products"][0]["framing"][item["market"]][item["ratio"]]["logo_placement"]
            == item["placement"]
        )
    assert state.plan(run).model_dump() == old_plan and not state.attempts(run)
    # Opening or replanning with the same attachment does not invent another fit.
    again = client.post(
        f"/api/motif/campaigns/{cid}/plan", json={"version": action(client, cid)["version"]}
    )
    assert again.status_code == 200 and again.json()["fitted_outputs"] == []


def test_brief_preview_rechecks_saved_owner_after_render_without_publishing(
    preview_ui, monkeypatch
):
    import campaign_studio.brief_preview as module

    client, state, run, cid = preview_ui
    request = preview_request(client, cid)
    render = module.compose

    def save_during_render(*args, **kwargs):
        projects = AuthoredProjects(Workspace(state))
        current = projects.read(cid)
        current["authored"]["brief"]["audience"] = "Newer saved audience"
        projects.save(cid, AuthoredSave(**{k: current[k] for k in ("version", "base", "authored")}))
        return render(*args, **kwargs)

    monkeypatch.setattr(module, "compose", save_during_render)
    result = client.post(f"/api/motif/campaigns/{cid}/brief-preview", json=request)
    assert result.status_code == 409 and result.json()["code"] == "stale_brief"
    assert action(client, cid)["authored"]["brief"]["audience"] == "Newer saved audience"
    assert state.revision_count(run) == 0 and not state.attempts(run)


def test_representative_preview_uses_only_intact_same_intent_retained_concept(tmp_path):
    import httpx

    state, runner, run, body = generated_case(tmp_path)
    provider = OpenAIImages(
        "dummy-fixture-only",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    asyncio.run(runner.execute(run, provider))
    cid = state.plan(run).brief.campaign_id
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Preview constructed a provider"),
    )
    attempts = state.attempts(run)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        request = preview_request(client, cid)
        url = f"/api/motif/campaigns/{cid}/brief-preview"
        result = client.post(url, json=request).json()
        assert result["status"] == "ready" and result["kind"] == "composition", result
        changed = copy.deepcopy(request)
        changed["brief"]["creative_direction"] += " A genuinely different scene."
        result = client.post(url, json=changed).json()
        assert result["status"] == "ready" and result["kind"] == "typography", result
        assert state.attempts(run) == attempts and state.revision_count(run) == 1
