"""Broken rendering resources block previews and delivery, not saved instructions."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.app import create_app
from campaign_studio.campaign_drafts import read as read_draft
from campaign_studio.cli import main
from campaign_studio.config import Settings
from campaign_studio.imaging import FontProblem, supported_characters
from campaign_studio.smoke import run_smoke
from campaign_studio.storage import Storage


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_font_blocks_planning_and_dispatch_but_preserves_saved_work(
    tmp_path, monkeypatch, capsys, corrupt
):
    state, runner, run_id, _ = setup_case(tmp_path)
    asyncio.run(runner.execute(run_id))
    cid = state.run(run_id)["campaign_id"]
    saved = read_draft(state.storage, cid)
    source_bytes = {path: path.read_bytes() for path in state.storage.root.rglob("*.png")}
    # A separate generated fixture provides a real confirmed request prepared while
    # the font was intact. Removing it after planning must still prevent dispatch.
    pending_root = tmp_path / "pending"
    pending_root.mkdir()
    pending_state, pending_runner, pending_id, _ = generated_case(pending_root)
    font = tmp_path / "missing-or-corrupt.ttf"
    if corrupt:
        font.write_bytes(b"synthetic damaged font")
    supported_characters()  # Prove a warm glyph cache cannot mask a missing resource.
    monkeypatch.setattr("campaign_studio.imaging.font_path", lambda: str(font))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider_calls = []

    def forbidden_provider():
        provider_calls.append("constructed")
        pytest.fail("Font failure must precede provider construction")

    class ForbiddenProvider:
        async def generate(self, *args):
            pytest.fail("Font failure must precede dispatch intent")

    with pytest.raises(FontProblem, match="Reinstall Motif – Campaign Studio"):
        asyncio.run(pending_runner.execute(pending_id, ForbiddenProvider()))
    assert pending_state.attempts(pending_id) == []
    pending_app = create_app(
        Settings(data_dir=pending_state.storage.root),
        provider_factory=forbidden_provider,
        key_available=lambda: True,
    )
    with TestClient(pending_app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = pending_app.state.csrf
        result = client.post(
            "/api/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/start",
            json={
                "run_id": pending_id,
                "digest": pending_state.plan(pending_id).digest,
                "allowance": 1,
            },
        )
        assert result.status_code == 503
        assert result.json()["code"] == "font_unavailable"
        assert "Reinstall Motif – Campaign Studio" in result.json()["message"]
        assert str(tmp_path) not in result.text
    assert pending_state.attempts(pending_id) == [] and not provider_calls

    app = create_app(
        Settings(data_dir=state.storage.root),
        provider_factory=forbidden_provider,
        key_available=lambda: False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = app.state.csrf
        assert client.get("/health").status_code == 200
        assert (
            client.get("/campaigns/80d58cbe-5e57-5544-9b07-a4f018714dde/studio").status_code == 200
        )
        authored = client.get(f"/api/motif/campaigns/{cid}/authored")
        assert authored.status_code == 200
        assert authored.json()["authored"]["brief"] == saved["authored"]["brief"]
        # There are no stored final PNGs to fall back to. The current preview and
        # download must explain the damaged installation rather than show an old image.
        data = client.get(f"/api/motif/campaigns/{cid}")
        assert data.status_code == 503 and data.json()["code"] == "font_unavailable"
        result = client.post(
            f"/api/campaigns/{cid}/delivery/zip", json={"version": saved["version"]}
        )
        assert result.status_code == 503 and result.json()["code"] == "font_unavailable"
        assert read_draft(state.storage, cid)["version"] == saved["version"]
        edit = {
            "ratio": "1:1",
            "run_id": run_id,
            "revision_id": saved["version"],
            "market": "es-MX",
            "product_id": "product-1",
            "field_scopes": {"message": "all_products", "cta": "all_products"},
            "values": saved["authored"]["brief"]["markets"]["es-MX"]["copy"]
            | {"message": "Un cambio local."},
        }
        # Saving instructions is independent of preview rendering and still works.
        updated = client.post(f"/api/campaigns/{cid}/text/save", json=edit)
        assert updated.status_code == 200, updated.text
        version = updated.json()["version"]
        assert version != saved["version"]
        assert (
            read_draft(state.storage, cid)["authored"]["brief"]["markets"]["es-MX"]["copy"][
                "message"
            ]
            == "Un cambio local."
        )
        plan = client.post(
            f"/api/campaigns/{cid}/text/preview", json=edit | {"revision_id": version}
        )
        assert plan.status_code == 503 and plan.json()["code"] == "font_unavailable"
        assert read_draft(state.storage, cid)["version"] == version
        assert all(path.read_bytes() == content for path, content in source_bytes.items())
        assert not list(state.storage.root.glob("Campaigns/**/Outputs"))
        with state.storage.connect() as db:
            assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    empty_app = create_app(
        Settings(data_dir=tmp_path / "empty"),
        provider_factory=forbidden_provider,
        key_available=lambda: False,
    )
    with TestClient(empty_app, base_url="http://127.0.0.1") as client:
        client.headers["X-Campaign-Token"] = empty_app.state.csrf
        created = client.post("/api/motif/campaigns", json={"name": "Editable without fonts"})
        assert created.status_code == 200  # A broken renderer does not prevent authoring.
    with pytest.raises(FontProblem):
        run_smoke(Storage(tmp_path / "smoke"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAMPAIGN_STUDIO_DATA_DIR", str(tmp_path / "smoke"))
    assert main(["smoke"]) == 1
    assert "Reinstall Motif – Campaign Studio" in capsys.readouterr().err
    assert state.attempts(run_id) == []
