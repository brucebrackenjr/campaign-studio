"""Product-photo transparency feedback does not prohibit transparent logos or cutouts."""

import copy
import io

import pytest
from campaign_fixtures import authored_edit, seed_campaign
from PIL import Image

from campaign_studio.brief import Brief
from campaign_studio.intake import Intake
from campaign_studio.planning import make_plan

pytest_plugins = ["test_web_workflow"]

EVIDENCE = "This image has transparent areas."
REMEDY = "Select a product image without transparency."


def attach(client, workspace, campaign, image, role="product"):
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        payload = buffer.getvalue()
    asset = Intake(workspace.storage).upload(payload, "image/png", campaign, role)
    assert client.get(asset["url"]).content == payload
    return asset


def check_preview(client, campaign, document, value):
    response = client.post(
        f"/api/motif/campaigns/{campaign}/brief-preview",
        json={
            "version": document["version"],
            "brief": value,
            "product_id": value["products"][0]["id"],
            "market": "en-US",
            "ratio": "1:1",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("mode", "alpha", "size", "blocked"),
    [
        ("RGBA", 0, (64, 64), True),
        ("RGBA", 254, (64, 64), True),
        ("RGB", None, (64, 64), False),
        ("RGBA", 255, (64, 64), False),
        ("RGB", None, (1, 1), False),
    ],
)
def test_finished_scene_requires_opaque_pixels_and_explains_remedy(ui, mode, alpha, size, blocked):
    client, workspace = ui
    campaign = seed_campaign(workspace)
    document = authored_edit(client, campaign)
    value = copy.deepcopy(document["authored"]["brief"])
    with Image.new(mode, size, "#305A60") as image:
        if alpha is not None:
            image.putpixel((0, 0), (48, 90, 96, alpha))
        asset = attach(client, workspace, campaign, image)
    value["products"][0]["source"]["path"] = asset["reference"]
    plan = make_plan(Brief.model_validate(value), workspace.storage)
    failures = [item for item in plan.findings if item.rule == "scene_coverage"]
    preview = check_preview(client, campaign, document, value)
    if blocked:
        assert plan.blocked and len(failures) == 1
        assert failures[0].evidence == EVIDENCE and failures[0].remedy == REMEDY
        assert failures[0].severity == "blocking"
        assert failures[0].item == value["products"][0]["id"]
        assert preview["status"] == "invalid" and preview["image"] is None
        issue = next(item for item in preview["issues"] if item["code"] == "scene_coverage")
        assert issue["message"] == EVIDENCE + " " + REMEDY
        assert issue["path"] == ["products", 0, "source"]
    else:
        assert not plan.blocked and not failures
        assert preview["status"] == "ready" and preview["kind"] == "composition"
        assert not preview["issues"]


def test_transparent_logo_remains_valid_in_upload_plan_and_preview(ui):
    client, workspace = ui
    campaign = seed_campaign(workspace)
    document = authored_edit(client, campaign)
    value = copy.deepcopy(document["authored"]["brief"])
    with Image.new("RGBA", (80, 40), (0, 0, 0, 0)) as logo:
        logo.paste((255, 255, 255, 255), (10, 10, 70, 30))
        asset = attach(client, workspace, campaign, logo, "logo")
    assert asset["opaque"] is False
    value["brand"]["logo_path"] = asset["reference"]
    assert not make_plan(Brief.model_validate(value), workspace.storage).blocked
    preview = check_preview(client, campaign, document, value)
    assert preview["status"] == "ready" and not preview["issues"]
    assert preview["logo"]["opaque"] is False


def test_prepared_product_still_accepts_transparent_cutout_with_margin(ui):
    client, workspace = ui
    campaign = seed_campaign(workspace)
    document = authored_edit(client, campaign)
    value = copy.deepcopy(document["authored"]["brief"])
    with Image.new("RGBA", (100, 100), (0, 0, 0, 0)) as image:
        image.paste((150, 70, 40, 255), (25, 25, 75, 75))
        asset = attach(client, workspace, campaign, image)
    value["products"][0]["source"] = {
        "role": "prepared_product",
        "path": asset["reference"],
        "opaque_nonreflective_product_confirmed": True,
        "provenance": {"label": "Synthetic cutout", "rights": "Local fixture", "synthetic": True},
    }
    before = workspace.storage.path(asset["reference"]).read_bytes()
    plan = make_plan(Brief.model_validate(value), workspace.storage)
    assert not plan.blocked
    assert plan.generation_products == [value["products"][0]["id"]]
    assert not any(item.rule in {"scene_coverage", "prepared_product"} for item in plan.findings)
    preview = check_preview(client, campaign, document, value)
    assert preview["status"] == "ready" and preview["kind"] == "typography"
    assert not preview["issues"]
    assert workspace.storage.path(asset["reference"]).read_bytes() == before
