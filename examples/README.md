# Campaign examples and engineering fixtures

For Motif **0.3.0**. These inputs exercise the same local application services as ordinary campaigns.

## Included seasonal campaigns

The official launcher installs two ordinary campaigns once in an absent or empty workspace. **Autumn at Home**, for Autumn Winds, has five products, both markets, and editable sources, wording, and layouts for 30 image variations. Installation imports those inputs; the application shows previews and creates full-size PNGs when the producer downloads them. Packaged prepared-output evidence remains a reference fixture, not a permanent final-image set inside the installed campaign. **Winter at the Ranch**, for Bridle & Bough, has three products, both markets, and an unconfirmed 18-output plan with one missing Brass sleigh-bell strap concept. Both begin with zero application provider attempts. The supplied scenes and logos were prepared externally with built-in image generation; their provenance does not imply a new Motif request.

For the import walkthrough, use **Walkthrough Assets** beside the main README in the Motif folder. Import **Winter at the Ranch.json**, attach **Bentwood toboggan.png** and **Long Ride sleigh blanket.png** to their matching products, and attach **Bridle and Bough logo.png** in Brand. The Brass sleigh-bell strap deliberately has no supplied image, so Production can demonstrate the separately confirmed generation request. All inputs are together in that one folder; each new import creates a separate campaign and leaves these files unchanged.

The source package also contains the [Autumn brief and assets](../src/campaign_studio/resources/campaigns/autumn-winds/) and [Bridle brief and assets](../src/campaign_studio/resources/campaigns/bridle-and-bough/). To import another copy, select its `brief.json` in **Import brief**, then attach the named logo and image files. If using a separately supplied import ZIP, extract it first. Motif imports JSON, not ZIP files. Every import receives a fresh UUID, including duplicate display names.

Autumn can be inspected, corrected, and exported without a key. Bridle’s missing concept requires the producer’s own key and explicit plan confirmation. Existing workspaces are not automatically populated, and deleted examples are not reinserted. An interrupted first setup retains its records and resumes verified unfinished examples at the next launch; edited or deleted examples are not replaced.

## Geometric service fixtures

The images in this `examples/` directory are locally authored geometric graphics. They are not photographs, genuine image-generation evidence, or a claim of final campaign quality.

- [Two products, two markets](synthetic-2/brief.json) names complete local assets and plans 12 outputs. No prohibited words are configured initially.
- [Three products, US only](synthetic-3/brief.json) names complete local assets and plans nine outputs.

Asset references in each JSON brief are relative to that example's directory. The [brief contract](../docs/brief-format.md) explains roles, provenance, phrase rules, and omitted versus null overrides. To exercise the installed services without moving sample files manually:

```sh
uv run --locked campaign-studio sample-run --data-dir ./motif-check-data
uv run --locked campaign-studio sample-run --products 3 --us-only --data-dir ./motif-check-data
```

The commands register a separate campaign on each invocation, create fresh synthetic inputs in the selected workspace, confirm a zero-request source plan, and save the current editing instructions. The default sample autosaves a Spanish wording correction affecting six output instructions, then reopens the campaign. Only the download step composes the finished PNGs. It builds and checks a temporary offline ZIP and reports its size as `handoff_zip_bytes`; it retains neither the final PNGs nor the ZIP in the campaign. Earlier sample campaigns remain available because each command invocation creates a different campaign. Ordinary image edits within one campaign do not create another campaign or retain every earlier edit.

`uv run --locked campaign-studio smoke --data-dir ./motif-check-data` is the smaller installation check. It writes a labeled 128 × 128 geometric PNG and a manifest, checks SQLite and packaged template resources, and verifies image/JSON round trips. Both commands make zero provider requests. Neither is a replay of genuine provider work.

The separate [curated genuine replay](../src/campaign_studio/resources/replay/README.md) is packaged with the application. Run `uv run --locked campaign-studio sample-run --replay --data-dir ./motif-check-data`. It uses the actual saved brush-roll concept master and supplied chalk scene, with zero new calls. Historical generation remains separate from the replay run's empty attempt ledger.

The alternate three-product, U.S.-only case is available through the command above. The CLI creates ordinary registered UUID campaigns in its explicitly selected workspace. To preview or download its nine image variations, start Motif with the same `--data-dir`, open that sample campaign from the Workbench, and use Handoff. Download ZIP sends the package to the browser’s selected destination; it does not add an archive to the campaign folder.
