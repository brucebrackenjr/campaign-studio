"""Persistent scene contract: exact layers, one save owner, and bounded visuals."""

import io
from pathlib import Path

from PIL import Image, ImageChops
from test_prohibited_exports import campaign

from campaign_studio.campaign_drafts import read
from campaign_studio.imaging import compose
from campaign_studio.motif import campaign_data


def request_scene(client, ws, cid, **patch):
    data = campaign_data(ws, cid)
    output = data["manifest"]["outputs"][0]
    body = dict(
        revision_id=data["version"],
        product_id=output["product_id"],
        market=output["market"],
        ratio=output["ratio"],
        **patch,
    )
    return body, client.post(f"/api/campaigns/{cid}/scene/preview", json=body)


def test_scene_layers_recompose_exactly_and_save_is_atomic(tmp_path):
    with campaign(tmp_path, products=2) as (client, ws, url):
        cid = url.rsplit("/", 1)[1]
        before = read(ws.storage, cid)
        body, response = request_scene(client, ws, cid)
        assert response.status_code == 200, response.text
        scene = response.json()

        def image(url):
            response = client.get(url)
            assert response.status_code == 200
            return Image.open(io.BytesIO(response.content)).convert("RGBA")

        source = image(scene["source"]["url"])
        result = source.resize((scene["width"], scene["height"]))
        # Use the renderer's exact crop, placement, and glyph-layer contract.
        from campaign_studio.brief import Framing
        from campaign_studio.imaging import composition_base

        result.close()
        result = composition_base(
            source, body["ratio"], framing=Framing.model_validate(scene["framing"])
        )
        logo = image(scene["logo"]["url"]) if scene["logo"] else None
        if logo:
            from campaign_studio.imaging import logo_placement

            art, pos = logo_placement(
                logo, result.size, Framing.model_validate(scene["framing"]).logo_placement
            )
            result.alpha_composite(art, pos)
            art.close()
        for block in scene["text_blocks"].values():
            with image(block["url"]) as layer:
                result.alpha_composite(layer)
        current = ws.preview_composition(cid)
        output = next(o for o in current["plan"].outputs if o.id == scene["target"])
        with compose(
            source,
            logo,
            output.text,
            output.ratio,
            current["plan"].brief.brand.text_color,
            framing=output.framing,
        ) as expected:
            assert ImageChops.difference(result.convert("RGB"), expected).getbbox() is None
        result.close()
        source.close()
        if logo:
            logo.close()
        assert read(ws.storage, cid)["version"] == before["version"]
        patch = {
            **body,
            "framing": {"zoom": 1.2, "logo_placement": {"left": 0.2, "top": 0.2, "width": 0.2}},
            "values": {"message": "A changed headline"},
            "field_scopes": {"message": "product"},
        }
        saved = client.post(f"/api/campaigns/{cid}/scene/save", json=patch)
        assert saved.status_code == 200, saved.text
        after = read(ws.storage, cid)
        product = after["authored"]["brief"]["products"][0]
        assert product["copy_overrides"][body["market"]]["message"] == "A changed headline"
        assert product["framing"][body["market"]][body["ratio"]]["zoom"] == 1.2
        assert (
            after["authored"]["brief"]["products"][1] == before["authored"]["brief"]["products"][1]
        )
        assert (
            client.post(f"/api/campaigns/{cid}/scene/save", json=patch).json()["version"]
            == saved.json()["version"]
        )
        stale = client.post(
            f"/api/campaigns/{cid}/scene/save", json={**patch, "values": {"message": "stale"}}
        )
        assert stale.status_code == 409


def test_scene_description_and_wording_scope_preserve_other_fields(tmp_path):
    with campaign(tmp_path, products=2) as (client, ws, url):
        cid = url.rsplit("/", 1)[1]
        body, _ = request_scene(client, ws, cid)
        saved = client.post(
            f"/api/campaigns/{cid}/scene/save",
            json={
                **body,
                "values": {"cta": "Come explore"},
                "field_scopes": {"cta": "all_products"},
                "description_edit": {
                    "description": "A product beside the window.",
                    "expected_version": 0,
                },
            },
        )
        assert saved.status_code == 200, saved.text
        data = saved.json()
        assert data["value"]["markets"][body["market"]]["copy"]["cta"] == "Come explore"
        assert (
            data["metadata"]["creatives"][
                f"{body['product_id']}/{body['market']}/{body['ratio'].replace(':', 'x')}"
            ]["description"]
            == "A product beside the window."
        )
        broken = {
            **body,
            "revision_id": data["version"],
            "values": {"message": "must not save"},
            "field_scopes": {"message": "product"},
            "description_edit": {"description": "new", "expected_version": 0},
        }
        assert client.post(f"/api/campaigns/{cid}/scene/save", json=broken).status_code == 409
        assert read(ws.storage, cid)["version"] == data["version"]


def test_thumbnails_are_bounded_cached_and_stale_requests_fail(tmp_path):
    with campaign(tmp_path, products=1) as (client, ws, url):
        cid = url.rsplit("/", 1)[1]
        data = campaign_data(ws, cid)
        output = data["manifest"]["outputs"][0]
        for field, edge in [("thumbnail_url", 160), ("gallery_url", 640)]:
            response = client.get(output[field])
            assert response.status_code == 200, response.text
            with Image.open(io.BytesIO(response.content)) as image:
                assert max(image.size) <= edge
            assert response.headers["cache-control"] == "private, no-cache"
        source = client.get(data["source_thumbnail_urls"][output["product_id"]])
        assert source.status_code == 200
        with Image.open(io.BytesIO(source.content)) as image:
            assert max(image.size) <= 640
        body, _ = request_scene(client, ws, cid)
        client.post(f"/api/campaigns/{cid}/scene/save", json={**body, "framing": {"zoom": 1.1}})
        stale = client.get(output["gallery_url"])
        assert stale.status_code == 409
        assert stale.headers["cache-control"] == "no-store"
        assert client.get(f"/api/motif/campaigns/{cid}").headers["cache-control"] == "no-store"


def test_same_path_source_replacement_invalidates_visuals_and_rejects_old_scene_source(tmp_path):
    with campaign(tmp_path, products=1) as (client, ws, url):
        cid = url.rsplit("/", 1)[1]
        before = client.get(url).json()
        output = before["manifest"]["outputs"][0]
        _, packet = request_scene(client, ws, cid)
        old_source = packet.json()["source"]
        initial = client.get(output["gallery_url"])
        assert initial.status_code == 200
        source = ws.preview_composition(cid)["sources"][output["product_id"]]
        target = ws.storage.path(source.reference)
        with Image.new("RGB", (source.width, source.height), (200, 100, 50)) as image:
            image.save(target, format="PNG")
        after = client.get(url).json()
        assert after["version"] == before["version"]
        assert after["visual_sources"] != before["visual_sources"]
        latest = client.get(after["manifest"]["outputs"][0]["gallery_url"])
        assert latest.status_code == 200
        assert latest.headers["cache-control"] == "private, no-cache"
        assert latest.content != initial.content
        _, fresh_packet = request_scene(client, ws, cid)
        fresh_source = fresh_packet.json()["source"]
        assert fresh_source["key"] != old_source["key"]
        assert fresh_source["url"] != old_source["url"]
        assert client.get(fresh_source["url"]).status_code == 200
        # A late fetch cannot cache new bytes under the old packet's verified SHA.
        refused = client.get(old_source["url"])
        assert refused.status_code == 409
        assert refused.headers["cache-control"] == "no-store"


def test_same_path_font_replacement_changes_visual_identity_and_pixels(tmp_path, monkeypatch):
    from campaign_studio import imaging

    original_font_path = imaging.font_path
    temporary_font = tmp_path / "font.ttf"
    temporary_font.write_bytes(Path(original_font_path()).read_bytes())
    monkeypatch.setattr(
        imaging,
        "font_path",
        lambda font_id="noto_sans": (
            str(temporary_font) if font_id == "noto_sans" else original_font_path(font_id)
        ),
    )
    with campaign(tmp_path, products=1) as (client, ws, url):
        before = client.get(url).json()
        visual_url = before["manifest"]["outputs"][0]["gallery_url"]
        initial = client.get(visual_url)
        assert initial.status_code == 200
        temporary_font.write_bytes(Path(original_font_path("barlow_condensed")).read_bytes())
        after = client.get(url).json()
        assert after["version"] == before["version"]
        assert after["visual_fonts"] != before["visual_fonts"]
        latest = client.get(visual_url)
        assert latest.status_code == 200
        assert latest.headers["cache-control"] == "private, no-cache"
        assert latest.content != initial.content
