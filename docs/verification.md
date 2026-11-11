# Verification

Motif 0.3.2 saves editing instructions throughout the workflow and creates finished images for download. The checks below cover that distinction, the files delivered to a producer, and the packaged installation.

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

## Unreleased portfolio-maintenance candidate

This local candidate keeps the existing stack and has not been published as a new release. Focused Chromium and WebKit checks cover canceled rename/rule proposals, uninterrupted typing before shared-copy confirmation, text movement after Reset, description saves while Source details is open, and optional empty CTA styling. Required headlines can temporarily be blank: the active canvas removes the old headline while retaining the photograph, logo, and CTA. Other products and markets remain editable, including a combined case with a separate incomplete product. Strict and partial ZIP, report, and individual-image delivery remain blocked until required wording is complete. These checks use disposable workspaces without a provider key.

Preview-cache regressions cover source and font contents, file ownership, unchanged filenames/size/timestamps with changed image bytes, stale campaign versions, simultaneous planning requests, defensive copies, expiration, and memory bounds. Delivery performs its own strict validation. A separate comparison of 33 gallery/editor PNGs across all three formats found identical decoded RGBA pixels and dimensions. Faster temporary compression increased the combined PNG payload by about 2.37%; it did not alter source files.

The table records individual local Chromium measurements using the same pristine bundled campaigns on the same computer, before and after the maintenance changes. Autumn has five supplied product photographs; Winter has two available sources and one product awaiting generation. Each action waits for the relevant tool state and image decoding. A cold gallery clears the browser cache, not the operating-system file cache; these are reproducible fixture observations rather than a cross-device benchmark or percentile guarantee.

| Interaction | Autumn baseline → candidate | Winter baseline → candidate |
| --- | ---: | ---: |
| Initial Studio gallery | 2878 → 822ms | 1502 → 549ms |
| Gallery reload | 2801 → 523ms | 1356 → 358ms |
| First Text tool | 952 → 314ms | 693 → 298ms |
| First Logo tool | 921 → 358ms | 834 → 522ms |
| Edit to saved preview and thumbnails | 2390 → 1506ms | 1970 → 1430ms |

These measurements describe the first preview-cache pass, before the persistent-scene refactor below. Its intentional one-second Studio typing delay remained, and a preview could finish before autosave and then refresh after the save. That measured Autumn edit downloaded more local preview data despite finishing sooner. At that checkpoint, HTTP responses remained `no-store`; reuse was inside the application.

### Persistent Studio scene and gallery reuse

The next local candidate keeps one canvas and save queue across Logo, Text, Framing, and Inspect. Geometry tests cover reverse layer selection, transparent bounds, movement limits, and resize anchors. Scene-controller tests cover edits during an in-flight save, acknowledged-version adoption, byte-identical uncertain retries, conflict pause, pending font choices before initial geometry, stale thumbnail suppression, per-field wording scope, keyboard framing, safety bounds, and gesture cancellation.

Service checks recompose the returned source, logo, and exact text layers and compare them with Pillow's finished composition. They exercise combined layout/wording saves, description conflicts with rollback, duplicate requests, stale versions, and bounded 160/640-pixel thumbnails. Visual-cache checks distinguish changed source bytes, branding, layout, and campaign identities, account for decoded pixel memory, and allow retry after a failed request. Successful versioned visual responses require HTTP revalidation; mutable state and errors remain `no-store`. This prevents a same-path asset or font replacement from returning stale HTTP-cached pixels under a new browser visual identity.

Focused Playwright journeys in Chromium and WebKit cover direct selection, overlapping padded text bounds, actual logo attachment/replacement through file pickers, pointer and keyboard resize, crop/safety controls, delayed saves with newer input, failed-save retry, genuine stale-tab conflicts, late raster responses, empty CTA styling, field-specific Apply to all products, and market/format switches. A failed save retains its recovery controls across tool changes without retrying merely because a field blurred. Production partial refresh preserves unchanged image nodes while updating changed content.

A follow-up holds the pointer while a changed-wording response arrives, then releases it and checks that the latest text renders without another edit. Controller regressions also cover a preview timer firing during a held gesture and the resize handles' accessible names as the selected layer changes.

One WebKit observation measured six tool clicks from event capture through two animation frames: 29–32ms, with the same canvas retained and zero preview requests. Returning from All Images to All Products displayed 20 already-decoded images with zero thumbnail requests. Moving or resizing the logo sent only the save request; its local thumbnail changed while other formats stayed unchanged. Text resize can show a provisional scaled thumbnail while awaiting exact glyph rasters, but that provisional image is never cached as a finished saved composition. These are local observations, not cross-device latency guarantees. A first visit still has to load and decode imagery.

A real browser ZIP download contained 30 PNGs. All dimensions, encoded-file checksums, and decoded-pixel checksums matched the manifest. The report displayed all 30 images with the browser offline, and the changed product wording appeared in its three English formats. A separate prepared-product service fixture verified that the layered source retains its transparent cutout over a mocked generated background in every format; this was not a live provider request. This focused browser pass uses macOS WebKit and Chromium, not native Windows or a screen-reader certification.

Provider deadline checks use synthetic localhost responses and the real SDK/HTTP transport. A 65-second silent response was abandoned by the baseline at about 60 seconds and received by the candidate at about 65 seconds. Fast regressions and independent probes also cover a silent response body, overall deadline expiry, disconnects, cancellation, one dispatch, and unknown-outcome accounting. No live provider request was made for this maintenance candidate. These measurements do not establish actual provider latency or image quality.

### Regeneration, navigation, and delivery follow-up

The next portfolio changes add a deliberately confirmed replacement request, browser history, and Handoff description persistence. Regeneration service and HTTP tests use controlled provider transport. They check simultaneous duplicate confirmations, an old confirmation after a later replacement, stale inputs, failure, timeout, Stop followed by a late response, and a changed campaign when a successful result arrives. The old accepted image stays available for PNG and ZIP export during the request. Separate cases reject supplied-scene regeneration and verify that regenerating a prepared-product background preserves the uploaded cutout bytes. Generic start, retry, and local recovery paths cannot bypass the replacement decision or silently adopt a stopped or conflicting result. No paid provider call is made.

Actual Chromium journeys exercise the regeneration review, its prompt and cost details, cancellation without dispatch, and successful replacement. WebKit journeys exercise failure, success, and Stop while a controlled request is in flight. After its late response, the current version and accepted source URLs remain unchanged, while the dispatched attempt is still counted. Screenshots are inspected separately from service results.

Chromium and WebKit navigation journeys cover Back and Forward, a delayed Studio save, shared-copy confirmation and Cancel, and a conflicting save. The current URL and editing controls stay in place while a save or decision is pending. Handoff descriptions survive retry, close, and reload and remain specific to the selected product, market, and format. Tests dispatch the actual beforeunload handler with pending and failed description saves; this verifies the warning condition, not the appearance of every browser's native prompt. Studio gallery-return checks cover All images and Compare, including rejecting another campaign's retained view.

Actual individual PNG downloads distinguish `en-US` and `es-MX` and decode at the expected 1080 × 1080 dimensions. A 30-PNG ZIP passed archive integrity checks and carried the saved description in both the manifest and offline HTML. A separate mocked-generation report displays verified generated and supplied source classifications while preserving the producer's provenance. These follow-up browser checks use macOS Chromium and WebKit, not native Windows or Safari, and do not constitute a new live provider test.

## Version 0.3.2 installation checks

The launcher regression test starts a fresh Python process outside the project, skips editable path files, and runs the installed console entry point. The smoke command must find the application and complete its installation check. On macOS, the installer review also exercised an editable installation with the actual hidden file flag set and verified that the server returned the workbench, health check, and stylesheet.

## Version 0.3.2 first-logo attachment checks

Focused Chromium and WebKit browser checks start without a campaign logo, attach it through Studio, and drag it while inspecting the visible photograph and wording. Chromium checks also cover resizing, keyboard movement, all three formats, replacing artwork, and reopening the Logo tool. JavaScript regression tests check decoded editing layers, first-attachment installation, rejection of obsolete responses, and the retained preview while layers load. Failure-injection checks also verify that **Retry preview** restores editing after an HTTP error or a failed image-layer request, without reopening Studio. These are local rendering checks with no paid provider requests.

## Version 0.3.2 navigation checks

The focused browser checks follow the two local links from a page on a different site. They also open the campaign menu from Inspect, Text, Framing, and Logo, dismiss it, and continue editing. To check autosave during navigation, the tests change a headline, immediately switch campaigns, reopen the first campaign, and read back the saved headline.

## Version 0.3.1 correction checks

The asset-retention checks remove and replace product photos and logos, reopen the campaign, and attach the retained files again. They verify the file bytes and database ownership records remain intact. Deleting the whole campaign must remove both attached and detached assets without changing another campaign or an original file outside the workspace. An older pending attachment cleanup must be canceled on restart. These asset checks are separate from the wording and preview checks below.

This update covers Campaign wording entry, Studio corrections, Handoff checks, optional logos, and Studio format switching. Production checks only whether source photographs are available or can be prepared. Wording findings remain available in Studio and Handoff and continue to block export; they do not prevent source attachment or generation.

The browser checks use a three-product Winter campaign with both markets. They introduce prohibited wording in shared copy and in individual product wording, follow the correction to its source, and check whether notices and field errors disappear without a page refresh. A separate case combines prohibited wording with text extending outside the image. The preview must remain visible for correction, while download stays blocked until both problems are resolved. The checks also cover a readable, source-focused Production sidebar, affected Handoff cards, and returning from an editor correction.

The wording checks follow a complete round trip: change Campaign text, edit a product in Studio, use **Apply to all products**, return to Campaign, and reload. The corresponding Campaign field must show the new shared wording. A later ordinary Studio edit must affect only that product, across its three formats. A subsequent Campaign change must ask before replacing custom wording. Cancelling must preserve the previous Campaign field and every individual wording; confirming must update only the selected field and language. Separate cases cover the CTA, immediate clicks before autosave finishes, repeated clicks, pending changes during navigation, and preserved fonts, colors, positions, and other languages.

The follow-up browser checks use the five-product Autumn campaign to attach a transparent logo from the Studio canvas placeholder, replace it from the right-hand controls, reload, remove it in Campaign, and attach it again from Studio. They also test immediate prohibited-word errors for both Campaign text fields, preservation of unfinished wording across navigation and refresh, and correction without losing unrelated values. Adding a prohibited rule to existing saved wording leaves its image editable and prevents download until corrected. Missing-logo delivery is checked separately from invalid attached artwork.

The latest browser checks enter multiple prohibited words in both Campaign text fields, remove one word at a time, and compare the form, preview request, Studio text, and consolidated warning. Current wording must save and remain visible while correction is unfinished. Custom headline and CTA replacements are tested with both Cancel and Replace. ZIP, individual PNG, and report-preview requests must refuse prohibited copy; a corrected campaign must download successfully.

Upgrade checks reopen unfinished headline and CTA fields saved by an earlier build. Those values enter the current save flow, including the confirmation before replacing custom wording. Browser checks cover both languages, Cancel and Replace, matching previews, removing prohibited words one at a time, successful corrected downloads, and reopening without repeating a settled confirmation.

The missing-logo prompt was checked after removal and on reopening the Logo tool. It appears without a logo-preview request. Both the canvas and sidebar file pickers attach artwork successfully, and subsequent movement and resizing survive reload. The original retained logo and product photograph remain on disk. Delayed uploads also check that both attachment controls stay disabled until the upload finishes.

Backend regressions cover legacy `require_logo` inputs, source preparation with prohibited wording or an unusable logo, and an upload arriving while a preview holds the workspace lock. That upload waits for the preview; the existing refusal during a real competing workspace operation remains intact. Mock-provider tests verify source preparation without spending money, and export tests retain the prohibited-word gate. Four main-page heading checks retain the numbered navigation and product counter while removing duplicated heading labels.

Format-switching checks cover the main canvas and all three thumbnails during edits and navigation. The displayed image stays in place until its replacement has loaded. A format-specific edit must leave the other format thumbnails unchanged. Local browser results include screenshots, timing measurements, and inspection of downloaded PNGs and the offline report; they are not inferred from successful requests alone.

These follow-up checks do not make live provider requests. Separate live OpenAI checks passed on an earlier v0.3.1 candidate on native Windows with Edge and on macOS with WebKit. They covered a newly generated source, Studio editing, verified 18-image downloads, saved credentials, and restart without another generation request. Those results belong to that candidate’s recorded source and ZIP checksums; the later wording and logo-prompt fixes received focused local checks. These browser checks run on macOS with Playwright. WebKit automation is separate from testing the native Safari application. The v0.3.0 platform checks below describe that earlier release, rather than a new Windows test of this correction.

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

