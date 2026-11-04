"""Synthetic headless evidence only; no provider, browser, or visual approval."""

import asyncio
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from zipfile import ZipFile

import pytest
from campaign_fixtures import export_ref, independent_brief
from pydantic import ValidationError
from test_workflow import edit_copy, revise, setup_case

from campaign_studio.brief import Brief
from campaign_studio.exporting import (
    dated_payloads,
    export_package,
    export_zip,
    folder_matches,
    package_payloads,
    public_manifest,
)
from campaign_studio.intake import BriefDraft, Intake
from campaign_studio.planning import make_plan
from campaign_studio.review import CreativeUpdate
from campaign_studio.state import State, StateError
from campaign_studio.workspace import Workspace


def package_is_intact(
    state: State, revision_id: str, destination: str, *, expected_snapshot: str | None = None
) -> bool:
    """Reuse only a package matching the saved manifest and every named image."""
    payloads = package_payloads(
        state, revision_id, allow_partial=True, expected_snapshot=expected_snapshot
    )
    try:
        saved = json.loads(state.storage.path(f"{destination}/manifest.json").read_bytes())
        payloads = dated_payloads(state, revision_id, payloads, saved["report_created_at"])
    except OSError, ValueError, TypeError, KeyError:
        return False
    return folder_matches(state, destination, payloads)


@pytest.fixture
def produced(tmp_path):
    state, runner, run_id, _ = setup_case(tmp_path)
    result = asyncio.run(runner.execute(run_id))
    return state, runner, run_id, result


def values(state, revision_id):
    return state.metadata_snapshot(revision_id)["creatives"]


def describe_all(state, manifest):
    for item in manifest["outputs"]:
        state.update_creative(
            manifest["revision_id"],
            item["id"],
            CreativeUpdate(expected_version=0, description=f"Description of {item['id']}."),
        )


def test_spanish_six_reset_english_and_old_revision_preserved(produced):
    state, runner, run_id, first = produced
    describe_all(state, first)
    old_id = first["revision_id"]
    spanish = next(item["id"] for item in first["outputs"] if item["market"] == "es-MX")
    english = next(item["id"] for item in first["outputs"] if item["market"] == "en-US")
    state.update_creative(
        old_id,
        english,
        CreativeUpdate(
            expected_version=1,
            description="A geometric subject beside English wording.",
        ),
    )
    state.update_creative(
        old_id,
        spanish,
        CreativeUpdate(
            expected_version=1,
            description="A test subject and Spanish copy.",
        ),
    )
    before = state.metadata_snapshot(old_id)
    corrected = revise(state, runner, run_id, edit_copy(state, run_id, "Para cada día."))
    current = values(state, corrected["revision_id"])
    assert len(current) == 12
    assert all(item["description"] is None for key, item in current.items() if "/es-MX/" in key)
    assert all(item["description"] for key, item in current.items() if "/en-US/" in key)
    assert current[spanish]["description"] is None
    assert current[english]["description"] == before["creatives"][english]["description"]
    assert state.metadata_snapshot(old_id) == before
    assert (
        values(State(state.storage), old_id)[spanish]["description"]
        == "A test subject and Spanish copy."
    )
    assert state.attempts(run_id) == []


def test_rule_checks_preserve_content_bound_descriptions_without_rendering(produced, monkeypatch):
    state, _, run_id, first = produced
    workspace, intake = Workspace(state), Intake(state.storage)
    intake.write(first["campaign_id"], state.plan(run_id).brief.model_dump(mode="json"), None)

    def recheck(value):
        document = intake.current_document(first["campaign_id"], workspace)
        return intake.recheck_rules(
            first["campaign_id"],
            BriefDraft(
                value=value,
                version=document["version"],
                base_revision=state.run(run_id)["current_revision"],
            ),
            run_id,
            workspace,
        )

    describe_all(state, first)
    descriptions = {
        key: row["description"] for key, row in values(state, first["revision_id"]).items()
    }
    value = state.plan(run_id).brief.model_dump()
    value["markets"]["en-US"]["phrase_rules"].append(
        {
            "id": "new-review",
            "phrase": "everyday",
        }
    )
    monkeypatch.setattr("campaign_studio.rendering.compose", lambda *a: pytest.fail("composed"))
    second = recheck(value)
    current = values(state, second["revision_id"])
    assert {key: row["description"] for key, row in current.items()} == descriptions
    assert any(
        f["rule"] == "new-review" and f["severity"] == "blocking" for f in second["findings"]
    )
    assert sum(bool(item["file"]) for item in second["outputs"]) == 6
    assert {
        item["id"]: item["sha256"] for item in first["outputs"] if item["market"] == "es-MX"
    } == {item["id"]: item["sha256"] for item in second["outputs"] if item["file"]}
    value["markets"]["en-US"]["phrase_rules"][-1]["phrase"] = "not in any creative"
    third = recheck(value)
    assert {
        key: row["description"] for key, row in values(state, third["revision_id"]).items()
    } == descriptions
    assert [item["sha256"] for item in third["outputs"]] == [
        item["sha256"] for item in first["outputs"]
    ]
    assert state.attempts(run_id) == []


def test_metadata_edits_reopen_without_pixels_attempts_or_new_revision(produced, monkeypatch):
    state, _, run_id, first = produced
    revision_id = first["revision_id"]
    revision = state.revision(revision_id)
    prefix = revision["manifest_ref"].rsplit("/", 1)[0]
    before = {
        item["file"]: state.storage.path(f"{prefix}/{item['file']}").read_bytes()
        for item in first["outputs"]
    }
    original_manifest = state.storage.path(revision["manifest_ref"]).read_bytes()
    for method in (
        "campaign_studio.rendering.compose",
        "campaign_studio.provider.OpenAIImages.generate",
    ):
        monkeypatch.setattr(method, lambda *a, **kw: pytest.fail("Metadata edit did image work"))
    export_package(state, revision_id, export_ref(state, revision_id, "before"))
    assert package_is_intact(state, revision_id, export_ref(state, revision_id, "before"))
    old_export = state.storage.path(
        export_ref(state, revision_id, "before/manifest.json")
    ).read_bytes()
    descriptions = ["Orange synthetic subject beside English wording.", "Vertical geometric scene."]
    for item, description in zip(first["outputs"][:2], descriptions, strict=True):
        state.update_creative(
            revision_id,
            item["id"],
            CreativeUpdate(
                expected_version=0,
                description=description,
            ),
        )
    snapshot = public_manifest(State(state.storage), revision_id)
    assert [item["accessible_description"] for item in snapshot["outputs"][:2]] == descriptions
    assert snapshot["outputs"][2]["accessible_description"] is None
    assert all(set(item["copy_provenance"]) == {"origin"} for item in snapshot["outputs"])
    assert (
        snapshot["outputs"][0]["copy_provenance"]["origin"]
        == state.plan(run_id).brief.copy_provenance.model_dump()
    )
    assert not package_is_intact(state, revision_id, export_ref(state, revision_id, "before"))
    assert (
        state.storage.path(export_ref(state, revision_id, "before/manifest.json")).read_bytes()
        == old_export
    )
    export_package(state, revision_id, export_ref(state, revision_id, "after"))
    exported = json.loads(
        state.storage.path(export_ref(state, revision_id, "after/manifest.json")).read_bytes()
    )
    assert exported.pop("report_created_at").endswith("+00:00")
    assert exported["product_names"] == snapshot["product_names"]
    assert exported["producer_metadata_snapshot"] == snapshot["producer_metadata_snapshot"]
    assert [o["sha256"] for o in exported["outputs"]] == [o["sha256"] for o in snapshot["outputs"]]
    assert [o["accessible_description"] for o in exported["outputs"]] == descriptions + [None] * (
        len(exported["outputs"]) - 2
    )
    assert state.storage.path(revision["manifest_ref"]).read_bytes() == original_manifest
    assert all(
        state.storage.path(f"{prefix}/{name}").read_bytes() == data for name, data in before.items()
    )
    assert state.revision_count(run_id) == 1 and state.attempts(run_id) == []


def test_unknown_invalid_stale_and_concurrent_edits(produced):
    state, _, _, first = produced
    revision_id, creative_id = first["revision_id"], first["outputs"][0]["id"]
    with pytest.raises(StateError, match="Revision not found"):
        state.update_creative(
            "unknown", creative_id, CreativeUpdate(expected_version=0, description="Description.")
        )
    with pytest.raises(StateError, match="Creative not found"):
        state.update_creative(
            revision_id, "unknown", CreativeUpdate(expected_version=0, description="Description.")
        )
    with pytest.raises(ValidationError):
        CreativeUpdate(expected_version=0, unknown_field="unsupported")
    with pytest.raises(ValidationError):
        CreativeUpdate(expected_version=0, description=" ")
    with pytest.raises(ValidationError):
        CreativeUpdate(expected_version=0)

    def save(description):
        try:
            return state.update_creative(
                revision_id,
                creative_id,
                CreativeUpdate(expected_version=0, description=description),
            )
        except StateError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["First description.", "Other description."]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, str) and "metadata changed" in item for item in results) == 1
    assert values(state, revision_id)[creative_id]["version"] == 1


def test_metadata_snapshot_does_not_mix_concurrent_edits(produced, monkeypatch):
    state, _, _, first = produced
    revision_id = first["revision_id"]
    initial = state.metadata_snapshot(revision_id)
    from campaign_studio import state as state_module

    original = state_module.public_metadata
    edited = False

    def during_serialization(value):
        nonlocal edited
        if not edited:
            edited = True
            state.update_creative(
                revision_id,
                first["outputs"][-1]["id"],
                CreativeUpdate(
                    expected_version=0,
                    description="A description saved during snapshot serialization.",
                ),
            )
        return original(value)

    monkeypatch.setattr(state_module, "public_metadata", during_serialization)
    concurrent = state.metadata_snapshot(revision_id)
    assert concurrent == initial
    assert state.metadata_snapshot(revision_id)["snapshot_id"] != initial["snapshot_id"]


class LocalLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.links.extend(value for name, value in attrs if name in {"src", "href"})


@pytest.mark.parametrize("partial", [False, True])
def test_zip_matches_folder_moves_offline_and_excludes_private_files(produced, tmp_path, partial):
    state, runner, run_id, manifest = produced
    if partial:
        value = state.plan(run_id).brief.model_dump()
        value["markets"]["en-US"]["copy"]["message"] = "TESTBLOCK"
        manifest = revise(state, runner, run_id, Brief.model_validate(value))
    revision_id = manifest["revision_id"]
    prefix = state.revision(revision_id)["manifest_ref"].rsplit("/", 1)[0]
    for reference in (
        f"{prefix}/private/response.json",
        f"{prefix}/unlisted.png",
        "private/config.env",
    ):
        state.storage.write(reference, b"PRIVATE_SYNTHETIC_SENTINEL")
    (state.storage.root / ".env").write_bytes(b"PRIVATE_SYNTHETIC_SENTINEL")
    if partial:
        with pytest.raises(StateError, match="explicitly"):
            export_zip(state, revision_id, export_ref(state, revision_id, "blocked.zip"))
    export_package(
        state, revision_id, export_ref(state, revision_id, "folder"), allow_partial=partial
    )
    export_zip(
        state, revision_id, export_ref(state, revision_id, "result.zip"), allow_partial=partial
    )
    relocated = tmp_path / "recipient.zip"
    shutil.copyfile(state.storage.path(export_ref(state, revision_id, "result.zip")), relocated)
    extracted = tmp_path / "extracted"
    with ZipFile(relocated) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist()) == (8 if partial else 14)
        assert all(
            not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
            for name in archive.namelist()
        )
        assert all(
            b"PRIVATE_SYNTHETIC_SENTINEL" not in archive.read(name) for name in archive.namelist()
        )
        archive.extractall(extracted)
    extracted = next(extracted.iterdir())
    for path in extracted.rglob("*"):
        if path.is_file():
            folder_bytes = state.storage.path(
                export_ref(state, revision_id, f"folder/{path.relative_to(extracted).as_posix()}")
            ).read_bytes()
            if path.name == "manifest.json":
                archived, folder = json.loads(path.read_bytes()), json.loads(folder_bytes)
                assert datetime.fromisoformat(archived.pop("report_created_at")).tzinfo
                assert datetime.fromisoformat(folder.pop("report_created_at")).tzinfo
                assert archived == folder
            elif path.name == "index.html":
                # Separately created packages retain their own creation times; all other
                # report content and every PNG remain byte-identical.
                import re

                stamp = rb"Report snapshot \xc2\xb7 [^<]+ Later work is not included\."
                assert re.search(stamp, path.read_bytes()) and re.search(stamp, folder_bytes)
                assert re.sub(stamp, b"<package creation time>", path.read_bytes()) == re.sub(
                    stamp, b"<package creation time>", folder_bytes
                )
            else:
                assert path.read_bytes() == folder_bytes
    saved = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
    assert saved["revision_id"] == revision_id and saved["complete"] == (not partial)
    assert sum(bool(item["file"]) for item in saved["outputs"]) == (6 if partial else 12)
    assert all(item.get("omission") for item in saved["outputs"] if not item["file"])
    assert (
        saved["producer_metadata_snapshot"]["snapshot_id"]
        == state.metadata_snapshot(revision_id)["snapshot_id"]
    )
    parser = LocalLinks()
    parser.feed((extracted / "index.html").read_text(encoding="utf-8"))
    assert all(
        f'id="{link[1:]}"' in (extracted / "index.html").read_text(encoding="utf-8")
        if link.startswith("#")
        else ":" not in link and (extracted / link).is_file()
        for link in parser.links
    )
    for item in saved["outputs"]:
        if item["file"]:
            assert (
                hashlib.sha256((extracted / item["file"]).read_bytes()).hexdigest()
                == item["sha256"]
            )
    before = relocated.read_bytes()
    with pytest.raises(StateError, match="existing packages"):
        export_zip(
            state, revision_id, export_ref(state, revision_id, "result.zip"), allow_partial=partial
        )
    assert state.storage.path(export_ref(state, revision_id, "result.zip")).read_bytes() == before


def test_export_rejects_private_metadata_and_unsafe_entry(produced):
    state, _, _, first = produced
    revision_id = first["revision_id"]
    creative_id = first["outputs"][0]["id"]
    private_paths = [
        "/Users/example/private/source.png",
        r"C:\Users\example\source.png",
        r"\\fileserver\shared\private-photo.png",
        "//fileserver/shared/photo.png",
    ]
    for version, path in enumerate(private_paths):
        state.update_creative(
            revision_id, creative_id, CreativeUpdate(expected_version=version, description=path)
        )
        with pytest.raises(StateError, match="private-looking"):
            export_zip(state, revision_id, export_ref(state, revision_id, "private.zip"))
    state.update_creative(
        revision_id,
        creative_id,
        CreativeUpdate(
            expected_version=len(private_paths),
            description="See https://example.com/collection for photo credits.",
        ),
    )
    export_zip(state, revision_id, export_ref(state, revision_id, "public-link.zip"))
    reference = state.revision(revision_id)["manifest_ref"]
    damaged = json.loads(state.storage.path(reference).read_bytes())
    damaged["outputs"][0]["file"] = "../../private/response.json"
    state.storage.write(reference, json.dumps(damaged).encode())
    with pytest.raises(StateError, match="unsafe or unexpected"):
        export_zip(state, revision_id, export_ref(state, revision_id, "unsafe.zip"))
    assert not state.storage.path(export_ref(state, revision_id, "unsafe.zip")).exists()


def test_new_run_inherits_only_same_campaign_and_unchanged_pixels(produced):
    state, runner, run_id, first = produced
    describe_all(state, first)
    value = state.plan(run_id).brief.model_dump()
    value["name"] = "Renamed sample campaign"
    new_run = state.create(make_plan(Brief.model_validate(value), state.storage), 0)
    state.confirm(new_run, state.plan(new_run).digest)
    second = asyncio.run(runner.execute(new_run))
    assert all(item["description"] for item in values(state, second["revision_id"]).values())
    value["campaign_id"] = "3ebf5c55-37c0-5e04-80d1-8c2054da62bb"
    other_brief = independent_brief(state, Brief.model_validate(value), value["campaign_id"])
    other = state.create(make_plan(other_brief, state.storage), 0)
    state.confirm(other, state.plan(other).digest)
    third = asyncio.run(runner.execute(other))
    assert all(item["description"] is None for item in values(state, third["revision_id"]).values())


def test_existing_database_defaults_without_inventing_descriptions_or_rewriting_files(produced):
    state, _, run_id, manifest = produced
    revision_id = manifest["revision_id"]
    reference = state.revision(revision_id)["manifest_ref"]
    original = state.storage.path(reference).read_bytes()
    with state.storage.connect() as db:
        db.execute("DROP TABLE creative_metadata")
        db.execute("PRAGMA user_version=2")
    from campaign_studio.storage import EarlierWorkspace

    with pytest.raises(EarlierWorkspace, match="earlier Motif layout"):
        State(state.storage)
    assert state.storage.path(reference).read_bytes() == original
    with state.storage.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='creative_metadata'"
        ).fetchone()


def test_description_cannot_make_a_corrupt_image_exportable(produced):
    state, _, _, manifest = produced
    revision_id = manifest["revision_id"]
    output = manifest["outputs"][0]
    prefix = state.revision(revision_id)["manifest_ref"].rsplit("/", 1)[0]
    state.storage.write(f"{prefix}/{output['file']}", b"corrupt synthetic PNG")
    state.update_creative(
        revision_id,
        output["id"],
        CreativeUpdate(expected_version=0, description="Supplied description."),
    )
    assert values(state, revision_id)[output["id"]]["description"] == "Supplied description."
    with pytest.raises(StateError, match="saved output changed"):
        export_zip(state, revision_id, export_ref(state, revision_id, "corrupt.zip"))
    assert not state.storage.path(export_ref(state, revision_id, "corrupt.zip")).exists()


def test_retired_metadata_is_preserved_in_storage_but_not_editable_or_exported(produced):

    state, _, run_id, manifest = produced
    revision_id, output = manifest["revision_id"], manifest["outputs"][0]
    with state.storage.connect() as db:
        raw = json.loads(
            db.execute(
                "SELECT value_json FROM creative_metadata WHERE revision_id=? AND creative_id=?",
                (revision_id, output["id"]),
            ).fetchone()[0]
        )
        raw.update(marker="reviewed", language_review="An old stored note.", marketing_review=None)
        db.execute(
            "UPDATE creative_metadata SET value_json=? WHERE revision_id=? AND creative_id=?",
            (json.dumps(raw), revision_id, output["id"]),
        )
    # Archived user records remain data; no active marker command or report projection remains.
    for retired in ("marker", "language_review", "marketing_review"):
        with pytest.raises(ValidationError):
            CreativeUpdate.model_validate({"expected_version": 0, retired: "value"})
    before = state.metadata_snapshot(revision_id)
    assert set(before["creatives"][output["id"]]) == {
        "version",
        "description",
        "updated_at",
        "inherited_from",
        "reset_reason",
    }
    state.update_creative(
        revision_id,
        output["id"],
        CreativeUpdate(expected_version=0, description="Current description."),
    )
    with state.storage.connect() as db:
        stored = json.loads(
            db.execute(
                "SELECT value_json FROM creative_metadata WHERE revision_id=? AND creative_id=?",
                (revision_id, output["id"]),
            ).fetchone()[0]
        )
    assert all(stored[key] == raw[key] for key in ("marker", "language_review", "marketing_review"))
    export_package(state, revision_id, export_ref(state, revision_id, "current-description"))
    public = json.loads(
        state.storage.path(
            export_ref(state, revision_id, "current-description/manifest.json")
        ).read_bytes()
    )
    html = state.storage.path(
        export_ref(state, revision_id, "current-description/index.html")
    ).read_text()
    assert "Current description." in html
    assert "human_review" not in json.dumps(public)
    assert "human reviewed" not in html and "Human review:" not in html
    assert public["outputs"][0]["sha256"] == output["sha256"]
    assert state.attempts(run_id) == [] and state.revision_count(run_id) == 1
