import asyncio
import copy
import hashlib

import httpx
import pytest
from campaign_fixtures import authored_edit, seed_campaign
from fastapi.testclient import TestClient
from test_provider_runner import generated_case

from campaign_studio.app import create_app
from campaign_studio.config import Settings
from campaign_studio.costing import estimate_requests
from campaign_studio.generation import GenerationRequest
from campaign_studio.intake import BriefDraft, Intake
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
        assert ws.state.attempts(planned.json()["run"]["id"]) == []
        action = authored_edit(client, cid)
        revised = action["authored"]["brief"]
        revised["name"] = "Changed after planning"
        edited = client.post(f"/api/motif/campaigns/{cid}/authored", json=action)
        assert edited.status_code == 200
        current = client.get(f"/api/motif/campaigns/{cid}").json()
        assert current["unplanned_draft"]
        assert current["plan_brief"]["name"] != revised["name"]
        refused = client.post(
            f"/api/campaigns/{cid}/start",
            json={
                "run_id": planned.json()["run"]["id"],
                "digest": planned.json()["plan"]["digest"],
                "allowance": 0,
            },
        )
        assert refused.status_code == 409 and "fresh production plan" in refused.json()["message"]
        assert ws.state.attempts(planned.json()["run"]["id"]) == []
        assert (
            client.post(
                f"/api/motif/campaigns/{cid}/authored",
                json=action | {"version": document["version"]},
            ).status_code
            == 409
        )


def test_workbench_image_expectation_uses_first_product_source_receipt(tmp_path):
    state, _, run, _ = generated_case(tmp_path)
    plan = state.plan(run)
    # Keep the synthetic fixture visible as an ordinary saved draft, without
    # relabeling its provenance or constructing a provider.
    Intake(state.storage).write(plan.brief.campaign_id, plan.brief.model_dump(mode="json"), None)
    app = create_app(
        Settings(data_dir=state.storage.root),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Catalog inspection must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:

        def first_card():
            return next(
                c
                for c in client.get("/api/motif/workspace").json()["campaigns"]
                if c["id"] == plan.brief.campaign_id
            )

        # Product two's supplied image must not turn product one's absent
        # concept image into a missing-image claim or borrowed thumbnail.
        card = first_card()
        assert card["thumbnail_expected"] is False and card["thumbnail"] is None
        state.set_status(run, "running")
        attempt = state.reserve(run, plan.brief.products[0].id)
        assert first_card()["thumbnail_expected"] is False  # Reserved path is not imagery.
        source = state.storage.path(attempt["source_ref"])
        source.write_bytes(state.storage.path(plan.brief.products[1].source.path).read_bytes())
        state.finish_attempt(attempt["id"], "decoded")
        state.set_status(run, "paused")
        assert first_card()["thumbnail_expected"] is True
        source.unlink()
        assert first_card()["thumbnail_expected"] is True  # Recorded image is now unavailable.
        assert first_card()["thumbnail"] is None
        assert state.revision_count(run) == 0


@pytest.mark.parametrize("instructions_changed", [False, True])
def test_new_concept_reuses_unchanged_generated_product_and_reopens(
    tmp_path, monkeypatch, instructions_changed
):
    state, runner, run_id, response = generated_case(tmp_path)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    before = asyncio.run(runner.execute(run_id, provider))
    if instructions_changed:
        monkeypatch.setattr(
            "campaign_studio.generation.CONCEPT_FRAMING", "Updated composition instructions."
        )
    old_source = state.attempts(run_id)[0]
    old_hash = hashlib.sha256(state.storage.path(old_source["source_ref"]).read_bytes()).hexdigest()
    ws = Workspace(state)
    intake = Intake(state.storage)
    value = state.plan(run_id).brief.model_dump(mode="json")
    new_product = copy.deepcopy(value["products"][0])
    new_product["id"] = "new-product"
    new_product["name"] = "Another concept"
    new_product["source"]["description"] = "A different geometric synthetic subject."
    value["products"].append(new_product)
    document = intake.save(
        value["campaign_id"], BriefDraft(value=value, base_revision=before["revision_id"]), ws
    )
    plan = intake.prepare(value["campaign_id"], document["version"], ws)
    new_id = plan.brief.products[-1].id
    assert plan.generation_products == [new_id]
    assert set(plan.brief.replay.sources) == {value["products"][0]["id"]}
    assert plan.brief.replay.partial
    next_run = ws.run_for(value["campaign_id"])
    state.confirm(next_run["id"], state.plan(next_run["id"]).digest)
    result = asyncio.run(runner.execute(next_run["id"], provider))
    assert result["complete"] and len(result["outputs"]) == 18
    assert len(calls) == 2  # One original concept, then only the newly added concept.
    assert len(state.attempts(next_run["id"])) == 1
    assert (
        hashlib.sha256(state.storage.path(old_source["source_ref"]).read_bytes()).hexdigest()
        == old_hash
    )
    # A fresh owner reads the same persisted plan and complete revision.
    from campaign_studio.state import State

    reopened = State(state.storage)
    assert reopened.plan(next_run["id"]).brief.replay.partial
    assert reopened.run(next_run["id"])["current_revision"] == result["revision_id"]


@pytest.mark.parametrize("origin", ["included", "generated"])
def test_brief_concept_preview_follows_current_plan_after_replanning(tmp_path, origin):
    state, runner, run_id, body = generated_case(tmp_path)
    calls = []
    if origin == "included":
        ws = Workspace(state)
        cid = seed_campaign(ws, replay=True)
        run_id = ws.run_for(cid)["id"]
        state.confirm(run_id, state.plan(run_id).digest)
        asyncio.run(runner.execute(run_id))
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
    attempts = state.attempts(run_id)
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
        assert current["manifest"]["revision_id"] == original["manifest"]["revision_id"]
        assert client.get(preview).status_code == 404  # The UI must not request this absent source.
        supplied = current["value"]["products"][1]["id"]
        assert client.get(f"/campaigns/{cid}/sources/{supplied}/1x1").status_code == 200
        assert app.state.workspace.state.attempts(current["run"]["id"]) == []
    assert state.attempts(run_id) == attempts and len(calls) == (origin == "generated")
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


def test_framing_preview_save_checks_pixels_history_and_no_calls(tmp_path):
    from campaign_studio.planning import make_plan
    from campaign_studio.review import CreativeUpdate

    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Framing cannot call a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.workspace.state
        _, brief = write_sample(state.storage)
        plan = make_plan(brief, state.storage)
        run_id = state.create(plan, allowance=0)
        state.confirm(run_id, state.plan(run_id).digest)
        before = asyncio.run(app.state.runner.execute(run_id, None))
        creative_id = before["outputs"][1]["id"]
        chosen = before["outputs"][1]
        state.update_creative(
            before["revision_id"],
            creative_id,
            CreativeUpdate(expected_version=0, description="Initial composition."),
        )
        client.headers["X-Campaign-Token"] = app.state.csrf
        target = f"/api/campaigns/{brief.campaign_id}/framing"
        body = {
            "run_id": run_id,
            "revision_id": before["revision_id"],
            "product_id": chosen["product_id"],
            "market": chosen["market"],
            "ratio": chosen["ratio"],
            "framing": {
                "focal_x": 0.6,
                "focal_y": 0.4,
                "copy_placement": "bottom",
                "subject_box": None,
            },
        }
        preview = client.post(target + "/preview", json=body)
        assert preview.status_code == 200, preview.text
        preview = preview.json()
        assert not preview["blocked"]
        assert preview["impact"]["render_outputs"] == [creative_id]
        assert preview["impact"]["generation_products"] == []
        assert len(preview["images"]) == 1
        assert client.get(preview["images"][0]["url"]).status_code == 200
        assert state.run(run_id)["current_revision"] == before["revision_id"]
        body["preview_digest"] = preview["digest"]
        saved = client.post(target + "/apply", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["complete"]
        after = app.state.workspace.manifest(state.run(run_id))
        for old, new in zip(before["outputs"], after["outputs"], strict=True):
            assert (old["sha256"] != new["sha256"]) == (old["id"] == creative_id)
        assert state.attempts(run_id) == []
        assert (
            state.metadata_snapshot(before["revision_id"])["creatives"][creative_id]["description"]
            == "Initial composition."
        )
        assert (
            state.metadata_snapshot(after["revision_id"])["creatives"][creative_id]["reset_reason"]
            == "creative_changed"
        )
        assert (
            state.metadata_snapshot(after["revision_id"])["creatives"][creative_id]["description"]
            is None
        )
        assert client.post(target + "/apply", json=body).status_code == 409
        body.update(revision_id=after["revision_id"], preview_digest=None, ratio="9:16")
        body["framing"]["subject_box"] = {"left": 0, "top": 0, "right": 1, "bottom": 1}
        blocked = client.post(target + "/preview", json=body)
        assert blocked.status_code == 200, blocked.text
        # The current crop is visible for diagnosis, but cannot be saved while it
        # cuts the declared region. This is not a successful output revision.
        assert blocked.json()["blocked"] and len(blocked.json()["images"]) == 1
        finding = next(f for f in blocked.json()["findings"] if f["rule"] == "subject_crop")
        assert finding["context"]["fields"][0]["path"] == [
            "products",
            0,
            "framing",
            chosen["market"],
            "9:16",
            "subject_box",
        ]
        body["preview_digest"] = blocked.json()["digest"]
        assert client.post(target + "/apply", json=body).status_code == 422
        assert state.run(run_id)["current_revision"] == after["revision_id"]
        assert state.attempts(run_id) == []


def test_motif_current_revision_and_missing_file_are_truthful(tmp_path):
    from campaign_studio.planning import make_plan

    app = create_app(Settings(data_dir=tmp_path / "data"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        plan = make_plan(brief, ws.storage)
        run_id = ws.state.create(plan, allowance=0)
        ws.state.confirm(run_id, ws.state.plan(run_id).digest)
        manifest = asyncio.run(app.state.runner.execute(run_id, None))
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        response = client.get(url).json()
        assert response["manifest"]["complete"]
        assert len(response["metadata"]["creatives"]) == 12
        assert response["source_sizes"]
        prefix = ws.state.revision(manifest["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
        ws.storage.path(f"{prefix}/{manifest['outputs'][0]['file']}").unlink()
        damaged = client.get(url).json()
        assert damaged["manifest"]["complete"]
        assert damaged["file_integrity"]["damaged_outputs"] == [manifest["outputs"][0]["id"]]
        assert damaged["can_edit"] and damaged["can_describe"]
        assert all(damaged["editable_products"].values())
        assert damaged["manifest"]["metrics"]["completed_images"] == 11
        assert damaged["manifest"]["outputs"][0]["url"] is None


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


def test_revise_rechecks_accepted_output_inside_write_boundary(tmp_path):
    from campaign_studio.brief import Brief
    from campaign_studio.planning import make_plan
    from campaign_studio.state import StateError

    state, runner, run_id, _ = generated_case(tmp_path, concept_count=0)
    before = asyncio.run(runner.execute(run_id, None))
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["es-MX"]["copy"]["message"] = "New saved wording"
    change = Brief.model_validate(value)
    new_plan = make_plan(change, state.storage)
    pending = state.create(state.plan(run_id), allowance=0)
    result = runner.revise(run_id, change, before["revision_id"], new_plan.digest)
    assert state.current_output(value["campaign_id"]) == (run_id, result["revision_id"])
    assert state.run(pending)["state"] == "planned"
    with pytest.raises(StateError, match="changed"):
        runner.revise(run_id, change, before["revision_id"], new_plan.digest)
    assert state.run(run_id)["current_revision"] == result["revision_id"]


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

        partial = asyncio.run(app.state.runner.execute(run["id"], Unknown()))
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        shown = client.get(url).json()
        assert shown["manifest"]["revision_id"] == partial["revision_id"]
        assert shown["manifest"]["metrics"]["completed_images"] == 6
        assert shown["can_edit"] and shown["can_describe"]
        assert shown["editable_products"] == {"product-1": False, "product-2": True}
        assert not shown["unplanned_draft"]
        assert shown["local_recovery"] == {"output_ids": [], "saved_result": False}
        retry = shown["retry_plan"]
        assert retry["run_id"] == run["id"] and retry["allowance"] == 2
        assert retry["previous_attempts"] == 1
        assert retry["generation_products"] == [brief.products[0].id]
        assert ws.state.run(run["id"])["allowance"] == 1
        assert len(ws.state.attempts(run["id"])) == 1
        assert shown["manifest"]["usage_cost_estimate"]["estimated_usd"] is None
        client.headers["X-Campaign-Token"] = app.state.csrf
        package_url = (
            f"/api/campaigns/{brief.campaign_id}/runs/{run['id']}"
            f"/revisions/{partial['revision_id']}/zip"
        )
        assert (
            client.post(
                package_url,
                json={"snapshot_id": shown["metadata"]["snapshot_id"], "allow_partial": False},
            ).status_code
            == 409
        )
        packaged = client.post(
            package_url,
            json={"snapshot_id": shown["metadata"]["snapshot_id"], "allow_partial": True},
        )
        assert packaged.status_code == 200, packaged.text
        assert packaged.json()["included"] == 6 and packaged.json()["omitted"] == 6
        assert client.get(packaged.json()["url"]).status_code == 200
        recovered = app.state.runner.recover_local(run["id"])
        assert not recovered["complete"] and recovered["metrics"]["completed_images"] == 6
        assert len(ws.state.attempts(run["id"])) == 1
        reopened = client.get(url).json()
        assert reopened["retry_plan"] == retry
        assert reopened["manifest"]["revision_id"] == recovered["revision_id"]
        assert ws.state.revision(partial["revision_id"])
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


def test_damaged_revision_requires_recovery_before_complete_package(tmp_path):
    from campaign_studio.planning import make_plan

    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Damage recovery is local"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        run_id = ws.state.create(make_plan(brief, ws.storage), allowance=0)
        ws.state.confirm(run_id, ws.state.plan(run_id).digest)
        before = asyncio.run(app.state.runner.execute(run_id))
        record = ws.state.revision(before["revision_id"])
        prefix = record["manifest_ref"].rsplit("/", 1)[0]
        ws.storage.path(f"{prefix}/{before['outputs'][0]['file']}").unlink()
        shown = client.get(f"/api/motif/campaigns/{brief.campaign_id}").json()
        assert shown["manifest"]["complete"]  # Immutable production result remains complete.
        assert len(shown["file_integrity"]["damaged_outputs"]) == 1
        assert shown["manifest"]["metrics"]["completed_images"] == 11
        assert shown["local_recovery"]["output_ids"] == [before["outputs"][0]["id"]]
        recovered = app.state.runner.recover_local(run_id)
        assert recovered["revision_id"] != before["revision_id"]
        assert recovered["complete"] and recovered["metrics"]["completed_images"] == 12
        assert ws.state.attempts(run_id) == []
        assert ws.state.revision(before["revision_id"])
        after = client.get(f"/api/motif/campaigns/{brief.campaign_id}").json()
        assert after["file_integrity"]["damaged_outputs"] == [] and after["can_edit"]
        response = client.post(
            f"/api/campaigns/{brief.campaign_id}/runs/{run_id}/revisions/{recovered['revision_id']}/zip",
            json={"snapshot_id": after["metadata"]["snapshot_id"], "allow_partial": False},
        )
        assert response.status_code == 200 and response.json()["included"] == 12


@pytest.mark.parametrize("damage_asset", [False, True])
def test_generated_rule_recheck_retains_request_and_checks_assets(
    tmp_path, monkeypatch, damage_asset
):
    from campaign_studio.workspace import ActionError

    state, runner, run_id, response = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
    )
    first = asyncio.run(runner.execute(run_id, provider))
    old = state.plan(run_id)
    attempts = state.attempts(run_id)
    ws, intake = Workspace(state), Intake(state.storage)
    cid = old.brief.campaign_id
    current = intake.current_document(cid, ws)
    value = old.brief.model_dump(mode="json")
    value["markets"]["en-US"]["phrase_rules"] = [
        {"id": "unused-rule", "phrase": "A phrase absent from the campaign"}
    ]
    monkeypatch.setattr(
        "campaign_studio.generation.CONCEPT_FRAMING", "Updated composition instructions."
    )
    monkeypatch.setattr(
        "campaign_studio.rendering.compose",
        lambda *a: pytest.fail("Rule recheck must not compose new pixels"),
    )
    document = BriefDraft(
        value=value,
        version=current["version"] if current else None,
        base_revision=first["revision_id"],
    )
    if damage_asset:
        logo = state.storage.path(old.brief.brand.logo_path)
        logo.write_bytes(logo.read_bytes() + b"changed bytes")
        with pytest.raises(ActionError, match="source changed"):
            intake.recheck_rules(cid, document, run_id, ws)
        assert state.effective_revision(run_id) == first["revision_id"]
    else:
        result = intake.recheck_rules(cid, document, run_id, ws)
        assert result["complete"]
        assert {o["id"]: o["sha256"] for o in result["outputs"]} == {
            o["id"]: o["sha256"] for o in first["outputs"]
        }
        assert state.plan(run_id).generation_requests == old.generation_requests
    assert state.attempts(run_id) == attempts


def test_rule_recheck_publishes_findings_without_pixels_and_preserves_history(
    tmp_path, monkeypatch
):
    from campaign_studio.brief import Brief
    from campaign_studio.planning import make_plan
    from campaign_studio.review import CreativeUpdate

    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Rule recheck constructed a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        plan = make_plan(brief, ws.storage)
        rid = ws.state.create(plan, 0)
        ws.state.confirm(rid, ws.state.plan(rid).digest)
        first = asyncio.run(app.state.runner.execute(rid))
        for item in first["outputs"]:
            ws.state.update_creative(
                first["revision_id"],
                item["id"],
                CreativeUpdate(expected_version=0, description="Supplied description."),
            )
        before_notes = ws.state.metadata_snapshot(first["revision_id"])
        monkeypatch.setattr(
            "campaign_studio.rendering.compose",
            lambda *a: pytest.fail("Rule recheck composed pixels"),
        )
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        current = client.get(url).json()
        body = {
            "value": current["value"],
            "version": current["version"],
            "base_revision": first["revision_id"],
            "run_id": rid,
        }
        body["value"]["markets"]["en-US"]["phrase_rules"].append(
            {
                "id": "",
                "phrase": "everyday",
            }
        )
        assert client.post(url + "/recheck", json=body).status_code == 403
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(url + "/recheck", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        after = result["manifest"]
        assert not after["complete"] and after["revision_id"] != first["revision_id"]
        assert sum(bool(i["file"]) for i in after["outputs"]) == 6
        assert {i["id"]: i["sha256"] for i in after["outputs"] if i["file"]} == {
            i["id"]: i["sha256"] for i in first["outputs"] if i["market"] == "es-MX"
        }
        matches = [
            f
            for f in after["findings"]
            if (f.get("context") or {}).get("phrase")
            and f["context"]["phrase"].get("configured_phrase") == "everyday"
        ]
        assert len(matches) == 6 and all(f["severity"] == "blocking" for f in matches)
        assert all(
            v["description"] == "Supplied description."
            for k, v in result["metadata"]["creatives"].items()
        )
        assert ws.state.metadata_snapshot(first["revision_id"]) == before_notes
        assert len(ws.runs(brief.campaign_id)) == 1 and ws.state.attempts(rid) == []
        assert not result["unplanned_draft"]
        assert client.post(url + "/recheck", json=body).status_code == 409

        fresh = {
            "value": result["value"],
            "version": result["version"],
            "base_revision": after["revision_id"],
            "run_id": rid,
        }
        fresh["value"]["markets"]["en-US"]["phrase_rules"][-1]["phrase"] = "absent wording"
        original_draft = Intake(ws.storage).read(brief.campaign_id)
        for section, key in (("copy", "message"), ("brand", "name"), ("source", "path")):
            changed = copy.deepcopy(fresh)
            target = (
                changed["value"]["markets"]["en-US"]["copy"]
                if section == "copy"
                else changed["value"]["brand"]
                if section == "brand"
                else changed["value"]["products"][0]["source"]
            )
            target[key] = "Unrelated edit"
            rejected = client.post(url + "/recheck", json=changed)
            assert rejected.status_code == 409 and "only rule changes" in rejected.json()["message"]
            assert Intake(ws.storage).read(brief.campaign_id) == original_draft
        stale = copy.deepcopy(fresh)
        stale["version"] = "old-version"
        assert client.post(url + "/recheck", json=stale).status_code == 409
        # A newer run supersedes even an unchanged older run's current revision.
        ws.state.create(make_plan(Brief.model_validate(fresh["value"]), ws.storage), 0)
        assert client.post(url + "/recheck", json=fresh).status_code == 409
        assert ws.state.revision_count(rid) == 2


def test_rule_recheck_invalid_message_and_damaged_image_do_not_mutate(tmp_path, monkeypatch):
    from campaign_studio.planning import make_plan

    app = create_app(Settings(data_dir=tmp_path / "data"), key_available=lambda: False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        value = brief.model_dump(mode="json")
        from campaign_studio.brief import Brief

        rid = ws.state.create(make_plan(Brief.model_validate(value), ws.storage), 0)
        ws.state.confirm(rid, ws.state.plan(rid).digest)
        first = asyncio.run(app.state.runner.execute(rid))
        monkeypatch.setattr(
            "campaign_studio.rendering.compose", lambda *a: pytest.fail("Unexpected composition")
        )
        client.headers["X-Campaign-Token"] = app.state.csrf
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        current = client.get(url).json()
        body = {
            "value": current["value"],
            "version": current["version"],
            "base_revision": first["revision_id"],
            "run_id": rid,
        }
        original_message = body["value"]["markets"]["en-US"]["copy"]["message"]
        body["value"]["markets"]["en-US"]["copy"]["message"] = ""
        rejected = client.post(url + "/recheck", json=body)
        assert rejected.status_code == 422
        assert any(f["market"] == "en-US" for f in rejected.json()["field_errors"])
        body["value"]["markets"]["en-US"]["copy"]["message"] = original_message
        body["value"]["markets"]["en-US"]["phrase_rules"] = None
        prefix = ws.state.revision(first["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
        ws.storage.path(f"{prefix}/{first['outputs'][0]['file']}").unlink()
        rejected = client.post(url + "/recheck", json=body)
        assert (
            rejected.status_code == 409 and "Recover the saved images" in rejected.json()["message"]
        )
        assert ws.state.revision_count(rid) == 1 and ws.state.attempts(rid) == []
        assert Intake(ws.storage).read(brief.campaign_id) is None


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_recovery_reports_unavailable_input_without_retry_loop(tmp_path, damage):
    from test_web_workflow import wait_for_work

    from campaign_studio.planning import make_plan

    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Recovery must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, brief = write_sample(ws.storage)
        rid = ws.state.create(make_plan(brief, ws.storage), 0)
        ws.state.confirm(rid, ws.state.plan(rid).digest)
        first = asyncio.run(app.state.runner.execute(rid))
        source = ws.storage.path(
            ws.state.plan(rid).assets[f"products/{brief.products[0].id}"].reference
        )
        if damage == "missing":
            source.unlink()
        else:
            source.write_bytes(b"changed input")
        record = ws.state.revision(first["revision_id"])
        output = ws.storage.path(record["manifest_ref"]).parent / first["outputs"][0]["file"]
        output.unlink()
        client.headers["X-Campaign-Token"] = app.state.csrf
        response = client.post(f"/api/campaigns/{brief.campaign_id}/recover", json={"run_id": rid})
        assert response.status_code == 202
        status = wait_for_work(client, brief.campaign_id)
        assert status["notice_code"] == "source_unavailable"
        assert "Open Production, attach the intended source again" in status["notice"]
        assert "No image request was sent" in status["notice"]
        assert str(source) not in status["notice"]
        assert ws.state.attempts(rid) == [] and ws.state.revision_count(rid) == 1
        assert not output.exists()
