"""Source preparation does not enforce finished-ad wording or logo policy."""

import base64
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_production_product_changes import generate, prepare
from test_web_workflow import enable_actions

from campaign_studio.app import create_app
from campaign_studio.campaign_drafts import write
from campaign_studio.config import Settings
from campaign_studio.provider import OpenAIImages
from campaign_studio.sample import write_sample


@pytest.mark.parametrize("logo", ["absent", "broken"])
@pytest.mark.parametrize("rule_id", ["blocked", "asset"])
def test_source_plan_and_start_ignore_copy_and_logo_but_export_still_checks(
    tmp_path, logo, rule_id
):
    calls, body = [], {}

    def response(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    app = create_app(
        Settings(data_dir=tmp_path / "workspace"),
        key_available=lambda: True,
        provider_factory=lambda: OpenAIImages("mock", transport=httpx.MockTransport(response)),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        enable_actions(client)
        ws = app.state.workspace
        _, brief = write_sample(ws.storage, one_market=True)
        value = brief.model_dump(mode="json")
        cid = brief.campaign_id
        body["data"] = [
            {
                "b64_json": base64.b64encode(
                    ws.storage.path(value["products"][0]["source"]["path"]).read_bytes()
                ).decode()
            }
        ]
        original_logo = value["brand"]["logo_path"]
        if logo == "broken":
            ws.storage.path(original_logo).write_bytes(b"not an image")
        else:
            value["brand"]["logo_path"] = ""
        value["brand"]["require_logo"] = True
        value["markets"]["en-US"]["phrase_rules"] = [{"id": rule_id, "phrase": "guaranteed"}]
        value["markets"]["en-US"]["copy"]["message"] = "A guaranteed adventure"
        value["products"][1]["source"] = {
            "role": "concept",
            "imagery_absent": True,
            "description": "A controlled synthetic product scene.",
            "provenance": value["products"][0]["source"]["provenance"],
        }
        write(
            ws.storage,
            cid,
            {"composition_schema": 1, "version": uuid4().hex, "authored": {"brief": value}},
        )
        url = f"/api/motif/campaigns/{cid}"
        opened = client.get(url).json()
        prepared = prepare(client, cid, opened["version"])
        assert prepared["wording_issue"]
        current = generate(client, cid, prepared)
        assert len(calls) == 1
        assert len(current["source_urls"]) == 2
        assert all(current["editable_products"].values())
        assert current["project"]["authored"]["brief"]["brand"]["logo_path"] == (
            original_logo if logo == "broken" else ""
        )
        assert current["wording_issue"]
        exported = client.post(
            f"/api/campaigns/{cid}/delivery/zip",
            json={"version": current["version"], "allow_partial": True},
        )
        assert exported.status_code == 409
        assert exported.json()["code"] == "prohibited_phrase"
        assert len(calls) == 1


def test_upload_waits_for_preview_lock_instead_of_refusing_valid_logo(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    app = create_app(Settings(data_dir=tmp_path / "workspace"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        enable_actions(client)
        ws = app.state.workspace
        _, brief = write_sample(ws.storage, one_market=True)
        payload = ws.storage.path(brief.brand.logo_path).read_bytes()
        entered, release, uploaded = Event(), Event(), Event()

        def render_held_preview():
            with app.state.previews.lock, ws.storage.exclusive_run():
                entered.set()
                assert release.wait(5)

        def attach():
            try:
                return client.post(
                    "/api/uploads",
                    content=payload,
                    headers={
                        "Content-Type": "image/png",
                        "X-Campaign-Id": brief.campaign_id,
                        "X-Image-Role": "logo",
                        "X-Asset-Name": "New logo",
                    },
                )
            finally:
                uploaded.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            preview = pool.submit(render_held_preview)
            assert entered.wait(5)
            request = pool.submit(attach)
            try:
                assert not uploaded.wait(0.15), "Upload must wait, not refuse or bypass the preview"
            finally:
                release.set()
            preview.result(timeout=5)
            result = request.result(timeout=5)
        assert result.status_code == 200, result.text
        assert result.json()["opaque"] is False
        # A real workspace operation remains protected by its original lock.
        with ws.storage.exclusive_run():
            refused = attach()
        assert refused.status_code == 409
