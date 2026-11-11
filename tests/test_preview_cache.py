"""Preview reuse never replaces current-version, source, or delivery checks."""

import asyncio
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageChops
from test_current_composition import save_campaign
from test_prohibited_exports import campaign
from test_provider_runner import generated_case

from campaign_studio import compositions, imaging
from campaign_studio import preview_cache as cache_module
from campaign_studio.campaign_drafts import read
from campaign_studio.provider import OpenAIImages
from campaign_studio.workspace import ActionError, Workspace


@pytest.fixture
def cache(monkeypatch):
    instance = cache_module.PreviewCache()
    monkeypatch.setattr(cache_module, "preview_cache", instance)
    return instance


@pytest.fixture
def editing(tmp_path, cache):
    with campaign(tmp_path, products=1) as (client, ws, url):
        yield client, ws, url.rsplit("/", 1)[1], url


def render(ws, cid):
    version = read(ws.storage, cid)["version"]
    return compositions.preview_image(ws, cid, version, "product-1", "en-US", "1:1")


def source_path(ws, cid):
    document = read(ws.storage, cid)
    return ws.storage.path(document["authored"]["brief"]["products"][0]["source"]["path"])


def rewrite_pixels(path, *, invert=False):
    with Image.open(path) as image:
        rgb = image.convert("RGB")
    try:
        if invert:
            inverted = ImageChops.invert(rgb)
            rgb.close()
            rgb = inverted
        rgb.save(path, format="PNG", compress_level=0)
    finally:
        rgb.close()


def test_repeated_preview_reuses_plan_and_png_but_delivery_plans_independently(
    editing, monkeypatch
):
    client, ws, cid, _ = editing
    planned, composed, strict = [], [], []
    original_plan, original_compose = cache_module.make_plan, compositions.compose
    original_strict = compositions.make_plan

    def plan(*args, **kwargs):
        planned.append(1)
        return original_plan(*args, **kwargs)

    def compose(*args, **kwargs):
        composed.append(1)
        return original_compose(*args, **kwargs)

    def strict_plan(*args, **kwargs):
        strict.append(1)
        return original_strict(*args, **kwargs)

    monkeypatch.setattr(cache_module, "make_plan", plan)
    monkeypatch.setattr(compositions, "compose", compose)
    monkeypatch.setattr(compositions, "make_plan", strict_plan)
    first = render(ws, cid)
    assert render(ws, cid) == first
    assert planned == composed == [1]
    version = read(ws.storage, cid)["version"]
    response = client.get(
        f"/api/campaigns/{cid}/composition/{version}/download/product-1/en-US/1x1"
    )
    assert response.status_code == 200, response.text
    assert strict, "Delivery must build and validate its own strict composition"
    with (
        Image.open(io.BytesIO(first)) as preview,
        Image.open(io.BytesIO(response.content)) as final,
    ):
        assert preview.size == final.size
        assert preview.convert("RGB").tobytes() == final.convert("RGB").tobytes()
    with ws.storage.connect() as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    source_path(ws, cid).write_bytes(b"Corrupt after a successful cached preview")
    refused = client.get(f"/api/campaigns/{cid}/composition/{version}/download/product-1/en-US/1x1")
    assert refused.status_code in {409, 422}


def test_changed_pixels_in_same_file_size_and_mtime_invalidate_both_caches(editing):
    _, ws, cid, _ = editing
    path = source_path(ws, cid)
    rewrite_pixels(path)
    before = path.stat()
    first = render(ws, cid)
    rewrite_pixels(path, invert=True)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (
        before.st_size,
        before.st_mtime_ns,
    )
    second = render(ws, cid)
    with Image.open(io.BytesIO(first)) as a, Image.open(io.BytesIO(second)) as b:
        assert a.convert("RGB").tobytes() != b.convert("RGB").tobytes()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unregistered", "foreign"])
def test_warm_preview_cannot_bypass_broken_or_unowned_source(editing, damage):
    _, ws, cid, _ = editing
    render(ws, cid)
    path = source_path(ws, cid)
    reference = str(path.relative_to(ws.storage.root))
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"This is no longer an image.")
    else:
        with ws.storage.connect() as db:
            if damage == "unregistered":
                db.execute("DELETE FROM file_owners WHERE reference=?", (reference,))
            else:
                db.execute(
                    "INSERT INTO campaigns(id,folder) VALUES (?,?)",
                    ("foreign-campaign", "foreign-campaign"),
                )
                db.execute(
                    "UPDATE file_owners SET campaign_id=? WHERE reference=?",
                    ("foreign-campaign", reference),
                )
    with pytest.raises(ActionError):
        render(ws, cid)


def test_cached_old_version_url_is_rejected_after_save(editing):
    client, ws, cid, _ = editing
    # Use the manifest's real route rather than assuming download and preview share a gate.
    model = client.get(f"/api/motif/campaigns/{cid}").json()
    old_url = model["manifest"]["outputs"][0]["url"]
    assert client.get(old_url).status_code == 200
    save_campaign(ws, cid, lambda brief: brief.update(name="A new saved version"))
    assert client.get(old_url).status_code == 409


def test_returned_plan_mutations_cannot_poison_next_preview(editing):
    _, ws, cid, _ = editing
    first = compositions.snapshot(ws, cid, preview=True)
    digest = first["plan"].digest
    asset = first["plan"].assets["products/product-1"].model_copy(deep=True)
    first["plan"].assets.clear()
    first["plan"].outputs[0].text.message = "Poisoned cached copy"
    first["plan"].brief.name = "Poisoned brief"
    second = compositions.snapshot(ws, cid, preview=True)
    assert second["plan"].digest == digest
    assert second["plan"].assets["products/product-1"] == asset
    assert second["plan"].outputs[0].text.message != "Poisoned cached copy"
    assert second["plan"].brief.name != "Poisoned brief"


def test_plan_capacity_and_expiry_require_fresh_planning(editing, cache, monkeypatch):
    _, ws, cid, _ = editing
    brief = compositions.snapshot(ws, cid)["plan"].brief
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cache, "max_plans", 2)
    calls = []
    original = cache_module.make_plan

    def plan(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache_module, "make_plan", plan)
    a = brief.model_copy(update={"name": "Plan A"})
    b = brief.model_copy(update={"name": "Plan B"})
    c = brief.model_copy(update={"name": "Plan C"})
    cache.plan(ws.storage, a)
    cache.plan(ws.storage, b)
    cache.plan(ws.storage, a)  # A becomes most recently used.
    cache.plan(ws.storage, c)
    assert len(calls) == 3
    cache.plan(ws.storage, a)
    assert len(calls) == 3
    cache.plan(ws.storage, b)
    assert len(calls) == 4
    clock[0] += cache.lifetime + 1
    cache.plan(ws.storage, b)
    assert len(calls) == 5


def test_png_capacity_bytes_expiry_and_oversize_are_bounded(cache, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cache, "max_images", 2)
    monkeypatch.setattr(cache, "max_image_bytes", 6)
    cache.keep_image("a", b"aa")
    cache.keep_image("b", b"bb")
    assert cache.image("a") == b"aa"
    cache.keep_image("c", b"cc")
    assert cache.image("b") is None
    cache.keep_image("d", b"dddd")
    assert cache.image("a") is None
    assert cache.image("c") == b"cc"
    cache.keep_image("oversize", b"1234567")
    assert cache.image("oversize") is None
    assert cache.image("d") == b"dddd"
    clock[0] += cache.lifetime + 1
    assert cache.image("c") is cache.image("d") is None


def test_concurrent_cold_plans_coalesce(editing, cache, monkeypatch):
    _, ws, cid, _ = editing
    brief = compositions.snapshot(ws, cid)["plan"].brief
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original = cache_module.make_plan

    def plan(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return original(*args, **kwargs)

    def waiting_plan():
        queued.set()
        return cache.plan(ws.storage, brief)

    monkeypatch.setattr(cache_module, "make_plan", plan)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cache.plan, ws.storage, brief)
        assert entered.wait(10)
        second = executor.submit(waiting_plan)
        assert queued.wait(10)
        release.set()
        assert first.result(timeout=10).digest == second.result(timeout=10).digest
    assert calls == [1]


def test_source_replaced_during_planning_is_not_cached_under_previous_bytes(
    editing, cache, monkeypatch
):
    _, ws, cid, _ = editing
    brief = compositions.snapshot(ws, cid)["plan"].brief
    path = source_path(ws, cid)
    old_bytes = path.read_bytes()
    calls = []
    original = cache_module.make_plan

    def replace_after_planning(*args, **kwargs):
        calls.append(1)
        plan = original(*args, **kwargs)
        if len(calls) == 1:
            rewrite_pixels(path, invert=True)
        return plan

    monkeypatch.setattr(cache_module, "make_plan", replace_after_planning)
    first = cache.plan(ws.storage, brief)
    second = cache.plan(ws.storage, brief)
    assert first.assets["products/product-1"].sha256 != second.assets["products/product-1"].sha256
    path.write_bytes(old_bytes)
    third = cache.plan(ws.storage, brief)
    assert third.assets["products/product-1"].sha256 == first.assets["products/product-1"].sha256
    assert len(calls) == 3


def test_retained_generated_source_is_checked_before_warm_png_reuse(tmp_path, cache):
    state, runner, run_id, body = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    assert asyncio.run(runner.execute(run_id, provider))["complete"]
    ws = Workspace(state)
    cid = state.run(run_id)["campaign_id"]
    render(ws, cid)
    attempt = state.attempts(run_id)[0]
    state.storage.path(attempt["source_ref"]).write_bytes(b"Changed retained generated source")
    with pytest.raises(ActionError, match="source"):
        render(ws, cid)
    assert len(state.attempts(run_id)) == 1


def test_waiting_plan_verifies_source_after_acquiring_lock(editing, cache, monkeypatch):
    _, ws, cid, _ = editing
    brief = compositions.snapshot(ws, cid)["plan"].brief
    old = cache.plan(ws.storage, brief)
    underlying = threading.Lock()
    attempting = threading.Event()

    class ObservedLock:
        def __enter__(self):
            attempting.set()
            underlying.acquire()

        def __exit__(self, *args):
            underlying.release()

    monkeypatch.setattr(cache, "plan_lock", ObservedLock())
    underlying.acquire()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cache.plan, ws.storage, brief)
        try:
            assert attempting.wait(10)
            rewrite_pixels(source_path(ws, cid), invert=True)
        finally:
            underlying.release()
        fresh = future.result(timeout=10)
    assert fresh.assets["products/product-1"].sha256 != old.assets["products/product-1"].sha256


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_warm_preview_rechecks_active_nondefault_font(editing, tmp_path, monkeypatch, damage):
    _, ws, cid, _ = editing
    font = tmp_path / "active-barlow.ttf"
    font.write_bytes(Path(imaging.font_path("barlow_condensed")).read_bytes())
    original_path = imaging.font_path
    monkeypatch.setattr(
        imaging,
        "font_path",
        lambda font_id="noto_sans": (
            str(font) if font_id == "barlow_condensed" else original_path(font_id)
        ),
    )

    def use_barlow(brief):
        brief["products"][0]["framing"] = {
            "en-US": {
                "1:1": {
                    "text_placements": {
                        "message": {
                            "font_id": "barlow_condensed",
                            "font_size": 40,
                            "left": 0.1,
                            "top": 0.24,
                            "width": 0.8,
                        }
                    }
                }
            }
        }

    save_campaign(ws, cid, use_barlow)
    render(ws, cid)
    if damage == "missing":
        font.unlink()
    else:
        font.write_bytes(b"Corrupt font data")
    with pytest.raises((imaging.FontProblem, ActionError)):
        render(ws, cid)


def test_valid_font_replacement_changes_warm_pixels_and_matches_fresh_cache(
    editing, tmp_path, monkeypatch
):
    _, ws, cid, _ = editing
    font = tmp_path / "active-barlow.ttf"
    original_path = imaging.font_path
    font.write_bytes(Path(original_path("barlow_condensed")).read_bytes())
    replacement = Path(original_path("noto_sans")).read_bytes()
    monkeypatch.setattr(
        imaging,
        "font_path",
        lambda font_id="noto_sans": (
            str(font) if font_id == "barlow_condensed" else original_path(font_id)
        ),
    )
    placement = {
        "font_id": "barlow_condensed",
        "font_size": 40,
        "left": 0.1,
        "top": 0.24,
        "width": 0.8,
    }
    save_campaign(
        ws,
        cid,
        lambda brief: brief["products"][0].update(
            framing={"en-US": {"1:1": {"text_placements": {"message": placement}}}}
        ),
    )
    document = read(ws.storage, cid)
    original_source = source_path(ws, cid).read_bytes()
    before = render(ws, cid)
    font.write_bytes(replacement)
    after = render(ws, cid)  # Keep both warm caches intact for this request.
    assert read(ws.storage, cid) == document
    assert source_path(ws, cid).read_bytes() == original_source
    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        assert a.convert("RGB").tobytes() != b.convert("RGB").tobytes()
    monkeypatch.setattr(cache_module, "preview_cache", cache_module.PreviewCache())
    fresh = render(ws, cid)
    with Image.open(io.BytesIO(after)) as a, Image.open(io.BytesIO(fresh)) as b:
        assert a.size == b.size
        assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes()
