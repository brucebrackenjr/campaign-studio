# Verification

Motif 0.3.1 saves editing instructions throughout the workflow and creates finished images for download. The checks below cover that distinction, the files delivered to a producer, and the packaged installation.

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

## Version 0.3.1 correction checks

The asset-retention checks remove and replace product photos and logos, reopen the campaign, and attach the retained files again. They verify the file bytes and database ownership records remain intact. Deleting the whole campaign must remove both attached and detached assets without changing another campaign or an original file outside the workspace. An older pending attachment cleanup must be canceled on restart. These focused checks do not change prohibited-word behavior or preview performance.

This update covers Campaign wording entry, Studio corrections, Handoff checks, optional logos, and Studio format switching. Production checks only whether source photographs are available or can be prepared. Wording findings remain available in Studio and Handoff and continue to block export; they do not prevent source attachment or generation.

The browser checks use a three-product Winter campaign with both markets. They introduce prohibited wording in shared copy and in individual product wording, follow the correction to its source, and check whether notices and field errors disappear without a page refresh. A separate case combines prohibited wording with text extending outside the image. The preview must remain visible for correction, while download stays blocked until both problems are resolved. The checks also cover a readable, source-focused Production sidebar, affected Handoff cards, and returning from an editor correction.

The wording checks follow a complete round trip: change Campaign text, edit a product in Studio, use **Apply to all products**, return to Campaign, and reload. The corresponding Campaign field must show the new shared wording. A later ordinary Studio edit must affect only that product, across its three formats. A subsequent Campaign change must ask before replacing custom wording. Cancelling must preserve the previous Campaign field and every individual wording; confirming must update only the selected field and language. Separate cases cover the CTA, immediate clicks before autosave finishes, repeated clicks, pending changes during navigation, and preserved fonts, colors, positions, and other languages.

The follow-up browser checks use the five-product Autumn campaign to attach a transparent logo from the Studio canvas placeholder, replace it from the right-hand controls, reload, remove it in Campaign, and attach it again from Studio. They also test immediate prohibited-word errors for both Campaign text fields, preservation of unfinished wording across navigation and refresh, and correction without losing unrelated values. Adding a prohibited rule to existing saved wording leaves its image editable and prevents download until corrected. Missing-logo delivery is checked separately from invalid attached artwork.

Further checks reload unfinished prohibited copy, then remove or change its rule. Newly allowed wording must follow the same shared-copy confirmation as a normal Campaign edit; it cannot appear applied in the preview while Studio still uses older wording. Both text fields and languages cover Cancel and Replace. Delayed real uploads verify that both Studio logo attachment controls are disabled during an upload and become available afterward, without a second upload request.

Backend regressions cover legacy `require_logo` inputs, source preparation with prohibited wording or an unusable logo, and an upload arriving while a preview holds the workspace lock. That upload waits for the preview; the existing refusal during a real competing workspace operation remains intact. Mock-provider tests verify source preparation without spending money, and export tests retain the prohibited-word gate. Four main-page heading checks retain the numbered navigation and product counter while removing duplicated heading labels.

Format-switching checks cover the main canvas and all three thumbnails during edits and navigation. The displayed image stays in place until its replacement has loaded. A format-specific edit must leave the other format thumbnails unchanged. Local browser results include screenshots, timing measurements, and inspection of downloaded PNGs and the offline report; they are not inferred from successful requests alone.

These follow-up checks do not make live provider requests. Earlier live-provider results belong to their recorded source and ZIP checksums, not automatically to this later candidate. These browser checks run on macOS with Playwright. WebKit automation is separate from testing the native Safari application. The v0.3.0 platform checks below describe that earlier release, rather than a new Windows test of this correction.

## Version 0.3.0 check coverage

Automated checks, independent browser journeys, and release-package checks cover the following behavior. Release evidence records the exact source and ZIP checksums, installed dependency versions, and files inspected.

| Area | Checks |
| --- | --- |
| Current editing instructions | Campaign and Studio share the current SQLite document. Tests cover inherited color and wording, deliberate individual overrides, refresh persistence, stale saves, lost-response acknowledgements, and repeated editing without accumulated revision rows or campaign folders. |
| Production decisions and activity | Actual browser brief import preserves the selected source-generation choice. Changing that choice updates the product card and survives refresh. A controlled 12-second request showed an advancing elapsed timer and ongoing activity while the completed-image count stayed accurate. The Production sidebar fit within a 1280 × 800 viewport. |
| Uploads and credentials | New-product images are validated before storage and remain browser-held until Add. Tests cover rejected transparent images, cancelled forms, failed saves, and retry after a lost response. Local API-key removal preserves other configuration, rejects unauthorized requests, and leaves generation under explicit user control. |
| Preview continuity | Browser probes cover immediate default-color feedback, preserved individual colors, logo replacement, and tool switching after edits. Delayed-image tests cross an autosave boundary and rapidly change targets; the current display stays visible, and an obsolete response cannot replace the selected image. |
| Delivery | Tests cover capturing one saved campaign, independently rendering and checking each image, retaining successful renders when others fail, retrying only failures, offline relative image links, ZIP integrity, and cleanup of temporary files. Source edits and local exports do not make provider requests. |
| Storage | Tests inspect database and campaign-folder counts after repeated saves. Startup does not create an unused smoke folder and removes only an empty legacy placeholder; existing check results remain. |
| Fresh installation | Release checks compare the ZIP inventory and checksums with its source, extract into a fresh Motif folder, follow both README installation methods, and exercise browser editing, download, offline HTML, and restart on macOS and native Windows. |

Provider failure and timing checks use controlled transport and test credentials. A successful local preview or source test is not substituted for inspection of the downloaded PNGs and offline report.

## Version 0.2.0 verification – historical


The v0.2.0 release preparation included fresh ZIP installations, independent GPT and Claude Code reviews, browser journeys, service tests, and inspection of the actual exported files. Broad application checks were followed by focused checks for subsequent changes. Historical test totals are not presented as a new full-suite result for every packaging revision; GitHub Actions records the complete suite results for each published commit.

| Area | Checks performed |
| --- | --- |
| Installation and workspace | Both documented installation routes were exercised. ZIPs extract to `Motif/`; the installed environment finds `Motif/Workspace` and the shared SQLite database without depending on the current terminal directory. Fresh setup creates the two included campaigns with new identities, 30 prepared Autumn PNGs, 18 planned Winter outputs, and no provider attempts. Source files, installed package resources, and served interface files were compared with the candidate ZIP. |
| Campaign creation and import | Browser journeys started campaigns from scratch and imported the supplied briefs. Imports opened Campaign before all photos were attached. Subsequent attachments, product edits, name changes, field limits, autosave, and reopen were exercised. Imported file references did not grant access to external files. |
| Campaign ownership and deletion | Tests and actual application journeys checked separate campaign copies, registered file ownership, original-import preservation, deletion, interrupted cleanup, and database integrity. Unregistered user files and externally saved exports were preserved. Windows file-in-use refusal and completion after releasing the file handle were checked on Windows. |
| Studio and Brand | Browser checks exercised text, logo, and framing edits, all three formats, Apply/Cancel, navigation, stale tabs, and failed responses. Brand checks covered Update Images, the three leave choices, inherited and custom colors, logo addition/replacement/removal, and exported results with no new provider attempt. |
| Layout and input | Desktop checks included 1440 × 900 and 1366 × 768 viewports, the brand/campaign header, text limits, inside-field overage counts, preview controls, and export navigation. These are bounded desktop checks, not universal accessibility or device certification. |
| Delivery and restart | Checks covered downloaded PNGs and ZIPs, report previews, and the offline HTML opened after extracting a ZIP. PNG bytes, decoded pixels, dimensions, and manifest hashes were compared with accepted images; moved reports retained their relative image links. Restart checks compared persisted campaign records and files. |
| Native Windows behavior | Windows Server 2025 and Edge were used for real installation, browser editing, export, restart, and deletion checks. Platform tests exercised native locking, current-user credential encryption, long paths, junction refusal, and held-open files. This is native Windows execution, not Linux simulation; Windows 11 desktop and every native file-picker interaction were not separately certified. |

The macOS browser evidence includes Chromium and WebKit. WebKit automation is distinguished from hands-on Safari use. Controlled network failures and test credentials are identified separately from live generation; browser tests do not silently make paid requests.

To build the portable source ZIP from a Motif Git checkout, run `python scripts/package_source.py ../motif-campaign-studio.zip`. Supply a new destination. The packager includes Git-tracked distribution files under one `Motif/` root and refuses runtime workspaces, installed environments, and credential files. New source files must be included in the distribution inventory before packaging. Verify the resulting ZIP against its intended source and follow the README installation instructions in a fresh extraction; a successful source test run does not establish that a later ZIP contains those same files.

## Genuine generation and examples – historical evidence

The supplied seasonal imagery was prepared outside Motif. In v0.2.0, fresh-workspace setup installed Autumn Winds’ packaged final PNGs with their editable inputs and left Bridle & Bough’s missing image planned but unconfirmed. Both included starting campaigns had zero application attempts. Later edits and deliberate local production used the ordinary renderer. The current workflow instead installs the editable inputs and source imagery, then creates final PNGs when they are requested for download.

During v0.2.0 preparation, a separate explicitly authorized Bridle review made one successful application request. One generated source plus two reused scenes produced 18 final PNGs. Recorded usage supports an estimated AI cost of $0.09943. The shipped Bridle starting state is not replaced by that completed review campaign.

Separate live framing checks generated one bells, one toboggan, and one blanket source using the configured 1920-square settings. The resulting centered Story and Landscape crops retained the main products, with tight margins on some small details. These three examples support the prompt adjustment; they do not guarantee every generated product will fit without human reframing.

The corresponding Autumn and Bridle review exports contain 30 and 18 PNGs. All 48 were checked against encoded bytes, decoded pixels, dimensions, manifests, and moved offline reports.

The separate CLI replay fixture retains an October 29, 2026 master with 244 text-input tokens and 3,276 image-output tokens. Replaying it creates no new provider attempt. Its provenance, model, date, and source hash remain separate from the seasonal review request.

## Platform and CI boundary

[README](../README.md) documents Python 3.14.4, the two installation routes, configuration, example inputs, and recovery behavior. CI runs the full Python suite on Linux and native Windows. Platform-specific cases cover locking, encrypted key storage, long local paths, and junction protection; tests for behavior exclusive to the other operating system are explicitly skipped.

The [GitHub Actions workflow](../.github/workflows/checks.yml) checks locked installation, style, cold CLI startup, Python/JavaScript behavior, requirements-export consistency, conventional installation, smoke, and samples without provider credentials. Measured results are recorded with each [CI run](https://github.com/brucebrackenjr/campaign-studio/actions).

