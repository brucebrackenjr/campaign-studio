# ruff: noqa: F811
"""Campaign Brand autosaves current instructions; previews and exports use those settings."""

import asyncio
import copy
import hashlib
import io
import json
from zipfile import ZipFile

import httpx
import pytest
from PIL import Image
from test_current_composition import current, save_campaign  # noqa: F401
from test_provider_runner import generated_case

from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Framing
from campaign_studio.campaign_drafts import read
from campaign_studio.delivery import current_image, prepare_current_zip
from campaign_studio.intake import Intake
from campaign_studio.motif import campaign_data
from campaign_studio.provider import OpenAIImages
from campaign_studio.state import StateError
from campaign_studio.workspace import ActionError, FrameEdit, Workspace


@pytest.fixture
def branded(current):
    ws, cid = current

    def settings(brief):
        brief["brand"].update(palette=["#FFFFFF", "#FFEE00", "#733B31"], text_color="#FFFFFF")
        brief["products"][0]["framing"] = {
            "en-US": {
                ratio: {
                    "zoom": 1.1,
                    "focal_x": 0.45,
                    "logo_placement": {"left": 0.7, "top": 0.04, "width": 0.15},
                    "text_placements": {
                        "message": {
                            "left": 0.06,
                            "top": 0.7,
                            "width": 0.8,
                            "font_size": 30,
                            "color": "#733B31",
                        },
                        "cta": {
                            "left": 0.06,
                            "top": 0.85,
                            "width": 0.8,
                            "font_size": 25,
                            "color": None,
                        },
                    },
                }
                for ratio in ("1:1", "9:16", "16:9")
            }
        }

    save_campaign(ws, cid, settings)
    return ws, cid


def export(ws, cid):
    with (
        prepare_current_zip(ws.state, ws.current_composition(cid)) as package,
        ZipFile(package.stream) as archive,
    ):
        assert archive.testzip() is None
        name = next(n for n in archive.namelist() if n.endswith("/manifest.json"))
        prefix = name.rsplit("/", 1)[0] + "/"
        manifest = json.loads(archive.read(name))
        files = {n.removeprefix(prefix): archive.read(n) for n in archive.namelist()}
    return manifest, files


def test_default_color_autosaves_without_rendering_and_preserves_custom_layout(
    branded, monkeypatch
):
    ws, cid = branded
    before = copy.deepcopy(read(ws.storage, cid)["authored"]["brief"])
    pngs = {p: p.read_bytes() for p in ws.storage.root.rglob("*.png")}
    monkeypatch.setattr(
        "campaign_studio.compositions.compose",
        lambda *a, **k: pytest.fail("Autosave rendered an image"),
    )
    save_campaign(ws, cid, lambda brief: brief["brand"].update(text_color="#FFEE00"))
    data = campaign_data(ws, cid)
    assert data["output_brief"]["brand"]["text_color"] == "#FFEE00"
    assert data["output_brief"]["products"][0]["framing"] == before["products"][0]["framing"]
    assert all(
        o.framing.text_placements["message"].color == "#733B31"
        for o in ws.current_composition(cid)["plan"].outputs
        if o.product_id == "product-1"
    )
    assert all(p.read_bytes() == content for p, content in pngs.items())
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("ratio", ["1:1", "9:16", "16:9"])
def test_explicit_default_color_reset_changes_only_selected_format(branded, ratio):
    ws, cid = branded
    before = ws.current_composition(cid)["plan"].brief.products[0].framing["en-US"]
    framing = before[ratio].model_copy(deep=True)
    framing.text_placements["message"].color = None
    ws.save_composition(
        cid,
        FrameEdit(
            revision_id=read(ws.storage, cid)["version"],
            product_id="product-1",
            market="en-US",
            ratio=ratio,
            framing=framing,
        ),
    )
    save_campaign(ws, cid, lambda brief: brief["brand"].update(text_color="#FFEE00"))
    frames = ws.current_composition(cid)["plan"].brief.products[0].framing["en-US"]
    assert frames[ratio].text_placements["message"].color is None
    for other in set(before) - {ratio}:
        assert frames[other] == before[other]


@pytest.mark.parametrize("operation", ["add", "remove", "replace"])
def test_logo_changes_are_current_and_previous_download_bytes_stay_intact(branded, operation):
    ws, cid = branded
    if operation == "add":
        save_campaign(
            ws, cid, lambda brief: brief["brand"].update(logo_path="", require_logo=False)
        )
    before, oldfiles = export(ws, cid)
    old_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in oldfiles.items()}
    old_framing = read(ws.storage, cid)["authored"]["brief"]["products"][0]["framing"]
    payload = io.BytesIO()
    Image.new("RGB", (200, 80), "#FF7700").save(payload, format="PNG")
    reference = (
        ""
        if operation == "remove"
        else Intake(ws.storage).upload(payload.getvalue(), "image/png", cid)["reference"]
    )
    save_campaign(
        ws, cid, lambda brief: brief["brand"].update(logo_path=reference, require_logo=False)
    )
    current = ws.current_composition(cid)
    assert bool(current["plan"].brief.brand.logo_path) == bool(reference)
    assert read(ws.storage, cid)["authored"]["brief"]["products"][0]["framing"] == old_framing
    if reference:
        assert ws.storage.path(reference).read_bytes() == payload.getvalue()
    after, files = export(ws, cid)
    assert before["composition_version"] != after["composition_version"]
    assert any(
        a["sha256"] != b["sha256"] for a, b in zip(before["outputs"], after["outputs"], strict=True)
    )
    assert all(hashlib.sha256(oldfiles[n]).hexdigest() == h for n, h in old_hashes.items())
    assert len([n for n in files if n.endswith(".png")]) == 6
    assert not list(ws.storage.root.rglob("*.zip"))
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))


def test_incomplete_unrelated_fields_remain_saved_and_current_preview_reports_them(branded):
    ws, cid = branded

    def change(brief):
        brief["brand"]["text_color"] = "#FFEE00"
        brief["audience"] = ""
        brief["markets"]["en-US"]["copy"]["message"] = ""

    save_campaign(ws, cid, change)
    saved = read(ws.storage, cid)["authored"]["brief"]
    assert saved["brand"]["text_color"] == "#FFEE00" and saved["audience"] == ""
    data = campaign_data(ws, cid)
    assert data["composition_issues"] and data["manifest"] is None
    assert read(ws.storage, cid)["authored"]["brief"] == saved


def test_removing_custom_color_from_palette_never_silently_recolors_it(branded):
    ws, cid = branded
    save_campaign(ws, cid, lambda brief: brief["brand"].update(palette=["#FFFFFF", "#FFEE00"]))
    data = campaign_data(ws, cid)
    assert data["composition_issues"]
    assert (
        read(ws.storage, cid)["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"][
            "text_placements"
        ]["message"]["color"]
        == "#733B31"
    )


@pytest.mark.parametrize("asset", ["logo", "products/product-1"])
@pytest.mark.parametrize("damage", ["missing", "invalid"])
def test_source_damage_does_not_prevent_metadata_save_but_refuses_delivery(branded, asset, damage):
    ws, cid = branded
    path = ws.storage.path(ws.current_composition(cid)["plan"].assets[asset].reference)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"not image bytes")
    save_campaign(
        ws, cid, lambda brief: brief["brand"].update(text_color="#FFEE00", require_logo=True)
    )
    data = campaign_data(ws, cid)
    assert read(ws.storage, cid)["authored"]["brief"]["brand"]["text_color"] == "#FFEE00"
    assert not data["manifest"]["complete"]
    with pytest.raises(StateError):
        export(ws, cid)
    assert not list(ws.storage.root.rglob("*.zip"))


def test_stale_campaign_save_does_not_overwrite_newer_studio_color(branded):
    ws, cid = branded
    projects = AuthoredProjects(ws)
    old = projects.read(cid)
    framing = Framing.model_validate(
        old["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]
    )
    framing.text_placements["message"].color = "#FFEE00"
    ws.save_composition(
        cid,
        FrameEdit(
            revision_id=old["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            framing=framing,
        ),
    )
    old["authored"]["brief"]["brand"]["text_color"] = "#FFEE00"
    with pytest.raises(ActionError):
        projects.save(
            cid, AuthoredSave(version=old["version"], base=old["base"], authored=old["authored"])
        )
    assert (
        read(ws.storage, cid)["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"][
            "text_placements"
        ]["message"]["color"]
        == "#FFEE00"
    )


def test_current_previews_and_download_use_default_and_custom_colors(branded):
    ws, cid = branded
    save_campaign(ws, cid, lambda brief: brief["brand"].update(text_color="#FFEE00"))
    snapshot = ws.current_composition(cid)
    image, _ = current_image(ws.state, snapshot, "product-1/en-US/1x1", preview=True)
    manifest, files = export(ws, cid)
    record = next(o for o in manifest["outputs"] if o["id"] == "product-1/en-US/1x1")
    with (
        Image.open(io.BytesIO(image)) as preview,
        Image.open(io.BytesIO(files[record["file"]])) as final,
    ):
        expected_preview = final.copy()
        expected_preview.thumbnail((900, 900), Image.Resampling.LANCZOS)
        assert preview.convert("RGB").tobytes() == expected_preview.convert("RGB").tobytes()
        expected_preview.close()
        colors = final.convert("RGB").get_flattened_data()
        assert sum(pixel == (115, 59, 49) for pixel in colors) > 100
        assert sum(pixel == (255, 238, 0) for pixel in colors) > 100


def test_existing_generated_source_is_reused_after_brand_changes(tmp_path):
    state, runner, run, response = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    result = asyncio.run(
        runner.execute(run, OpenAIImages("fixture-only", transport=httpx.MockTransport(transport)))
    )
    ws = Workspace(state)
    cid = result["campaign_id"]
    save_campaign(
        ws, cid, lambda brief: brief["brand"].update(text_color=brief["brand"]["palette"][0])
    )
    snapshot = ws.current_composition(cid)
    assert not snapshot["plan"].generation_products and len(calls) == 1
    assert snapshot["plan"].cost_estimate["estimated_usd"] == 0
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))


def test_partial_sources_stay_partial_without_paid_work(current):
    ws, cid = current

    def change(brief):
        brief["products"][1]["source"] = {
            "role": "concept",
            "description": "An ungenerated scene",
            "imagery_absent": True,
            "provenance": brief["products"][1]["source"]["provenance"],
        }
        brief["brand"]["text_color"] = brief["brand"]["palette"][0]

    save_campaign(ws, cid, change)
    data = campaign_data(ws, cid)
    assert not data["manifest"]["complete"]
    assert data["manifest"]["metrics"]["completed_images"] == 3
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
