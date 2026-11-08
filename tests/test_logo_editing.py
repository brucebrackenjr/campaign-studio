# ruff: noqa: F811
"""Logo metadata saves, accurate temporary layers, and ownership-safe replacements."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops
from test_current_composition import current  # noqa: F401

from campaign_studio.app import create_app
from campaign_studio.brief import Framing, LogoPlacement
from campaign_studio.campaign_drafts import read
from campaign_studio.compositions import save_metadata
from campaign_studio.config import Settings
from campaign_studio.delivery import current_image
from campaign_studio.imaging import save_srgb
from campaign_studio.intake import Intake
from campaign_studio.previews import DraftPreviews
from campaign_studio.review import CreativeUpdate
from campaign_studio.workspace import ActionError, FrameEdit, LogoEdit, Workspace


def command(state, run_id, **changes):
    cid = state.run(run_id)["campaign_id"]
    return LogoEdit(
        run_id=run_id,
        revision_id=read(state.storage, cid)["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        placement=LogoPlacement(left=0.04, top=0.03, width=0.2),
        **changes,
    )


def replacement(state, cid, size=(300, 900)):
    with Image.new("RGBA", size, "#167c52") as image, io.BytesIO() as buffer:
        save_srgb(image, buffer)
        return Intake(state.storage).upload(buffer.getvalue(), "image/png", cid)["reference"]


def edit(ws, cid, **changes):
    return LogoEdit(
        revision_id=read(ws.storage, cid)["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
        placement=LogoPlacement(left=0.1, top=0.1, width=0.2),
        **changes,
    )


def apply_logo(state, runner, action):
    from campaign_studio.motif import campaign_data

    ws = Workspace(state)
    cid = state.run(action.run_id)["campaign_id"]
    _, _, impact = ws.logo(cid, action, allow_unchanged=True)
    version = ws.save_composition(cid, action)
    return {**campaign_data(ws, cid)["manifest"], "revision_id": version}, impact


def test_selected_logo_layers_match_download_and_other_images_are_unchanged(current):
    ws, cid = current
    old = ws.current_composition(cid)
    before = {o.id: current_image(ws.state, old, o.id)[0] for o in old["plan"].outputs}
    for identifier in ("product-1/en-US/1x1", "product-2/en-US/1x1"):
        save_metadata(
            ws,
            cid,
            read(ws.storage, cid)["version"],
            identifier,
            CreativeUpdate(expected_version=0, description="Supplied description."),
        )
    action = edit(ws, cid)
    _, plan, impact = ws.logo(cid, action)
    assert impact["render_outputs"] == [action.output_id]
    previews = DraftPreviews(ws.state)
    rendered = previews.render(cid, action, plan, impact, logo_preview=True)[0]
    token = rendered["url"].split("/")[-4]

    def image(suffix=""):
        return previews.image(cid, token, action.product_id, action.market, "1x1" + suffix)

    proposed = image()
    with (
        Image.open(io.BytesIO(image("-base"))) as base,
        Image.open(io.BytesIO(image("-copy"))) as text,
        Image.open(io.BytesIO(image("-logo"))) as logo,
    ):
        assert text.mode == "RGBA"
        left, top, right, bottom = rendered["layers"]["bounds"]
        combined = base.convert("RGBA")
        artwork = logo.resize((right - left, bottom - top), Image.Resampling.LANCZOS)
        combined.alpha_composite(artwork, (left, top))
        combined.alpha_composite(text)
        with Image.open(io.BytesIO(proposed)) as final:
            assert combined.convert("RGB").tobytes() == final.convert("RGB").tobytes()
        combined.close()
        artwork.close()
    ws.save_composition(cid, action)
    after = ws.current_composition(cid)
    assert image() == proposed  # Immutable issued preview remains readable during decoding.
    for output in after["plan"].outputs:
        payload = current_image(ws.state, after, output.id)[0]
        if output.id == action.output_id:
            with (
                Image.open(io.BytesIO(payload)) as actual,
                Image.open(io.BytesIO(proposed)) as preview,
            ):
                assert actual.convert("RGB").tobytes() == preview.convert("RGB").tobytes()
        else:
            with (
                Image.open(io.BytesIO(payload)) as a,
                Image.open(io.BytesIO(before[output.id])) as b,
            ):
                assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes()
    assert all(v["description"] == "Supplied description." for v in after["metadata"].values())
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))


@pytest.mark.parametrize("replace", [False, True])
def test_lost_logo_acknowledgement_retries_exact_request_without_another_write(current, replace):
    ws, cid = current
    action = edit(
        ws, cid, **({"replacement": replacement(ws.state, cid, (200, 80))} if replace else {})
    )
    version = ws.save_composition(cid, action)
    saved = read(ws.storage, cid)
    assert Workspace(type(ws.state)(ws.storage)).save_composition(cid, action) == version
    assert read(ws.storage, cid) == saved
    newer = action.model_copy(update={"placement": LogoPlacement(left=0.2, top=0.2, width=0.2)})
    with pytest.raises(ActionError, match="another tab"):
        ws.save_composition(cid, newer)
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM campaign_drafts").fetchone()[0] == 1


def test_logo_save_requires_csrf_and_origin_but_no_successful_preview(current):
    ws, cid = current
    app = create_app(
        Settings(data_dir=ws.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Provider constructed"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        body = edit(ws, cid).model_dump(mode="json")
        url = f"/api/campaigns/{cid}/logo/save"
        assert client.post(url, json=body).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert (
            client.post(url, json=body, headers={"Origin": "https://other.invalid"}).status_code
            == 403
        )
        saved = client.post(url, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["composition_version"] != body["revision_id"]
        assert (
            client.post(url, json=body).json()["composition_version"]
            == saved.json()["composition_version"]
        )
        preview = client.post(f"/api/campaigns/{cid}/logo/preview", json=body)
        assert preview.status_code == 409
    assert not list(ws.storage.root.glob("Campaigns/**/Outputs"))


def test_logo_save_does_not_open_sources_or_delete_other_files(current, monkeypatch):
    ws, cid = current
    action = edit(ws, cid)
    original = Path.open

    def protected(path, *args, **kwargs):
        if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            pytest.fail("Metadata save opened an image")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", protected)
    monkeypatch.setattr(Path, "unlink", lambda *a, **k: pytest.fail("Metadata save deleted a file"))
    ws.save_composition(cid, action)
    assert (
        read(ws.storage, cid)["authored"]["brief"]["products"][0]["framing"]["en-US"]["1:1"][
            "logo_placement"
        ]
        == action.placement.model_dump()
    )


def test_unrelated_missing_source_does_not_block_selected_edit_and_frame_keeps_logo(current):
    ws, cid = current
    asset = ws.current_composition(cid)["plan"].assets["products/product-2"]
    ws.storage.path(asset.reference).unlink()
    action = edit(ws, cid)
    version = ws.save_composition(cid, action)
    ws.save_composition(
        cid,
        FrameEdit(
            revision_id=version,
            product_id="product-1",
            market="en-US",
            ratio="1:1",
            framing=Framing(zoom=1.1),
        ),
    )
    snapshot = ws.current_composition(cid)
    assert (
        snapshot["plan"].brief.products[0].framing["en-US"]["1:1"].logo_placement
        == action.placement
    )
    assert snapshot["sources"]["product-1"]
    assert "product-2" not in snapshot["sources"]


@pytest.mark.parametrize("reference", ["/tmp/logo.png", "../../logo.png"])
def test_logo_replacement_never_opens_unowned_paths(current, reference):
    ws, cid = current
    before = read(ws.storage, cid)
    with pytest.raises((ActionError, ValueError)):
        ws.save_composition(cid, edit(ws, cid, replacement=reference))
    assert read(ws.storage, cid) == before


def test_foreign_campaign_logo_cannot_be_attached(current):
    ws, cid = current
    other, _ = Intake(ws.storage).new()
    reference = replacement(ws.state, other, (200, 80))
    before = read(ws.storage, cid)
    with pytest.raises((ActionError, ValueError)):
        ws.save_composition(cid, edit(ws, cid, replacement=reference))
    assert read(ws.storage, cid) == before


def test_missing_old_logo_can_be_replaced_without_a_provider(current):
    ws, cid = current
    old = ws.current_composition(cid)["plan"].assets["logo"]
    ws.storage.path(old.reference).unlink()
    reference = replacement(ws.state, cid, (200, 80))
    action = edit(ws, cid, replacement=reference)
    ws.save_composition(cid, action)
    snapshot = ws.current_composition(cid)
    assert snapshot["plan"].brief.brand.logo_path == reference
    assert current_image(ws.state, snapshot, action.output_id)[0].startswith(b"\x89PNG")


def test_identical_logo_upload_and_placement_preserve_download_pixels(current):
    ws, cid = current
    first = edit(ws, cid)
    ws.save_composition(cid, first)
    before = ws.current_composition(cid)
    old = current_image(ws.state, before, first.output_id)[0]
    reference = Intake(ws.storage).upload(
        ws.storage.path(before["plan"].assets["logo"].reference).read_bytes(), "image/png", cid
    )["reference"]
    ws.save_composition(cid, edit(ws, cid, replacement=reference))
    after = current_image(ws.state, ws.current_composition(cid), first.output_id)[0]
    with Image.open(io.BytesIO(old)) as a, Image.open(io.BytesIO(after)) as b:
        assert ImageChops.difference(a, b).getbbox() is None


@pytest.mark.parametrize("ratio", ["1:1", "9:16", "16:9"])
def test_open_logo_retains_custom_rectangle_and_explicit_reset_still_works(current, ratio):
    ws, cid = current
    app = create_app(Settings(data_dir=ws.storage.root), key_available=lambda: False)
    custom = {"left": 0.06, "top": 0.03, "width": 0.38}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace

        def context():
            return {
                "revision_id": read(ws.storage, cid)["version"],
                "product_id": "product-1",
                "market": "en-US",
                "ratio": ratio,
            }

        url = f"/api/campaigns/{cid}/logo"
        saved = client.post(url + "/save", json={**context(), "placement": custom})
        assert saved.status_code == 200, saved.text
        before_open = read(ws.storage, cid)
        opened = client.post(url + "/preview", json=context())
        assert opened.status_code == 200, opened.text
        layer = opened.json()["images"][0]["layers"]
        assert layer["placement"] == custom
        assert layer["bounds"] != layer["default_bounds"]
        assert read(ws.storage, cid) == before_open

        nudged = {**layer["placement"], "left": layer["placement"]["left"] + 0.01}
        assert (
            client.post(url + "/save", json={**context(), "placement": nudged}).status_code == 200
        )
        reopened = client.post(url + "/preview", json=context()).json()["images"][0]["layers"]
        assert reopened["placement"] == nudged

        reset = {**context(), "placement": None}
        preview = client.post(url + "/preview", json=reset).json()["images"][0]["layers"]
        assert preview["placement"] is None
        assert preview["bounds"] == preview["default_bounds"]
        assert client.post(url + "/save", json=reset).status_code == 200
        assert (
            client.post(url + "/preview", json=context()).json()["images"][0]["layers"]["placement"]
            is None
        )


def test_logo_receipt_distinguishes_omitted_placement_from_explicit_reset(current):
    ws, cid = current
    ws.save_composition(cid, edit(ws, cid))
    opened = LogoEdit(
        revision_id=read(ws.storage, cid)["version"],
        product_id="product-1",
        market="en-US",
        ratio="1:1",
    )
    ws.save_composition(cid, opened)
    before = read(ws.storage, cid)
    # An omitted rectangle and a requested reset are different commands, even
    # though both models expose placement=None. A stale reset must not be
    # mistaken for a successful retry of the previous command.
    with pytest.raises(ActionError, match="another tab"):
        ws.save_composition(cid, opened.model_copy(update={"placement": None}))
    assert read(ws.storage, cid) == before
