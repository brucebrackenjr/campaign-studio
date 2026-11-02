# Campaign examples and engineering fixtures

## Included seasonal campaigns

The official launcher installs two ordinary campaigns once in an absent or empty workspace. Autumn Winds has five products, both markets, and 30 pre-rendered PNGs packaged with their editable sources, wording, and layouts. Installation copies those finished images without composing them. Bridle & Bough has three products, both markets, and an unconfirmed 18-output plan with one missing Brass sleigh-bell strap concept. Both begin with zero application provider attempts. The supplied scenes and logos were prepared externally with built-in image generation; their provenance does not imply a new Motif request.

The source package contains the [Autumn brief and assets](../src/campaign_studio/resources/campaigns/autumn-winds/) and [Bridle brief and assets](../src/campaign_studio/resources/campaigns/bridle-and-bough/). To import another copy, select its `brief.json` in **Import brief**, then attach the named logo and image files. If using a separately supplied import ZIP, extract it first. Motif imports JSON, not ZIP files. Every import receives a fresh UUID, including duplicate display names.

Autumn can be inspected, corrected, and exported without a key. Bridle’s missing concept requires the producer’s own key and explicit plan confirmation. Existing workspaces are not automatically populated, and deleted examples are not reinserted. Setup failure stops that startup and retains ordinary records; it does not create a recovery queue or automatically repeat setup.

## Geometric service fixtures

The images in this `examples/` directory are locally authored geometric graphics. They are not photographs, genuine image-generation evidence, or a claim of final campaign quality.

- [Two products, two markets](synthetic-2/brief.json) names complete local assets and plans 12 outputs. No prohibited words are configured initially.
- [Three products, US only](synthetic-3/brief.json) names complete local assets and plans nine outputs.

Asset references in each JSON brief are relative to that example's directory. The [brief contract](../docs/brief-format.md) explains roles, provenance, phrase rules, and omitted versus null overrides. To exercise the installed services without moving sample files manually:

```sh
uv run --locked campaign-studio sample-run --data-dir ./motif-check-data
uv run --locked campaign-studio sample-run --products 3 --us-only --data-dir ./motif-check-data
```

The commands create fresh synthetic inputs within the configured runtime directory, snapshot a validated zero-request plan, compose PNGs, and export a complete offline package. The default sample also corrects six shared Spanish outputs, preserving unchanged English files in the current revision. Each invocation preserves previous runs.

`uv run --locked campaign-studio smoke --data-dir ./motif-check-data` is the smaller installation check. It writes a labeled 128 × 128 geometric PNG and a manifest, checks SQLite and packaged template resources, and verifies image/JSON round trips. Both commands make zero provider requests. Neither is a replay of genuine provider work.

The separate [curated genuine replay](../src/campaign_studio/resources/replay/README.md) is packaged with the application. Run `uv run --locked campaign-studio sample-run --replay --data-dir ./motif-check-data`. It uses the actual saved brush-roll concept master and supplied chalk scene, with zero new calls. Historical generation remains separate from the replay run's empty attempt ledger.

The alternate three-product, U.S.-only case is available through the command above. The CLI creates ordinary registered UUID campaigns in its explicitly selected workspace. Use the exported offline package to inspect its nine files.
