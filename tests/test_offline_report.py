"""Recipient-facing report evidence from saved state, without a provider or browser."""

import asyncio
import json
from html.parser import HTMLParser

import httpx
import pytest
from test_provider_runner import generated_case
from test_workflow import setup_case

from campaign_studio.brief import Brief
from campaign_studio.exporting import package_payloads
from campaign_studio.planning import make_plan
from campaign_studio.provider import OpenAIImages
from campaign_studio.review import CreativeUpdate


class Report(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links, self.alts, self.text, self.scripts = [], [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.links.extend(value for name, value in attrs if name in {"href", "src"})
        self.alts.extend(value for name, value in attrs if tag == "img" and name == "alt")
        if tag == "script":
            self.scripts.append(attrs)

    def handle_data(self, data):
        self.text.append(data)


@pytest.mark.parametrize("partial", [False, True])
def test_complete_and_partial_report_preserves_content_and_safe_origins(
    tmp_path, partial, monkeypatch
):
    state, runner, _, brief = setup_case(tmp_path)
    value = brief.model_dump()
    value["brand"]["name"] = "Bridle & Bough" if partial else "Autumn Winds"
    value["name"] = value["brand"]["name"] + " – Slogan"
    value["creative_direction"] = "PRIVATE_PROMPT_CANARY must not enter the package."
    value["brand"]["logo_provenance"] = {
        "label": "Logo supplied by café <studio>",
        "rights": "Usage terms not provided.",
        "synthetic": True,
    }
    value["products"][0]["source"]["provenance"].update(
        label="Supplied example.", rights="Created for fictional concepts."
    )
    value["copy_provenance"] = {
        "label": "Supplied wording?",
        "rights": "Local use only!",
        "synthetic": True,
    }
    value["markets"]["en-US"]["phrase_rules"] = None
    run_id = state.create(make_plan(Brief.model_validate(value), state.storage), 0)
    state.confirm(run_id, state.plan(run_id).digest)
    import campaign_studio.rendering as rendering
    from campaign_studio.imaging import ImageProblem

    inspect = rendering.inspect_png

    def controlled_failure(path, output, *args, **kwargs):
        if partial and output.market == "en-US":
            raise ImageProblem("Controlled output integrity failure")
        return inspect(path, output, *args, **kwargs)

    monkeypatch.setattr(rendering, "inspect_png", controlled_failure)
    manifest = asyncio.run(runner.execute(run_id))
    revision = manifest["revision_id"]
    description = 'Un círculo <azul> & una etiqueta "café".'
    state.update_creative(
        revision,
        "product-1/es-MX/1x1",
        CreativeUpdate(
            expected_version=0,
            description=description,
        ),
    )
    state.update_creative(
        revision,
        "product-2/es-MX/1x1",
        CreativeUpdate(expected_version=0, description="Another supplied image description."),
    )
    state.update_creative(
        revision,
        "product-2/es-MX/9x16",
        CreativeUpdate(expected_version=0, description="A third supplied image description."),
    )
    prefix = state.revision(revision)["manifest_ref"].rsplit("/", 1)[0]
    state.storage.write(f"{prefix}/response.json", b"PRIVATE_RESPONSE_CANARY")
    state.storage.write("private/config.env", b"PRIVATE_KEY_CANARY")
    payloads = package_payloads(state, revision, allow_partial=partial)
    html = payloads["index.html"].decode()
    public = json.loads(payloads["manifest.json"])
    package_name = public["package_name"]
    assert package_name.startswith(value["brand"]["name"] + "-")
    assert len(package_name.removeprefix(value["brand"]["name"] + "-")) == 8
    assert "Slogan" not in package_name and " - " not in package_name
    report = Report(html)
    text = " ".join(report.text)
    assert len([name for name in payloads if name.endswith(".png")]) == (6 if partial else 12)
    assert ("Partial campaign package" if partial else "Complete campaign package") in text
    assert "Human review:" not in text and "Reviewed" not in text
    assert description in report.alts and description in text
    assert "Image description:" in text and "Not supplied." in text
    assert "Another supplied image description." in text
    assert "A third supplied image description." in text
    assert "Language review:" not in text and "Marketing review:" not in text
    assert all(set(item["copy_provenance"]) == {"origin"} for item in public["outputs"])
    assert public["logo_provenance"] == value["brand"]["logo_provenance"]
    assert "Logo supplied by café <studio>" in text and "Usage terms not provided." in text
    assert "Logo supplied by café <studio>. Usage terms not provided." in text
    assert "Supplied example. Created for fictional concepts." in text
    assert "Supplied wording? Local use only!" in text
    assert "provided.." not in text and "example.." not in text and "concepts.." not in text
    assert public["sources"][0]["provenance"] == value["products"][0]["source"]["provenance"]
    assert all(
        output["copy_provenance"]["origin"] == value["copy_provenance"]
        for output in public["outputs"]
    )
    assert "&lt;studio&gt;" in html and "&lt;azul&gt;" in html
    for retired in (
        "Findings & coverage",
        "Check details",
        "Review warning",
        "Not checked",
        "Source color profile",
        "Untagged image",
        "No phrase rules",
        "Configured phrase:",
    ):
        assert retired not in text
    assert all(f["severity"] in {"pass", "blocking"} for f in public["findings"])
    assert "0 image requests for this saved run" in text
    assert "$0.00 USD for 0 planned new generation requests" in text
    assert "not total campaign working time" in text
    assert "Time saved has not been measured." in text
    assert public["producer_metadata_snapshot"]["snapshot_id"] in text
    assert not report.scripts
    assert all(
        (f'id="{link[1:]}"' in html if link.startswith("#") else link in payloads)
        for link in report.links
    )
    assert all(f'href="#{section}"' in html for section in ("images", "production", "provenance"))
    assert "The complete picture." in text and "Origins stay with the images." in text
    serialized = html + payloads["manifest.json"].decode()
    for private in (
        "PRIVATE_PROMPT_CANARY",
        "PRIVATE_RESPONSE_CANARY",
        "PRIVATE_KEY_CANARY",
        "private/",
        str(tmp_path),
        "response_ref",
    ):
        assert private not in serialized


@pytest.mark.parametrize("unknown", [False, True])
def test_report_distinguishes_failed_unknown_and_unreported_charges(tmp_path, unknown):
    state, runner, run_id, _ = generated_case(tmp_path)

    def transport(request):
        if unknown:
            raise httpx.ReadTimeout("Synthetic interruption", request=request)
        return httpx.Response(500, json={"error": {"message": "Synthetic failure"}})

    manifest = asyncio.run(
        runner.execute(
            run_id,
            OpenAIImages(
                "synthetic-test-credential",
                transport=httpx.MockTransport(transport),
            ),
        )
    )
    payloads = package_payloads(state, manifest["revision_id"], allow_partial=True)
    text = " ".join(Report(payloads["index.html"].decode()).text)
    assert "1 image request for this saved run" in text
    assert ("1 request without a confirmed result" if unknown else "1 failed request") in text
    assert "usage:" in text and "Not reported." in text
    assert "This run · usage estimate:" in text and "Estimate incomplete." in text
    assert "Estimates are not provider bills." in text
    assert "Failed or interrupted requests may still incur charges." in text
    assert "No new image requests or generation charges" not in text
    assert "Saved plan estimate:" in text and "not invoices or spending caps" in text
    assert state.attempts(run_id)[0]["state"] == ("unknown" if unknown else "failed")


def test_generated_and_supplied_synthetic_sources_keep_actual_origins(tmp_path):
    state, runner, run_id, response = generated_case(tmp_path)
    provider = OpenAIImages(
        "synthetic-test-credential",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)),
    )
    manifest = asyncio.run(runner.execute(run_id, provider))
    assert manifest["complete"] and manifest["evidence"] == "synthetic"
    attempts = state.attempts(run_id)
    payloads = package_payloads(state, manifest["revision_id"])
    report = Report(payloads["index.html"].decode())
    text = " ".join(report.text)
    public = json.loads(payloads["manifest.json"])
    assert "geometric test fixtures for engineering verification" not in text
    assert "Description-only concept" in text and "Supplied finished scene" in text
    assert public["sources"] == manifest["sources"]
    assert public["metrics"] == manifest["metrics"]
    assert public["report_format_version"] == 9
    for source in public["sources"]:
        assert source["provenance"]["label"] in text
        assert source["provenance"]["rights"] in text
    for output in public["outputs"]:
        assert output["file"] in payloads
    assert state.attempts(run_id) == attempts


def test_delivery_names_are_portable_unique_and_deterministic():
    import unicodedata

    from campaign_studio.exporting import delivery_component, delivery_paths

    names = {
        "a": "Café",
        "b": "Cafe\u0301",
        "c": "CAFÉ",
        "d": "CON.txt",
        "e": " ../ ",
        "f": "雪" * 100,
        "g": "lamp#1%20?<>:\\|*\x00",
    }
    manifest = {
        "product_names": names,
        "outputs": [
            {
                "id": key,
                "product_id": key,
                "market": "en-US",
                "ratio": "1:1",
                "width": 1080,
                "height": 1080,
            }
            for key in names
        ],
    }
    paths = delivery_paths(manifest)
    assert paths == delivery_paths(
        {**manifest, "product_names": dict(reversed(list(names.items())))}
    )
    assert len({unicodedata.normalize("NFKC", p).casefold() for p in paths.values()}) == len(paths)
    assert paths["d"].startswith("images/_CON.txt/")
    assert paths["f"].startswith("images/雪")
    for path in paths.values():
        assert path.endswith("/English (United States)/Square - 1080x1080.png")
        assert all(
            part not in ("", ".", "..") and len(part.encode()) < 120 for part in path.split("/")
        )
        assert not any(c in path for c in '<>:"\\|?*#%\x00')
    assert delivery_component("...", "Campaign") == "Campaign"
    assert delivery_component(" LPT1. ", "Product") == "_LPT1"
