"""Intentional rule decisions remain current without substituting stale pixels."""

import asyncio
import copy
import hashlib
from contextlib import contextmanager

import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.imaging import ImageProblem
from campaign_studio.intake import Intake
from campaign_studio.planning import make_plan
from campaign_studio.review import CreativeUpdate
from campaign_studio.sample import write_sample
from campaign_studio.state import State
from campaign_studio.storage import StorageError


@contextmanager
def prepare(tmp_path, initial_partial=False):
    app = create_app(
        Settings(data_dir=tmp_path / "data"),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Rule decisions must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        _, brief = write_sample(ws.storage, 2, True)
        value = brief.model_dump(mode="json")
        value["products"][0]["copy_overrides"] = {"en-US": {"message": "LOCAL CONTROL"}}
        value["markets"]["en-US"]["phrase_rules"] = []
        rid = ws.state.create(make_plan(Brief.model_validate(value), ws.storage), 0)
        ws.state.confirm(rid, ws.state.plan(rid).digest)
        original_write = ws.storage.write

        def fail_initial_product(reference, payload):
            if "/images/product-1/" in reference:
                raise OSError("Controlled initial product output failure")
            return original_write(reference, payload)

        if initial_partial:
            ws.storage.write = fail_initial_product
        try:
            first = asyncio.run(app.state.runner.execute(rid))
        finally:
            ws.storage.write = original_write
        client.headers["X-Campaign-Token"] = app.state.csrf
        url = f"/api/motif/campaigns/{brief.campaign_id}"
        project = client.get(url + "/authored").json()
        saved = client.post(
            url + "/authored", json={key: project[key] for key in ("version", "base", "authored")}
        )
        assert saved.status_code == 200, saved.text
        yield app, client, rid, first, f"/api/motif/campaigns/{brief.campaign_id}"


def recheck(client, url, phrase, enabled=True):
    current = client.get(url).json()
    body = {
        "value": copy.deepcopy(current["value"]),
        "version": current["version"],
        "base_revision": current["base_revision"],
        "run_id": current["run"]["id"],
    }
    body["value"]["markets"]["en-US"]["phrase_rules"] = (
        [
            {
                "id": "local-control",
                "phrase": phrase,
            }
        ]
        if enabled
        else []
    )
    return client.post(url + "/recheck", json=body), body


def package(client, cid, rid, shown):
    return client.post(
        f"/api/campaigns/{cid}/runs/{rid}/revisions/{shown['manifest']['revision_id']}/zip",
        json={"snapshot_id": shown["metadata"]["snapshot_id"], "allow_partial": True},
    )


@pytest.mark.parametrize("mixed_generation", [False, True])
def test_reused_generation_rules_after_applied_copy_and_authored_reopen(
    tmp_path, monkeypatch, mixed_generation
):
    import httpx
    from test_provider_runner import generated_case

    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.intake import BriefDraft
    from campaign_studio.provider import OpenAIImages
    from campaign_studio.workspace import ActionError, TextEdit, Workspace

    state, runner, original_run, body = generated_case(tmp_path, 2 if mixed_generation else 1)
    calls = []

    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body)

    provider = OpenAIImages("synthetic-test-credential", transport=httpx.MockTransport(transport))
    first = asyncio.run(runner.execute(original_run, provider))
    ws, intake = Workspace(state), Intake(state.storage)
    projects = AuthoredProjects(ws)
    cid = first["campaign_id"]

    def save(change=None):
        record = projects.read(cid)
        if change:
            change(record["authored"])
        projects.save(cid, AuthoredSave(**{k: record[k] for k in ("version", "base", "authored")}))

    def initial(authored):
        authored["brief"]["markets"]["en-US"]["copy"]["message"] = "New shared wording."
        authored["brief"]["markets"]["es-MX"]["phrase_rules"] = []
        authored["form_drafts"] = {"fields": {"brief/copy/en": {"copy-headline": "  unfinished  "}}}
        if mixed_generation:
            authored["brief"]["products"][1]["source"]["description"] = (
                "A different fictional concept."
            )

    save(initial)
    plan = intake.prepare(cid, projects.read(cid)["version"], ws)
    run_id = ws.run_for(cid)["id"]
    assert plan.brief.replay
    assert plan.generation_products == ([plan.brief.products[1].id] if mixed_generation else [])
    assert plan.brief.replay.partial is mixed_generation
    state.confirm(run_id, plan.digest)
    result = asyncio.run(runner.execute(run_id, provider))
    for field, scope, text in (
        ("message", "all_products", "Applied shared message."),
        ("cta", "product", "Applied product CTA."),
    ):
        command = TextEdit(
            ratio="1:1",
            run_id=run_id,
            revision_id=result["revision_id"],
            market="en-US",
            product_id=plan.brief.products[0].id,
            field_scopes={field: scope},
            values={field: text},
        )
        brief, preview, _ = ws.text(cid, command)
        result = runner.revise(
            run_id, brief, result["revision_id"], preview.digest, text_edit=command
        )
        save()
    projects = AuthoredProjects(Workspace(State(state.storage)))
    reopened = projects.read(cid)
    assert reopened["authored"]["brief"]["replay"] is None
    accepted = state.plan(run_id).brief.model_dump(mode="json")
    normalized = Brief.model_validate(reopened["authored"]["brief"]).model_dump(mode="json")
    normalized["replay"] = accepted["replay"]
    assert normalized == accepted
    sources = {
        a.reference: state.storage.path(a.reference).read_bytes()
        for a in state.plan(run_id).assets.values()
    }
    metrics = state.campaign_metrics(cid)
    attempts = state.attempts(original_run)
    current_attempts = state.attempts(run_id)
    image_hashes = {o["id"]: o["sha256"] for o in result["outputs"]}

    rule = {"id": "spanish-control", "phrase": accepted["markets"]["es-MX"]["copy"]["message"]}

    def request(enabled=True):
        record = projects.read(cid)
        value = copy.deepcopy(record["authored"]["brief"])
        value["markets"]["es-MX"]["phrase_rules"] = [rule] if enabled else []
        return BriefDraft(
            value=value, version=record["version"], base_revision=record["base"]["revision_id"]
        )

    # Neither source intent nor an explicit replay mapping becomes rule-only.
    save(lambda a: a["brief"]["markets"]["es-MX"].update(phrase_rules=[rule]))
    original = request()
    for change in (
        lambda v: v.update(audience="A genuinely different audience"),
        lambda v: v["brand"].update(text_color="#123456"),
        lambda v: v["markets"]["en-US"]["copy"].update(cta="Different copy"),
        lambda v: v["products"][0].update(name="Changed product"),
        lambda v: v.update(replay={**accepted["replay"], "history": "Explicit changed mapping"}),
        lambda v: v.update(replay={}),
        lambda v: v.update(replay=False),
    ):
        refused = original.model_copy(deep=True)
        change(refused.value)
        with pytest.raises(ActionError) as error:
            intake.recheck_rules(cid, refused, run_id, ws)
        assert error.value.code in {"rule_changes_only", "brief_fields"}
        assert state.effective_revision(run_id) == result["revision_id"]

    stale = original.model_copy(update={"version": "stale-authored-version"})
    with pytest.raises(ActionError, match="another tab"):
        intake.recheck_rules(cid, stale, run_id, ws)
    master = next(iter(accepted["replay"]["sources"].values()))
    changed = original.model_copy(deep=True)
    changed.value["replay"] = copy.deepcopy(accepted["replay"])
    next(iter(changed.value["replay"]["sources"].values()))["sha256"] = "0" * 64
    with pytest.raises(ActionError, match="only rule changes"):
        intake.recheck_rules(cid, changed, run_id, ws)
    master_path = state.storage.path(master["path"])
    master_bytes = master_path.read_bytes()
    master_path.write_bytes(master_bytes + b"changed source")
    try:
        with pytest.raises(ActionError, match="source changed"):
            intake.recheck_rules(cid, original, run_id, ws)
        assert state.effective_revision(run_id) == result["revision_id"]
    finally:
        master_path.write_bytes(master_bytes)

    def no_composition(*args, **kwargs):
        pytest.fail("Pure rule changes must reuse the exact accepted pixels")

    monkeypatch.setattr("campaign_studio.rendering.compose", no_composition)
    blocked = intake.recheck_rules(cid, original, run_id, ws)
    assert sum(bool(o.get("file")) for o in blocked["outputs"]) == 6
    with pytest.raises(ActionError, match="images changed"):
        intake.recheck_rules(cid, original, run_id, ws)
    save()
    state.require_authored_plan(run_id)
    save(lambda a: a["brief"]["markets"]["es-MX"].update(phrase_rules=[]))
    restored = intake.recheck_rules(cid, request(False), run_id, ws)
    assert restored["complete"]
    assert {o["id"]: o["sha256"] for o in restored["outputs"]} == image_hashes
    assert projects.read(cid)["authored"]["form_drafts"] == reopened["authored"]["form_drafts"]
    assert projects.read(cid)["authored"]["brief"]["replay"] is None
    assert sources == {p: state.storage.path(p).read_bytes() for p in sources}
    assert state.attempts(original_run) == attempts and state.attempts(run_id) == current_attempts
    assert state.campaign_metrics(cid) == metrics and len(calls) == (3 if mixed_generation else 1)


def test_rule_recheck_does_not_hide_removed_explicit_replay(tmp_path):
    from campaign_studio.authored import AuthoredProjects, AuthoredSave
    from campaign_studio.intake import BriefDraft
    from campaign_studio.runner import Runner
    from campaign_studio.storage import Storage
    from campaign_studio.workspace import ActionError, Workspace

    ws = Workspace(State(Storage(tmp_path / "runtime")))
    cid = seed_campaign(ws, replay=True)
    run_id = ws.run_for(cid)["id"]
    ws.state.confirm(run_id, ws.state.plan(run_id).digest)
    asyncio.run(Runner(ws.state).execute(run_id))
    projects = AuthoredProjects(ws)
    record = projects.read(cid)
    assert record["authored"]["brief"]["replay"]
    record["authored"]["brief"]["replay"] = None
    projects.save(cid, AuthoredSave(**{k: record[k] for k in ("version", "base", "authored")}))
    record = projects.read(cid)
    value = record["authored"]["brief"]
    value["markets"]["en-US"]["phrase_rules"] = [
        {
            "id": "new-rule",
            "phrase": "hold",
        }
    ]
    with pytest.raises(ActionError) as error:
        Intake(ws.storage).recheck_rules(
            cid,
            BriefDraft(
                value=value, version=record["version"], base_revision=record["base"]["revision_id"]
            ),
            run_id,
            ws,
        )
    assert error.value.code == "rule_changes_only"
    assert projects.read(cid)["authored"]["brief"]["replay"] is None
    assert ws.state.attempts(run_id) == []


@pytest.mark.parametrize("phrase,available", [(None, 6), ("LOCAL CONTROL", 3), ("Explore", 0)])
def test_unstarted_plan_keeps_current_result_and_package_admission(
    tmp_path, monkeypatch, phrase, available
):
    from campaign_studio.motif import workbench_outputs

    with prepare(tmp_path) as (app, client, rid, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        original = client.get(url).json()
        registered = package(client, cid, rid, original).json()
        zip_bytes = client.get(registered["url"]).content
        if phrase:
            response, _ = recheck(client, url, phrase, True)
            assert response.status_code == 200, response.text
        accepted = client.get(url).json()
        revision = accepted["manifest"]["revision_id"]
        project = client.get(url + "/authored").json()
        project["authored"]["brief"]["name"] = "Next plan; current images remain"
        project["authored"]["brief"]["markets"]["en-US"]["phrase_rules"] = []
        saved = client.post(
            url + "/authored", json={key: project[key] for key in ("version", "base", "authored")}
        )
        assert saved.status_code == 200, saved.text
        planned = client.post(url + "/plan", json={"version": saved.json()["version"]})
        assert planned.status_code == 200, planned.text
        new_run = planned.json()["run"]["id"]
        assert new_run != rid
        shown = client.get(url).json()
        assert shown["manifest"]["revision_id"] == revision
        assert shown["output_run_id"] == rid
        assert "historical" not in shown
        assert shown["can_edit"] and shown["can_describe"]
        assert all(shown["logo_editable_products"].values())
        assert sum(bool(item["url"]) for item in shown["manifest"]["outputs"]) == available
        assert State(ws.storage).current_output(cid) == (rid, revision)
        home, images, _ = workbench_outputs(ws, cid)
        assert home["revision_id"] == revision and len(images) == available
        assert len(shown["rule_withheld_outputs"]) == 6 - available
        ws.state.require_current_revision(rid, revision)

        # Recovery is not an alternate start for an unconfirmed or merely
        # confirmed new plan, even when all its imagery is already local.
        def snapshot():
            with ws.storage.connect() as db:
                database = list(db.iterdump())
            files = {
                str(path.relative_to(ws.storage.root)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in ws.storage.root.rglob("*")
                if path.is_file() and path.name != "state.sqlite3"
            }
            return database, files

        for confirmed in (False, True):
            if confirmed:
                ws.state.confirm(new_run, ws.state.plan(new_run).digest)
            before = snapshot()
            refused = client.post(f"/api/campaigns/{cid}/recover", json={"run_id": new_run})
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "recovery_not_started"
            assert "not started" in refused.json()["message"]
            assert snapshot() == before
            assert ws.state.current_output(cid) == (rid, revision)
            assert zip_bytes in [p.read_bytes() for p in ws.storage.root.rglob("*.zip")]
            assert client.get(url).json()["manifest"]["revision_id"] == revision

        admission = ws.state.require_current_output
        checked = []

        def under_publication_lock(target):
            with (
                pytest.raises(StorageError, match="already active"),
                ws.storage.exclusive_run(),
            ):
                pytest.fail("Package admission must run inside the publication lock")
            checked.append(target.revision_id)
            return admission(target)

        monkeypatch.setattr(ws.state, "require_current_output", under_publication_lock)
        exported = package(client, cid, rid, shown)
        folder = client.post(
            f"/api/campaigns/{cid}/export",
            json={"run_id": rid, "revision_id": revision, "allow_partial": True},
        )
        assert exported.status_code == (200 if not phrase else 409), exported.text
        assert folder.status_code == (200 if not phrase else 409), folder.text
        assert checked.count(revision) == (2 if available else 1)
        if not phrase:
            assert exported.json()["included"] == folder.json()["included"] == available
        assert zip_bytes in [p.read_bytes() for p in ws.storage.root.rglob("*.zip")]

        ws.state.confirm(new_run, ws.state.plan(new_run).digest)
        completed = asyncio.run(app.state.runner.execute(new_run))
        assert completed["complete"]
        current = client.get(url).json()
        assert current["manifest"]["revision_id"] == completed["revision_id"]
        assert ws.state.current_output(cid) == (new_run, completed["revision_id"])
        assert package(client, cid, rid, shown).status_code == 409
        assert (
            client.post(
                f"/api/campaigns/{cid}/export",
                json={"run_id": rid, "revision_id": revision, "allow_partial": True},
            ).status_code
            == 409
        )
        assert package(client, cid, new_run, current).status_code == 200
        assert zip_bytes in [p.read_bytes() for p in ws.storage.root.rglob("*.zip")]
        assert ws.state.attempts(rid) == ws.state.attempts(new_run) == []
        assert ws.state.campaign_metrics(cid)["usage_cost_estimate"]["estimated_usd"] == 0


@pytest.mark.parametrize("kind", ["wording", "framing", "text", "logo"])
@pytest.mark.parametrize("initial_partial", [False, True])
def test_apply_refuses_unaccepted_candidate_and_preserves_current_partial(
    tmp_path, monkeypatch, kind, initial_partial
):
    from test_logo_editing import replacement

    with prepare(tmp_path, initial_partial) as (app, client, rid, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        if initial_partial:
            assert ws.state.run(rid)["current_revision"] is None
            assert not first["complete"]
        else:
            blocked, _ = recheck(client, url, "LOCAL CONTROL", True)
            assert blocked.status_code == 200, blocked.text
        before = client.get(url).json()
        revision = before["manifest"]["revision_id"]
        body = {
            "run_id": rid,
            "revision_id": revision,
            "product_id": "product-2",
            "market": "en-US",
        }
        route = f"/api/campaigns/{cid}"
        if kind == "wording":
            route += "/text"
            body["ratio"] = "1:1"
            body.update(values={"cta": "Keep this pending CTA"}, field_scopes={"cta": "product"})
        else:
            body["ratio"] = "1:1"
            route += "/" + kind
            if kind == "framing":
                body["framing"] = {"focal_x": 0.42, "focal_y": 0.45, "zoom": 1.15}
            elif kind == "text":
                body["placements"] = {
                    "cta": {"left": 0.1, "top": 0.4, "width": 0.7, "font_size": 36}
                }
            else:
                body.update(
                    replacement=replacement(ws.state, cid, (400, 160)),
                    draft_version=before["version"],
                )
        preview = client.post(route + "/preview", json=body)
        assert preview.status_code == 200 and not preview.json()["blocked"], preview.text
        body["preview_digest"] = preview.json()["digest"]
        authored = client.get(url + "/authored").json()
        metrics = ws.state.campaign_metrics(cid)
        pngs = {
            item["url"]: client.get(item["url"]).content
            for item in before["manifest"]["outputs"]
            if item.get("url")
        }
        original_write = ws.storage.write

        def fail_selected(reference, payload):
            if "/Revisions/" in reference and reference.endswith("/images/product-2/1x1/en-US.png"):
                raise OSError("Controlled output-write failure")
            return original_write(reference, payload)

        with monkeypatch.context() as patch:
            patch.setattr(ws.storage, "write", fail_selected)
            failed = client.post(route + "/apply", json=body)
        assert failed.status_code == 409, failed.text
        assert failed.json()["code"] == "apply_not_accepted"
        assert failed.json()["message"] == "Could not apply. Your edits are still here."
        assert ws.state.effective_revision(rid) == revision
        assert client.get(url + "/authored").json() == authored
        assert ws.state.campaign_metrics(cid) == metrics
        assert all(client.get(path).content == content for path, content in pngs.items())
        current = client.get(url).json()
        assert current["manifest"]["revision_id"] == revision
        assert package(client, cid, rid, current).status_code == (200 if initial_partial else 409)
        retry = client.post(route + "/apply", json=body)
        assert retry.status_code == 200, retry.text
        assert ws.state.effective_revision(rid) != revision
        assert ws.state.attempts(rid) == []


@pytest.mark.parametrize("phrase,available", [("LOCAL CONTROL", 3), ("Explore", 0)])
def test_intentional_rule_decision_is_current_and_reversible_without_composition(
    tmp_path, monkeypatch, phrase, available
):
    with prepare(tmp_path) as (app, client, rid, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        client.headers["X-Campaign-Token"] = app.state.csrf
        original = client.get(url).json()
        old_package = package(client, cid, rid, original).json()
        old_zip = client.get(old_package["url"]).content
        metrics = ws.state.campaign_metrics(cid)
        monkeypatch.setattr(
            "campaign_studio.rendering.compose",
            lambda *a: pytest.fail("Rule recheck must reuse verified pixels"),
        )
        response, stale = recheck(client, url, phrase, True)
        assert response.status_code == 200, response.text
        blocked = response.json()["manifest"]
        assert ws.state.effective_revision(rid) == blocked["revision_id"]
        shown = client.get(url).json()
        assert shown["manifest"]["revision_id"] == blocked["revision_id"]
        assert not shown["unplanned_draft"]
        assert not shown["unplanned_draft"]
        assert sum(bool(item["url"]) for item in shown["manifest"]["outputs"]) == available
        assert State(ws.storage).effective_revision(rid) == blocked["revision_id"]
        assert State(ws.storage).plan(rid).brief.markets["en-US"].phrase_rules[0].phrase == phrase
        from campaign_studio.motif import workbench_outputs

        home_manifest, home_images, damaged = workbench_outputs(ws, cid)
        assert home_manifest["revision_id"] == blocked["revision_id"]
        assert len(home_images) == available and damaged == 0
        for page in ("studio", "handoff", "production"):
            assert client.get(f"/campaigns/{cid}/{page}").status_code == 200
        exported = package(client, cid, rid, shown)
        assert exported.status_code == (200 if not phrase else 409), exported.text
        assert package(client, cid, rid, original).status_code == 409
        stale_folder = client.post(
            f"/api/campaigns/{cid}/export",
            json={
                "run_id": rid,
                "revision_id": first["revision_id"],
                "allow_partial": True,
            },
        )
        assert stale_folder.status_code == 409
        assert client.post(url + "/recheck", json=stale).status_code == 409
        import campaign_studio.rendering as rendering

        inspected = []
        original_inspect = rendering.inspect_png

        def inspect(path, output, *args):
            inspected.append(output.id)
            return original_inspect(path, output, *args)

        monkeypatch.setattr(rendering, "inspect_png", inspect)
        response, _ = recheck(client, url, phrase, False)
        assert response.status_code == 200, response.text
        restored = response.json()["manifest"]
        assert set(inspected) == {o["id"] for o in restored["outputs"]}
        assert restored["complete"]
        assert {o["id"]: o["sha256"] for o in restored["outputs"]} == {
            o["id"]: o["sha256"] for o in first["outputs"]
        }
        assert client.get(url).json()["manifest"]["revision_id"] == restored["revision_id"]
        assert client.get(old_package["url"]).content == old_zip
        assert ws.state.campaign_metrics(cid) == metrics
        assert ws.state.attempts(rid) == []


def test_rule_withholding_keeps_current_descriptions_and_never_resurrects_cleared_text(
    tmp_path, monkeypatch
):
    from campaign_studio.workspace import TextEdit

    with prepare(tmp_path) as (app, client, rid, first, url):
        ws = app.state.workspace
        cid = first["campaign_id"]
        for output in first["outputs"]:
            ws.state.update_creative(
                first["revision_id"],
                output["id"],
                CreativeUpdate(
                    expected_version=0, description=f"Saved description of {output['id']}."
                ),
            )
        cleared = first["outputs"][0]["id"]
        ws.state.update_creative(
            first["revision_id"], cleared, CreativeUpdate(expected_version=1, description=None)
        )
        original = client.get(url).json()
        descriptions = {k: v["description"] for k, v in original["metadata"]["creatives"].items()}
        registered = package(client, cid, rid, original).json()
        zip_bytes = client.get(registered["url"]).content
        metrics = ws.state.campaign_metrics(cid)

        def no_composition(*args, **kwargs):
            pytest.fail("Rule-only withholding/restoration must not compose")

        with monkeypatch.context() as local:
            local.setattr("campaign_studio.rendering.compose", no_composition)
            for phrase, enabled, count in (
                ("LOCAL CONTROL", True, 3),
                ("Explore", True, 0),
                ("Explore", False, 6),
            ):
                response, _ = recheck(client, url, phrase, enabled)
                assert response.status_code == 200, response.text
                shown = client.get(url).json()
                assert sum(bool(o["file"]) for o in shown["manifest"]["outputs"]) == count
                current = State(ws.storage).metadata_snapshot(shown["manifest"]["revision_id"])
                assert {
                    k: v["description"] for k, v in current["creatives"].items()
                } == descriptions
                assert all(v["reset_reason"] is None for v in current["creatives"].values())
                assert package(client, cid, rid, shown).status_code == (200 if not enabled else 409)
                assert zip_bytes in [p.read_bytes() for p in ws.storage.root.rglob("*.zip")]
            assert {o["id"]: o["sha256"] for o in shown["manifest"]["outputs"]} == {
                o["id"]: o["sha256"] for o in first["outputs"]
            }

        # A genuinely changed creative still resets, and a later rule-only
        # cycle must not search an older description to fill that deliberate gap.
        product = ws.state.plan(rid).brief.products[1].id
        edit = TextEdit(
            ratio="1:1",
            run_id=rid,
            revision_id=shown["manifest"]["revision_id"],
            market="en-US",
            product_id=product,
            field_scopes={"message": "product"},
            values={"message": "A newly changed creative."},
        )
        brief, preview, _ = ws.text(cid, edit)
        edited = app.state.runner.revise(rid, brief, edit.revision_id, preview.digest)
        changed = ws.state.metadata_snapshot(edited["revision_id"])["creatives"]
        assert all(
            v["description"] is None and v["reset_reason"] == "creative_changed"
            for k, v in changed.items()
            if k.startswith(product + "/")
        )
        with monkeypatch.context() as local:
            local.setattr("campaign_studio.rendering.compose", no_composition)
            for enabled in (True, False):
                response, _ = recheck(client, url, "newly changed", enabled)
                assert response.status_code == 200, response.text
                current = response.json()["metadata"]["creatives"]
                assert all(
                    v["description"] is None
                    for k, v in current.items()
                    if k.startswith(product + "/") or k == cleared
                )
        assert zip_bytes in [p.read_bytes() for p in ws.storage.root.rglob("*.zip")]
        assert ws.state.campaign_metrics(cid) == metrics and ws.state.attempts(rid) == []


def test_partial_rule_decision_keeps_accepted_edit_through_failed_correction_and_recheck(
    tmp_path, monkeypatch
):
    with prepare(tmp_path) as (app, client, rid, first, url):
        ws = app.state.workspace
        client.headers["X-Campaign-Token"] = app.state.csrf
        response, _ = recheck(client, url, "LOCAL CONTROL", True)
        assert response.status_code == 200, response.text
        blocked = response.json()["manifest"]
        assert ws.state.effective_revision(rid) == blocked["revision_id"]
        value = ws.state.plan(rid).brief.model_dump()
        value["products"][1]["copy_overrides"] = {"en-US": {"message": "A deliberate new image"}}
        revised = make_plan(Brief.model_validate(value), ws.storage)
        edited = app.state.runner.revise(rid, revised.brief, blocked["revision_id"], revised.digest)
        assert not edited["complete"] and sum(bool(o["file"]) for o in edited["outputs"]) == 3
        assert ws.state.effective_revision(rid) == edited["revision_id"]
        visible = next(o for o in edited["outputs"] if o["file"])
        ws.state.update_creative(
            edited["revision_id"],
            visible["id"],
            CreativeUpdate(expected_version=0, description="Current edited image."),
        )
        assert package(client, first["campaign_id"], rid, client.get(url).json()).status_code == 409
        actual_compose = __import__("campaign_studio.rendering", fromlist=["compose"]).compose

        def fail_one(*args):
            if args[3] == "1:1":
                raise ImageProblem("Controlled local failure")
            return actual_compose(*args)

        value["products"][1]["copy_overrides"]["en-US"]["message"] = "Unaccepted correction"
        failed_plan = make_plan(Brief.model_validate(value), ws.storage)
        with monkeypatch.context() as failed:
            failed.setattr("campaign_studio.rendering.compose", fail_one)
            result = app.state.runner.revise(
                rid, failed_plan.brief, edited["revision_id"], failed_plan.digest
            )
        assert sum(bool(o["file"]) for o in result["outputs"]) == 2
        assert ws.state.effective_revision(rid) == edited["revision_id"]
        assert not ws.state.revision(edited["revision_id"])["images_retired"]
        reloaded = client.get(url).json()
        assert reloaded["manifest"]["revision_id"] == edited["revision_id"]
        assert all(o["url"] for o in reloaded["manifest"]["outputs"] if o["file"])
        assert package(client, first["campaign_id"], rid, reloaded).status_code == 409
        assert State(ws.storage).effective_revision(rid) == edited["revision_id"]
        with monkeypatch.context() as failed:
            failed.setattr(
                "campaign_studio.rendering.compose",
                lambda *a: (_ for _ in ()).throw(ImageProblem("Controlled total local failure")),
            )
            empty = app.state.runner.revise(
                rid, failed_plan.brief, edited["revision_id"], failed_plan.digest
            )
        assert not any(o["file"] for o in empty["outputs"])
        assert State(ws.storage).effective_revision(rid) == edited["revision_id"]
        recovered = app.state.runner.recover_local(rid)
        assert sum(bool(o["file"]) for o in recovered["outputs"]) == 3
        assert ws.state.effective_revision(rid) == recovered["revision_id"]
        assert {o["id"]: o.get("pixel_sha256") for o in recovered["outputs"] if o["file"]} == {
            o["id"]: o["pixel_sha256"] for o in edited["outputs"] if o["file"]
        }
        with monkeypatch.context() as no_render:
            no_render.setattr(
                "campaign_studio.rendering.compose", lambda *a: pytest.fail("No recomposition")
            )
            response, _ = recheck(client, url, "LOCAL CONTROL", False)
        assert response.status_code == 200, response.text
        restored = response.json()["manifest"]
        expected = {o["id"]: o["sha256"] for o in first["outputs"]}
        expected.update({o["id"]: o["sha256"] for o in recovered["outputs"] if o["file"]})
        assert {o["id"]: o["sha256"] for o in restored["outputs"]} == expected
        assert (
            response.json()["metadata"]["creatives"][visible["id"]]["description"]
            == "Current edited image."
        )
        assert ws.state.attempts(rid) == []


def test_recheck_does_not_restore_pixels_with_a_different_composition_context(tmp_path):
    from test_logo_editing import apply_logo, replacement

    from campaign_studio.workspace import LogoEdit

    with prepare(tmp_path) as (app, client, rid, _, url):
        ws = app.state.workspace
        client.headers["X-Campaign-Token"] = app.state.csrf
        response, _ = recheck(client, url, "LOCAL CONTROL", True)
        assert response.status_code == 200, response.text
        cid = ws.state.run(rid)["campaign_id"]
        # Campaign replacement touches available images, while retaining the
        # blocking omission. An older complete image still carries the old logo.
        edit = LogoEdit(
            run_id=rid,
            revision_id=ws.state.effective_revision(rid),
            product_id="product-2",
            market="en-US",
            ratio="1:1",
            replacement=replacement(ws.state, cid),
            draft_version=client.get(url).json()["version"],
        )
        result, _ = apply_logo(ws.state, app.state.runner, edit)
        assert ws.state.effective_revision(rid) == result["revision_id"]
        before = ws.state.revision_count(rid)
        response, pending = recheck(client, url, "LOCAL CONTROL", False)
        assert (
            response.status_code == 409 and "updated production plan" in response.json()["message"]
        )
        assert ws.state.revision_count(rid) == before
        assert ws.state.effective_revision(rid) == result["revision_id"]
        assert ws.state.attempts(rid) == []
        # The offered existing remedy is real: save the retained rule draft,
        # review its zero-request plan, and compose from the snapshotted sources.
        project = client.get(url + "/authored").json()
        project["authored"]["brief"] = pending["value"]
        saved = client.post(
            url + "/authored", json={key: project[key] for key in ("version", "base", "authored")}
        )
        assert saved.status_code == 200, saved.text
        planned = client.post(url + "/plan", json={"version": saved.json()["version"]})
        assert planned.status_code == 200, planned.text
        assert planned.json()["plan"]["generation_products"] == []
        new_run = planned.json()["run"]["id"]
        ws.state.confirm(new_run, ws.state.plan(new_run).digest)
        rebuilt = asyncio.run(app.state.runner.execute(new_run))
        assert rebuilt["complete"] and len(rebuilt["outputs"]) == 6
        assert ws.state.attempts(new_run) == []


@pytest.mark.parametrize("partial_base,fail_all", [(False, False), (True, False), (True, True)])
def test_failed_rule_recheck_does_not_accept_or_retire_the_previous_decision(
    tmp_path, monkeypatch, partial_base, fail_all
):
    with prepare(tmp_path) as (app, client, rid, first, url):
        ws = app.state.workspace
        if partial_base:
            response, _ = recheck(client, url, "LOCAL CONTROL", True)
            assert response.status_code == 200, response.text
        accepted = client.get(url).json()
        current = accepted["manifest"]["revision_id"]
        previous_draft = Intake(ws.storage).read(first["campaign_id"])
        previous_metrics = ws.state.campaign_metrics(first["campaign_id"])
        published = ws.state.revision_count(rid)
        write = ws.storage.write

        def failed_output(reference, payload):
            if (
                "/Revisions/" in reference
                and "/images/" in reference
                and (fail_all or "/1x1/" in reference)
            ):
                raise OSError("Controlled output write failure")
            return write(reference, payload)

        with monkeypatch.context() as failure:
            failure.setattr(ws.storage, "write", failed_output)
            response, _ = recheck(client, url, "LOCAL CONTROL", not partial_base)
        assert response.status_code == 409, response.text
        assert "current images and rules are unchanged" in response.json()["message"]
        assert ws.state.effective_revision(rid) == current
        assert not ws.state.revision(current)["images_retired"]
        assert Intake(ws.storage).read(first["campaign_id"]) == previous_draft
        shown = client.get(url).json()
        assert shown["manifest"]["revision_id"] == current
        assert {o["id"]: o["url"] for o in shown["manifest"]["outputs"]} == {
            o["id"]: o["url"] for o in accepted["manifest"]["outputs"]
        }
        assert package(client, first["campaign_id"], rid, shown).status_code == (
            409 if partial_base else 200
        )
        assert ws.state.revision_count(rid) == published + 1  # Recorded local failure only.
        assert ws.state.attempts(rid) == []
        assert ws.state.campaign_metrics(first["campaign_id"]) == previous_metrics


def test_current_rule_withholding_and_local_damage_are_independent(tmp_path):
    with prepare(tmp_path) as (app, client, rid, first, url):
        ws = app.state.workspace
        assert client.get(url).json()["rule_withheld_outputs"] == []
        response, _ = recheck(client, url, "LOCAL CONTROL", True)
        assert response.status_code == 200
        shown = client.get(url).json()
        withheld = [o["id"] for o in first["outputs"] if o["product_id"] == "product-1"]
        assert shown["rule_withheld_outputs"] == withheld
        assert shown["local_recovery"]["output_ids"] == []
        current = response.json()["manifest"]
        available = next(o for o in current["outputs"] if o["file"])
        row = ws.state.revision(current["revision_id"])
        (ws.storage.path(row["manifest_ref"]).parent / available["file"]).unlink()
        mixed = client.get(url).json()
        assert mixed["rule_withheld_outputs"] == withheld
        assert mixed["local_recovery"]["output_ids"] == [available["id"]]
        assert mixed["file_integrity"]["damaged_outputs"] == [available["id"]]
        assert mixed["run_attempts"] == []
