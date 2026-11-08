"""Campaign folders and the records that bind managed files to their owner."""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from campaign_studio.brief import valid_campaign_id
from campaign_studio.storage import Storage, StorageError, delivery_component

UPLOAD_NAME = re.compile(r"(?:(?P<label>.+)-)?[a-f0-9]{32}\.(png|jpg)")


def valid_upload_name(name):
    """Accept registered legacy names and bounded readable names, never paths."""
    match = UPLOAD_NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        return False
    label = match.group("label")
    return label is None or label == delivery_component(label, "Image")


def _product_suffix(product_id):
    return hashlib.sha256(product_id.encode()).hexdigest()


def _readable_product_folder(folder, product_id):
    name, separator, suffix = folder.rpartition("-")
    return bool(
        separator
        and name
        and name == delivery_component(name, "Product")
        and len(suffix) in range(12, 65, 4)
        and suffix == _product_suffix(product_id)[: len(suffix)]
    )


def product_folders(products, manifest=None):
    """Keep a saved product directory stable, even when its display name changes."""
    result = {}
    for item in (manifest or {}).get("outputs", []):
        relative = item.get("file")
        product_id = item.get("product_id")
        if relative and matches_output_file(
            relative, product_id, item.get("ratio"), item.get("market")
        ):
            folder = relative.split("/")[1]
            if _readable_product_folder(folder, product_id):
                if product_id in result and result[product_id] != folder:
                    raise StorageError("A product has conflicting saved image folders.")
                result[product_id] = folder
    used = {folder.casefold() for folder in result.values()}
    for product in sorted(products, key=lambda item: item.id):
        if product.id in result:
            continue
        name = delivery_component(product.name, "Product")
        suffix = _product_suffix(product.id)
        for length in range(12, 65, 4):
            folder = f"{name}-{suffix[:length]}"
            if folder.casefold() not in used:
                result[product.id] = folder
                used.add(folder.casefold())
                break
        else:
            raise StorageError("Choose a new product identity for its image folder.")
    return result


def output_file(product_id, ratio, market, revision_id, *, folder=None):
    if not re.fullmatch(r"[0-9a-f]{32}", revision_id):
        raise StorageError("Invalid image revision.")
    if folder is not None and not _readable_product_folder(folder, product_id):
        raise StorageError("The image folder does not match its product.")
    return f"images/{folder or product_id}/{ratio.replace(':', 'x')}/{market}-{revision_id}.png"


def matches_output_file(relative, product_id, ratio, market):
    """A saved PNG must belong to its planned product, format, and market."""
    if not all(isinstance(value, str) for value in (relative, product_id, ratio, market)):
        return False
    parts = relative.split("/")
    if len(parts) != 4 or (
        parts[1] != product_id and not _readable_product_folder(parts[1], product_id)
    ):
        return False
    stem = f"images/{parts[1]}/{ratio.replace(':', 'x')}/{market}"
    return isinstance(relative, str) and bool(
        re.fullmatch(re.escape(stem) + r"(?:-[0-9a-f]{32})?\.png", relative)
    )


@dataclass(frozen=True)
class ProjectFiles:
    storage: Storage
    campaign_id: str
    folder: str

    def ref(self, relative):
        reference = f"Campaigns/{self.folder}/{relative}"
        self.storage.path(reference)
        return reference

    @property
    def draft(self):
        return self.ref("Brief/draft.json")

    def run(self, run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise StorageError("Invalid production run.")
        return self.ref(f"Runs/{run_id}")

    def check(self, reference, *, inspect_file=True):
        if not isinstance(reference, str) or not reference.startswith(f"Campaigns/{self.folder}/"):
            raise StorageError("Choose a file belonging to this campaign.")
        if inspect_file:
            self.storage.path(reference)
        else:
            self.storage.reference_parts(reference)
        return reference

    def claim(self, reference, db=None):
        """Record the intended destination before a write can leave bytes on disk."""
        self.check(reference)
        if db is None:
            with self.storage.connect() as connection:
                self.claim(reference, connection)
        else:
            existing = db.execute(
                "SELECT campaign_id FROM file_owners WHERE reference=?", (reference,)
            ).fetchone()
            if existing is None:
                if self.storage.path(reference).exists():
                    raise StorageError(
                        "This file is outside Motif’s records. Choose a new location."
                    )
                db.execute("INSERT INTO file_owners VALUES (?,?)", (reference, self.campaign_id))
            elif existing[0] != self.campaign_id:
                raise StorageError("The file belongs to another campaign.")
        return reference

    def require(self, reference, *, inspect_file=True):
        self.check(reference, inspect_file=inspect_file)
        with self.storage.connect() as db:
            if not db.execute(
                "SELECT 1 FROM file_owners WHERE reference=? AND campaign_id=?",
                (reference, self.campaign_id),
            ).fetchone():
                raise StorageError("Choose a file registered to this campaign.")
        return reference

    def check_brief(self, brief, assets=(), *, inspect_files=True):
        """An attachment from a saved or imported document must belong to this project."""
        from campaign_studio.authored import attachment_paths

        paths = list(attachment_paths({"brief": brief}))
        replay = brief.get("replay")
        if isinstance(replay, dict) and isinstance(replay.get("sources"), dict):
            paths.extend(
                item.get("path") for item in replay["sources"].values() if isinstance(item, dict)
            )
        with self.storage.connect() as db:
            for path in set(ref for ref in (*paths, *assets) if ref):
                self.check(path, inspect_file=inspect_files)
                if not db.execute(
                    "SELECT 1 FROM file_owners WHERE reference=? AND campaign_id=?",
                    (path, self.campaign_id),
                ).fetchone():
                    raise StorageError("Choose a file registered to this campaign.")

    def upload_name(self, reference):
        self.require(reference)
        name = PurePosixPath(reference).name
        if not valid_upload_name(name) or reference not in (
            self.ref(f"Assets/Brand/{name}"),
            self.ref(f"Assets/Products/{name}"),
        ):
            raise StorageError("Choose an image with this campaign’s file picker.")
        return name

    def write(self, reference, data):
        self.storage.write(self.claim(reference), data)


def create_project(storage, db, campaign_id, label):
    if not valid_campaign_id(campaign_id):
        raise StorageError("Invalid campaign identity.")
    if db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
        raise sqlite3.IntegrityError("Campaign identity is already in use.")
    suffix = campaign_id.replace("-", "")
    for length in range(8, 33, 4):
        folder = f"{delivery_component(label or 'Campaign', 'Campaign')}-{suffix[:length]}"
        root = storage.path(f"Campaigns/{folder}")
        if (
            root.exists()
            or db.execute(
                "SELECT 1 FROM campaigns WHERE folder=? COLLATE NOCASE", (folder,)
            ).fetchone()
        ):
            continue
        db.execute("INSERT INTO campaigns(id,folder) VALUES (?,?)", (campaign_id, folder))
        root.mkdir(mode=0o700)
        return ProjectFiles(storage, campaign_id, folder)
    raise StorageError("Choose a new campaign identity. Its project folder already exists.")
