"use strict";

// Readable disclosures are projections of the saved plan and attempt record.
// They never infer a provider charge, probe access, or alter request allowance.
let readiness = null;
let productionRecovery = null;
let ruleRecovery = null;
let retryContext = null;
let blockedProposal = null;
let productionPreparation = null;
let recoveryTarget = null;
const shownProductionFailures = new Set();
function activeRetry() {
  return retryContext &&
    retryContext.campaign === draft?.campaign_id &&
    retryContext.run_id === model?.run?.id &&
    retryContext.digest === model?.plan?.digest &&
    !model.unplanned_draft
    ? retryContext
    : null;
}
function activeBlockedProposal() {
  return blockedProposal?.campaign_id === draft?.campaign_id &&
    blockedProposal.version === model?.version
    ? blockedProposal
    : null;
}
function reconcilePreparedSources(before, after) {
  for (const product of before.products) {
    const prepared = after.products.find(item => item.id === product.id);
    if (!prepared) continue;
    const advance = source => {
      if (source?.path && source.path === product.source.path) source.path = prepared.source.path;
    };
    advance(projectBuffers.form_drafts.product_images?.[product.id]);
    if (modalProduct?.id === product.id) advance(modalProduct.source);
  }
}
function acknowledgePlanSources(before, result) {
  reconcilePreparedSources(before, result.project.authored.brief);
  reconcileAppliedBrief(draft, before, result.project.authored.brief);
  model.project = copy(result.project);
  model.version = result.version;
  projectQueue.applied(result.project);
  dirty = true;
  model.unplanned_draft = true;
}
function currentPreparation() {
  return productionPreparation?.campaign === draft?.campaign_id &&
    (productionPreparation.status === "pending" || productionPreparation.basis === JSON.stringify(currentBriefValue()))
    ? productionPreparation : null;
}
function productionNeedsRun() {
  const retainedSourceRecovery = !!model.local_recovery?.saved_result;
  return (!model.run?.id || (model.run.state === "complete" && !retainedSourceRecovery)) &&
    !!model.plan?.generation_products?.length;
}
async function boundedEstimateRequest(action, milliseconds = 30000) {
  const controller = new AbortController();
  let timer;
  const expired = new Promise((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("The estimate is taking longer than expected. Try again. No image generation has started."));
    }, milliseconds);
  });
  try { return await Promise.race([action(controller.signal), expired]); }
  finally { clearTimeout(timer); }
}
function ensureProductionPlan() {
  if (page !== "production" || modal || busy || !draft?.products.length || operation?.active ||
      currentPreparation()?.status === "pending" || getSaveState()?.problem) return;
  if (currentPreparation()?.status === "error") return;
  // An imported campaign can already have a composition preview plan without
  // a production run. Generation still needs its own confirmed run record.
  if (!dirty && model.plan && !productionNeedsRun() && !model.unplanned_draft) return;
  prepareCurrentPlan();
}
function productionCredentialState() {
  const code = operation?.notice_code || model?.notice?.code;
  const authentication = ["rejected_key", "access_denied"].includes(code);
  const last = (operation?.attempts || model?.run_attempts || []).at(-1);
  const attemptedAt = Date.parse(last?.created_at || "");
  const savedAt = typeof keySavedAt === "number" ? keySavedAt * 1000 : 0;
  const keyUpdated = authentication && keyAvailable && Number.isFinite(attemptedAt) && savedAt > attemptedAt;
  const keyReloaded = authentication && keyAvailable && Number.isFinite(attemptedAt) &&
    typeof keyLoadedAt === "number" && keyLoadedAt * 1000 > attemptedAt;
  return { credentialIssue: authentication && !keyUpdated && !keyReloaded ? code : !keyAvailable ? "missing_key" : null, keyUpdated, keyReloaded };
}
function sourceFindingBlocks(finding) {
  return finding.severity === "blocking" &&
    (["prepared_product", "scene_coverage"].includes(finding.rule) || finding.rule === "asset" && finding.item !== "logo");
}
function productionContext() {
  const activity = operation?.run_id === model.run?.id ? operation : null;
  const active = !!activity?.active;
  const retry = activeRetry(), proposal = activeBlockedProposal(), preparation = currentPreparation();
  const pending = !active && (dirty || model.unplanned_draft || !model.plan || productionNeedsRun() || !!proposal);
  // Product management always reflects the current campaign. Only the action
  // panel uses a reviewed plan; existing accepted images remain separate.
  const value = currentBriefValue();
  const plan = pending ? null : retry ? {
    ...model.plan, generation_products: retry.generation_products,
    cost_estimate: retry.cost_estimate,
  } : model.plan;
  const results = !pending && (!!model.composition_version || (!!model.run &&
    model.output_run_id === model.run.id && !["planned", "confirmed"].includes(model.run.state)));
  const outputs = model.manifest?.outputs || [];
  const attempts = activity?.attempts || model.run_attempts || [];
  const findings = proposal?.findings || (pending ? [] : plan?.findings) || [];
  const markets = Object.keys(value.markets).map(id => ({
    id, label: LANG[id === "es-MX" ? "es" : "en"],
  }));
  const factsCurrent = !dirty && model.concept_preview_version === model.version;
  const products = value.products.map(product => {
    const requested = plan?.generation_products.includes(product.id);
    const attempt = attempts.filter(item => item.product_id === product.id).at(-1);
    const assetFailed = findings.some(f => f.severity === "blocking" && f.rule === "asset" && f.context?.product_id === product.id);
    const concept = product.source.role === "concept";
    let image = concept ? factsCurrent ? model.concept_previews?.[product.id] || null : null : assetURL(product.source.path, product.id);
    let sourceLabel = concept ? image ? "Saved generated image" : "Image will be generated." : image ? "Photo supplied" : "Photo needed";
    if (assetFailed) { image = null; sourceLabel = "Replace the unavailable photo"; }
    else if (!pending && (active || results) && attempt) {
      if (attempt.source_available) {
        // A decoded response is counted immediately, but the current source URL
        // is published after source preparation finishes. Do not invent a URL
        // for the interval while another product's request is still running.
        image = model.source_urls?.[product.id] || image;
        sourceLabel = image ? "Saved generated image" : "Source image received";
      } else if (attempt.state === "dispatching" && active) sourceLabel = "Generating image…";
      else if (["unknown", "dispatching"].includes(attempt.state)) sourceLabel = "Result not confirmed";
      else if (attempt.response_recoverable) sourceLabel = "Result saved for recovery";
      else sourceLabel = "Image not created";
    }
    const saved = outputs.filter(output => output.product_id === product.id && output.url).length;
    const expected = outputs.filter(output => output.product_id === product.id).length || markets.length * FMT.length;
    return { id: product.id, name: product.name,
      description: concept ? product.source.description : product.description,
      mode: concept ? "concept" : "photo", image, sourceLabel,
      filename: product.source.path?.split("/").pop() || "", requested,
      outputSummary: model.composition_version
        ? saved ? `${saved} ${saved === 1 ? "preview" : "previews"}` : "No preview available"
        : saved ? saved === expected ? `${saved} saved ${saved === 1 ? "image" : "images"}` : `${saved} of ${expected} saved` : "No saved images",
    };
  });
  const supplied = products.filter(product => product.mode === "photo" && product.image).length;
  const reused = products.filter(product => product.mode === "concept" && product.image && !product.requested).length;
  const requests = plan?.generation_products.length || 0;
  const sourceSummary = plan ? [
    supplied ? `${supplied} ${supplied === 1 ? "photo supplied" : "photos supplied"}` : "",
    reused ? `${reused} saved ${reused === 1 ? "generated image" : "generated images"}` : "",
    requests ? `${requests} ${requests === 1 ? "image will" : "images will"} be generated` : "",
  ].filter(Boolean).join(" · ") : `${products.length} ${products.length === 1 ? "product" : "products"} selected`;
  return { value, products, markets, plan, pending, active, retry, proposal, preparation, results, sourceSummary,
    ...(requests || active ? productionCredentialState() : { credentialIssue: null, keyUpdated: false, keyReloaded: false }),
    resumeGeneration: !active && !!model.retry_plan, resumeAction: "resumeGeneration",
    blocked: findings.some(sourceFindingBlocks),
    total: products.length * markets.length * FMT.length,
    complete: results && products.length > 0 && products.every(product => !!product.image) && !findings.some(sourceFindingBlocks),
  };
}

async function reviewRetryPlan() {
  const campaign = draft.campaign_id;
  const result = await api(`/api/motif/campaigns/${campaign}`);
  if (campaign !== draft.campaign_id) return;
  // Refresh server facts without discarding a newer local brief draft.
  if (dirty) model = result;
  else useData(result);
  if (!result.retry_plan || dirty || result.unplanned_draft)
    throw new Error(
      "Review the edited brief before starting another attempt. Your draft is preserved.",
    );
  retryContext = { campaign, ...result.retry_plan };
  closeModal();
  page = "production";
  updateURL();
  render();
  scrollTo(0, 0);
}
async function startDisplayedPlan() {
  const context = productionContext();
  if (!model.run?.id || !model.plan || context.pending || context.blocked || context.active || currentPreparation()?.status === "pending")
    throw new Error("The current plan is not ready. Check the updated estimate before generating images.");
  if (Object.keys(displayedPlan()?.generation_requests || {}).length && !keyAvailable) {
    await openKeySetup();
    return;
  }
  const retry = activeRetry();
  const campaign = draft.campaign_id,
    run = model.run.id;
  const result = await api(`/api/campaigns/${campaign}/start`, {
    run_id: run,
    digest: retry?.digest || model.plan.digest,
    allowance: retry?.allowance ?? model.run.allowance,
    ...(retry ? { retry: true } : {}),
  });
  if (!isCurrentRun(campaign, run)) return;
  retryContext = null;
  if (result.active && result.run_id === run) {
    operation = result;
    if (page === "production") refreshProductionStatus();
  }
  if (result.disposition === "already_active") {
    operation = result;
    productionRecovery = {
      kind: "duplicate-start",
      campaign,
      run,
      message:
        "This confirmed run is already active. The existing operation retains ownership.",
    };
    openModal("duplicate-start");
    return;
  }
  await poll(campaign, run);
}
function showNewProductionFailure() {
  const code = operation?.notice_code || model?.notice?.code;
  const message = operation?.notice || model?.notice?.message;
  // Local capacity refusal belongs to the latest operation, not a paid attempt.
  // Reopening or deliberately retrying must still show its measured remedy.
  if (code === "storage_capacity")
    return showProductionIssue(code, message, operation?.capacity);
  const attempt = (model?.run_attempts || []).at(-1);
  const identity = JSON.stringify([
    C.id,
    model?.run?.id,
    attempt?.created_at,
    attempt?.error_kind,
    code,
  ]);
  if (!message || shownProductionFailures.has(identity)) return;
  if (showProductionIssue(code, message)) shownProductionFailures.add(identity);
}

function dataRow(label, value) {
  return `<div class="data-row"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
}
function displayedPlan() {
  return ["review", "handoff"].includes(page) && model?.output_plan
    ? model.output_plan
    : activeRetry()
      ? {
          ...model.plan,
          generation_requests: Object.fromEntries(
            Object.entries(model.plan.generation_requests).filter(([id]) =>
              activeRetry().generation_products.includes(id),
            ),
          ),
        }
      : model?.plan;
}
function settingsModal() {
  const plan = displayedPlan();
  const requests = Object.entries(plan?.generation_requests || {});
  return dialog(
    "Prompt and settings",
    (requests.length
      ? requests
          .map(([id, request]) => {
            const product = C.products.find((item) => item.id === id);
            return `<section><h3>${esc(product?.name || "Product")}</h3>${dataRow("Provider", "OpenAI Images API")}${dataRow("Model", request.model)}${dataRow("Method", product?.treatment === "concept" ? "Text-only concept image" : "Text-only empty setting")}${dataRow("Output", `${request.size} · ${request.quality} quality · ${request.background} · ${request.output_format.toUpperCase()}`)}${dataRow("Request limits", `${request.n} image · ${request.max_retries} automatic retries · ${request.deadline_seconds}s deadline`)}<div class="field"><label>Exact saved prompt assembled from the brief</label><pre class="small" style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(request.prompt)}</pre></div><div class="rule"></div></section>`;
          })
          .join("")
      : notice(
          "No new image request is in this plan.",
          "The imagery is reused locally; no prompt is sent.",
          "info",
        )) +
      `<p class="small muted">${requests.length ? "Sent: audience, scene direction, and the concept description where needed. Not sent: supplied product pixels, logo, or advertising copy. The credential authenticates the API request; it is never prompt content." : "Nothing leaves this computer for image generation."}</p><div class="rule"></div><p class="small muted">One source is shared across languages. Final composition stays local. A source, scene, appearance, or generation-method change needs a new reviewed plan.</p>`,
    btn("Close", "close"),
  );
}
function attemptsModal() {
  const selected = ["review", "handoff"].includes(page) && model?.manifest;
  const attempts = selected ? model.attempts || [] : model.run_attempts || [];
  const campaign = campaignMetricDisplay();
  const allowance = selected
    ? model.output_plan?.allowance
    : model.run?.allowance;
  const uncertain = attempts.some((item) =>
    ["unknown", "dispatching"].includes(item.state),
  );
  const states = {
    decoded: "Image received",
    received: "Response retained",
    failed: "Known failure",
    unknown: "Result not confirmed",
    dispatching: "Request dispatched",
  };
  const rows = attempts
    .map((attempt, index) => {
      const usage = Object.entries(attempt.usage || {})
        .filter(([, count]) => Number.isFinite(count))
        .map(([key, count]) => `${key.replaceAll("_", " ")}: ${count}`)
        .join(" · ");
      return `${dataRow(`Attempt ${index + 1} · ${C.products.find((p) => p.id === attempt.product_id)?.name || "Product"}`, states[attempt.state] || "Not reported")}${dataRow("Recorded at", new Date(attempt.created_at).toLocaleString())}${dataRow("Source receipt", attempt.source_available ? "Saved original verified" : attempt.response_recoverable ? "Image response retained; local decoding remains" : attempt.response_retained ? "Diagnostic or unverified response retained; no image verified" : "No saved result verified")}${dataRow("Recorded usage", `${usage || "Usage not recorded"}`)}<div class="rule"></div>`;
    })
    .join("");
  return dialog(
    "A useful record of the work.",
    `${dataRow("Campaign / revision", `${C.name} / ${selected ? C.revision : "current production run"}`)}${dataRow("This run · request allowance", `${attempts.length} used of ${allowance ?? "not recorded"}`)}${selected ? dataRow("Attempts at this revision", `${model.manifest.metrics.attempted_requests} used of ${model.manifest.metrics.request_allowance} allowance`) : ""}${uncertain ? notice("The request result was not confirmed.", "A charge may still apply. Saved work is preserved; nothing is resent automatically.") : ""}${campaign.detail ? `<p class="small muted"><strong>Campaign estimate:</strong> ${campaign.has_known_portion ? esc(campaign.spend_helper) + " " : ""}${esc(campaign.detail)}</p>` : ""}${rows || "<p>No image requests were dispatched in this saved run.</p>"}${dataRow("Local processing", selected && model.manifest.metrics.elapsed_processing_seconds != null ? `${model.manifest.metrics.elapsed_processing_seconds} seconds for this saved operation` : operation?.elapsed_seconds != null ? `${operation.elapsed_seconds} seconds for this operation` : "Not recorded for this operation")}<p class="small muted">Receipt availability and attempt outcomes describe the selected run’s current retained record; later recovery can update them. Exact prompts, settings, and retained responses remain in the private local record. Only sanitized provenance travels in a handoff. Usage supports an estimate, not a provider bill.</p>`,
    btn("View prompt context", "settings") + btn("Done", "close", "primary"),
  );
}
async function checkReadiness() {
  readiness = null;
  openModal("setup");
  try {
    const result = await api("/api/motif/readiness", {});
    readiness = result;
    keyAvailable = result.key_available;
    if (modal === "setup" || modal === "local-diagnostics")
      openModal(result.ready ? "setup" : "local-diagnostics");
  } catch {
    readiness = {
      ready: false,
      checks: [],
      message:
        "The local readiness check could not finish. Confirm the local service is running, then try again.",
    };
    if (modal === "setup") openModal("local-diagnostics");
  }
}
function storageDetails() {
  const d = readiness?.storage;
  if (!d) return "";
  const view = MotifStorage.getView("workspace", {
    location: d.location,
    usedBytes: d.used_bytes,
    budgetBytes: d.budget_bytes,
    freeBytes: d.free_bytes,
    reserveBytes: d.reserve_bytes,
  });
  // Keep the designer's storage block; real readiness rows below replace its fixtures.
  const first = view.body.indexOf('<div class="rule"></div>');
  const last = view.body.lastIndexOf('<div class="rule"></div>');
  return (
    view.body.slice(first, last) +
    `<p class="small muted">${esc(d.location_notice || "")}</p>`
  );
}
function setupModal() {
  const checks = readiness?.checks || [];
  if (modal === "local-diagnostics") {
    const failed = checks.filter((item) => !item.ready);
    return dialog(
      failed.some((item) => item.id === "font")
        ? "The local composition font needs attention."
        : "The local workspace needs attention.",
      `${notice(failed.map((item) => item.message).join(" ") || readiness?.message || "The local check could not finish.", "Saved campaigns are retained. No provider request is used for setup checks.", "block")}`,
      btn("Keep my work", "close") +
        btn("Check readiness again", "checkReadiness", "primary"),
    );
  }
  return dialog(
    "Your local workspace.",
    `${notice("Saved work stays available. Image generation is optional.", "Readiness checks are local and do not send a paid probe.", "info")}${checks.map((item) => dataRow(item.label, item.message)).join("") || dataRow("Local readiness", "Checking locally…")}${storageDetails()}${dataRow("Generation credential", keyAvailable ? "Configured locally; provider access not tested" : "Not configured")}<h3 style="margin:24px 0 12px">When you need new imagery</h3><ol class="help-list small"><li>When Production needs a key, choose Add your OpenAI API key. Saving locally activates it without restarting, but does not verify access or start an image request.</li><li>An explicit launch-environment key is managed outside Motif. Update that environment and restart; saved campaigns stay in the configured workspace.</li><li>Review the exact production plan and request allowance before starting.</li></ol><p class="small muted">Keep credentials out of briefs, exports, and screenshots. Missing or rejected credentials do not lock local reuse, correction, or handoff.</p>`,
    btn("Check readiness again", "checkReadiness") +
      btn("Continue with saved work", "close", "primary"),
  );
}
function productionRecoveryModal() {
  const kind = productionRecovery?.kind || modal;
  const titles = {
    "missing-key": "Missing image generation is not set up.",
    "rejected-key": "The credential was rejected.",
    "provider-unavailable": "The provider request did not finish.",
    "network-unavailable": "The provider cannot be reached.",
    "duplicate-start": "This production run is already active.",
  };
  const duplicate = kind === "duplicate-start";
  const credential = ["missing-key", "rejected-key"].includes(kind);
  return dialog(
    titles[kind],
    `${notice(productionRecovery?.message || "No image request was sent.", duplicate ? "This view follows the existing operation. A second request is not started." : "Your draft and saved images remain available. No automatic retry will occur.", "block")}<p class="muted">${credential ? "Configure the credential locally; never paste it into campaign content. A key is needed only for deliberately creating missing imagery." : duplicate ? "One active operation, one serial request stream." : "If a request was dispatched, a charge may still apply. Look for a saved result before reviewing another attempt."}</p>`,
    btn("Keep my work", "close") +
      btn(
        duplicate
          ? "Follow the active run"
          : credential
            ? "Open setup steps"
            : "Review saved work",
        duplicate ? "followRun" : credential ? "setup" : "partialReview",
        "primary",
      ),
  );
}
function showProductionIssue(code, message, capacity = null) {
  if (code === "storage_capacity")
    return showCapacity({ capacity }, "Completing this image operation");
  if (["rejected_key", "access_denied"].includes(code) &&
      (productionCredentialState().keyUpdated || productionCredentialState().keyReloaded)) return false;
  if (["missing_key", "rejected_key", "access_denied"].includes(code)) {
    openKeySetup(code !== "missing_key", code === "access_denied" ? message : null).catch(error => toast(error.message));
    return true;
  }
  const last = (model?.run_attempts || []).at(-1);
  const network = ["APIConnectionError", "APITimeoutError"].includes(
    last?.error_kind,
  );
  const kind =
    code === "missing_key"
      ? "missing-key"
      : ["rejected_key", "access_denied"].includes(code)
        ? "rejected-key"
        : ["unknown_request", "provider_failure", "billing_limit", "rate_limited"].includes(code)
          ? network
            ? "network-unavailable"
            : "provider-unavailable"
          : null;
  if (!kind) return false;
  productionRecovery = { kind, message, campaign: C.id, run: model?.run?.id };
  openModal(kind);
  return true;
}
async function recheckRules() {
  const owner = draft,
    sent = JSON.stringify(draft);
  try {
    const result = await withBriefPreviewPaused(() => api(
      `/api/motif/campaigns/${draft.campaign_id}/recheck`,
      {
        value: JSON.parse(sent),
        version: model.version,
        base_revision: model.base_revision,
        run_id: model.run?.id,
      },
    ), () => owner === draft);
    if (!result) return;
    if (owner !== draft) return;
    if (JSON.stringify(draft) !== sent) {
      model = result;
      baseline = JSON.stringify(result.value);
      dirty = true;
    } else {
      useData(result, { applied: true });
    }
    closeModal();
    render();
    showPendingImageCleanup();
  } catch (error) {
    if (owner !== draft) return;
    ruleRecovery = error.message;
    openModal("rules-recheck-blocked");
  }
}
