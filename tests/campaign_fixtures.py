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


def run_folder(state, run_id):
    return state.storage.project(state.run(run_id)["campaign_id"]).run(run_id)


def export_ref(state, revision_id, relative):
    run_id = state.revision(revision_id)["run_id"]
    return state.storage.project(state.run(run_id)["campaign_id"]).ref("Exports/" + relative)


def independent_brief(state, brief, campaign_id):
    from campaign_studio.brief import Brief
    from campaign_studio.intake import Intake

    state.register_campaign(campaign_id, brief.brand.name)
    intake = Intake(state.storage)
    value = brief.model_dump(mode="json") | {"campaign_id": campaign_id}

    def copied(ref, role):
        return intake.upload(state.storage.path(ref).read_bytes(), None, campaign_id, role)[
            "reference"
        ]

    value["brand"]["logo_path"] = copied(value["brand"]["logo_path"], "logo")
    for product in value["products"]:
        if product["source"].get("path"):
            product["source"]["path"] = copied(product["source"]["path"], "product")
    if value.get("replay"):
        for entry in value["replay"]["sources"].values():
            entry["path"] = copied(entry["path"], "product")
    return Brief.model_validate(value)
