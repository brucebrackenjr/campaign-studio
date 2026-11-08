"""Current producer descriptions, consistent snapshots, and private offline delivery."""

import asyncio
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import PurePosixPath
from zipfile import ZipFile

import pytest
from campaign_fixtures import independent_brief
from pydantic import ValidationError
from test_workflow import setup_case

from campaign_studio.authored import AuthoredProjects, AuthoredSave
from campaign_studio.brief import Brief
from campaign_studio.campaign_drafts import read
from campaign_studio.compositions import save_metadata
from campaign_studio.delivery import prepare_current_zip
from campaign_studio.handoff import DeliveryAction, Handoff
from campaign_studio.motif import campaign_data
from campaign_studio.planning import make_plan
from campaign_studio.review import CreativeUpdate
from campaign_studio.state import State, StateError
from campaign_studio.workspace import ActionError, Workspace


@pytest.fixture
def produced(tmp_path):
    state, runner, run, brief = setup_case(tmp_path)
    result = asyncio.run(runner.execute(run))
    assert result["outputs"] == [] and result["sources_ready"] == 2
    return Workspace(state), brief.campaign_id, run


def model(ws, cid):
    return campaign_data(ws, cid)


def metadata(ws, cid):
    return model(ws, cid)["metadata"]["creatives"]


def describe(ws, cid, creative, description, expected=None):
    current = model(ws, cid)
    return save_metadata(
        ws,
        cid,
        current["version"],
        creative,
        CreativeUpdate(
            expected_version=current["metadata"]["creatives"][creative]["version"]
            if expected is None
            else expected,
            description=description,
        ),
    )


def save(ws, cid, change):
    projects = AuthoredProjects(ws)
    record = projects.read(cid)
    change(record["authored"]["brief"])
    return projects.save(
        cid, AuthoredSave(**{k: record[k] for k in ("version", "base", "authored")})
    )


def download(ws, cid, partial=False):
    snapshot = ws.current_composition(cid)
    with Handoff(ws.state).create_current_zip(
        cid, DeliveryAction(version=snapshot["version"], allow_partial=partial)
    ) as result:
        payload = result.stream.read()
    with ZipFile(io.BytesIO(payload)) as archive:
        manifest_name = next(n for n in archive.namelist() if n.endswith("/manifest.json"))
        return json.loads(archive.read(manifest_name)), payload


def source_bytes(ws, cid):
    return {
        a.reference: ws.storage.path(a.reference).read_bytes()
        for a in ws.current_composition(cid)["sources"].values()
    }


def test_descriptions_survive_ordinary_copy_and_layout_changes_and_reopen(produced):
    ws, cid, run = produced
    for creative in metadata(ws, cid):
        describe(ws, cid, creative, f"Description of {creative}.")
    before = metadata(ws, cid)
    save(ws, cid, lambda b: b["markets"]["es-MX"]["copy"].update(message="Para cada día."))
    save(ws, cid, lambda b: b["brand"].update(text_color=b["brand"]["palette"][0]))
    assert metadata(ws, cid) == before
    assert metadata(Workspace(State(ws.storage)), cid) == before
    assert ws.state.attempts(run) == [] and ws.state.revision_count(run) == 0


def test_source_change_marks_description_without_erasing_producer_text(produced):
    ws, cid, _ = produced
    creative = "product-1/en-US/1x1"
    describe(ws, cid, creative, "An intentionally supplied description.")
    save(ws, cid, lambda b: b["products"][0]["source"].update(path=""))
    value = read(ws.storage, cid)["creative_metadata"][creative]
    assert value["description"] == "An intentionally supplied description."
    assert value["reset_reason"] == "source_changed"


def test_rule_changes_preserve_descriptions_without_rendering_or_provider_calls(
    produced, monkeypatch
):
    ws, cid, run = produced
    creative = "product-1/en-US/1x1"
    describe(ws, cid, creative, "A saved description.")
    before = metadata(ws, cid)
    sources = source_bytes(ws, cid)
    for method in (
        "campaign_studio.delivery.compose",
        "campaign_studio.provider.OpenAIImages.generate",
    ):
        monkeypatch.setattr(method, lambda *a, **k: pytest.fail("Rule save did image work"))
    phrase = ws.current_composition(cid)["plan"].brief.markets["en-US"].text.message
    save(
        ws,
        cid,
        lambda b: b["markets"]["en-US"].update(phrase_rules=[{"id": "hold", "phrase": phrase}]),
    )
    assert model(ws, cid)["wording_issue"]["code"] == "prohibited_phrase"
    assert metadata(ws, cid) == before
    save(ws, cid, lambda b: b["markets"]["en-US"].update(phrase_rules=[]))
    assert model(ws, cid)["wording_issue"] is None and metadata(ws, cid) == before
    assert source_bytes(ws, cid) == sources and ws.state.attempts(run) == []


def test_metadata_autosave_does_not_render_and_latest_description_is_exported(
    produced, monkeypatch
):
    ws, cid, run = produced
    before = source_bytes(ws, cid)
    old_manifest, _ = download(ws, cid)
    with monkeypatch.context() as patch:
        for method in (
            "campaign_studio.delivery.compose",
            "campaign_studio.provider.OpenAIImages.generate",
        ):
            patch.setattr(
                method, lambda *a, **k: pytest.fail("Description autosave did image work")
            )
        describe(ws, cid, "product-1/en-US/1x1", "Orange subject beside English wording.")
        describe(ws, cid, "product-1/en-US/9x16", "Vertical geometric scene.")
    exported, _ = download(ws, cid)
    assert exported["producer_metadata_snapshot"] != old_manifest["producer_metadata_snapshot"]
    by_id = {o["id"]: o for o in exported["outputs"]}
    assert (
        by_id["product-1/en-US/1x1"]["accessible_description"]
        == "Orange subject beside English wording."
    )
    assert by_id["product-1/en-US/9x16"]["accessible_description"] == "Vertical geometric scene."
    assert by_id["product-2/en-US/1x1"]["accessible_description"] is None
    assert {o["id"]: o["pixel_sha256"] for o in old_manifest["outputs"]} == {
        o["id"]: o["pixel_sha256"] for o in exported["outputs"]
    }
    assert source_bytes(ws, cid) == before and ws.state.revision_count(run) == 0


def test_unknown_invalid_stale_and_concurrent_descriptions(produced):
    ws, cid, _ = produced
    current = model(ws, cid)
    creative = "product-1/en-US/1x1"
    with pytest.raises(ActionError, match="Choose an image"):
        save_metadata(
            ws,
            cid,
            current["version"],
            "unknown",
            CreativeUpdate(expected_version=0, description="Text."),
        )
    with pytest.raises(ValidationError):
        CreativeUpdate(expected_version=0, unknown_field="unsupported")
    with pytest.raises(ValidationError):
        CreativeUpdate(expected_version=0, description=" ")
    with pytest.raises(ValidationError):
        CreativeUpdate(expected_version=0)

    def attempt(text):
        try:
            return save_metadata(
                ws,
                cid,
                current["version"],
                creative,
                CreativeUpdate(expected_version=0, description=text),
            )
        except (ActionError, StateError) as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["First description.", "Other description."]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, (ActionError, StateError)) for r in results) == 1
    assert metadata(ws, cid)[creative]["version"] == 1
    with pytest.raises(ActionError, match="description changed"):
        describe(ws, cid, creative, "Stale overwrite.", expected=0)


def test_export_snapshot_keeps_description_captured_before_later_save(produced, monkeypatch):
    from campaign_studio import delivery

    ws, cid, _ = produced
    creative = "product-1/en-US/1x1"
    describe(ws, cid, creative, "First saved description.")
    initial = ws.current_composition(cid)
    original = delivery.render_png
    changed = False

    def render(snapshot, paths, output, destination, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            describe(ws, cid, creative, "Later saved description.")
        return original(snapshot, paths, output, destination, **kwargs)

    monkeypatch.setattr(delivery, "render_png", render)
    with prepare_current_zip(ws.state, initial) as result, ZipFile(result.stream) as archive:
        name = next(n for n in archive.namelist() if n.endswith("/manifest.json"))
        saved = json.loads(archive.read(name))
    assert (
        next(o for o in saved["outputs"] if o["id"] == creative)["accessible_description"]
        == "First saved description."
    )
    assert saved["composition_version"] == initial["version"]
    assert metadata(ws, cid)[creative]["description"] == "Later saved description."


class LocalLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links.extend(value for name, value in attrs if name in {"src", "href"})


@pytest.mark.parametrize("partial", [False, True])
def test_downloaded_zip_moves_offline_and_excludes_private_files(produced, tmp_path, partial):
    ws, cid, _ = produced
    if partial:

        def missing(b):
            b["products"][0]["source"] = {
                "role": "concept",
                "imagery_absent": True,
                "description": "Missing source.",
                "provenance": b["products"][0]["source"]["provenance"],
            }

        save(ws, cid, missing)
        with pytest.raises(StateError, match="explicitly"):
            download(ws, cid)
    for reference in ("private/response.json", "private/config.env", "unlisted.png"):
        ws.storage.write(reference, b"PRIVATE_SYNTHETIC_SENTINEL")
    (ws.storage.root / ".env").write_bytes(b"PRIVATE_SYNTHETIC_SENTINEL")
    manifest, payload = download(ws, cid, partial)
    extracted = tmp_path / "recipient"
    with ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == (8 if partial else 14)
        assert all(
            not PurePosixPath(n).is_absolute() and ".." not in PurePosixPath(n).parts
            for n in archive.namelist()
        )
        assert all(b"PRIVATE_SYNTHETIC_SENTINEL" not in archive.read(n) for n in archive.namelist())
        archive.extractall(extracted)
    root = next(extracted.iterdir())
    html = (root / "index.html").read_text()
    parser = LocalLinks()
    parser.feed(html)
    assert all(
        f'id="{link[1:]}"' in html
        if link.startswith("#")
        else ":" not in link and (root / link).is_file()
        for link in parser.links
    )
    for item in manifest["outputs"]:
        if item["file"]:
            assert hashlib.sha256((root / item["file"]).read_bytes()).hexdigest() == item["sha256"]
        else:
            assert item["omission"]
    assert manifest["complete"] is (not partial)
    assert not list(ws.storage.root.rglob("*.zip"))


@pytest.mark.parametrize(
    "path",
    [
        "/Users/example/private/source.png",
        r"C:\Users\example\source.png",
        r"\\fileserver\shared\photo.png",
        "//fileserver/shared/photo.png",
    ],
)
def test_export_rejects_private_paths_in_description(produced, path):
    ws, cid, _ = produced
    creative = "product-1/en-US/1x1"
    describe(ws, cid, creative, path)
    with pytest.raises(StateError, match="private paths"):
        download(ws, cid)
    describe(ws, cid, creative, "See https://example.com/collection for photo credits.")
    assert download(ws, cid)[0]["complete"]


def test_new_campaign_cannot_inherit_another_campaign_description(produced):
    ws, cid, _ = produced
    describe(ws, cid, "product-1/en-US/1x1", "Only this campaign owns this description.")
    value = ws.current_composition(cid)["plan"].brief.model_dump()
    value["campaign_id"] = "3ebf5c55-37c0-5e04-80d1-8c2054da62bb"
    brief = independent_brief(ws.state, Brief.model_validate(value), value["campaign_id"])
    from campaign_studio.runner import Runner

    run = ws.state.create(make_plan(brief, ws.storage), 0)
    ws.state.confirm(run, ws.state.plan(run).digest)
    asyncio.run(Runner(ws.state).execute(run))
    assert all(row["description"] is None for row in metadata(ws, brief.campaign_id).values())
    assert metadata(ws, cid)["product-1/en-US/1x1"]["description"]


def test_unsupported_old_schema_does_not_rewrite_sources_or_current_document(produced):
    from campaign_studio.storage import EarlierWorkspace

    ws, cid, _ = produced
    sources = source_bytes(ws, cid)
    with ws.storage.connect() as db:
        current = json.loads(
            db.execute(
                "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (cid,)
            ).fetchone()[0]
        )
        db.execute("DROP TABLE creative_metadata")
        db.execute("PRAGMA user_version=2")
    with pytest.raises(EarlierWorkspace, match="earlier Motif layout"):
        State(ws.storage)
    assert all(ws.storage.path(ref).read_bytes() == payload for ref, payload in sources.items())
    with ws.storage.connect() as db:
        assert (
            json.loads(
                db.execute(
                    "SELECT document_json FROM campaign_drafts WHERE campaign_id=?", (cid,)
                ).fetchone()[0]
            )
            == current
        )
        assert db.execute("PRAGMA user_version").fetchone() == (2,)


def test_description_cannot_make_a_damaged_source_exportable(produced):
    ws, cid, _ = produced
    describe(ws, cid, "product-1/en-US/1x1", "Supplied description.")
    asset = ws.current_composition(cid)["sources"]["product-1"]
    ws.storage.path(asset.reference).write_bytes(b"corrupt synthetic PNG")
    assert metadata(ws, cid)["product-1/en-US/1x1"]["description"] == "Supplied description."
    with pytest.raises(StateError):
        download(ws, cid, True)
    assert not list(ws.storage.root.rglob("Outputs"))


@pytest.mark.parametrize("retired", ["marker", "language_review", "marketing_review"])
def test_retired_review_fields_are_not_active_description_commands(produced, retired):
    ws, cid, run = produced
    with pytest.raises(ValidationError):
        CreativeUpdate.model_validate({"expected_version": 0, retired: "value"})
    describe(ws, cid, "product-1/en-US/1x1", "Current description.")
    manifest, payload = download(ws, cid)
    with ZipFile(io.BytesIO(payload)) as archive:
        html = archive.read(f"{manifest['package_name']}/index.html").decode()
    assert "Current description." in html and "Human review:" not in html
    assert "human_review" not in json.dumps(manifest)
    assert ws.state.attempts(run) == [] and ws.state.revision_count(run) == 0
