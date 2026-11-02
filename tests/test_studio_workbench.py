"""Workbench facts come from actual saved files and run state, never planned counts."""

import asyncio
import copy
from pathlib import Path
from unittest.mock import patch

import pytest
from campaign_fixtures import seed_campaign
from fastapi.testclient import TestClient

from campaign_studio.app import create_app
from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief
from campaign_studio.config import Settings
from campaign_studio.planning import make_plan
from campaign_studio.provider import ProviderFailure
from campaign_studio.sample import write_replay
from campaign_studio.state import StateError


@pytest.mark.parametrize("confirmed", [False, True])
def test_home_plan_readiness_follows_saved_authored_inputs(tmp_path, confirmed):
    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("Home must not construct a provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        ws = app.state.workspace
        cid = seed_campaign(ws, replay=True)
        run = ws.run_for(cid)["id"]
        if confirmed:
            ws.state.confirm(run, ws.state.plan(run).digest)

        def card():
            return next(
                c for c in client.get("/api/motif/workspace").json()["campaigns"] if c["id"] == cid
            )

        def save(change):
            current = client.get(f"/api/motif/campaigns/{cid}/authored").json()
            change(current["authored"])
            response = client.post(
                f"/api/motif/campaigns/{cid}/authored",
                json={key: current[key] for key in ("version", "base", "authored")},
            )
            assert response.status_code == 200, response.text

        expected = "confirmed" if confirmed else "planned"
        assert card()["status"] == expected
        save(lambda a: a["form_drafts"].update({"brief": {"name": "unfinished form"}}))
        assert card()["status"] == expected  # Editor buffers do not invalidate a plan.
        ws.state.require_authored_plan(run)
        save(lambda a: a["brief"].update({"products": []}))
        assert card()["status"] == "draft"
        assert card()["products"] == card()["planned_images"] == 0
        assert client.get(f"/api/motif/campaigns/{cid}").json()["unplanned_draft"]
        with pytest.raises(StateError, match="authored brief changed"):
            ws.state.require_authored_plan(run)
        assert ws.state.run(run)["state"] == expected  # Display never rewrites the run.
        assert ws.state.attempts(run) == []


def test_workbench_planned_partial_paused_unknown_and_damaged_are_distinct(tmp_path, monkeypatch):
    app = create_app(
        Settings(data_dir=tmp_path),
        key_available=lambda: False,
        provider_factory=lambda: pytest.fail("No live provider"),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ws = app.state.workspace
        state = ws.state
        runner = app.state.runner
        base = write_replay(ws.storage).model_dump(mode="json")
        calls = []

        class Failed:
            def __init__(self, outcome):
                self.outcome = outcome

            async def generate(self, *args):
                calls.append(self.outcome)
                raise ProviderFailure(
                    self.outcome, "overall_deadline" if self.outcome == "unknown" else "http_401"
                )

        def create(name, missing=False):
            value = copy.deepcopy(base)
            value["campaign_id"] = name
            if missing:
                value["replay"] = None
            plan = make_plan(Brief.model_validate(value), ws.storage)
            run = state.create(plan, len(plan.generation_products))
            state.confirm(run, state.plan(run).digest)
            return run

        planned = create("5ba1a733-7478-57e5-9015-91a28e66cc47")
        paused = create("6e34cb9c-bbbb-510d-a4b8-7cc5bfe8326a")
        with (
            patch("campaign_studio.runner.render_revision", side_effect=OSError("fixture")),
            pytest.raises(OSError),
        ):
            asyncio.run(runner.execute(paused))
        partial = create("7877bdcc-22c8-53fe-9eae-f5f890afea95", True)
        asyncio.run(runner.execute(partial, Failed("failed")))
        unknown = create("6dd89920-fa80-5ba0-9be8-8c1ca75bb9ff", True)
        asyncio.run(runner.execute(unknown, Failed("unknown")))
        complete = create("6f2de4d1-61cb-5a79-a010-71822f9944ce")
        saved = asyncio.run(runner.execute(complete))

        def catalog():
            response = client.get("/api/motif/workspace")
            assert response.status_code == 200, response.text
            return {c["id"]: c for c in response.json()["campaigns"]}

        rows = catalog()
        assert rows["5ba1a733-7478-57e5-9015-91a28e66cc47"]["available_images"] == 0
        assert rows["5ba1a733-7478-57e5-9015-91a28e66cc47"]["planned_images"] == 12
        assert rows["5ba1a733-7478-57e5-9015-91a28e66cc47"]["status"] == "confirmed"
        assert (
            rows["6e34cb9c-bbbb-510d-a4b8-7cc5bfe8326a"]["available_images"] == 0
            and rows["6e34cb9c-bbbb-510d-a4b8-7cc5bfe8326a"]["status"] == "paused"
        )
        assert (
            rows["7877bdcc-22c8-53fe-9eae-f5f890afea95"]["available_images"] == 6
            and rows["7877bdcc-22c8-53fe-9eae-f5f890afea95"]["status"] == "partial"
        )
        assert (
            rows["6dd89920-fa80-5ba0-9be8-8c1ca75bb9ff"]["available_images"] == 6
            and rows["6dd89920-fa80-5ba0-9be8-8c1ca75bb9ff"]["outcome_unknown"]
        )
        assert (
            rows["6f2de4d1-61cb-5a79-a010-71822f9944ce"]["available_images"] == 12
            and rows["6f2de4d1-61cb-5a79-a010-71822f9944ce"]["status"] == "complete"
        )
        # An unchanged home refresh must not read every PNG again.
        original = Path.read_bytes
        with monkeypatch.context() as m:

            def no_repeat(path):
                if path.suffix == ".png":
                    pytest.fail("Unchanged Workbench reread image bytes")
                return original(path)

            m.setattr(Path, "read_bytes", no_repeat)
            assert catalog() == rows
        prefix = state.revision(saved["revision_id"])["manifest_ref"].rsplit("/", 1)[0]
        path = ws.storage.path(prefix + "/" + saved["outputs"][0]["file"])
        payload = path.read_bytes()
        path.write_bytes(b"X" + payload[1:])  # Same length: a size-only check is insufficient.
        damaged = catalog()["6f2de4d1-61cb-5a79-a010-71822f9944ce"]
        assert damaged["available_images"] == 11 and damaged["damaged_images"] == 1
        assert damaged["status"] == "damaged"
        assert state.run(complete)["current_revision"] == saved["revision_id"]
        # Losing every format of the first product must not borrow the other product.
        first = saved["outputs"][0]["product_id"]
        for output in saved["outputs"]:
            if output["product_id"] == first:
                ws.storage.path(prefix + "/" + output["file"]).unlink()
        missing_first = catalog()["6f2de4d1-61cb-5a79-a010-71822f9944ce"]
        assert missing_first["available_images"] == 6
        assert missing_first["thumbnail"] is None
        runner.recover_local(complete)
        repaired = catalog()["6f2de4d1-61cb-5a79-a010-71822f9944ce"]
        assert repaired["available_images"] == 12 and repaired["damaged_images"] == 0
        assert repaired["status"] == "complete"
        assert state.attempts(planned) == state.attempts(paused) == state.attempts(complete) == []
        assert calls == ["failed", "unknown"]
        # A later authored change does not relabel actual results or requests as Draft.
        projects = AuthoredProjects(ws)
        for cid in (
            "6e34cb9c-bbbb-510d-a4b8-7cc5bfe8326a",
            "7877bdcc-22c8-53fe-9eae-f5f890afea95",
            "6dd89920-fa80-5ba0-9be8-8c1ca75bb9ff",
            "6f2de4d1-61cb-5a79-a010-71822f9944ce",
        ):
            current = projects.read(cid)
            current["authored"]["brief"]["audience"] = "New unfinished direction"
            projects.save(
                cid, AuthoredSave(**{key: current[key] for key in ("version", "base", "authored")})
            )
        changed = catalog()
        for cid in (
            "6e34cb9c-bbbb-510d-a4b8-7cc5bfe8326a",
            "7877bdcc-22c8-53fe-9eae-f5f890afea95",
            "6dd89920-fa80-5ba0-9be8-8c1ca75bb9ff",
            "6f2de4d1-61cb-5a79-a010-71822f9944ce",
        ):
            assert changed[cid]["status"] == rows[cid]["status"]
            assert changed[cid]["available_images"] == rows[cid]["available_images"]
        assert changed["6dd89920-fa80-5ba0-9be8-8c1ca75bb9ff"]["outcome_unknown"]
        with ws.storage.connect() as db:
            db.execute("UPDATE runs SET state='running' WHERE id=?", (paused,))
        assert catalog()["6e34cb9c-bbbb-510d-a4b8-7cc5bfe8326a"]["status"] == "running"
