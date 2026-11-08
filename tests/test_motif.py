import asyncio
import copy
import hashlib
import io
import json
from zipfile import ZipFile

import httpx
import pytest
from campaign_fixtures import authored_edit, seed_campaign, zip_root
from fastapi.testclient import TestClient
from test_provider_runner import generated_case

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.costing import estimate_requests
from campaign_studio.generation import GenerationRequest
from campaign_studio.intake import Intake
from campaign_studio.provider import OpenAIImages
from campaign_studio.sample import write_sample
from campaign_studio.workspace import Workspace


def test_campaignless_page_aliases_reach_supported_home_without_creating_work(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Opening a page must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        with ws.storage.connect() as db:
            before = list(db.iterdump())
        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 204
        assert favicon.content == b""
        files_before = sorted(
            path.relative_to(ws.storage.root) for path in ws.storage.root.rglob("*")
        )
        for page in ("campaign", "production", "studio", "handoff"):
            response = client.get(f"/{page}", follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/"
            opened = client.get(f"/{page}")
            assert opened.status_code == 200 and opened.url.path == "/"
            assert 'data-page="library"' in opened.text
            assert "/static/motif/actions.js" in opened.text
            assert "/example-image/" not in opened.text
        assert ws.catalog() == [] and ws.runs() == []
        with ws.storage.connect() as db:
            assert list(db.iterdump()) == before
        assert (
            sorted(path.relative_to(ws.storage.root) for path in ws.storage.root.rglob("*"))
            == files_before
        )


def test_unknown_motif_pages_use_recovery_shell_without_mutating_workspace(tmp_path):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("A missing page must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with app.state.workspace.storage.connect() as db:
            before = list(db.iterdump())
        for path, heading, action in (
            ("/a-missing-page", "This page isn’t available.", "Open workbench"),
            ("/campaigns/not-a-campaign/not-a-page", "Campaign not found", "Back to workbench"),
        ):
            response = client.get(path)
            assert response.status_code == 404
            assert heading in response.text
            assert action in response.text
            assert "/static/motif/actions.js" not in response.text
        api = client.get("/api/not-a-route")
        assert api.status_code == 404 and api.headers["content-type"].startswith("application/json")
        assert app.state.workspace.catalog() == []
        with app.state.workspace.storage.connect() as db:
            assert list(db.iterdump()) == before


@pytest.mark.parametrize("count", [1, 5])
def test_motif_import_upload_plan_and_invalid_import_atomicity(tmp_path, count):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("No provider during intake"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/").status_code == 200
        assert client.post("/api/motif/import", json={}).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        _, fixture = write_sample(ws.storage)
        value = fixture.model_dump(mode="json")
        value["products"] = [copy.deepcopy(value["products"][0]) for _ in range(count)]
        for index, product in enumerate(value["products"]):
            product["id"] = f"imported-{index}"
        created = client.post("/api/motif/import", json=value)
        assert created.status_code == 200, created.text
        cid = created.json()["campaign_id"]
        document = client.get(f"/api/motif/campaigns/{cid}").json()
        assert len(document["value"]["products"]) == count
        row = next(
            c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
        )
        assert row["thumbnail"] is None and row["thumbnail_expected"] is False
        for route, page in {
            "campaign": "campaign",
            "production": "production",
            "studio": "review",
            "handoff": "handoff",
        }.items():
            url = f"/campaigns/{cid}/{route}"
            shell = client.get(url, follow_redirects=False)
            assert shell.status_code == 200 and "location" not in shell.headers
            assert f'data-campaign="{cid}"' in shell.text
            assert f'data-page="{page}"' in shell.text
        assert document["value"]["brand"]["logo_path"] == ""
        assert all(p["source"]["path"] == "" for p in document["value"]["products"])
        assert len(document["attachment_hints"]) == count + 1
        assert all(
            "/" not in name and "\\" not in name for name in document["attachment_hints"].values()
        )
        count_before = len(client.get("/api/motif/workspace").json()["campaigns"])
        value["brand"]["palette"] = None
        assert client.post("/api/motif/import", json=value).status_code == 422
        assert len(client.get("/api/motif/workspace").json()["campaigns"]) == count_before

        def upload(reference):
            result = client.post(
                "/api/uploads",
                content=ws.storage.path(reference).read_bytes(),
                headers={"Content-Type": "image/png", "X-Campaign-Id": cid},
            )
            assert result.status_code == 200
            return result.json()["reference"]

        brief = document["value"]
        brief["brand"]["logo_path"] = upload(fixture.brand.logo_path)
        photo = upload(fixture.products[0].source.path)
        for product in brief["products"]:
            product["source"]["path"] = photo
        action = authored_edit(client, cid)
        action["authored"]["brief"] = brief
        saved = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
        assert saved.status_code == 200, saved.text
        current = client.get(f"/api/motif/campaigns/{cid}").json()
        assert current["attachment_hints"] == document["attachment_hints"]
        row = next(
            c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
        )
        assert row["thumbnail"] and row["thumbnail_expected"] is True
        planned = client.post(
            f"/api/motif/campaigns/{cid}/plan", json={"version": saved.json()["version"]}
        )
        assert planned.status_code == 200, planned.text
        assert planned.json()["plan"]["outputs"] == count * 6
        assert planned.json()["plan"]["generation_products"] == []
        assert planned.json()["run"] is None
        action = authored_edit(client, cid)
        action["authored"]["brief"]["name"] = "Changed after planning"
        edited = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
        assert edited.status_code == 200
        current = client.get(f"/api/motif/campaigns/{cid}").json()
        assert current["output_brief"]["name"] == "Changed after planning"
        assert current["manifest"]["complete"] and current["can_edit"]
        assert current["run"] is None
        assert (
            client.post(
                f"/api/motif/campaigns/{cid}/authored",
                json=action | {"version": document["version"]},
            ).status_code
            == 409
        )
        with ws.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0


def test_workbench_image_expectation_uses_first_product_source_receipt(tmp_path):
    state, runner, run, body = generated_case(tmp_path)
    cid = state.run(run)["campaign_id"]
    app = create_app(Settings(data_dir=state.storage.root), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:

        def card():
            return next(
                c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
            )

        assert card()["thumbnail"] is None and not card()["thumbnail_expected"]
        provider = OpenAIImages(
            "synthetic-only",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        )
        assert asyncio.run(runner.execute(run, provider))["complete"]
        assert card()["thumbnail"] and card()["thumbnail_expected"]
        state.storage.path(state.attempts(run)[0]["source_ref"]).unlink()
        assert card()["thumbnail"] is None
        assert state.revision_count(run) == 0


@pytest.mark.parametrize("instructions_changed", [False, True])
def test_new_concept_reuses_unchanged_generated_product_and_reopens(
    tmp_path, monkeypatch, instructions_changed
):
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.state import State

    state, runner, run, response = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    provider = OpenAIImages("synthetic-only", transport=httpx.MockTransport(transport))
    assert asyncio.run(runner.execute(run, provider))["complete"]
    if instructions_changed:
        monkeypatch.setattr(
            "campaign_studio.generation.CONCEPT_FRAMING", "Updated composition instructions."
        )
    ws = Workspace(state)
    cid = state.run(run)["campaign_id"]
    project = AuthoredProjects(ws)
    current = project.read(cid)
    value = copy.deepcopy(current["authored"])
    new = copy.deepcopy(value["brief"]["products"][0])
    new.update(id="new-product", name="Another concept")
    new["source"]["description"] = "A different geometric synthetic subject."
    value["brief"]["products"].append(new)
    saved = project.save(
        cid, AuthoredSave(version=current["version"], base=current["base"], authored=value)
    )
    plan = Intake(state.storage).prepare(cid, saved["version"], ws)
    assert plan.generation_products == ["new-product"]
    next_run = ws.run_for(cid)["id"]
    state.confirm(next_run, state.plan(next_run).digest)
    assert asyncio.run(runner.execute(next_run, provider))["complete"]
    assert len(calls) == 2 and len(state.attempts(next_run)) == 1
    reopened = Workspace(State(state.storage)).current_composition(cid)
    assert len(reopened["sources"]) == 3 and len(reopened["plan"].outputs) == 18
    assert state.run(next_run)["current_revision"] is None


@pytest.mark.parametrize("origin", ["included", "generated"])
def test_brief_concept_preview_follows_current_plan_after_replanning(tmp_path, origin):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []
    if origin == "included":
        ws = Workspace(state)
        cid = seed_campaign(ws, replay=True)

    else:

        def transport(request):
            calls.append(request)
            return httpx.Response(200, json=body)

        asyncio.run(
            runner.execute(
                run_id,
                OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport)),
            )
        )
        cid = state.run(run_id)["campaign_id"]
    files = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.storage.root.rglob("*.png")
    }
    attempts = state.attempts(run_id) if origin == "generated" else []
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Replanning must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        endpoint = f"/api/motif/campaigns/{cid}"
        original = client.get(endpoint).json()
        product = original["value"]["products"][0]["id"]
        original_preview = original["concept_previews"][product]
        assert client.get(original_preview).status_code == 200
        if origin == "generated":
            # The first generated source is in its attempt record, before a later
            # reuse plan promotes it to a plan asset. Damage must not look available.
            source = state.storage.path(attempts[0]["source_ref"])
            payload = source.read_bytes()
            source.write_bytes(b"Controlled damaged source")
            assert client.get(endpoint).json()["concept_previews"] == {}
            assert client.get(original_preview).status_code == 404
            source.write_bytes(payload)
        authored = client.get(endpoint + "/authored").json()
        authored["authored"]["brief"]["markets"]["en-US"]["copy"]["cta"] = "Explore locally"
        saved = client.post(
            endpoint + "/authored",
            json={key: authored[key] for key in ("version", "base", "authored")},
        )
        assert saved.status_code == 200, saved.text
        # A copy-only replan explicitly retains the available master in its plan.
        retained = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
        assert retained.status_code == 200, retained.text
        current = retained.json()
        product = current["value"]["products"][0]["id"]
        preview = current["concept_previews"][product]
        assert client.get(preview).status_code == 200
        action = client.get(endpoint + "/authored").json()
        action = {key: action[key] for key in ("version", "base", "authored")}
        action["authored"]["brief"]["audience"] += " A different audience."
        saved = client.post(endpoint + "/authored", json=action)
        assert saved.status_code == 200, saved.text
        assert client.get(endpoint).json()["concept_previews"] == {}
        assert client.get(preview).status_code == 404
        replanned = client.post(endpoint + "/plan", json={"version": saved.json()["version"]})
        assert replanned.status_code == 200, replanned.text
        current = replanned.json()
        assert current["concept_previews"] == {}
        assert current["plan"]["generation_products"] == [product]
        assert current["version"] != original["version"]
        assert client.get(preview).status_code == 404  # The UI must not request this absent source.
        supplied = current["value"]["products"][1]["id"]
        assert client.get(f"/campaigns/{cid}/sources/{supplied}/1x1").status_code == 200
        assert app.state.workspace.state.attempts(current["run"]["id"]) == []
    assert (state.attempts(run_id) if origin == "generated" else []) == attempts
    assert len(calls) == (origin == "generated")
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in files.items()
    )


def test_cost_estimate_is_per_generation_request_and_unknown_for_other_model():
    request = GenerationRequest(prompt="A complete fictional product, centered in a scene.")
    one = estimate_requests({"product-a": request})
    two = estimate_requests({"product-a": request, "product-b": request})
    assert 0.09 < one["estimated_usd"] < 0.11
    assert two["estimated_usd"] == round(one["estimated_usd"] * 2, 6)
    assert one["actual_charge"] is None
    assert estimate_requests({})["estimated_usd"] == 0
    assert (
        estimate_requests({"p": request.model_copy(update={"model": "unsupported"})})[
            "estimated_usd"
        ]
        is None
    )


def test_usage_estimate_does_not_turn_unknown_attempts_into_free_work():
    from campaign_studio.costing import estimate_recorded_usage

    request = GenerationRequest(prompt="Complete subject.")
    known = {"product_id": "p", "usage_json": '{"input_tokens":200,"output_tokens":3276}'}
    result = estimate_recorded_usage([known], {"p": request})
    assert result["estimated_usd"] == 0.09928
    assert result["actual_charge"] is None
    missing = {"product_id": "p", "usage_json": None}
    uncertain = estimate_recorded_usage([known, missing], {"p": request})
    assert uncertain["estimated_usd"] is None
    assert uncertain["known_usage_subtotal_usd"] == result["estimated_usd"]
    assert uncertain["unreported_attempts"] == 1
    assert estimate_recorded_usage([], {})["estimated_usd"] == 0


def test_partial_first_run_review_recovery_retry_and_partial_export(tmp_path):
    from campaign_studio.brief import Brief
    from campaign_studio.provider import ProviderFailure

    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Retry review must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, fixture = write_sample(ws.storage)
        value = fixture.model_dump(mode="json")
        value["products"][0]["source"] = {
            "role": "concept",
            "imagery_absent": True,
            "description": "An invented geometric fixture.",
            "provenance": value["products"][0]["source"]["provenance"],
        }
        brief = Brief.model_validate(value)
        intake = Intake(ws.storage)
        draft = intake.write(brief.campaign_id, brief.model_dump(mode="json"), None)
        intake.prepare(brief.campaign_id, draft["version"], ws)
        run = ws.run_for(brief.campaign_id)
        ws.state.confirm(run["id"], ws.state.plan(run["id"]).digest)

        class Unknown:
            async def generate(self, *args):
                raise ProviderFailure("unknown", "deadline")

        asyncio.run(app.state.runner.execute(run["id"], Unknown()))
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        shown = client.get(url).json()
        assert shown["manifest"]["preview_only"]
        assert sum(bool(o.get("url")) for o in shown["manifest"]["outputs"]) == 6
        assert shown["can_edit"] and shown["can_describe"]
        assert shown["editable_products"] == {"product-1": False, "product-2": True}
        assert not shown["unplanned_draft"]
        retry = shown["retry_plan"]
        assert retry["run_id"] == run["id"] and retry["allowance"] == 2
        assert retry["previous_attempts"] == 1
        assert retry["generation_products"] == [brief.products[0].id]
        assert ws.state.run(run["id"])["allowance"] == 1
        assert len(ws.state.attempts(run["id"])) == 1
        assert shown["campaign_metrics"]["usage_cost_estimate"]["estimated_usd"] is None
        client.headers["X-Campaign-Token"] = app.state.csrf
        package_url = f"/api/campaigns/{brief.campaign_id}/delivery/zip"
        assert (
            client.post(
                package_url,
                json={"version": shown["version"], "allow_partial": False},
            ).status_code
            == 409
        )
        packaged = client.post(
            package_url,
            json={"version": shown["version"], "allow_partial": True},
        )
        assert packaged.status_code == 200, packaged.text
        with ZipFile(io.BytesIO(packaged.content)) as archive:
            manifest = json.loads(archive.read(f"{zip_root(archive)}/manifest.json"))
            assert sum(bool(item["file"]) for item in manifest["outputs"]) == 6
            assert sum(not item["file"] for item in manifest["outputs"]) == 6
        recovered = app.state.runner.recover_local(run["id"])
        assert not recovered["complete"] and recovered["metrics"]["completed_images"] == 0
        assert len(ws.state.attempts(run["id"])) == 1
        reopened = client.get(url).json()
        assert reopened["retry_plan"] == retry
        assert sum(bool(o.get("url")) for o in reopened["manifest"]["outputs"]) == 6
        assert ws.state.revision_count(run["id"]) == 0
        refused = client.post(
            f"/api/campaigns/{brief.campaign_id}/start",
            json={
                "run_id": retry["run_id"],
                "digest": retry["digest"],
                "allowance": retry["allowance"],
                "retry": True,
            },
        )
        assert refused.status_code == 409 and "key" in refused.json()["message"].lower()
        assert len(ws.state.attempts(run["id"])) == 1
        assert ws.state.run(run["id"])["allowance"] == 1


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_current_source_damage_blocks_download_and_restoration_recovers_without_paid_request(
    tmp_path, damage
):
    from campaign_fixtures import seed_campaign

    app = create_app(
        Settings(data_dir=tmp_path / "runtime"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Local repair cannot call provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        cid = seed_campaign(ws)
        url = f"/api/motif/campaigns/{cid}"
        before = client.get(url).json()
        asset = ws.current_composition(cid)["sources"][before["value"]["products"][0]["id"]]
        path = ws.storage.path(asset.reference)
        payload = path.read_bytes()
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b"damaged source")
        damaged = client.get(url).json()
        assert not damaged["manifest"]["complete"]
        assert (
            client.post(
                f"/api/campaigns/{cid}/delivery/zip", json={"version": damaged["version"]}
            ).status_code
            == 409
        )
        path.write_bytes(payload)
        restored = client.get(url).json()
        assert restored["manifest"]["complete"]
        assert restored["version"] == before["version"]
        assert (
            client.post(
                f"/api/campaigns/{cid}/delivery/zip", json={"version": restored["version"]}
            ).status_code
            == 200
        )
        with ws.storage.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0


def test_http_framing_autosave_refresh_and_export_use_same_current_settings(tmp_path):
    from campaign_fixtures import seed_campaign

    app = create_app(Settings(data_dir=tmp_path / "runtime"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        cid = seed_campaign(app.state.workspace)
        route = f"/api/motif/campaigns/{cid}"
        before = client.get(route).json()
        chosen = before["manifest"]["outputs"][0]
        body = {
            "revision_id": before["version"],
            "product_id": chosen["product_id"],
            "market": chosen["market"],
            "ratio": chosen["ratio"],
            "framing": {"zoom": 1.25, "focal_x": 0.6, "focal_y": 0.4},
        }
        preview = client.post(f"/api/campaigns/{cid}/framing/preview", json=body)
        assert preview.status_code == 200 and not preview.json()["blocked"]
        saved = client.post(f"/api/campaigns/{cid}/framing/save", json=body)
        assert saved.status_code == 200, saved.text
        assert (
            client.post(f"/api/campaigns/{cid}/framing/save", json=body).json()["version"]
            == saved.json()["version"]
        )
        current = client.get(route).json()
        selected = next(o for o in current["manifest"]["outputs"] if o["id"] == chosen["id"])
        assert (
            current["output_brief"]["products"][0]["framing"][chosen["market"]][chosen["ratio"]][
                "zoom"
            ]
            == 1.25
        )
        stale = body | {"framing": {"zoom": 1.5}}
        assert client.post(f"/api/campaigns/{cid}/framing/save", json=stale).status_code == 409
        assert client.get(route).json()["version"] == current["version"]
        assert client.get(chosen["url"]).status_code == 409
        assert client.get(selected["url"]).status_code == 200
        assert not list(app.state.storage.root.glob("Campaigns/*/Outputs"))
