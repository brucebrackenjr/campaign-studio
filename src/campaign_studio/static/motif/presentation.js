"use strict";
// Selected Motif markup; data and mutations are owned by the local service.
function shell(content) {
  const focused =
    page === "review" && reviewTab === "product" && C.products.length > 0;
  return `<header class="topbar">
<div class="motif-brand"><a href="#library" class="wordmark" data-action="library" aria-label="Motif – Campaign Studio home"><i class="mark" aria-hidden="true"></i>Motif</a><span class="motif-descriptor">Campaign Studio</span></div>
<div class="top-context">${
    page !== "library"
      ? `<button data-action="campaigns" aria-label="Select a saved campaign" title="${esc(draft?.name || "Untitled campaign")}">${esc(draft?.name || "Untitled campaign")} <span aria-hidden="true">⌄</span>
</button>`
      : ""
  }</div>
<div class="mf-workspace-actions">
<button class="local" data-action="setup">
<i class="dot">
</i>Local workspace</button>
<button class="mf-neutral-mark" data-action="setup" aria-label="Workspace setup"><img src="/static/motif/assets/motif-mark.svg" alt="Motif"></button>
</div>
</header>${
    page !== "library" && !focused
      ? `<nav class="workflow" aria-label="Campaign workflow">${[
          ["campaign", "Campaign"],
          ["production", "Production"],
          ["review", "Studio"],
          ["handoff", "Handoff"],
        ]
          .map(
            (
              [p, t],
              i,
            ) => `<button class="${page === p ? "active" : ""}" data-action="nav:${p}" ${page === p ? 'aria-current="page"' : ""}>
<span class="step-no">0${i + 1}</span>${t}</button>`,
          )
          .join("")}</nav>`
      : ""
  }<main id="main" class="${["production", "review"].includes(page) ? "pr-surface" : ""}" tabindex="-1"><div id="project-save-recovery"></div>${content}</main>
${
  focused
    ? ""
    : `<footer class="bottom-meta">
<span>Made for the work between the idea and the final file.</span>
<span>
<span>Local workspace</span>
</span>
</footer>`
}`;
}
function head(title, sub = "", actions = "", eyebrow = "", extraClass = "") {
  return `<div class="page-head ${esc(extraClass)}">
<div>${eyebrow ? `<div class="eyebrow">${eyebrow}</div>` : ""}<h1>${esc(title)}</h1>${sub ? `<p>${esc(sub)}</p>` : ""}</div>
<div class="head-actions">${actions}</div>
</div>`;
}
// This renderer consumes one ordinary workbench entry; it owns no state or I/O.
function homeHero(latest) {
  const distinction = latest ? campaignDistinction(latest) : "";
  const identity = latest
    ? [latest.name || "Untitled campaign", distinction].filter(Boolean).join(" · ")
    : "";
  const placeholder = !latest
    ? "Your next idea belongs here."
    : !latest.products
      ? "Awaiting products"
      : workbenchEmptyImage(latest);
  const artwork = latest?.products && latest.thumbnail
    ? `<img class="creative-image" data-workbench-thumbnail src="${esc(latest.thumbnail)}" alt="${esc(latest.thumbnail_label || `Campaign preview for ${latest.name}`)}">`
    : `<span class="awaiting-art"><i class="mark" aria-hidden="true"></i><span>${esc(placeholder)}</span></span>`;
  return `<section class="hero home-continuation">
<div class="hero-copy">
<span class="eyebrow">${latest ? "YOUR MOST RECENT CAMPAIGN" : "YOUR NEXT CAMPAIGN"}</span>
<h1>Make the<br>next move.</h1>
${latest
    ? `<p class="home-campaign-name" title="${esc(identity)}"><span class="home-campaign-title">${esc(latest.name || "Untitled campaign")}</span>${distinction ? `<span class="small muted home-campaign-distinction">${esc(distinction)}</span>` : ""}</p>`
    : `<p>Bring your products, your words, and your point of view.</p>`}
<div class="row home-continue-row">${latest
    ? `${btn("Continue working", "openCampaign", "dark", `data-campaign-id="${esc(latest.id)}" aria-label="${esc(`Continue working on ${identity}`)}"`)}<span class="small muted">Pick up your most recent campaign.</span>`
    : btn("Create campaign", "new", "dark") + btn("Import brief", "import")}</div>
</div>
<div class="hero-art" aria-label="${latest ? esc(`Preview of ${identity}`) : "Your campaign workspace"}">
<div class="art-card home-art-card"><div class="home-art-image">${artwork}</div><div class="caption">${latest ? "CAMPAIGN PREVIEW" : "YOUR WORKSPACE"}</div></div>
<div class="hero-stamp" role="img" aria-label="From idea to handoff"><span class="stamp-lines"><span>FROM</span><span>IDEA TO</span><span>HANDOFF</span></span><span class="stamp-arrow" aria-hidden="true">↗</span></div>
</div>
</section>`;
}

function workbenchCard(c) {
  const id = esc(c.id);
  const identity = [c.name, campaignDistinction(c)].filter(Boolean).join(" · ");
  const art = !c.products
    ? '<span class="awaiting-art"><i class="mark" aria-hidden="true"></i><span>Awaiting<br>products</span></span>'
    : c.thumbnail
      ? `<img data-workbench-thumbnail src="${esc(c.thumbnail)}" alt="${esc(c.thumbnail_label || `${c.brand} campaign artwork`)}">`
      : `<span class="awaiting-art">${workbenchEmptyImage(c)}</span>`;
  const state = c.damaged_images || c.outcome_unknown ? "warning" : c.status === "complete" ? "" : "neutral";
  return `<div class="workbench-item">
    <button class="campaign-card" data-action="openCampaign" data-campaign-id="${id}" title="${esc(identity)}">
      <span class="campaign-image">${art}${campaignDistinctionTag(c)}</span>
      <span class="info">
        <span class="eyebrow">${esc(c.brand || "New brand")}</span>
        <strong class="workbench-title">${esc(c.name)}</strong>
        <span class="small muted">${c.products} ${c.products === 1 ? "product" : "products"} · ${c.markets} ${c.markets === 1 ? "market" : "markets"} · ${workbenchCounts(c)}</span>
        <span class="workbench-footer"><span class="small muted">${esc(activityLabel(c.updated))}</span>${badge(workbenchStatus(c), state)}</span>
      </span>
    </button>
    <button type="button" class="workbench-delete" data-action="storage:card-delete:${id}" aria-label="${esc(`Delete campaign: ${identity}`)}" title="${esc(`Delete campaign: ${identity}`)}">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/></svg>
    </button>
  </div>`;
}

function library() {
  let cards = workbenchCampaigns();
  return `${head("Good work starts here.", "Creative automation for social campaigns. Your next idea starts here.", cards.length ? btn("Import brief", "import") + btn("＋ New campaign", "new", "primary") : "", "YOUR WORKSPACE")}${homeHero(cards[0])}<div class="section-title"><h2>On your workbench <span class="muted small">${cards.length} saved ${cards.length === 1 ? "campaign" : "campaigns"}</span></h2><div class="workbench-tools"><span class="small muted" title="Campaigns are ordered by their most recent saved change, review, or completed run.">Recent activity</span>${pendingDeletionControl()}${cards.length ? `<button class="workbench-arrow" data-action="workbench:previous" aria-label="Previous campaigns" aria-controls="workbench-track">←</button><button class="workbench-arrow" data-action="workbench:next" aria-label="Next campaigns" aria-controls="workbench-track">→</button>` : ""}</div></div>${cards.length ? `<div class="campaign-grid" id="workbench-track" role="region" aria-roledescription="carousel" aria-label="Saved campaigns, most recent activity first. Use arrow keys to scroll." tabindex="0">${cards.map(workbenchCard).join("")}<button class="campaign-card" data-action="new" style="display:grid;place-content:center;text-align:center;min-height:280px;background:transparent;border-style:dashed"><div class="display" style="font-size:56px;color:#dd442d">＋</div><h3>Something new?</h3><p class="small muted" style="padding:8px 25px">Bring your products, your words,<br>and your point of view.</p></button></div>` : `<div class="empty"><div class="empty-symbol">↗</div><h2>Your next campaign belongs here.</h2><p>Create a campaign or import a brief above. Your saved work will appear here.</p></div>`}`;
}
function brief() {
  return '<div id="brief-root"></div>';
}
function dialog(title, body, foot = "", wide = false, eyebrow = null) {
  return `<div class="overlay">
<section class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
<header class="modal-head">
<div>
<span class="eyebrow">${esc(eyebrow ?? (modal === "new" ? "New campaign" : C.name || "MOTIF / CAMPAIGN STUDIO"))}</span>
<h2 id="dialog-title" style="margin-top:8px">${title}</h2>
</div>
<button class="close" data-action="close" aria-label="Close dialog">×</button>
</header>
<div class="modal-body">${body}</div>${foot ? `<footer class="modal-foot">${foot}</footer>` : ""}</section>
</div>`;
}

function modalView() {
  switch (modal) {
    case "draft-wording":
      return draftWordingView();
    case "new":
      return dialog(
        "What are we making?",
        `${input("Campaign name", "new-name", "", "text", "A name you will recognize in saved work.")}${input("Brand name", "new-brand", "")}${area("Audience", "new-audience", "")}`,
        `<span class="small muted">Start with a draft. Add the rest as you go.</span>${btn("Create campaign →", "create", "primary")}`,
      );
    case "product":
      return productEditorView();
    case "product-cap":
      return MotifProducerRepair.capDialog();
    case "source": {
      const p = modalProduct || C.products[currentProduct];
      return dialog(
        "Look closely at the source.",
        `<div class="large-inspector">
<div class="lightbox">
<img class="creative-image" src="${esc(modalProduct?.image || sourceArt(currentProduct))}" alt="${esc(p.name)} source">
</div>
<div>
<h3>${esc(p.name)}</h3>
<div class="data-row">
<span>Treatment</span>
<b>${treatment(p.treatment)}</b>
</div>
<div class="data-row">
<span>Origin</span>
<b>${esc(p.origin || "Unspecified")}</b>
</div>
<div class="data-row">
<span>Rights</span>
<b>${esc(p.rights || "Unspecified")}</b>
</div>
<div class="data-row">
<span>Original</span>
<b>${p.image ? "Preserved" : p.treatment === "concept" ? "Not generated yet" : "Not attached"}</b>
</div>
<div class="rule">
</div>
${btn(p.image ? "Replace source" : p.treatment === "concept" ? "Edit concept" : "Attach source", `product:${p.id}`, "small", 'style="margin-top:20px"')}</div>
</div>`,
        "",
        true,
      );
    }
    case "rename":
      return dialog(
        "A better name for the work.",
        input("Campaign name", "rename-name", draft.name),
        `<span class="small muted">Close keeps pending edits. Cancel edits discards this dialog’s draft.</span>${btn("Cancel edits", "cancelEdits")}${btn("Rename campaign", "saveName", "primary")}`,
      );
    case "rule":
      return MotifWording.ruleForm({
        phrase: C.rules[lang][ruleIndex]?.phrase || "", language: LANG[lang],
        existing: ruleIndex >= 0, returnLabel: {review: "Studio", handoff: "Handoff", production: "Production", campaign: "Campaign"}[wordingReturn?.page || page],
        hasImages: !!model?.manifest,
      });
    case "unsaved":
      return dialog(
        "Keep the work you’re shaping.",
        `${navigationError ? notice(navigationError, "Your remaining edits are retained. Nothing has been sent for generation.", "block") : ""}<p class="muted">There is unsaved work in this campaign. You can save it before leaving, discard it, or stay here.</p><p class="muted">Discard and leave restores the last confirmed save. Pending edits in that save are kept.</p>
<div class="rule">
</div>${dirty ? "<p>Campaign fields have unsaved changes.</p>" : ""}${hasStoredProductDraft() ? "<p>The open product form has unsaved changes.</p>" : ""}`,
        `${btn("Discard and leave", "discardLeave", "danger")}${btn("Keep editing", "close")}${btn("Save and leave", "saveLeave", "primary")}`,
      );
    default:
      return "";
  }
}

function unavailableSource() {
  return operation?.notice_code === "source_unavailable";
}
function sourceRecoveryNotice() {
  return notice(
    "A saved source needs attention.",
    operation.notice,
    "block",
    "nav:production",
    "Open Production to replace the source",
  );
}
function production() {
  const context = productionContext();
  const { plan, pending, active, retry, proposal } = context;
  if (active || (!pending && !retry && ["partial", "paused", "stopped"].includes(model.run.state)))
    return progressView(context);
  const notices = `${retry ? notice("This is the revised retry plan for the same saved run.", `${retry.previous_attempts} previous attempts remain recorded. ${plan.generation_products.length} new requests are proposed; cumulative allowance is ${retry.allowance}. The new-request estimate excludes earlier attempts.`) : ""}
    ${unavailableSource() ? sourceRecoveryNotice() : ""}${wordingNotice()}${planInputErrors(proposal?.findings || plan?.findings || [])}
    ${model.notice ? notice(model.notice.message, "Saved work remains available. No automatic retry occurs.", "block", model.notice.code === "missing_key" ? "missingKey" : ["rejected_key", "access_denied"].includes(model.notice.code) ? "updateKey" : "attempts", model.notice.code === "missing_key" ? "Add OpenAI API key" : ["rejected_key", "access_denied"].includes(model.notice.code) ? "Update API key" : "Attempt details") : ""}`;
  const done = !pending && !retry && !["planned", "confirmed"].includes(model.run.state);
  let operationHtml = "";
  if (done && !context.complete) operationHtml = `<div class="pr-eyebrow">SAVED IMAGES</div><h2>${damagedFiles() ? "Saved images need recovery." : rulesOnlyResult() ? "Images are withheld by current rules." : "Some images need attention."}</h2><p>${model.manifest?.outputs.filter(output => output.url).length || 0} of ${context.total} images saved.</p>
    ${btn("Open Studio →", "nav:review", "pr-button pr-primary pr-wide")}
    ${canRecoverCurrentOutputs() ? `${canRecoverSingleSavedOutput() ? '<p>Rebuild the missing format using its saved product image. No new AI request is needed.</p>' : ""}${btn(canRecoverSingleSavedOutput() ? "Recover missing image →" : "Recover saved sources · 0 calls", "recover", "pr-button pr-wide")}` : ""}
    ${model.retry_plan ? btn("Review retry plan", "retryPlan", "pr-button pr-wide") : ""}
    <div class="pr-rule"></div><p>No new request starts automatically from this saved run.</p>${Object.keys(model.plan?.generation_requests || {}).length ? btn("Exact prompt & settings", "settings", "pr-button pr-wide") : ""}`;
  return productionView(context, operationHtml, notices);
}

function emptyPage(title, text, action = "nav:campaign", label = "Open Campaign") {
  return `${head(title, text)}<div class="empty"><div class="empty-symbol">↗</div>${btn(label, action, "primary")}</div>`;
}
function planInputErrors(findings = model?.plan?.findings || []) {
  const seen = new Set();
  return findings.map((finding, index) => {
    if (finding.severity !== "blocking" || finding.context?.phrase) return "";
    const text = [finding.evidence, finding.remedy].filter(Boolean).join(" ");
    if (seen.has(text)) return "";
    seen.add(text);
    const field = finding.context?.fields?.[0];
    return notice("Correct this input before production.", text, "block",
      field ? `planInput:${index}` : "nav:campaign",
      field?.path[0] === "products" ? "Edit product" : "Open Campaign");
  }).join("");
}

function progressView(context = productionContext()) {
  const active = context.active;
  const currentOutputs =
    model.output_run_id === model.run.id ? model.manifest?.outputs || [] : [];
  const completed =
    active ? operation.completed ?? currentOutputs.filter((item) => item.url).length
      : currentOutputs.filter((item) => item.url).length;
  const expected = active ? operation.total ?? model.plan.outputs : model.plan.outputs;
  const attempts = operation?.attempts || model.run_attempts || [];
  const attempted = operation?.attempted ?? attempts.length;
  // A retry can increase allowance after this page's model was loaded. Keep
  // the line on one received status snapshot rather than mixing both records.
  const allowance = operation?.attempted != null && operation?.remaining != null
    ? operation.attempted + operation.remaining : model.run.allowance;
  const remaining = operation?.remaining ?? Math.max(0, allowance - attempted);
  const unknown = attempts.some((item) => item.state === "unknown" || (!active && item.state === "dispatching"));
  const dispatching = active && attempts.some(item => item.state === "dispatching");
  const stopped = model.run.state === "stopped";
  const recoverable = canRecoverCurrentOutputs();
  const rulesOnly = rulesOnlyResult();
  const retained = recoverable && model.local_recovery.saved_result;
  const singleLocalOutput = canRecoverSingleSavedOutput();
  const available = model.manifest?.outputs.filter((item) => item.url) || [];
  const noticeText = operation?.notice || model.notice?.message;
  const stateLabel = active
    ? "In progress"
    : unknown
      ? "Result not confirmed"
      : stopped
        ? "Stopped"
        : rulesOnly ? "Withheld" : "Paused";
  const title = active
    ? "Creating campaign images…"
    : unknown
      ? "The image request could not be confirmed."
      : stopped
        ? "Production stopped."
        : rulesOnly ? "Images are withheld by current rules." : "Some images need attention.";
  const body = `${wordingNotice()}${!active && retained && completed < expected ? notice("A provider result is already saved.", "A saved response is available for local recovery. No new image request is sent.", "block", "recover", "Recover saved imagery · 0 calls") : ""}${unknown ? notice("The request may have completed and may be charged.", `${attempted} of ${allowance} allowance used. Check for a saved result first. Nothing will be resent automatically.`, "", recoverable ? "recover" : null, recoverable ? "Look for saved result" : null) : noticeText ? notice(noticeText, "Saved work is retained. Nothing is resent automatically.", "block", ["rejected_key", "access_denied"].includes(operation?.notice_code || model.notice?.code) ? "updateKey" : "attempts", ["rejected_key", "access_denied"].includes(operation?.notice_code || model.notice?.code) ? "Update API key" : "Attempt details") : stopped ? notice("No further requests will be sent.", "An in-flight request may still finish and incur a charge. Completed files and any arriving result are retained.", "info") : ""}${unavailableSource() ? sourceRecoveryNotice() : ""}`;
  const card = `<div class="pr-eyebrow">COMPLETED / EXPECTED</div><div class="pr-operation-count">${completed} <small>/ ${expected} images</small></div>
    ${badge(stateLabel, active ? "neutral" : "warning")}
    <div class="pr-operation-progress"><i style="width:${expected ? Math.min(100, (completed / expected) * 100) : 0}%"></i></div>
    <p class="pr-operation-detail">${operation?.elapsed_seconds != null ? `${operation.elapsed_seconds}s` : model.output_run_id === model.run.id && model.manifest?.metrics.elapsed_processing_seconds != null ? `${model.manifest.metrics.elapsed_processing_seconds}s` : "Not recorded"} · This run · elapsed</p>
    <p class="pr-operation-detail">${allowance === 0 ? "No image requests in this run." : `This run: ${attempted} of ${allowance} requests used · ${remaining} remaining.${unknown ? " The request result was not confirmed. A charge may still apply." : dispatching ? " Image request in progress." : " This count is not a confirmed charge."}`}</p>
    ${active ? `<div class="pr-rule"></div><p class="pr-operation-detail">${esc(operation.stage || "Preparing images")}</p>${operation.stoppable ? btn("Stop unsent work", "stop", "pr-button pr-wide") : ""}` : ""}
    <div class="pr-rule"></div><p class="pr-operation-detail">${model.output_run_id && model.output_run_id !== model.run.id ? "Previously created images remain available in Studio." : "Available images remain inspectable while unfinished work is resolved."}</p>
    ${btn("Review available images", "partialReview", "pr-button pr-primary pr-wide", available.length ? "" : "disabled")}
    ${!active ? `${singleLocalOutput ? '<p class="pr-operation-detail">Rebuild the missing format using its saved product image. No new AI request is needed.</p>' : ""}${btn(singleLocalOutput ? "Recover missing image →" : "Recover saved imagery · 0 calls", "recover", "pr-button pr-wide", !recoverable || unavailableSource() ? "disabled" : "")}
      ${btn("Attach a replacement", "nav:products", "pr-button pr-wide")}${btn("Review a new attempt", "retryPlan", "pr-button pr-wide", !model.retry_plan ? "disabled" : "")}
      ${btn("Hand off available images", "nav:handoff", "pr-button pr-wide", available.length ? "" : "disabled")}` : ""}
    ${allowance > 0 ? '<p class="pr-operation-detail">A new attempt needs a reviewed plan and available allowance. Failed or interrupted requests may still incur charges.</p>' : ""}`;
  return productionView(context, `<h2>${esc(title)}</h2>${body}${card}`);
}

function currentRuleWithholding() {
  const identified = new Set(model.rule_withheld_outputs || []);
  return [...new Set((model.manifest?.outputs || [])
    .filter(item => !item.url && identified.has(item.id)).map(item => item.id))];
}
function otherUnavailableOutputs() {
  const withheld = new Set(currentRuleWithholding());
  return (model.manifest?.outputs || []).filter(item => !item.url && !withheld.has(item.id));
}
function nonRuleOmissions() {
  return otherUnavailableOutputs().length > 0;
}
function canRecoverCurrentOutputs() {
  return !operation?.active && !unavailableSource() &&
    !!model.local_recovery?.output_ids?.length &&
    (!model.manifest || model.output_run_id === model.run?.id) &&
    (!operation?.run_id || operation.run_id === model.run?.id);
}
function canRecoverSingleSavedOutput() {
  return canRecoverCurrentOutputs() && !model.local_recovery.saved_result &&
    model.local_recovery.output_ids.length === 1;
}
function rulesOnlyResult() {
  const attempts = operation?.attempts || model.run_attempts || [];
  return currentRuleWithholding().length > 0 && !nonRuleOmissions() &&
    !damagedFiles() && !unavailableSource() && !canRecoverCurrentOutputs() && !operation?.active &&
    !operation?.notice && !model.notice && model.run?.state !== "stopped" &&
    !attempts.some(item => ["unknown", "dispatching"].includes(item.state));
}
function partialExportOmissions() {
  const withheld = currentRuleWithholding().length, other = otherUnavailableOutputs().length;
  if (!withheld)
    return `This saved version has ${model.manifest.outputs.length - model.manifest.metrics.completed_images} missing images.`;
  return `This saved version has ${withheld} ${withheld === 1 ? "image" : "images"} withheld by current rules${other ? ` and ${other} other unavailable ${other === 1 ? "image" : "images"}` : ""}.`;
}
function savedResultNotice() {
  const rules = wordingNotice();
  if (unavailableSource()) return rules;
  if (model.manifest?.complete && !damagedFiles())
    return notice("Saved images are ready.", "Review, correct, or hand off the saved version.", "info", "nav:review", "Review saved images");
  if (model.notice || rulesOnlyResult()) return rules;
  return rules + notice("This saved run needs attention.", canRecoverCurrentOutputs()
    ? "Recover saved sources locally. A separate retry review is required before any new request."
    : "Review the available images. No new request starts automatically.", "info", "nav:review", "Review saved images");
}
function review() {
  if (!C.products.length)
    return emptyPage("The work will live here.", "Add your first product and review a production plan.", "nav:production", "Add products in Production");
  const notices = `${unavailableSource() ? sourceRecoveryNotice() : damagedFiles() ? notice("Saved image files need local recovery.", "Missing or changed files cannot be exported. Local recovery first verifies the saved sources before rebuilding outputs.", "block", "nav:production", "Open recovery") : ""}${wordingNotice()}${model.manifest && !model.manifest.complete && !unavailableSource() && nonRuleOmissions() ? notice("This is a partial saved version.", "Available images can be reviewed and exported. Recover locally before considering a deliberate retry.", "info", "nav:production", "Review recovery options") : ""}`;
  if (reviewTab === "activity")
    return `${head("Production record", "Saved requests, elapsed time, and estimates.", btn("← All products", "reviewTab:products"))}${activityView()}`;
  return notices + studioView();
}


function allEntries() {
  const a = [];
  C.markets.forEach((l) =>
    FMT.forEach((f, j) =>
      C.products.forEach((p, i) =>
        a.push({ p: i, l, f: j, key: `${p.id}-${l}-${j}` }),
      ),
    ),
  );
  return a;
}
function overviewView(exporting = false) {
  let entries = allEntries().filter(
    (e) =>
      filter === "all" ||
      (filter === "missing" && !outputFor(e.p, e.l, e.f)?.url),
  );
  const per = 6,
    max = Math.max(1, Math.ceil(entries.length / per));
  pageNum = Math.min(pageNum, max - 1);
  return `<div class="review-toolbar">
<span class="small muted">${entries.length} ${filter === "all" ? "expected" : "matching"} creatives · ${C.products.length} products · all selected markets</span>
<div class="review-filter">
<select id="overview-filter" aria-label="Filter creative overview">
<option value="all" ${filter === "all" ? "selected" : ""}>All creatives</option>
<option value="missing" ${filter === "missing" ? "selected" : ""}>Unavailable outputs</option>
</select>
</div>
</div>
<div class="overview-grid">${entries
    .slice(pageNum * per, (pageNum + 1) * per)
    .map((e) => {
      const output = outputFor(e.p, e.l, e.f), missing = !output?.url;
      const firstImages = !model.manifest;
      const withheld = missing && currentRuleWithholding().includes(output?.id);
      return `<article class="overview-item">${
        missing
          ? `<div class="missing-tile">
<strong>${firstImages ? "Ready for first images" : withheld ? "Image withheld by current rules." : "Image not available"}</strong>
<p class="small">${esc(firstImages ? "Review the production plan to create this image." : withheld ? "Correct the prohibited wording above to update this image." : unavailableSource() ? "Replace the unavailable source in Production before rebuilding this image." : outputFor(e.p, e.l, e.f)?.omission || "This saved output is unavailable. Open Production to review recovery options.")}</p>
</div>`
          : `<div class="overview-image">${creative(e.p, e.l, e.f, `data-action="inspect:${e.p}:${e.f}:${e.l}" tabindex="0" role="button"`)}</div>`
      }<div class="info">
<h3>${esc(C.products[e.p].name)}</h3>
<p class="small muted">${LANG[e.l]} · ${FMT[e.f].name} ${FMT[e.f].ratio}</p>
<div class="row between">${missing ? firstImages ? btn("Review production plan", "plan", "small quiet") : badge(withheld ? "Withheld" : "Missing", "block") : ""}${missing ? "" : btn(exporting ? "Download PNG" : "Inspect", exporting ? `download:${e.p}:${e.l}:${e.f}` : `inspect:${e.p}:${e.f}:${e.l}`, "small quiet", exporting ? `data-wording-export="available" ${wordingExportBlocked() ? "disabled" : ""}` : "")}</div>
</div>
</article>`;
    })
    .join(
      "",
    )}</div>${entries.length === 0 ? '<div class="empty"><h2>No creatives match this filter.</h2><p>Choose All creatives to see the complete campaign.</p></div>' : ""}<div class="pagination">
<span class="small muted">Page ${pageNum + 1} of ${max} · ${entries.length ? `${pageNum * per + 1}–${Math.min(entries.length, (pageNum + 1) * per)}` : "0"} of ${entries.length}</span>
<div class="row">${btn("← Previous", "prevPage", "small", pageNum === 0 ? "disabled" : "")}${btn("Next →", "nextPage", "small", pageNum >= max - 1 ? "disabled" : "")}</div>
</div>`;
}
function activityView() {
  const campaign = campaignMetricDisplay();
  const metrics = model.manifest?.metrics;
  const elapsed =
    metrics?.elapsed_processing_seconds ?? (operation?.run_id === model?.run?.id ? operation?.elapsed_seconds : null);
  return `<div class="metric-grid">
<div class="metric">
<div class="mini-number">${model.manifest?.metrics.completed_images ?? 0} / ${model.manifest?.outputs.length ?? model.plan?.outputs ?? 0}</div>
<div>Current images · available / expected</div>
</div>
<div class="metric">
<div class="mini-number">${campaign.attempts}</div>
<div>Campaign generation attempts${campaign.attempts_helper ? `<p class="small muted">${esc(campaign.attempts_helper)}</p>` : ""}</div>
</div>
<div class="metric">
<div class="mini-number">${elapsed != null ? `${elapsed}s` : "Not recorded"}</div>
<div>This operation · processing</div>
</div>
<div class="metric">
<div class="mini-number">${esc(campaign.spend)}</div>
<div>Campaign spend estimate${campaign.spend_helper ? `<p class="small muted">${esc(campaign.spend_helper)}</p>` : ""}</div>
</div>
</div>
<div class="stack" style="margin-top:24px">
<section class="panel">
<h2>Where the imagery came from</h2>${C.products
    .map(
      (p, i) => `<div class="data-row">
<span>${esc(p.name)}</span>
<b>${treatment(p.treatment)}<br>
<small>${esc(p.origin)}</small>
</b>
</div>`,
    )
    .join("")}<div class="rule">
</div>${btn("Attempt & source details", "attempts", "small")}</section>

</div>`;
}
function handoff() {
  const campaign = campaignMetricDisplay();
  if (!model?.manifest)
    return `${head("A good handoff starts with saved work.", "Your brief is safe. Create campaign images before exporting.", btn("Review production plan →", "plan", "primary"), "04 / HANDOFF", "wf-stage-header")}<div class="empty">
<div class="empty-symbol">↗</div>
<h2>No saved images to package yet.</h2>
<p>Saved brief and pending edits are preserved; exports need current image results. Return to the plan to create your first output set.</p>${btn("Open Production", "nav:production")}</div>`;
  const partial = model.manifest && !model.manifest.complete,
    empty = (model.manifest?.metrics.completed_images ?? 0) === 0,
    damaged = damagedFiles(),
    missing = otherUnavailableOutputs().length;
  return `${head("Ready for the next pair of eyes.", `${C.name} · Current images`, btn("← Studio", "nav:review", "small quiet"), "04 / HANDOFF", "wf-stage-header")}<section class="export-hero">
<div>
<span class="eyebrow">${damaged ? "RECOVERY REQUIRED" : partial ? "PARTIAL" : "COMPLETE"} CAMPAIGN PACKAGE</span>
<h2>${empty ? "Nothing to export yet." : `${model.manifest?.metrics.completed_images ?? 0} images. One clear handoff.`}</h2>
<p class="small muted" style="margin-top:8px">${C.markets.length} ${C.markets.length === 1 ? "market" : "markets"} · 3 formats · offline review included</p>
</div>
<div class="row wrap">${btn("Preview offline report ↗", "report", "", `data-wording-export="${damaged || empty ? "unavailable" : "available"}" ${damaged || empty || wordingExportBlocked() ? "disabled" : ""}`)}${btn("Download ZIP ↓", "exportZip", "primary", `data-wording-export="${damaged || empty ? "unavailable" : "available"}" ${damaged || empty || wordingExportBlocked() ? "disabled" : ""}`)}${btn("Export folder", "exportFolder", "", `data-wording-export="${damaged || empty ? "unavailable" : "available"}" ${damaged || empty || wordingExportBlocked() ? "disabled" : ""}`)}</div>
</section>${wordingNotice()}${unavailableSource() ? sourceRecoveryNotice() : damaged ? notice(`${damaged} saved image ${damaged === 1 ? "file needs" : "files need"} recovery.`, "These files were saved previously but are now missing or changed. Recover from saved sources before packaging this version.", "block", "nav:production", "Open local recovery") : empty && nonRuleOmissions() ? notice("No images are available from this run.", canRecoverCurrentOutputs() ? "Open Production to recover saved sources or review a deliberate retry. Export does not create imagery." : "Review the available images. No new request starts automatically.", "info", "nav:production", "Review recovery options") : partial && nonRuleOmissions() ? notice(`${missing} ${missing === 1 ? "output is" : "outputs are"} missing. The package will say so.`, "Only available images from this saved version will be included. Nothing is generated by export.", "", canRecoverCurrentOutputs() ? "recover" : "nav:production", canRecoverCurrentOutputs() ? "Recover missing work" : "Review recovery options") : ""}<div class="two-col" style="margin-top:24px">
<section class="panel">
<h3>Everything travels together.</h3>
<div class="file-list">Package contents<br>&nbsp; ├─ index.html &nbsp; <span class="muted">the visual review</span>
<br>&nbsp; ├─ images/ &nbsp; <span class="muted">full-size PNGs</span>
<br>&nbsp; └─ manifest.json &nbsp; <span class="muted">coverage, origins, and omissions</span>
</div>
</section>
<section class="panel">
<h3>Clear about what’s ready.</h3>
<div class="data-row">
<span>Campaign spend</span>
<b>${esc(campaign.spend)}${campaign.known_spend ? " estimated" : ""}<br><small class="muted">${esc(campaign.known_spend ? "USD · not a bill" : campaign.spend_helper)}</small></b>
</div>
<div class="data-row">
<span>Accessible descriptions</span>
<b>${descriptionCount()} of ${total()} supplied</b>
</div>
<p class="small muted">Export includes every available image in this saved revision. Filters below change the view, never the package.</p>
</section>
</div>
<div class="section-title">
<h2>The campaign, at a glance</h2>${badge(damaged ? "Recovery required" : empty ? "Nothing to export" : partial ? "Partial handoff" : "All formats included", damaged ? "block" : partial ? "warning" : "")}</div>${overviewView(true)}`;
}
function editorParts(body, foot) {
  return `${body}<footer class="modal-foot">${foot}</footer>`;
}
function inspectorModal() {
  const p = C.products[currentProduct],
    f = FMT[currentFormat],
    key = `${p.id}-${lang}-${currentFormat}`;
  return dialog(
    `${esc(p.name)} · ${f.ratio}`,
    `<div class="large-inspector">
<div class="lightbox">${creative(currentProduct, lang, currentFormat, `data-action="fullsize" tabindex="0" role="button"`)}</div>
<div>
<div class="row wrap">${badge(LANG[lang], "neutral")}${badge(f.name, "outline")}</div>
<div class="data-row">
<span>Saved file</span>
<b>${f.w} × ${f.h} PNG<br>Current image</b>
</div>
<div class="data-row">
<span>Source treatment</span>
<b>${treatment(p.treatment)}</b>
</div>
<div class="data-row">
<span>Origin</span>
<b>${esc(p.origin)}</b>
</div>
${area("Accessible image description", "image-description", C.descriptions[key] || "", "Producer supplied. Unspecified until you write it; no image generation.")}${btn("Update image details", "saveDescription", "small", !canDescribeCreative(currentProduct, lang, currentFormat) ? "disabled" : "")}<div class="rule">
</div>
<div class="row">${btn("Source details", `source:${currentProduct}`, "small")}</div>

</div>
</div>`,
    (canDescribeCreative(currentProduct, lang, currentFormat)
      ? '<span class="small muted">Close keeps pending edits. Cancel edits discards this target’s draft.</span>' + btn("Cancel edits", "cancelEdits")
      : "") + btn("Open full-size PNG ↗", "fullsize") +
      btn(
        "Download PNG ↓",
        `download:${currentProduct}:${lang}:${currentFormat}`,
        "primary",
      ),
    true,
  );
}
