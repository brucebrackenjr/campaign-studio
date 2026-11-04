# Verification

Motif is checked through deterministic service tests, controller tests, actual browser actions, clean installations, and exported-file inspection.

## Repeatable local checks

From the source root, using the pinned Python version:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
node --test tests/*.test.cjs
uv run --locked campaign-studio smoke --data-dir ./motif-check-data
uv run --locked campaign-studio sample-run --data-dir ./motif-check-data
uv run --locked campaign-studio sample-run --products 3 --us-only --data-dir ./motif-check-data
uv run --locked campaign-studio sample-run --replay --data-dir ./motif-check-data
```

The check commands use separate ordinary campaign records and require no provider key. The synthetic two-product case composes 12 outputs and corrects six. The three-product, one-market case produces nine. Saved-source replay composes 12, corrects six, reopens storage, and exports with zero new attempts. A missing or changed replay master is refused locally instead of triggering a purchase.

JavaScript tests use Node’s built-in runner without npm dependencies. Provider and configuration failure tests use dummy credentials and controlled transport. Do not point development checks at a workspace containing work you want to keep.

## Version 0.2.0 verification

Release preparation includes fresh ZIP installations, independent GPT and Claude Code reviews, browser journeys, service tests, and inspection of the actual exported files. Broad application checks were followed by focused checks for subsequent changes. Historical test totals are not presented as a new full-suite result for every packaging revision; GitHub Actions records the complete suite results for each published commit.

| Area | Checks performed |
| --- | --- |
| Installation and workspace | Both documented installation routes were exercised. ZIPs extract to `Motif/`; the installed environment finds `Motif/Workspace` and the shared SQLite database without depending on the current terminal directory. Fresh setup creates the two included campaigns with new identities, 30 prepared Autumn PNGs, 18 planned Winter outputs, and no provider attempts. Source files, installed package resources, and served interface files were compared with the candidate ZIP. |
| Campaign creation and import | Browser journeys started campaigns from scratch and imported the supplied briefs. Imports opened Campaign before all photos were attached. Subsequent attachments, product edits, name changes, field limits, autosave, and reopen were exercised. Imported file references did not grant access to external files. |
| Campaign ownership and deletion | Tests and actual application journeys checked separate campaign copies, registered file ownership, original-import preservation, deletion, interrupted cleanup, and database integrity. Unregistered user files and externally saved exports were preserved. Windows file-in-use refusal and completion after releasing the file handle were checked on Windows. |
| Studio and Brand | Browser checks exercised text, logo, and framing edits, all three formats, Apply/Cancel, navigation, stale tabs, and failed responses. Brand checks covered Update Images, the three leave choices, inherited and custom colors, logo addition/replacement/removal, and exported results with no new provider attempt. |
| Layout and input | Desktop checks included 1440 × 900 and 1366 × 768 viewports, the brand/campaign header, text limits, inside-field overage counts, preview controls, and export navigation. These are bounded desktop checks, not universal accessibility or device certification. |
| Delivery and restart | Downloaded PNGs, ZIPs, folder exports, and offline HTML reports were inspected. PNG bytes, decoded pixels, dimensions, and manifest hashes were compared with accepted images; moved reports retained their relative image links. Restart checks compared persisted campaign records and files. |
| Native Windows behavior | Windows Server 2025 and Edge were used for real installation, browser editing, export, restart, and deletion checks. Platform tests exercised native locking, current-user credential encryption, long paths, junction refusal, and held-open files. This is native Windows execution, not Linux simulation; Windows 11 desktop and every native file-picker interaction were not separately certified. |

The macOS browser evidence includes Chromium and WebKit. WebKit automation is distinguished from hands-on Safari use. Controlled network failures and test credentials are identified separately from live generation; browser tests do not silently make paid requests.

To build the portable source ZIP from a Motif Git checkout, run `python scripts/package_source.py ../motif-campaign-studio.zip`. Supply a new destination. The packager includes tracked distribution files under one `Motif/` root and refuses runtime workspaces, installed environments, and credential files.

## Genuine generation and examples

The supplied seasonal imagery was prepared outside Motif. Fresh-workspace setup installs Autumn Winds’ packaged final PNGs with their editable inputs and leaves Bridle & Bough’s missing image planned but unconfirmed. Both included starting campaigns have zero application attempts. Later edits and deliberate local production use the ordinary renderer.

A separate explicitly authorized Bridle review made one successful application request. One generated source plus two reused scenes produced 18 final PNGs. Recorded usage supports an estimated AI cost of $0.09943. The shipped Bridle starting state is not replaced by that completed review campaign.

Separate live framing checks generated one bells, one toboggan, and one blanket source using the configured 1920-square settings. The resulting centered Story and Landscape crops retained the main products, with tight margins on some small details. These three examples support the prompt adjustment; they do not guarantee every generated product will fit without human reframing.

The corresponding Autumn and Bridle review exports contain 30 and 18 PNGs. All 48 were checked against encoded bytes, decoded pixels, dimensions, manifests, and moved offline reports.

The separate CLI replay fixture retains an October 29, 2026 master with 244 text-input tokens and 3,276 image-output tokens. Replaying it creates no new provider attempt. Its provenance, model, date, and source hash remain separate from the seasonal review request.

## Platform and CI boundary

[README](../README.md) documents Python 3.14.4, the two installation routes, configuration, example inputs, and recovery behavior. CI runs the full Python suite on Linux and native Windows. Platform-specific cases cover locking, encrypted key storage, long local paths, and junction protection; tests for behavior exclusive to the other operating system are explicitly skipped.

The [GitHub Actions workflow](../.github/workflows/checks.yml) checks locked installation, style, cold CLI startup, Python/JavaScript behavior, requirements-export consistency, conventional installation, smoke, and samples without provider credentials. Measured results are recorded with each [CI run](https://github.com/brucebrackenjr/campaign-studio/actions).

