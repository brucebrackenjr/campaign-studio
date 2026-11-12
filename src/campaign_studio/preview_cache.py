"""Bounded visual reuse, with fresh ownership and byte checks before every hit.

Only temporary previews call this module. Paid production and delivery keep their
normal independent admission, source verification, and rendering paths.
"""

import hashlib
import threading
import time
from collections import OrderedDict
from pathlib import Path

from campaign_studio import imaging
from campaign_studio.imaging import require_font
from campaign_studio.planning import canonical_hash, make_plan
from campaign_studio.storage import StorageError


class PreviewCache:
    lifetime = 60
    max_plans = 16
    max_images = 32
    max_image_bytes = 64 * 1024 * 1024

    def __init__(self):
        self.plans = OrderedDict()
        self.images = OrderedDict()
        self.plan_lock = threading.Lock()
        self.image_lock = threading.Lock()

    def _prune(self, entries, limit, byte_limit=None):
        now = time.monotonic()
        for key, (created, _) in list(entries.items()):
            if now - created > self.lifetime:
                del entries[key]
        while len(entries) > limit or (
            byte_limit is not None
            and sum(len(payload) for _, payload in entries.values()) > byte_limit
        ):
            entries.popitem(last=False)

    @staticmethod
    def font_key(brief):
        selected = {"noto_sans"}
        for product in brief.products:
            for market in brief.markets:
                text = brief.effective_copy(product, market)
                for framing in product.framing.get(market, {}).values():
                    selected.update(
                        placement.font_id
                        for block, placement in framing.text_placements.items()
                        if getattr(text, block, None)
                    )
        try:
            return tuple(
                (
                    font_id,
                    hashlib.sha256(
                        Path(
                            imaging.font_path()
                            if font_id == "noto_sans"
                            else imaging.font_path(font_id)
                        ).read_bytes()
                    ).hexdigest(),
                )
                for font_id in sorted(selected)
            )
        except OSError as error:
            raise imaging.FontProblem() from error

    @staticmethod
    def source_key(storage, brief):
        """A filename or mtime alone cannot prove a preview still has valid inputs."""
        references = {brief.brand.logo_path} if brief.brand.logo_path else set()
        references.update(
            product.source.path for product in brief.products if product.source.role != "concept"
        )
        if brief.replay:
            references.update(source.path for source in brief.replay.sources.values())
        project = storage.project(brief.campaign_id)
        sources = []
        for reference in sorted(references):
            path = storage.path(project.require(reference))
            sources.append((reference, hashlib.sha256(path.read_bytes()).hexdigest()))
        return (
            str(storage.root),
            canonical_hash(brief.model_dump(mode="json")),
            tuple(sources),
            PreviewCache.font_key(brief),
        )

    def plan(self, storage, brief):
        require_font()
        # Coalesce identical cold requests. This lock does not guard saves,
        # exports, provider calls, or image rendering.
        with self.plan_lock:
            # Verify after any wait, so a queued request cannot reuse a plan
            # whose source was replaced or detached while another plan built.
            try:
                key = self.source_key(storage, brief)
            except OSError, StorageError:
                # Preserve the planner's normal findings for broken or foreign files.
                return make_plan(brief, storage)
            self._prune(self.plans, self.max_plans)
            entry = self.plans.get(key)
            if entry is not None:
                self.plans.move_to_end(key)
                return entry[1].model_copy(deep=True)
            plan = make_plan(brief, storage)
            try:
                unchanged = self.source_key(storage, brief) == key
            except OSError, StorageError:
                unchanged = False
            # An externally replaced source during planning must not populate
            # a cache under the identity of the preceding bytes.
            if unchanged:
                self.plans[key] = (time.monotonic(), plan.model_copy(deep=True))
                self._prune(self.plans, self.max_plans)
            return plan

    @staticmethod
    def image_key(storage, campaign_id, current, output_id):
        # Called only after snapshot has freshly checked version, ownership,
        # source bytes, and preview findings, including retained generated inputs.
        return (
            str(storage.root),
            campaign_id,
            current["version"],
            current["plan"].digest,
            PreviewCache.font_key(current["plan"].brief),
            output_id,
            tuple(sorted((key, value.sha256) for key, value in current["sources"].items())),
        )

    def image(self, key):
        with self.image_lock:
            self._prune(self.images, self.max_images, self.max_image_bytes)
            entry = self.images.get(key)
            if entry is not None:
                self.images.move_to_end(key)
                return entry[1]
        return None

    def keep_image(self, key, payload):
        if len(payload) > self.max_image_bytes:
            return
        with self.image_lock:
            self.images[key] = (time.monotonic(), payload)
            self._prune(self.images, self.max_images, self.max_image_bytes)


preview_cache = PreviewCache()
