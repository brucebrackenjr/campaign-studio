# ruff: noqa: F811
"""Invalid text remains editable without making it eligible for delivery."""

import io

import pytest
from PIL import Image
from test_current_composition import current, save_campaign  # noqa: F401

from campaign_studio.brief import Framing, TextPlacement
from campaign_studio.campaign_drafts import read
from campaign_studio.compositions import preview_image
from campaign_studio.delivery import current_image, delivery_manifest
from campaign_studio.motif import campaign_data
from campaign_studio.previews import DraftPreviews
from campaign_studio.state import StateError
from campaign_studio.workspace import ActionError, FrameEdit, LogoEdit, TextEdit


@pytest.mark.parametrize("tool", ["text", "framing", "logo"])
def test_source_backed_text_overflow_has_temporary_repair_layers_and_still_blocks_export(
    current, tool
):
    ws, cid = current

    def overflow(brief):
        brief["markets"]["en-US"]["copy"]["message"] = "Warm nights start here – review edition"
        brief["products"][0]["framing"] = {
            "en-US": {
                "1:1": {
                    "text_placements": {
                        "message": {
                            "font_id": "barlow_condensed",
                            "font_size": 88,
                            "left": 0.106,
                            "top": 0.24,
                            "width": 1.0,
                        }
                    }
                }
            }
        }

    save_campaign(ws, cid, overflow)
    data = campaign_data(ws, cid)
    assert len(data["source_urls"]) == 2
    assert data["editable_products"]["product-1"]
    selected = next(o for o in data["manifest"]["outputs"] if o["id"] == "product-1/en-US/1x1")
    assert selected["url"] is None
    assert any(f["rule"] == "text_fit" for f in data["manifest"]["findings"])
    before = read(ws.storage, cid)
    with pytest.raises(ActionError) as gallery_blocked:
        preview_image(ws, cid, before["version"], "product-1", "en-US", "1:1")
    assert gallery_blocked.value.code == "preview_blocked"
    files = {p: p.read_bytes() for p in ws.storage.root.rglob("*.png")}
    command = {"text": TextEdit, "framing": FrameEdit, "logo": LogoEdit}[tool]
    edit = command(
        revision_id=before["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        **(
            {
                "framing": Framing.model_validate(
                    before["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"]
                )
            }
            if tool == "framing"
            else {}
        ),
    )
    _, plan, impact = getattr(ws, "frame" if tool == "framing" else tool)(
        cid, edit, allow_unchanged=True
    )
    previews = DraftPreviews(ws.state)
    assert previews.render(cid, edit, plan, impact) == [], (
        "Ordinary previews cannot bypass fit validation"
    )
    result = previews.render(cid, edit, plan, impact, **{f"{tool}_preview": True})
    assert len(result) == 1
    rendered = result[0]
    assert rendered["id"] == selected["id"]
    payload = next(reversed(previews.entries.values()))["images"][selected["id"]]
    with Image.open(io.BytesIO(payload)) as image:
        assert image.size == (1080, 1080)
    if tool == "text":
        block = rendered["layers"]["text_blocks"]["message"]
        assert block["placement"]["font_size"] == 88
        assert block["bounds"][2] > 1080, (
            "Diagnostic preserves the real overflow, without shrinking it"
        )
    elif tool == "logo":
        assert rendered["layers"]["base_url"] and rendered["layers"]["copy_url"]
    else:
        assert rendered["overlay_url"]
    snapshot = ws.current_composition(cid)
    for partial in (False, True):
        with pytest.raises(StateError) as blocked:
            delivery_manifest(snapshot, allow_partial=partial)
        assert blocked.value.code == "export_inputs"
    assert read(ws.storage, cid) == before
    assert {p: p.read_bytes() for p in ws.storage.root.rglob("*.png")} == files

    # The producer makes the correction. It persists as metadata, then delivery works.
    ws.save_composition(
        cid,
        TextEdit(
            revision_id=before["version"],
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            placements={
                "message": TextPlacement(
                    left=0.06, top=0.24, width=0.88, font_size=40, font_id="barlow_condensed"
                )
            },
        ),
    )
    corrected = ws.current_composition(cid)
    assert delivery_manifest(corrected)["complete"]
    assert current_image(ws.state, corrected, selected["id"])[0].startswith(b"\x89PNG")
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
