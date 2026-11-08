"""Source assets have readable, stable names without weakening owned-file access."""

import asyncio
import hashlib
import io
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from campaign_fixtures import authored_edit
from PIL import Image

from campaign_studio.bundled import install_campaigns
from campaign_studio.intake import Intake
from campaign_studio.projects import valid_upload_name
from campaign_studio.storage import asset_component

pytest_plugins = ["test_web_workflow"]


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "darkgreen").save(stream, format="PNG")
    return stream.getvalue()


def test_upload_labels_are_readable_unique_owned_and_served(ui):
    client, workspace = ui
    campaign, _ = Intake(workspace.storage).new()
    payload = image_bytes()
    refs = []
    for label, role in [
        ("Leaflight lantern", "product"),
        ("Leaflight lantern", "product"),
        ("Copperleaf wind sculpture", "product"),
        ("Bridle & Bough logo", "logo"),
        ("../雪のランタン\\extra:<script>#%", "product"),
    ]:
        response = client.post(
            "/api/uploads",
            content=payload,
            headers={
                "content-type": "image/png",
                "X-Campaign-Id": campaign,
                "X-Image-Role": role,
                "X-Asset-Name": quote(label, safe=""),
            },
        )
        assert response.status_code == 200, response.text
        upload = response.json()
        ref = upload["reference"]
        name = PurePosixPath(ref).name
        assert name.startswith(asset_component(label, "Image") + "-")
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-[a-f0-9]{32}\.png", name)
        assert valid_upload_name(name)
        assert workspace.storage.project(campaign).upload_name(ref) == name
        assert client.get(upload["url"]).content == payload
        assert workspace.storage.path(ref).read_bytes() == payload
        assert ref not in refs
        refs.append(ref)
    with workspace.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM uploads").fetchone()[0] == 5
        assert all(
            db.execute("SELECT campaign_id FROM file_owners WHERE reference=?", (ref,)).fetchone()[
                0
            ]
            == campaign
            for ref in refs
        )
    folder = workspace.storage.path(workspace.storage.project(campaign).ref("Assets"))
    assert {p.name for p in folder.iterdir()} == {"Brand", "Products"}
    assert len(list(folder.rglob("*.png"))) == 5


def test_legacy_upload_names_remain_readable_without_uncontrolled_paths(ui):
    client, workspace = ui
    campaign, _ = Intake(workspace.storage).new()
    project = workspace.storage.project(campaign)
    name = "a" * 32 + ".png"
    ref = project.ref("Assets/Products/" + name)
    payload = image_bytes()
    project.write(ref, payload)
    with workspace.storage.connect() as db:
        db.execute("INSERT INTO uploads VALUES (?,?)", (name, ref))
    assert project.upload_name(ref) == name
    assert client.get("/uploads/" + name).content == payload
    for unsafe in ["../" + name, "../Lantern-" + name, "NUL-" + name, "x/" + name]:
        assert not valid_upload_name(unsafe)
    # A plausible name alone never authorizes a file.
    missing = "Lantern-" + "b" * 32 + ".png"
    assert client.get("/uploads/" + missing).status_code == 404


def test_real_bundled_assets_use_names_and_autosaves_do_not_rename_or_copy(ui):
    client, workspace = ui
    receipt = asyncio.run(install_campaigns(workspace))
    assert receipt["state"] == "complete"
    intake = Intake(workspace.storage)
    refs = []
    supplied_campaign = None
    for entry in receipt["campaigns"]:
        campaign = entry["id"]
        brief = intake.read(campaign)["value"]
        expected = [brief["brand"]["name"] + " logo"]
        expected.extend(p["name"] for p in brief["products"] if p["source"]["role"] != "concept")
        if len(brief["products"]) == 5:
            supplied_campaign = campaign
        # A paid-source plan can use its frozen Inputs copies. Check the user-visible
        # original Assets names, and independently verify the current source references.
        current_refs = [brief["brand"]["logo_path"]] + [
            p["source"]["path"] for p in brief["products"] if p["source"]["role"] != "concept"
        ]
        assert all(workspace.storage.path(ref).is_file() for ref in current_refs)
        for ref, label in zip(current_refs, expected, strict=True):
            assert PurePosixPath(ref).name.startswith(asset_component(label, "Image") + "-")
        current = client.get(f"/api/motif/campaigns/{campaign}")
        assert current.status_code == 200, current.text
        assert len(current.json()["manifest"]["outputs"]) == len(brief["products"]) * 6
        with workspace.storage.connect() as db:
            uploaded = [
                row[0]
                for row in db.execute(
                    "SELECT u.reference FROM uploads u JOIN file_owners f "
                    "ON f.reference=u.reference WHERE f.campaign_id=?",
                    (campaign,),
                )
            ]
        assert len(uploaded) == len(expected)
        for label in expected:
            ref = next(
                ref
                for ref in uploaded
                if PurePosixPath(ref).name.startswith(asset_component(label, "Image") + "-")
            )
            name = PurePosixPath(ref).name
            assert valid_upload_name(name)
            assert intake.uploaded_path(name) == workspace.storage.path(ref)
            assert client.get("/uploads/" + quote(name, safe="")).status_code == 200
            refs.append(ref)
    assert len(refs) == 9  # Five Autumn photos, two Winter photos, and their two logos.
    before = {
        ref: hashlib.sha256(workspace.storage.path(ref).read_bytes()).hexdigest() for ref in refs
    }
    campaign = supplied_campaign
    assert campaign is not None
    for index in range(5):
        edit = authored_edit(client, campaign)
        edit["authored"]["brief"]["audience"] = f"Named-image autosave check {index}"
        response = client.post(f"/api/motif/campaigns/{campaign}/authored", json=edit)
        assert response.status_code == 200, response.text
    assert before == {
        ref: hashlib.sha256(workspace.storage.path(ref).read_bytes()).hexdigest() for ref in refs
    }
    with workspace.storage.connect() as db:
        assert {row[0] for row in db.execute("SELECT reference FROM uploads")} == set(refs)
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not list(workspace.storage.root.glob("Campaigns/*/Outputs"))
    assert not list(workspace.storage.root.glob("Campaigns/*/Exports"))


def test_asset_stems_remove_spaces_symbols_and_keep_collision_suffixes():
    assert asset_component("Bridle & Bough logo") == "bridle-bough-logo"
    assert asset_component("Leaflight   lantern") == "leaflight-lantern"
    assert asset_component("Café / bowl (large)") == "cafe-bowl-large"
    assert asset_component("..", "Product image") == "product-image"
    assert asset_component("CON") == "image-con"
    assert valid_upload_name("Earlier Brand & Logo-" + "c" * 32 + ".png")


def test_long_matching_product_labels_keep_their_frozen_input_identity(ui):
    from campaign_studio.planning import make_plan
    from campaign_studio.sample import write_sample

    _, workspace = ui
    _, brief = write_sample(workspace.storage)
    for product in brief.products:
        product.name = "A" * 80
    plan = make_plan(brief, workspace.storage)
    run_id = workspace.state.create(plan, 0)
    frozen = workspace.state.plan(run_id, current=False)
    names = [
        PurePosixPath(asset.reference).name
        for key, asset in frozen.assets.items()
        if key.startswith("products/")
    ]
    assert len(names) == len(brief.products) == len(set(names))
    assert all(re.fullmatch(r"a{80}-[a-f0-9]{12}\.png", name) for name in names)
    assert all(
        workspace.storage.path(asset.reference).is_file() for asset in frozen.assets.values()
    )
