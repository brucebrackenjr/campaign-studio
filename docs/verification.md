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

## Recorded local evidence

The application passed one complete run of **465 Python tests**, plus Ruff lint and formatting checks. The final frontend passed **264 JavaScript tests**. Tests cover versioned saves, edit publication, source recovery, provider failures, prohibited wording, portable export names, and offline-package integrity without paid requests. The Python result carries by exact backend, Python-test, and configuration identity across the final frontend accessibility and return-path corrections.

| Area | Recorded checks |
| --- | --- |
| Studio layout and editing | Actual browser checks measured the same image rectangle across Inspect, Framing, Logo, and Text in all three formats at 1440 × 900 and 1366 × 768 – 24 combinations. Held and failed Applies retained the controls and draft; successful Applies retained the selected tool, adopted the saved image, and supported a subsequent edit and Cancel. Logo replacement and delayed-opening ownership were also checked. |
| Apply responsiveness | On local 30-output Autumn tests, three-run server medians were 1.09 seconds for Text Apply and 1.05 for Logo Apply. Browser click-to-stable medians were 1.19 seconds for Text, 1.17 for Logo, and 1.25 for Framing, with no redundant preview after acknowledgment. Matching 30-output Bridle server medians were 1.46 seconds for Text and 1.39 for Logo; existing provider-attempt records were unchanged. |
| Exported files | Actual browser ZIP and folder exports used readable product, language, and format paths. All 30 PNGs matched accepted encoded bytes, decoded pixels, and dimensions; the HTML report loaded all 30 images. Naming tests verified brand-based package roots, collision handling, unchanged repeated-export reuse, and preservation of earlier packages. These checks made zero provider requests. |
| Clean installation | The pre-release source ZIP passed a fresh locked-uv installation in a path containing spaces. All 188 source files, 106 application-package files, and 52 served static files matched that ZIP. First start prepared both bundled campaigns, with 30 Autumn images, 18 planned Bridle outputs, no key, and zero attempts. Restart preserved the logical database and all 57 non-database files. |

The conventional Python/pip source-ZIP route was verified on the preceding release baseline. The locked-uv installation above used the pre-release package before the final frontend corrections; those corrections retain identical installation configuration. Dedicated design and independent browser/code reviews exercised the application and its exported files.

## Genuine generation and examples

The supplied seasonal imagery was prepared outside Motif. Fresh-workspace setup composes Autumn Winds locally and leaves Bridle & Bough’s missing image planned but unconfirmed. Both included starting campaigns have zero application attempts.

A separate explicitly authorized Bridle review made one successful application request. One generated source plus two reused scenes produced 18 final PNGs. Recorded usage supports an estimated AI cost of $0.09943. The shipped Bridle starting state is not replaced by that completed review campaign.

The corresponding Autumn and Bridle review exports contain 30 and 18 PNGs. All 48 were checked against encoded bytes, decoded pixels, dimensions, manifests, and moved offline reports.

The separate CLI replay fixture retains an October 29, 2026 master with 244 text-input tokens and 3,276 image-output tokens. Replaying it creates no new provider attempt. Its provenance, model, date, and source hash remain separate from the seasonal review request.

## Platform and CI boundary

[README](../README.md) documents Python 3.14.4, the two installation routes, configuration, example inputs, and recovery behavior. Local installation proof is macOS. Linux is the configured CI target; native Windows is unsupported because local locking uses POSIX `fcntl`.

The [GitHub Actions workflow](../.github/workflows/checks.yml) checks locked installation, style, Python/JavaScript behavior, requirements-export consistency, conventional installation, smoke, and samples without provider credentials. The workflow is configured; remote CI has not run.

