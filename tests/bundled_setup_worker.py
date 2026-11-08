"""Exit during real bundled-file publication without running cleanup handlers."""

import asyncio
import os
import sys
from pathlib import Path

from campaign_studio import intake as intake_module
from campaign_studio import storage as storage_module
from campaign_studio.bundled import install_campaigns
from campaign_studio.state import State
from campaign_studio.storage import Storage
from campaign_studio.workspace import Workspace


def interrupt_setup(root, checkpoint):
    workspace = Workspace(State(Storage(Path(root))))
    original_write = workspace.storage.write_new
    original_publish = storage_module.publish_file
    original_draft_write = intake_module.write_draft

    def write(reference, payload):
        if checkpoint == "registered" and reference.endswith("/Imports/original.json"):
            os._exit(73)
        original_write(reference, payload)
        if checkpoint == "original-written" and reference.endswith("/Imports/original.json"):
            os._exit(73)

    def publish(source, destination, *, replace):
        if checkpoint == "original-temporary" and destination.name == "original.json":
            os._exit(73)
        return original_publish(source, destination, replace=replace)

    def write_draft(*args, **kwargs):
        if checkpoint == "draft-before-commit":
            os._exit(73)
        original_draft_write(*args, **kwargs)
        if checkpoint == "draft-written":
            os._exit(73)

    intake_module.write_draft = write_draft
    workspace.storage.write_new = write
    storage_module.publish_file = publish
    asyncio.run(install_campaigns(workspace))
    raise AssertionError("Setup did not reach the requested interruption")


if __name__ == "__main__":
    interrupt_setup(*sys.argv[1:])
