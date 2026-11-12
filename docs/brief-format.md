# JSON brief contract

This reference describes the public brief input contract in **Motif 0.3.2**. [brief.schema.json](brief.schema.json) is generated from the application's Pydantic models; it is not the format of the application's internal SQLite records. Unknown fields are rejected. Import a JSON file with the ordinary file picker, then edit the same campaign, product, brand, and wording fields used for a new campaign. Imported file references are descriptions only – attach the product photos and any logo you want to use through their controls. The application assigns fresh identities and never reads an arbitrary path from an imported document.

The interface supports one to five products and either `en-US`, `es-MX`, or both. Each selected language produces three formats per product: 1080 × 1080, 1080 × 1920, and 1920 × 1080. That is three outputs per product for one language, or six for both. New campaigns default to both languages; imported briefs retain their selected languages.

## Campaign and copy

A brief uses a lowercase hyphenated UUID string for `campaign_id`. Create and import allocate a new UUID; duplicate names are allowed, and renaming keeps the same identity. A brief includes a campaign name, audience, creative direction, brand name, palette, permitted text color, copy provenance, products, and market-specific copy. The logo is optional. Older briefs containing `brand.require_logo` still import, but that retired option no longer requires a logo. A product with a photo can be previewed before a logo is attached. Origin and rights descriptions are producer-supplied assertions; leaving them unspecified does not invent an ownership claim.

Each market supplies a required message, optional nullable CTA, and up to 32 prohibited phrase rules. Each phrase may contain up to 256 characters. Missing, `null`, and empty `phrase_rules` all mean no configured rules. Every configured match blocks all current campaign exports until corrected or the rule is deliberately edited or removed. There is no advisory severity. Rules use literal or whole-word matching with Unicode NFC normalization and optional case sensitivity. Only printed headlines and CTAs are scanned; descriptions, generation instructions, and rule definitions are excluded. Structured matches identify the product, language, field, and configured phrase so the correction action opens the right control. Valid campaign content can be imported, opened, edited, and saved even when prohibited wording prevents export.

Product `copy_overrides` replace only fields present in the object. An omitted field inherits. Explicit `null` clears an optional field; it cannot clear a message. No language fallback, translation, truncation, or implicit rewrite occurs.

In the application, ordinary Studio typing creates a product-specific wording choice. Editing the Campaign headline or CTA deliberately replaces that field's wording for all products in the selected language and removes their corresponding `copy_overrides` entries. Studio's **Apply to all products** button performs the same one-time action and updates the Campaign field. The other text field, other languages, and visual settings remain unchanged. Simply navigating or saving unrelated fields does not remove individual wording.

## Sources and framing

- `finished_scene` supplies an existing PNG or JPEG with a background and no transparent areas for local crop and composition. Optional normalized `subject_box` bounds identify important source details.
- `concept` explicitly declares `imagery_absent: true` and supplies the generation description. A named missing or corrupt asset never turns into a concept automatically. Imported general and generation descriptions can be distinct; deliberately updating a generated product through the shared description control writes the approved text to both fields so its card and generation input agree.
- `prepared_product` is accepted from imported JSON and by the underlying service, but the product editor does not offer it. Supply a PNG with transparency, a fully visible product surrounded by margin, provenance, and `opaque_nonreflective_product_confirmed: true`. When no verified saved background is available, planning adds one background-generation request for that product. The request asks for an empty scene; the supplied cutout is composed over it locally and is not sent to the provider. The usual key, plan, estimate, and explicit confirmation are required before generation. Prepared-product framing is not adjustable, and the recorded live-provider verification does not cover this route.

Preflight decodes images, applies orientation, checks color profiles, glyph coverage, text fit, and known crop bounds, and preserves original assets. Uploads accept PNG or JPEG up to 20 MiB and 40 megapixels; logos accept both formats, and transparency is optional. Fully transparent logos are refused because they contain no visible artwork. These are technical intake bounds, not universal quality guarantees. Untagged RGB inputs use sRGB; no advisory is created for that assumption.

Per-creative `framing` is keyed by market and ratio. It records crop position and zoom, top/bottom copy placement, optional subject bounds, and individual text and logo placements. Studio autosaves those instructions into the current campaign document. Temporary previews and final export use the same compositor. A crop that cuts into a marked product area is identified for correction and cannot be exported as a valid image. There is no stored finished PNG to replace or restore while editing. The original source remains available. ZIPs already downloaded by the browser, and earlier packages preserved during upgrade, keep their original contents; new edits do not create an internal export archive.

`focal_x` and `focal_y` range from 0 to 1 and position the crop within the source space left over after fitting the selected format and zoom. They are not coordinates of a subject or point in the photograph. Zero aligns the crop to the left or top edge, one aligns it to the right or bottom edge, and 0.5 centers it. For example, if a crop leaves 400 source pixels of vertical room, `focal_y: 0.75` starts it 300 pixels from the top. An axis with at most half a source pixel of room stays centered. Subject bounds instead use normalized coordinates in the oriented source image.

Studio’s Framing tool is available when that product has a ready source image, except for the prepared-product route described above. Source readiness does not require a finished PNG. A missing concept must first be prepared through the confirmed Production workflow. Existing subject bounds are not silently relaxed when a source image changes.

Optional `framing.logo_placement` records normalized `left`, `top`, and `width` in the output canvas. The logo retains its proportions and stays inside that canvas. Omission uses the default bottom-right placement. A logo-position edit autosaves for the selected product, language, and format. Replacing the campaign logo changes the artwork used by the current composition; it does not render every final image. A different-aspect replacement must fit the output’s placement constraints. Compositions place exact campaign copy above the logo, so intentional overlap is allowed. Final reference checks inspect only pixels visible after later layers.

Optional `framing.text_placements` has only `message` and `cta` entries. Each records normalized `left`, `top`, and wrapping `width`, integer `font_size`, `font_id` (`noto_sans` or `barlow_condensed`), and nullable palette-member `color`. Null color inherits `brand.text_color`. The renderer measures and reflows actual glyphs; the wrapping column is not a clipping box. Actual glyphs must fit the canvas. Missing entries preserve default pixels. Studio’s Text tool combines wording and appearance for the selected Headline or CTA. It moves and resizes each block independently, with exact preview and final PNGs sharing the same font, layers, and checks. Reset removes both overrides for that output without changing words, crop, or logo. Intentional overlap is allowed; fully covered blocks have no visible-reference check.

The `provenance.synthetic` flag means **test fixture**. Actual demonstration inputs, including explicitly identified AI-created fictional imagery, use `false` and describe their origin in `label` and `rights`. This flag does not establish photographic authenticity.

The current editing document is separate from this public import schema. It lives in the `document_json` column of SQLite’s `campaign_drafts` table and includes the Campaign fields, image layout and style settings, descriptions, source references, and a version token. There is one current document per campaign. The application does not write a working `Brief/draft.json` file or parallel plan/manifest JSON copies.

Campaign fields and Studio controls autosave. Typing waits for a short pause, and dragging saves after the gesture finishes. Routine saves are silent; a failed save shows a retry message, and a conflicting save asks the producer to refresh. Expected-version checks prevent an outdated tab from overwriting a newer save. Normal navigation and Download ZIP wait for pending saves. Autosave never triggers a paid image-generation request or creates a finished PNG. Handoff previews the current saved instructions; Download ZIP captures that version, renders and verifies the final images in temporary storage, checks the ZIP, and sends it to the browser. Individual PNG downloads render only that selected image. See the [architecture reference](architecture.md) for the complete save and delivery sequence.

Changes to an existing product’s fields autosave; Done closes that editor after pending saves finish. Adding a new product is a deliberate action: its fields remain temporary until Add, and Cancel, ×, or Escape discard that unadded product. Previously committed photos remain separately retained for source-mode restoration.

## Saved imagery and examples

The official launcher’s two included seasonal campaigns use ordinary briefs and owned attachments. [Autumn at Home (Autumn Winds)](../src/campaign_studio/resources/campaigns/autumn-winds/brief.json) has five finished-scene products and both markets; setup imports the sources and saved settings for 30 variations. These can be previewed immediately, and final PNGs are created when downloaded. [Winter at the Ranch (Bridle & Bough)](../src/campaign_studio/resources/campaigns/bridle-and-bough/brief.json) has two finished scenes and one missing concept, with 18 planned outputs. Its generation plan remains unconfirmed. Both start with zero application attempts; externally prepared imagery is identified in provenance.

To import those inputs separately, choose the extracted `brief.json` and attach its named logo and image files. There is no ZIP importer. Import allocates a fresh UUID even when another campaign has the same name. The initial bundled installation and a later ordinary import are separate campaign records.

An optional `replay.sources` map binds a product ID to an opaque saved master, expected SHA-256, historical model, generation date, and provenance. Normal curated replay covers every concept or prepared product. The optional `partial` mapping supports verified same-campaign reuse of some generated products while another product still needs its own explicitly planned request. Finished scenes are not mapped as generated masters, and unknown product IDs are rejected.

For a concept, `imagery_absent: true` describes its original state before the historical master existed. Missing, changed, unreadable, transparent, or hash-mismatched mapped masters block locally. They never trigger fallback generation. Copy, branding, and local framing retain verified imagery; an imagery-affecting change requires a reviewed plan.

The separate [HOLD CLI replay fixture](../src/campaign_studio/resources/replay/README.md) contains one genuine saved brush-roll concept master and one supplied chalk scene. Its historical request count is one; every replay makes zero new requests and creates no fabricated attempt records. Exported provenance omits private source paths and raw responses.

[Two-product](../examples/synthetic-2/brief.json) and [three-product](../examples/synthetic-3/brief.json) geometric examples support repeatable service checks. Their relative references are resolved by the sample commands inside the configured runtime; they are not photographic or generation-quality evidence.

## Text field limits

The brand name identifies the company; the campaign name identifies its project. For example, the bundled Autumn Winds brand has a campaign named **Autumn at Home**.

| Field | Maximum characters |
| --- | ---: |
| Campaign name | 25 |
| Brand name | 20 |
| Product name | 80 |
| Headline | 120 |
| Call to action | 60 |
| Audience | 500 |
| Creative direction, product description, or concept description | 1,000 |
| Origin label | 200 |
| Usage rights | 1,000 |
| Prohibited phrase | 256 |
| File reference | 4,096 |
| Replay model identifier | 200 |
| Replay history | 2,000 |

Motif counts Unicode code points consistently in the browser and server. An emoji such as 🦊 counts as one; a letter followed by a combining accent counts as two. Over-limit text is retained in the open form for correction and is not saved until shortened. Incomplete fields can still be saved as drafts. Text is never automatically truncated.

Campaign Copy & Rules flags prohibited words as you type. The current wording saves and appears in previews and Studio while you correct it. Removing one prohibited word updates the text everywhere and leaves warnings only for the words that remain. Export stays blocked until the wording is corrected or the rule is deliberately changed. Production prepares source photos independently of these finished-ad checks.

## Starting from an example

Choose **New campaign → Import JSON → Download example JSON**. The download is a filled-in brief that validates against the public model, not a JSON Schema. Adapt its values yourself or give it to an AI assistant with your campaign requirements. It includes both supported markets and a fictional product photo filename. Import it, then attach your actual photograph and optional logo through the application. Import assigns fresh campaign and product identities, never reads arbitrary file paths, and never starts a paid request. Manual creation and JSON import are two routes in the same dialog; switching routes retains your temporary entries and chosen file.
