"""Start Motif even when the OS hides an editable installation's .pth file."""

import json
import sys
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path


def main():
    if find_spec("campaign_studio") is None:
        # The installer records the project location independently of its .pth file.
        origin = json.loads(distribution("campaign-studio").read_text("direct_url.json") or "{}")
        if origin.get("dir_info", {}).get("editable"):
            sys.path.append(str(Path.from_uri(origin["url"]) / "src"))

    from campaign_studio.cli import main as run

    return run()
