# Motif: from a brief to a finished campaign

Motif is a showcase of Bruce Bracken’s AI-native building and engineering expertise: rapid prototyping backed by full-stack technical breadth and depth. It brings creative direction and software engineering into the same workflow. A producer can import a brief, reuse photographs, generate a missing concept, arrange exact words and branding, and download a coordinated campaign. Five products, two supplied languages, and three formats produce 30 finished images.

Bruce Bracken directed the product, design, architecture, and development with AI collaboration. The initial working version took three and a half days. Subsequent work has strengthened editing reliability, preview performance, and recovery. This case study describes the engineering choices behind that progression.

## One photograph, many finished images

The central decision was to separate source generation from composition. OpenAI supplies a photograph when the producer confirms a request. Pillow adds the exact headline, call to action, and logo locally. Changing a Spanish headline or moving a logo therefore does not buy another image generation.

The editor saves instructions instead of flattening every adjustment into a new permanent PNG. Campaign and Studio read one current document in SQLite; source photographs and logos remain separate files. Download captures those inputs, renders the final PNGs, checks them, and packages an offline overview and manifest.

The [architecture diagram](architecture.md) follows those boundaries. The implementation divides responsibility among [campaign editing](../src/campaign_studio/authored.py), [image composition](../src/campaign_studio/compositions.py), [source production](../src/campaign_studio/runner.py), and [delivery](../src/campaign_studio/delivery.py).

## A save is more than writing a value

Two browser windows can load the same campaign and then edit it independently. Each save carries the version the window last received. Python holds a short save lock while it reads the current document, compares the version, and commits the replacement. An older window receives a conflict instead of overwriting newer work. The successful response returns the new version for the next save.

SQLite also groups related request records into transactions. Recording a paid attempt and its file ownership must succeed together before dispatch. The database transaction ends before the network wait. A database rollback cannot undo an OpenAI request already sent, so Motif records uncertainty and leaves another paid attempt to an explicit decision.

This design fits one producer and small campaigns. Its trade-off is a campaign-wide conflict boundary: two windows editing different products can still conflict. Shared team editing would need a more granular model.

## Keeping the canvas stable through asynchronous work

A difficult Studio defect appeared when the first logo was attached after the editor had opened. The displayed composite was available before the separate layers needed for dragging had finished loading. Switching to those unfinished layers could expose a white canvas and misplaced text.

The correction made readiness explicit. Studio keeps the existing image visible, decodes the photograph and copy layers, and only then enables the editable arrangement. Responses also have to belong to the current target and editing session. If loading fails, **Retry preview** restores the editing layers while preserving the attachment and placement.

That behavior was checked in real Chromium and WebKit sessions, including attachment, movement, failure, and recovery. Controller tests separately exercise late responses and layer ownership. A successful HTTP response alone would not have demonstrated that the canvas stayed usable.

The unified editor keeps one scene and save queue across all Studio tools. The photograph, logo, headline, and CTA stay loaded while selecting an element changes its controls. Moving a layer draws locally, and its format thumbnail copies the completed canvas instead of waiting for a server-rendered replacement. Exact wording and font changes still use Pillow's text layout. A save that finishes late updates the saved baseline without overwriting a newer gesture or sentence.

## Measuring the local preview cost

Local files still require work: decoding images, measuring text, planning crops, composing pixels, and encoding previews. Repeating that work for every gallery request made a small campaign feel slow.

Motif uses bounded in-memory reuse. Before using a cached plan or image, Motif checks the current instructions, file ownership, asset contents, and selected font contents. Export still performs its own strict checks. Faster lossless PNG compression reduces temporary layer encoding work.

Matched local Chromium observations on the bundled five-product Autumn campaign changed as follows in the first cache pass, before the persistent-scene work:

| Interaction | Before | After |
| --- | ---: | ---: |
| Initial Studio gallery | 2.878s | 0.822s |
| Gallery reload | 2.801s | 0.523s |
| First Text tool | 0.952s | 0.314s |
| First Logo tool | 0.921s | 0.358s |
| Edit to saved preview and thumbnails | 2.390s | 1.506s |

These are individual matched fixture observations on one computer, not cross-device percentile guarantees. The intentional one-second typing pause before autosave remains. A separate 33-image comparison found identical decoded RGBA pixels and dimensions; the faster encoding increased combined preview bytes by about 2.37%. [Verification notes](verification.md) describe the method, Winter comparison, and limits.

Gallery work then reduces both payload and repeat decoding: small format images use a 160-pixel maximum edge, larger gallery images use 640, and unchanged decoded images survive view changes. The visual identity includes the source content, wording, layout, language, and branding rather than treating every save-version change as a new picture. This reuse is for display; the server still checks current instructions and owned sources before saving or exporting.

## Verification at the boundaries

Python tests exercise validation, storage, source integrity, provider failures, and rendered outputs. Node’s built-in test runner checks JavaScript controllers and response ordering. Playwright journeys then exercise the actual controls in Chromium and WebKit, including delayed responses, failed saves, and export recovery. Fresh ZIP installations verify the package and README commands separately from the development checkout.

Provider tests use controlled transport and dummy credentials. Historical live-generation checks are identified separately; replaying their saved imagery does not count as a new provider call. Download checks inspect image dimensions, hashes, decoded pixels, ZIP contents, and an offline report opened outside the running application.

## Deliberate scope

Python, FastAPI, plain JavaScript, Pillow, and SQLite keep installation to one application without a frontend build or separate database server. That still requires full-stack coordination: visible browser state, asynchronous requests, transactional persistence, provider accounting, image processing, and portable delivery.

Motif serves one producer on a local computer. Authentication, shared editing, hosted workers, publishing, and scheduling would introduce different operating requirements. The current work concentrates on making its complete creative workflow reliable and understandable.
