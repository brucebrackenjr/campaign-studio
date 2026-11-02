"""Explicit test setup; the application has no dedicated example-opening route."""

from campaign_studio.planning import make_plan
from campaign_studio.sample import write_replay, write_sample


def authored_edit(client, campaign_id):
    """Capture the current HTTP Save payload, including its actual concurrency tokens."""
    response = client.get(f"/api/motif/campaigns/{campaign_id}/authored")
    assert response.status_code == 200, response.text
    current = response.json()
    return {key: current[key] for key in ("version", "base", "authored")}


def seed_campaign(workspace, *, replay=False):
    """Prepare local test imagery and a zero-request plan without a UI shortcut."""
    brief = write_replay(workspace.storage) if replay else write_sample(workspace.storage)[1]
    workspace.state.create(make_plan(brief, workspace.storage), 0)
    return brief.campaign_id


def zip_root(archive):
    """Every current delivery ZIP has exactly one portable campaign directory."""
    roots = {name.split("/", 1)[0] for name in archive.namelist()}
    assert len(roots) == 1
    root = roots.pop()
    assert root not in {"", ".", ".."}
    assert f"{root}/manifest.json" in archive.namelist()
    return root
