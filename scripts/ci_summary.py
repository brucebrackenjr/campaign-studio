"""Summarize existing CI results without inventing missing outcomes."""

import json
import os
import platform
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

lines = [
    "## Local application checks",
    f"Commit: `{os.environ['GITHUB_SHA']}`",
    f"Python: `{platform.python_version()}`; platform: `{platform.system()}`.",
]
for reference, label in (
    (".checks/startup.xml", "Cold CLI startup tests"),
    (".checks/pytest.xml", "Python tests"),
    (".checks/javascript.xml", "JavaScript controller tests"),
):
    report = Path(reference)
    if report.is_file():
        # Node emits direct testcase rows; pytest groups them in a testsuite.
        # Count the leaf records so either layout has the same truthful summary.
        cases = list(ET.parse(report).getroot().iter("testcase"))
        totals = {
            "tests": len(cases),
            "failures": sum(case.find("failure") is not None for case in cases),
            "errors": sum(case.find("error") is not None for case in cases),
            "skipped": sum(case.find("skipped") is not None for case in cases),
        }
        lines.append(
            label + ": " + ", ".join(f"{value} {name}" for name, value in totals.items()) + "."
        )
        failures = [
            (case, problem)
            for case in cases
            for tag in ("failure", "error")
            if (problem := case.find(tag)) is not None
        ]
        for case, problem in failures[:8]:
            name = "::".join(value for value in (case.get("classname"), case.get("name")) if value)
            detail = "\n".join(value for value in (problem.get("message"), problem.text) if value)
            lines.append(
                f"<details><summary>{escape(name or 'Unnamed test')}</summary>\n\n"
                f"<pre>{escape(detail[:4000] or 'No failure detail recorded.')}</pre>\n\n"
                "</details>"
            )
        if failures:
            lines.append(
                f"Showing up to 8 failures with 4,000 characters each. "
                f"Complete records: `{reference}` in the check-reports artifact."
            )
    else:
        lines.append(f"{label} report unavailable; no test pass is inferred.")
sample = Path(".checks/smoke.json")
if sample.is_file() and sample.stat().st_size:
    result = json.loads(sample.read_text(encoding="utf-8"))
    lines.append(
        f"Conventional-install sample: `{result['evidence']}`, "
        f"{result['width']} × {result['height']} PNG, "
        f"{result['provider_requests']} provider requests."
    )
else:
    lines.append("Conventional-install sample unavailable; no sample pass is inferred.")
lines.append(
    "Synthetic installation evidence only. Live generation and campaign quality are untested."
)
workflow_sample = Path(".checks/sample-run.json")
if workflow_sample.is_file() and workflow_sample.stat().st_size:
    result = json.loads(workflow_sample.read_text(encoding="utf-8"))
    lines.append(
        f"Installed synthetic workflow: {result['completed_images']}/{result['expected_images']} "
        f"images; {result['corrected_images']} corrected images; "
        f"{result['provider_requests']} provider requests; complete: {result['complete']}."
    )
else:
    lines.append("Installed workflow sample unavailable; no workflow pass is inferred.")
for reference, label in (
    (".checks/alternate-sample.json", "Installed three-product US sample"),
    (".checks/replay-sample.json", "Installed genuine saved-source replay"),
):
    report = Path(reference)
    if report.is_file() and report.stat().st_size:
        result = json.loads(report.read_text())
        lines.append(
            f"{label}: {result['completed_images']}/{result['expected_images']} images; "
            f"{result['provider_requests']} new provider requests; complete: {result['complete']}."
        )
        if result.get("visual_status"):
            lines.append(f"Replay visual status: {result['visual_status']}. No new generation.")
    else:
        lines.append(f"{label} unavailable; no pass is inferred.")
with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
    summary.write("\n\n".join(lines) + "\n")
