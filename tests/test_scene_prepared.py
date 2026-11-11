"""The unified source layer preserves the supported prepared-product route."""

import asyncio
import io

import httpx
from PIL import Image
from test_prohibited_exports import campaign, save
from test_regeneration import image_response

from campaign_studio.campaign_drafts import read
from campaign_studio.imaging import composition_base, load_image
from campaign_studio.intake import Intake
from campaign_studio.motif import campaign_data
from campaign_studio.provider import OpenAIImages
from campaign_studio.runner import Runner


def test_prepared_source_packet_contains_cutout_in_each_format(tmp_path):
    with campaign(tmp_path, products=1) as (client, ws, url):
        cid = url.rsplit("/", 1)[1]
        with Image.new("RGBA", (200, 200), (0, 0, 0, 0)) as image, io.BytesIO() as stream:
            image.paste((150, 70, 40, 255), (50, 50, 150, 150))
            image.save(stream, format="PNG")
            uploaded = Intake(ws.storage).upload(
                stream.getvalue(), "image/png", cid, source_role="prepared_product"
            )
        source = {
            "role": "prepared_product",
            "path": uploaded["reference"],
            "opaque_nonreflective_product_confirmed": True,
            "provenance": {
                "label": "Synthetic cutout",
                "rights": "Local fixture",
                "synthetic": True,
            },
        }
        saved = save(
            client, url, lambda authored: authored["brief"]["products"][0].update(source=source)
        )
        plan = Intake(ws.storage).prepare(cid, saved["version"], ws)
        run_id = read(ws.storage, cid)["planned_run"]
        ws.state.confirm(run_id, plan.digest)
        provider = OpenAIImages(
            "synthetic",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=image_response("#778899"))
            ),
        )
        asyncio.run(Runner(ws.state).execute(run_id, provider))
        data = campaign_data(ws, cid)
        snapshot = ws.preview_composition(cid)
        product = snapshot["plan"].brief.products[0]
        assert (
            data["visual_products"][product.id]
            == snapshot["plan"].assets[f"products/{product.id}"].sha256
        )
        with (
            load_image(ws.storage.path(snapshot["sources"][product.id].reference))[0] as background,
            load_image(ws.storage.path(source["path"]))[0] as cutout,
        ):
            for output in data["manifest"]["outputs"]:
                response = client.post(
                    f"/api/campaigns/{cid}/scene/preview",
                    json={
                        "revision_id": data["version"],
                        "product_id": product.id,
                        "market": output["market"],
                        "ratio": output["ratio"],
                    },
                )
                assert response.status_code == 200, response.text
                packet = response.json()
                assert packet["source"]["prepared"]
                raster = client.get(packet["source"]["url"])
                assert raster.status_code == 200
                with (
                    Image.open(io.BytesIO(raster.content)) as actual,
                    composition_base(background, output["ratio"], cutout) as expected,
                ):
                    assert actual.size == expected.size
                    assert actual.convert("RGBA").tobytes() == expected.convert("RGBA").tobytes()
