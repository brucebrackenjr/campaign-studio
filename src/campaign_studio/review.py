"""Content-bound image descriptions and revision targets, independent of rendering."""

from typing import Literal

from pydantic import Field, model_validator

from campaign_studio.brief import CampaignID, Contract, Description
from campaign_studio.planning import canonical_hash


class RevisionTarget(Contract):
    campaign_id: CampaignID
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    mode: Literal["current", "saved_version"]


class CreativeUpdate(Contract):
    expected_version: int = Field(ge=0, strict=True)
    description: Description | None = None

    @model_validator(mode="after")
    def meaningful_edit(self):
        if "description" not in self.model_fields_set:
            raise ValueError("Supply an image description, or clear the supplied description.")
        return self


def initial_metadata(output: dict) -> dict:
    visible_copy = {"copy": output["copy"]}
    return {
        "version": 0,
        "description": None,
        "updated_at": None,
        "inherited_from": None,
        "reset_reason": None,
        "_context": {
            "content": canonical_hash(
                {
                    "pixels": output.get("pixel_sha256") or output.get("sha256"),
                    "available": bool(output["file"]),
                    "visible_copy": visible_copy,
                    "size": [output["width"], output["height"]],
                }
            ),
        },
    }


def inherit_metadata(
    current: dict, previous: dict, parent_revision: str, *, rule_withheld: bool = False
) -> dict:
    current["inherited_from"] = parent_revision
    if rule_withheld:
        # A rule decision withholds the same creative; it does not change the
        # described content. Keep this parent's binding, including a user clear.
        current["_context"] = previous["_context"].copy()
    if previous["_context"]["content"] == current["_context"]["content"]:
        current["description"] = previous["description"]
    else:
        # A description can mention copy, framing, or branding. Do not export
        # an old description as if it describes newly changed image pixels.
        current["reset_reason"] = "creative_changed"
    return current


def public_metadata(value: dict) -> dict:
    # Expose the description contract, keeping its private pixel binding local.
    return {
        key: value.get(key)
        for key in ("version", "description", "updated_at", "inherited_from", "reset_reason")
    }
