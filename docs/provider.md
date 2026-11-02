# Image generation, cost, and recovery

The application uses the official OpenAI Python SDK through the small `OpenAIImages` adapter. The SDK is the client library for making API requests; it does not own campaign planning, revisions, costs, or the local workflow.

## Request contract

The configured model snapshot is `gpt-image-2.5-flare-2026-09-08`. The saved request specifies one 1920 × 1920 opaque PNG at high quality, zero automatic retries, and a 120-second overall deadline. Exact parameters and instructions participate in the saved plan digest, so a confirmed plan cannot silently send different request settings.

For the demonstrated missing-image concept, the app sends text describing the subject, intended audience, and scene direction. Supplied finished photographs, logos, and exact advertising copy stay in local processing for this path. Generated imagery is an invented concept, not a claim of fidelity to an existing photographed product.

The October 29 demonstration generated one brush-roll source and reused a supplied chalk photograph. Local composition produced twelve campaign PNGs. A shared Spanish correction then changed six files without another image request, retaining the six English files unchanged.

## Cost estimate

The application’s dated October 29, 2026 pricing basis records $5 per million text-input tokens and $30 per million image-output tokens for the configured model. These are saved estimation assumptions, not a claim that the rates or account access have been freshly verified. The live demonstration reported 244 text-input tokens and 3,276 image-output tokens, giving a usage-based estimate of **$0.09950** for that request. This is not a confirmed account charge. [OpenAI pricing](https://developers.openai.com/api/docs/pricing#image-generation)

Before generation, the plan uses the selected settings and estimated token usage to show an approximate cost. The estimate scales with new source requests, not the final number of locally composed ads. Supplied-image reuse, copy correction, and local framing add no image-generation request. If a failed or uncertain request provides no usable usage information, the report explains why the estimate is incomplete and labels any known subtotal as partial. Zero calls remains $0.00; reported usable usage survives a later local decoding or writing failure. Estimates are not account charges.

## Deliberate requests and recovery

The runner records dispatch intent before calling the provider. Automatic SDK retries are disabled because the application owns attempt accounting. A timeout can leave the remote outcome uncertain; it does not prove that generation stopped or that no charge occurred.

The adapter saves returned material privately before decoding the image. If a valid image cannot be written locally, or later composition fails, local recovery can reuse that material without buying another generation. A response containing an unusable image retains its receipt and usage, but is not offered as reconstructable imagery. Failed and uncertain attempts remain recorded and consume allowance. A further paid attempt requires a deliberate reviewed action.

Missing, expired, revoked, or rejected credentials produce a useful next step. They do not remove saved campaigns or prevent compatible local correction and export, subject to ordinary source-integrity and prohibited-wording gates. A local server cannot know a key is accepted until the relevant provider operation checks it; it does not make background paid requests just to test credentials.

Mock-transport tests cover request serialization, duplicate starts, failure accounting, deadline cleanup, stop behavior, interrupted attempts, and saved-response recovery. Those tests establish application behavior, while the separately recorded live run establishes actual provider integration. Neither replaces inspection of the resulting images.

## Why one model

One provider keeps the prototype focused and explainable. The adapter separates provider-specific code from planning and composition, leaving room for another integration later without building a model-selection framework now. A second provider would bring different request contracts, image characteristics, failure cases, cost assumptions, and verification work.
