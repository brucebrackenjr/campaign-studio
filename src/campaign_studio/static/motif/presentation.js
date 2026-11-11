"use strict";
// Selected Motif markup; data and mutations are owned by the local service.
function shell(content) {
  const identity = campaignIdentity();
  const focused =
    page === "review" && reviewTab === "product" && C.products.length > 0;
  return `<header class="topbar">
<div class="motif-brand"><a href="#library" class="wordmark" data-action="library" aria-label="Motif – Campaign Studio home"><i class="mark" aria-hidden="true"></i>Motif</a><span class="motif-descriptor">Campaign Studio</span></div>
<div class="top-context">${
    page !== "library"
      ? `<button class="campaign-heading" data-action="campaigns" aria-label="Select a saved campaign" title="${esc([identity.brand, identity.name].filter(Boolean).join(" · "))}"><span class="campaign-heading-text"><span class="campaign-heading-brand" ${identity.brand.trim() ? "" : "hidden"}>${esc(identity.brand)}</span><span class="campaign-heading-name">${esc(identity.name)}</span></span><span class="campaign-heading-caret" aria-hidden="true"></span>
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
  const identity = latest
    ? latest.name || "Untitled campaign"
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
    ? `<p class="home-campaign-name" title="${esc(identity)}"><span class="home-campaign-title">${esc(latest.name || "Untitled campaign")}</span></p>`
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
  const identity = c.name;
  const art = c.recovery_message
    ? '<span class="awaiting-art">Saved files unavailable</span>'
    : !c.products
    ? '<span class="awaiting-art"><i class="mark" aria-hidden="true"></i><span>Awaiting<br>products</span></span>'
    : c.thumbnail
      ? `<img data-workbench-thumbnail src="${esc(c.thumbnail)}" alt="${esc(c.thumbnail_label || `${c.brand} campaign artwork`)}">`
      : `<span class="awaiting-art">${workbenchEmptyImage(c)}</span>`;
  const state = c.recovery_message || c.damaged_images || c.outcome_unknown ? "warning" : c.status === "complete" ? "" : "neutral";
  return `<div class="workbench-item">
    <button class="campaign-card" data-action="openCampaign" data-campaign-id="${id}" title="${esc(identity)}">
      <span class="campaign-image">${art}</span>
      <span class="info">
        <span class="eyebrow">${esc(c.brand || "New brand")}</span>
        <strong class="workbench-title">${esc(c.name)}</strong>
        <span class="small muted">${c.recovery_message ? esc(c.recovery_message) : `${c.products} ${c.products === 1 ? "product" : "products"} · ${c.markets} ${c.markets === 1 ? "market" : "markets"} · ${workbenchCounts(c)}`}</span>
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
  return `${head("Good work starts here.", "Creative automation for social campaigns. Your next idea starts here.", cards.length ? btn("Import brief", "import") + btn("＋ New campaign", "new", "primary") : "", "YOUR WORKSPACE")}${typeof setupNotice !== "undefined" && setupNotice ? notice(setupNotice, "", "info") : ""}${homeHero(cards[0])}<div class="section-title"><h2>On your workbench <span class="muted small">${cards.length} saved ${cards.length === 1 ? "campaign" : "campaigns"}</span></h2><div class="workbench-tools"><span class="small muted" title="Campaigns are ordered by their most recent saved change, review, or completed run.">Recent activity</span>${pendingDeletionControl()}${cards.length ? `<button class="workbench-arrow" data-action="workbench:previous" aria-label="Previous campaigns" aria-controls="workbench-track">←</button><button class="workbench-arrow" data-action="workbench:next" aria-label="Next campaigns" aria-controls="workbench-track">→</button>` : ""}</div></div>${cards.length ? `<div class="campaign-grid" id="workbench-track" role="region" aria-roledescription="carousel" aria-label="Saved campaigns, most recent activity first. Use arrow keys to scroll." tabindex="0"><button class="campaign-card" data-action="new" style="display:grid;place-content:center;text-align:center;min-height:280px;background:#e8e2d5;border-style:dashed"><div class="display" style="font-size:56px;color:#dd442d">＋</div><h3>Something new?</h3><p class="small muted" style="padding:8px 25px;color:#596059">Bring your products, your words,<br>and your point of view.</p></button>${cards.map(workbenchCard).join("")}</div>` : `<div class="empty"><div class="empty-symbol">↗</div><h2>Your next campaign belongs here.</h2><p>Create a campaign or import a brief above. Your saved work will appear here.</p></div>`}`;
}
function brief() {
  return '<div id="brief-root"></div>';
}
function dialog(title, body, foot = "", wide = false, eyebrow = null) {
  return `<div class="overlay">
<section class="modal ${wide ? "wide" : ""} ${(["campaigns", "rename"].includes(modal) || (modal === "storage" && typeof storageView !== "undefined" && storageView?.state === "campaign-context")) ? "campaign-identity-dialog" : ""}" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
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
    case "campaign-copy-confirm":
      return campaignCopyView();
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
  const done = !!model.run && !pending && !retry && !["planned", "confirmed"].includes(model.run.state);
  if (active || (done && !context.complete) || (!pending && (context.resumeGeneration || model.notice))) return progressView(context);
  const notices = `${unavailableSource() ? sourceRecoveryNotice() : ""}${planInputErrors(proposal?.findings || plan?.findings || [])}`;
  return productionView(context, "", notices);
}

function emptyPage(title, text, action = "nav:campaign", label = "Open Campaign") {
  return `${head(title, text)}<div class="empty"><div class="empty-symbol">↗</div>${btn(label, action, "primary")}</div>`;
}
function planInputErrors(findings = model?.plan?.findings || []) {
  const seen = new Set();
  const products = currentBriefValue().products || [];
  const items = findings.map((finding, index) => {
    if (!sourceFindingBlocks(finding)) return "";
    const field = finding.context?.fields?.[0];
    const product = products.find(item => item.id === finding.context?.product_id) ||
      (field?.path?.[0] === "products" ? products[field.path[1]] : null);
    // The missing-image card already names this product and offers both paths.
    if (product && !product.source?.path && product.source?.role !== "concept" &&
        (finding.rule === "asset" || field?.path?.includes("source"))) return "";
    const text = [finding.evidence, finding.remedy].filter(Boolean).join(" ");
    if (seen.has(text)) return "";
    seen.add(text);
    return `<div class="pr-input-issue"><strong>${esc(product?.name || "Campaign details")}</strong><p>${esc(text)}</p>${btn(product ? "Edit product" : "Open Campaign", field ? `planInput:${index}` : "nav:campaign", "pr-button pr-small")}</div>`;
  }).join("");
  return items ? `<section class="pr-input-issues" role="status">${items}</section>` : "";
}

function productionSourceCounts(context) {
  if (operation?.run_id === model.run?.id &&
      Number.isInteger(operation?.sources_completed) && Number.isInteger(operation?.sources_total))
    return { completed: operation.sources_completed, total: operation.sources_total };
  const productIds = new Set(context.products.map(product => product.id));
  return {
    completed: Object.keys(model.source_urls || {}).filter(id => productIds.has(id)).length,
    total: productIds.size,
  };
}
function progressView(context = productionContext()) {
  const active = context.active;
  const { completed, total: expected } = productionSourceCounts(context);
  const attempts = operation?.attempts || model.run_attempts || [];
  const attempted = operation?.attempted ?? attempts.length;
  const allowance = operation?.attempted != null && operation?.remaining != null
    ? operation.attempted + operation.remaining : model.run.allowance;
  const remaining = operation?.remaining ?? Math.max(0, allowance - attempted);
  const unknown = attempts.some(item => item.state === "unknown" || (!active && item.state === "dispatching")) ||
    (operation?.notice_code || model.notice?.code) === "unknown_request";
  const stopped = model.run.state === "stopped";
  const recoverable = canRecoverCurrentOutputs();
  const retained = recoverable && model.local_recovery.saved_result;
  const noticeText = operation?.notice || model.notice?.message;
  const credentials = !active ? MotifProducerRepair.credentialPanel(context) : "";
  let content;
  if (credentials) content = credentials;
  else if (active) content = `<h2>Preparing source images…</h2><p>${esc(operation?.stage || "Generating the next product image.")}</p>${operation?.stoppable ? btn(model.regeneration ? "Stop regeneration" : "Stop unsent work", "stop", "pr-button pr-wide") : ""}`;
  else if (unknown) content = `<h2>The image request could not be confirmed.</h2><p>The request may have completed and may be charged. Nothing will be resent automatically.</p>${recoverable ? btn("Look for saved result", "recover", "pr-button pr-primary pr-wide") : context.resumeGeneration ? btn("Review a new attempt", context.resumeAction, "pr-button pr-primary pr-wide") : ""}`;
  else if (retained) content = `<h2>A provider result is already saved.</h2><p>Recover the source image without another AI request.</p>${btn("Recover saved source images · 0 calls", "recover", "pr-button pr-primary pr-wide")}`;
  else if (context.resumeGeneration) content = `<h2>${stopped ? "Production stopped." : "Generation paused."}</h2>${noticeText ? `<p>${esc(noticeText)}</p>` : ""}${btn("Resume generation", context.resumeAction, "pr-button pr-primary pr-wide")}`;
  else if (completed === expected && expected > 0) content = `<h2>Your source images are available.</h2><p>Open Studio to edit your campaign images.</p>${btn("Open Studio →", "nav:review", "pr-button pr-primary pr-wide")}`;
  else content = `<h2>${stopped ? "Production stopped." : noticeText ? "Generation paused." : "Add the remaining images."}</h2><p>${noticeText ? esc(noticeText) : "Use the product cards to select a photo or choose a new image to generate."}</p>`;
  const elapsed = Number.isFinite(operation?.elapsed_seconds) ? Math.max(0, Math.floor(operation.elapsed_seconds)) : 0;
  const count = `<div class="pr-source-progress" ${active ? 'aria-busy="true"' : ''}><div class="pr-operation-count">${completed} <small>/ ${expected} source images</small></div><div class="pr-operation-progress${active ? ' is-active' : ''}"><i style="width:${expected ? Math.min(100, (completed / expected) * 100) : 0}%"></i></div>${active ? `<p class="pr-generation-elapsed"><span class="pr-working-indicator" aria-hidden="true"></span><span>${elapsed}s elapsed</span></p>` : ''}</div>`;
  const accounting = allowance === 0 ? "No image requests in this run." : `This run: ${attempted} of ${allowance} requests used · ${remaining} remaining.${unknown ? " The request result was not confirmed. A charge may still apply." : " This count is not a confirmed charge."}`;
  const details = `<details class="pr-request-details"><summary>Request details</summary><p>${esc(accounting)}</p>${operation?.elapsed_seconds != null ? `<p>${operation.elapsed_seconds}s elapsed</p>` : ""}${btn("Attempt details", "attempts", "pr-button pr-small pr-wide")}${Object.keys(model.plan?.generation_requests || {}).length ? btn("Exact prompt & settings", "settings", "pr-button pr-small pr-wide") : ""}<p>Available sources can be edited in Studio. Finished formats are created when you download.</p></details>`;
  return productionView(context, `${content}${count}${details}`);
}

function currentRuleWithholding() {
  const identified = new Set(model.rule_withheld_outputs || []);
  return [...new Set((model.manifest?.outputs || [])
    .filter(item => !item.url && identified.has(item.id)).map(item => item.id))];
}
function otherUnavailableOutputs() {
  const withheld = new Set(currentRuleWithholding());
  return (model.manifest?.outputs || []).filter(item => !item.url && !withheld.has(item.id) && !layoutFindingsFor(item).length);
}
function layoutRepairTarget(output) {
  const issue = layoutFindingsFor(output)[0];
  const product = C.products.findIndex(item => item.id === output?.product_id);
  const language = Object.keys(MARKET).find(key => MARKET[key] === output?.market);
  const format = FMT.findIndex(item => item.ratio === output?.ratio);
  if (!issue || product < 0 || !language || format < 0) return null;
  const available = !!model.editable_products?.[output.product_id];
  return { action: available ? `repairLayout:${product}:${format}:${language}:text` : "nav:production",
    label: available ? "Adjust text in Studio" : "Restore source in Production",
    title: `${C.products[product].name} · ${LANG[language]} · ${FMT[format].name}` };
}
function layoutNotice() {
  if (page === "review" && reviewTab === "product") return "";
  const phraseOutputs = new Set((model?.wording_issue?.matches || []).map(finding => finding.item));
  const affected = (model?.manifest?.outputs || []).filter(output => layoutFindingsFor(output).length && !phraseOutputs.has(output.id));
  if (!affected.length) return "";
  return `<section class="notice block layout-notice" role="status"><div><strong>Adjust these layouts before exporting.</strong><p>${affected.every(output => model.editable_products?.[output.product_id]) ? "The source images are available. Move or resize the affected text in Studio." : "Restore unavailable source images in Production, then adjust the affected text in Studio."}</p>${affected.map(output => {
    const target = layoutRepairTarget(output);
    return `<div class="layout-issue"><strong>${esc(target?.title || output.id)}</strong><p>${esc([...new Set(layoutFindingsFor(output).map(finding => finding.evidence))].join(" "))}</p>${target ? btn(target.label, target.action, "small") : ""}</div>`;
  }).join("")}</div></section>`;
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
function review() {
  if (!C.products.length)
    return emptyPage("The work will live here.", "Add your first product and review a production plan.", "nav:production", "Add products in Production");
  const notices = `${unavailableSource() ? sourceRecoveryNotice() : damagedFiles() ? notice("Saved image files need local recovery.", "Missing or changed files cannot be exported. Local recovery first verifies the saved sources before rebuilding outputs.", "block", "nav:production", "Open recovery") : ""}${wordingNotice()}<div data-layout-slot>${layoutNotice()}</div>`;
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
// Show the server's current output findings even when a preview is available.
// A preview is useful for correction; it does not mean the image can be exported.
function outputCorrectionMessages(output) {
  if (!output) return [];
  const messages = [];
  const findings = (model.manifest?.findings || []).filter(finding =>
    finding.severity === "blocking" && (finding.item === output.id ||
      finding.context?.product_id === output.product_id &&
      finding.context?.market === output.market && finding.context?.ratio === output.ratio));
  for (const finding of findings) {
    const phrase = finding.context?.phrase;
    if (phrase?.matches?.length) {
      for (const match of phrase.matches) {
        const field = { message: "headline", cta: "call to action" }[match.field] || "text";
        messages.push(`Change “${phrase.configured_phrase}” in the ${field} before exporting.`);
      }
    } else if (finding.rule === "text_fit") {
      messages.push(finding.evidence || "Adjust the text layout in Studio before exporting.");
    }
  }
  return [...new Set(messages)];
}
function outputCorrectionNotice(output) {
  const messages = outputCorrectionMessages(output);
  return messages.length ? `<div class="overview-issue" role="note"><strong>Changes needed</strong>${messages.map(message => `<p>${esc(message)}</p>`).join("")}</div>` : "";
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
      const layout = layoutRepairTarget(output);
      const withheld = missing && currentRuleWithholding().includes(output?.id);
      return `<article class="overview-item">${
        missing
          ? `<div class="missing-tile">
<strong>${firstImages ? "Ready for first images" : layout ? "Layout needs attention" : withheld ? "Image withheld by current rules." : "Image not available"}</strong>
<p class="small">${esc(firstImages ? "Review the production plan to create this image." : layout ? layoutFindingsFor(output).map(finding => finding.evidence).join(" ") : withheld ? "Correct the prohibited wording above to update this image." : unavailableSource() ? "Replace the unavailable source in Production before rebuilding this image." : outputFor(e.p, e.l, e.f)?.omission || "This saved output is unavailable. Open Production to review recovery options.")}</p>
</div>`
          : `<div class="overview-image">${creative(e.p, e.l, e.f, `data-action="inspect:${e.p}:${e.f}:${e.l}" tabindex="0" role="button"`)}</div>`
      }<div class="info">
<h3>${esc(C.products[e.p].name)}</h3>
<p class="small muted">${LANG[e.l]} · ${FMT[e.f].name} ${FMT[e.f].ratio}</p>
${exporting ? outputCorrectionNotice(output) : ""}
<div class="row between">${missing ? firstImages ? btn("Review production plan", "plan", "small quiet") : layout ? btn(layout.label, layout.action, "small quiet") : badge(withheld ? "Withheld" : "Missing", "block") : ""}${missing ? "" : btn(exporting ? "Download PNG" : "Inspect", exporting ? `download:${e.p}:${e.l}:${e.f}` : `inspect:${e.p}:${e.f}:${e.l}`, "small quiet", exporting ? `data-wording-export="available" ${exportBlocked() ? "disabled" : ""}` : "")}</div>
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
function handoffLogoNotice() {
  if (model.manifest?.findings?.some(f => f.rule === "logo_presence" && f.severity === "warning"))
    return notice("These images have no logo. You can export them as they are.", "", "", "nav:brand", "Open Brand");
  return "";
}
function handoff() {
  const campaign = campaignMetricDisplay();
  if (!model?.manifest)
    return `${head("A good handoff starts with saved work.", "Your brief is safe. Create campaign images before exporting.", btn("Review production plan →", "plan", "primary"), "", "wf-stage-header")}<div class="empty">
<div class="empty-symbol">↗</div>
<h2>No composition is ready to export yet.</h2>
<p>Finish any incomplete campaign fields and provide source imagery for your products. Production can generate missing source images.</p>${btn("Open Production", "nav:production")}</div>`;
  const layoutBlocked = currentLayoutFindings().length > 0,
    changesNeeded = layoutBlocked || currentWordingView().phase === "blocked",
    partial = model.manifest && !model.manifest.complete,
    empty = (model.manifest?.metrics.completed_images ?? 0) === 0,
    damaged = damagedFiles(),
    missing = otherUnavailableOutputs().length;
  return `${head("Ready for the next pair of eyes.", `${C.name} · Current images`, btn("← Studio", "nav:review", "small quiet"), "", "wf-stage-header")}<section class="export-hero">
<div>
<span class="eyebrow">${changesNeeded ? "CHANGES NEEDED" : damaged ? "RECOVERY REQUIRED" : partial ? "PARTIAL" : "COMPLETE"} CAMPAIGN PACKAGE</span>
<h2>${layoutBlocked ? "Review the affected layouts." : empty ? "Nothing to export yet." : `${model.manifest?.metrics.completed_images ?? 0} images. One clear handoff.`}</h2>
<p class="small muted" style="margin-top:8px">${C.markets.length} ${C.markets.length === 1 ? "market" : "markets"} · 3 formats · offline review included</p>
</div>
<div class="row wrap">${btn("Preview offline report ↗", "report", "", `data-wording-export="${damaged || empty ? "unavailable" : "available"}" ${damaged || empty || exportBlocked() ? "disabled" : ""}`)}${btn("Download ZIP ↓", "exportZip", "primary", `data-wording-export="${damaged || empty ? "unavailable" : "available"}" ${damaged || empty || exportBlocked() ? "disabled" : ""}`)}</div>
</section><div data-logo-notice>${handoffLogoNotice()}</div>${wordingNotice()}${layoutNotice()}${unavailableSource() ? sourceRecoveryNotice() : damaged ? notice(`${damaged} saved image ${damaged === 1 ? "file needs" : "files need"} recovery.`, "These files were saved previously but are now missing or changed. Recover from saved sources before packaging this version.", "block", "nav:production", "Open local recovery") : !layoutBlocked && empty && nonRuleOmissions() ? notice("No images are available from this run.", canRecoverCurrentOutputs() ? "Open Production to recover saved source images or review a new AI request. Export creates finished formats from available sources." : "Review the available images. No new request starts automatically.", "info", "nav:production", "Review recovery options") : !layoutBlocked && partial && nonRuleOmissions() ? notice(`${missing} ${missing === 1 ? "output is" : "outputs are"} missing. The package will say so.`, "Export creates finished images from the available sources and current saved settings. Missing sources are listed in the package.", "", canRecoverCurrentOutputs() ? "recover" : "nav:production", canRecoverCurrentOutputs() ? "Recover missing work" : "Review recovery options") : ""}<div class="two-col" style="margin-top:24px">
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
<p class="small muted">Export renders every available image from the current saved settings. Filters below change the view, never the package.</p>
</section>
</div>
<div class="section-title">
<h2>The campaign, at a glance</h2>${badge(changesNeeded ? "Changes needed" : damaged ? "Recovery required" : empty ? "Nothing to export" : partial ? "Images missing" : "All formats included", changesNeeded || damaged ? "block" : partial ? "warning" : "")}</div>${overviewView(true)}`;
}
function editorParts(body, foot) {
  return `${body}<footer class="modal-foot">${foot}</footer>`;
}
function inspectorModal() {
  const p = C.products[currentProduct],
    f = FMT[currentFormat],
    key = `${p.id}-${lang}-${currentFormat}`;
  const detail = model.metadata?.creatives?.[`${p.id}/${MARKET[lang]}/${f.ratio.replace(":", "x")}`];
  return dialog(
    `${esc(p.name)} · ${f.ratio}`,
    `<div class="large-inspector">
<div class="lightbox">${creative(currentProduct, lang, currentFormat, `data-action="fullsize" tabindex="0" role="button"`)}</div>
<div>
<div class="row wrap">${badge(LANG[lang], "neutral")}${badge(f.name, "outline")}</div>
<div class="data-row">
<span>Export size</span>
<b>${f.w} × ${f.h} PNG<br>Rendered on download</b>
</div>
<div class="data-row">
<span>Source treatment</span>
<b>${treatment(p.treatment)}</b>
</div>
<div class="data-row">
<span>Origin</span>
<b>${esc(p.origin)}</b>
</div>
${area("Accessible image description", "image-description", C.descriptions[key] || "", detail?.reset_reason === "source_changed" ? "The source image changed. Check that this description still fits." : "Producer supplied. Unspecified until you write it; no image generation.")}<div class="rule">
</div>
<div class="row">${btn("Source details", `source:${currentProduct}`, "small")}</div>

</div>
</div>`,
    (canDescribeCreative(currentProduct, lang, currentFormat)
      ? '<span class="small muted" id="handoff-description-status" role="status">Changes save automatically.</span>' + btn("Try saving description again", "retryDescription", "quiet", "hidden")
      : "") + btn("Open full-size PNG ↗", "fullsize") +
      btn(
        "Download PNG ↓",
        `download:${currentProduct}:${lang}:${currentFormat}`,
        "primary",
      ),
    true,
  );
}
