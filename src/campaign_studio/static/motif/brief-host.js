"use strict";

// The Brief component owns markup. This adapter writes the existing authored
// envelope and coalesces representative previews; it never publishes imagery.
let briefHost = null;
let briefPreviewFlight = null;
const briefPreviewActions = new Map();
function briefPreviewPaused() { return briefPreviewActions.has(draft?.campaign_id); }
let briefFacts = { campaign: null, logos: new Map() };
const BRIEF_FIELD = {
  campaignName: "campaign-name", brandName: "brand-name", audience: "audience",
  direction: "scene", copyOrigin: "copy-origin", copyRights: "copy-rights",
  defaultColor: "text-color", message: "copy-headline", cta: "copy-cta",
};
function briefForm(section = tab, language = lang) {
  return projectBuffers.form_drafts.fields?.[`brief/${section}/${language}`] || {};
}
function briefFields() {
  // Preserve unopened language buffers. The active form wins if a predecessor
  // saved different raw values for the same field in Campaign and Brand.
  return Object.assign({}, briefForm("campaign"), briefForm("brand"), briefForm("copy"), briefForm(tab));
}
function currentBriefValue() {
  const value = copy(draft), fields = briefFields();
  for (const [key, property] of [["campaign-name", "name"], ["audience", "audience"], ["scene", "creative_direction"]])
    if (key in fields) value[property] = fields[key];
  if ("brand-name" in fields) value.brand.name = fields["brand-name"];
  if ("text-color" in fields) value.brand.text_color = fields["text-color"];
  if ("brand-palette" in fields) value.brand.palette = String(fields["brand-palette"]).split(",");
  for (const [key, property] of [["copy-origin", "label"], ["copy-rights", "rights"]])
    if (key in fields) value.copy_provenance[property] = fields[key];
  const wording = value.markets[MARKET[lang]]?.copy;
  if (wording) for (const [key, property] of [["copy-headline", "message"], ["copy-cta", "cta"]]) {
    const change = campaignCopyChanges.get(`${draft.campaign_id}/${MARKET[lang]}/${property}`);
    if (key in fields && change?.owner === draft && change.value === fields[key])
      wording[property] = property === "cta" ? fields[key] || null : fields[key];
  }
  return value;
}
function rememberBriefField(id, value) {
  const fields = projectBuffers.form_drafts.fields ||= {};
  const key = `brief/${tab}/${lang}`;
  (fields[key] ||= {})[id] = value;
  // An explicit edit resolves same-language duplicates only. The other
  // language's saved unfinished form remains owned by that form.
  for (const [name, values] of Object.entries(fields))
    if (name.startsWith("brief/") && name.endsWith(`/${lang}`) && id in values) values[id] = value;
}
function explicitBriefColors(value) {
  const uses = [];
  for (const product of value.products || [])
    for (const [market, formats] of Object.entries(product.framing || {}))
      for (const [ratio, framing] of Object.entries(formats))
        for (const [block, placement] of Object.entries(framing.text_placements || {}))
          if (placement?.color) uses.push({ product: product.id, name: product.name, market, ratio, block, color: placement.color.toUpperCase() });
  return uses;
}
function briefTargetLabel({ name, market, ratio }) {
  const format = FMT.find(item => item.ratio === ratio);
  return `${name || "Selected product"} · ${LANG[market === "es-MX" ? "es" : "en"]} · ${format?.name || ratio}${format?.name ? ` (${ratio})` : ""}`;
}
function briefViewModel(context) {
  const value = currentBriefValue(), fields = briefFields();
  const normalize = MotifBriefFlow.normalizeHex;
  const palette = Array.isArray(value.brand.palette) ? value.brand.palette : [];
  const normalized = palette.map(normalize), rawDefault = value.brand.text_color;
  const accepted = draft.brand.text_color;
  const color = normalize(accepted), next = normalize(rawDefault);
  const selected = value.products.find(p => p.id === context.productId) || value.products[0];
  context.productId = selected?.id || null;
  const info = briefFacts.logos.get(value.brand.logo_path);
  let preview = { phase: selected ? "pending" : "empty" }, colorMessage = "", colorAction = null;
  let paletteMessage = context.paletteMessage || "", paletteAction = context.paletteTarget
    ? { label: "Open affected text in Studio", action: "open-studio" } : null;
  const uses = explicitBriefColors(value);
  context.affectedColor = uses.find(item => !normalized.includes(item.color)) || null;
  if (!next) preview = { phase: "invalid", message: "Enter a complete six-digit default color. Your unfinished value stays saved." };
  else if (next !== color) {
    const present = normalized.includes(next);
    colorMessage = present ? "Use this palette color as the campaign default." : palette.length < 8
      ? `Add ${next} to the palette to use it as the default.` : "The palette has eight colors. Remove an unused color before adding this default.";
    colorAction = present ? { label: "Use as default", action: "use-default" } : palette.length < 8
      ? { label: "Add color and use as default", action: "add-default" } : null;
    preview = { phase: "color" };
  }
  if (!palette.length || palette.some(entry => !normalize(entry)))
    preview = { phase: "invalid", message: "Choose or enter each palette color. Your unfinished values stay saved." };
  else if (next && !normalized.includes(color)) {
    colorMessage = "The default color is no longer in the palette. Choose a palette default, or add it back.";
    colorAction = palette.length < 8 && color ? { label: "Add default back", action: "restore-default" } : null;
    preview = { phase: "invalid", message: "Choose a palette default before previewing. Your raw palette edits remain saved." };
  }
  if (context.affectedColor) {
    const use = context.affectedColor;
    paletteMessage = `${briefTargetLabel(use)}: custom ${use.block === "message" ? "Message" : "CTA"} still uses ${use.color}. Add it back, or update that block in Studio.`;
    paletteAction = palette.length < 8 ? { label: `Add ${use.color} back`, action: "restore-used-color" } : { label: "Open affected text in Studio", action: "open-studio" };
    preview = { phase: "invalid", message: paletteMessage };
  }
  // Brand updates can proceed with unfinished copy or products. Capture only
  // the color admission above, before checking the rest of the preview.
  const brandColorProblem = ["color", "invalid"].includes(preview.phase)
    ? preview.message || colorMessage : "";
  const override = selected?.copy_overrides?.[MARKET[lang]] || {};
  const headline = override.message ?? value.markets[MARKET[lang]]?.copy?.message;
  if (preview.phase === "pending" && !String(headline || "").trim() &&
      selected?.source?.role !== "concept" && selected?.source?.path) {
    preview = { phase: "source", kind: "source", url: assetURL(selected.source.path, selected.id),
      alt: selected.name + " – product photo", note: "Add a headline in Copy & Rules to preview the layout." };
  }
  return {
    id: value.campaign_id, name: value.name, tab, language: MARKET[lang],
    ratio: context.ratio, productId: context.productId, brandName: value.brand.name,
    audience: value.audience, direction: value.creative_direction,
    copyOrigin: value.copy_provenance.label, copyRights: value.copy_provenance.rights,
    markets: Object.keys(value.markets), productLimit, mutationsBlocked: !!operation?.active || model.run?.state === "running" || (typeof brandUpdating !== "undefined" && brandUpdating),
    products: value.products.map(p => ({
      id: p.id, name: p.name, description: p.description,
      mode: p.source?.role === "concept" ? "concept" : "photo",
      image: p.source?.role === "concept" ? model.concept_previews?.[p.id] || null : assetURL(p.source?.path, p.id),
      attachmentHint: p.source?.role !== "concept" && !p.source?.path
        ? model.attachment_hints?.[p.id] || null : null,
    })),
    logo: info ? { ...info, url: assetURL(value.brand.logo_path), filename: info.filename || "Campaign logo" } : null,
    hasLogo: !!value.brand.logo_path,
    logoStatus: context.logoStatus || "accepted", logoMessage: context.logoMessage || "",
    fitted: context.fitted || "", color, defaultDraft: rawDefault, palette,
    colorMessage, colorAction, paletteMessage, paletteAction, brandColorProblem,
    copy: Object.fromEntries(Object.entries(value.markets).map(([m, data]) => [m, m === MARKET[lang] ? {
      message: fields["copy-headline"] ?? data.copy.message, cta: fields["copy-cta"] ?? data.copy.cta,
    } : data.copy])),
    copyErrors: Object.fromEntries(["message", "cta"].map(field => [field,
      MotifWording.matchingTerms(fields[BRIEF_FIELD[field]] ?? value.markets[MARKET[lang]].copy[field], value.markets[MARKET[lang]].phrase_rules)])),
    rules: Object.fromEntries(Object.entries(value.markets).map(([m, data]) => [m, data.phrase_rules || []])),
    hasSavedImages: !!model.manifest,
    brandPending: typeof brandChangesPending === "function" && brandChangesPending(),
    brandUpdating: typeof brandUpdating !== "undefined" && brandUpdating,
    customPreviewColors: uses.some(item => item.product === context.productId && item.market === MARKET[lang] && item.ratio === context.ratio),
    preview, previewLayerKey: briefPreviewLayerKey(context),
  };
}
function briefBrandColorProblem() {
  return briefViewModel(briefHost || {}).brandColorProblem;
}
function briefLogoUploadPending() {
  return !!briefHost && ownsBrief(briefHost) && briefHost.logoStatus === "pending";
}
function cancelBriefLogoUpload(context) {
  if (!context.logoUpload) return;
  context.upload++;
  const upload = context.logoUpload;
  context.logoUpload = null;
  clearTimeout(upload.timer);
  upload.controller.abort();
  context.logoStatus = "accepted";
  context.logoMessage = "";
  if (ownsBrief(context)) refreshBriefFlow(context);
}
function destroyBriefFlow() {
  if (!briefHost) return;
  briefHost.dead = true;
  cancelBriefLogoUpload(briefHost);
  clearTimeout(briefHost.timer);
  briefHost.saveView?.destroy();
  briefHost.view.destroy();
  briefHost = null;
}
function mountBriefFlow() {
  if (page !== "campaign" || !draft) return;
  if (!["campaign", "products", "brand", "copy"].includes(tab)) tab = "campaign";
  if (briefFacts.campaign !== draft.campaign_id) briefFacts = { campaign: draft.campaign_id, logos: new Map() };
  const context = { owner: draft.campaign_id, productId: C.products[currentProduct]?.id, ratio: FMT[currentFormat]?.ratio || "1:1", generation: 0, upload: 0, dead: false };
  briefHost = context;
  context.view = MotifBriefFlow.mount($("#brief-root"), {
    onField: (key, value, meta) => changeBriefField(context, key, value, meta),
    onAction: action => briefAction(context, action).catch(error => toast(error.message)),
    onFile: file => attachBriefLogo(context, file),
    onPreview: request => requestBriefPreview(context, request),
  });
  refreshBriefFlow(context);
}
function ownsBrief(context) {
  return briefHost === context && !context.dead && draft?.campaign_id === context.owner && page === "campaign";
}
function renderBriefModel(context) {
  // Replacing a focused field emits blur synchronously. That teardown is not
  // a second completed user edit and must not recursively replace the DOM.
  context.rendering = true;
  try { context.view.render(context.vm); context.saveView?.refresh(); }
  finally { context.rendering = false; }
}
function briefPreviewBody(context) {
  return { version: model.version, brief: currentBriefValue(), product_id: context.productId, market: MARKET[lang], ratio: context.ratio };
}
function briefPreviewLayerKey(context) {
  const body = briefPreviewBody(context);
  delete body.version;
  delete body.brief.brand.text_color;
  delete body.brief.brand.palette;
  return JSON.stringify(body);
}
function refreshBriefFlow(context = briefHost, { factsOnly = false } = {}) {
  if (!context || !ownsBrief(context)) return;
  const next = briefViewModel(context);
  const body = briefPreviewBody(context);
  delete body.version; // A save acknowledgement changes admission, not the visible composition.
  const signature = JSON.stringify({ body, tab, preview: next.preview,
    source: next.products.find(product => product.id === context.productId)?.image,
    run: model.run?.id });
  if (signature === context.previewSignature) {
    next.preview = context.vm.preview;
    if (JSON.stringify(next) === JSON.stringify(context.vm)) return;
    // Source facts and logo metadata may arrive during the image decode. Keep
    // that component and its model alive until the owned preview settles.
    if (context.flight) {
      context.metadataChanged = true;
      return;
    }
    if (!context.flight) {
      context.vm = next;
      renderBriefModel(context);
      return;
    }
    next.preview = briefViewModel(context).preview;
  }
  context.previewSignature = signature;
  context.generation++;
  clearTimeout(context.timer);
  context.vm = next;
  renderBriefModel(context);
  if (!briefPreviewPaused() && ["brand", "copy"].includes(tab) && (context.vm.preview.phase === "pending" ||
      (tab === "brand" && currentBriefValue().brand.logo_path && !context.vm.logo)))
    context.timer = setTimeout(() => flushBriefPreview(context), 120);
}
async function refreshBriefSources(version, savedBrief) {
  const owner = model, authored = draft;
  if (!model?.manifest && !authored?.products?.some(product => product.source?.role === "concept")) return;
  const signature = JSON.stringify(authored);
  if (signature !== JSON.stringify(savedBrief)) return;
  const current = () => model === owner && draft === authored && model.version === version &&
    JSON.stringify(draft) === signature;
  try {
    // Source relevance is decided by the existing server imagery-intent check.
    // Refresh only these facts; never replace raw drafts or adopt another save.
    const result = await api(`/api/motif/campaigns/${encodeURIComponent(authored.campaign_id)}?after_edit=true`);
    if (!current() || result.version !== version || result.value.campaign_id !== authored.campaign_id ||
        result.run?.id !== owner.run?.id) return;
    owner.concept_previews = result.concept_previews || {};
    owner.concept_preview_version = version;
    owner.wording_issue = result.wording_issue;
    owner.logo_issue = result.logo_issue;
    wordingCheckPhase = null;
    paintWordingAvailability();
    viewModel();
    refreshBriefFlow(briefHost, { factsOnly: true });
    if (page === "production") refreshProductionStatus();
  } catch (_) {
    // A read failure cannot turn an acknowledged Save into a failed write or
    // retry it. The next normal refresh can obtain the server-owned facts.
  }
}
function flushBriefPreview(context) {
  if (!ownsBrief(context) || briefPreviewPaused()) return;
  if (briefPreviewFlight) { context.queued = true; return; }
  if (context.vm.preview.phase !== "pending" && !(tab === "brand" && currentBriefValue().brand.logo_path && !context.vm.logo)) return;
  clearTimeout(context.timer);
  let identity;
  const flight = (async () => {
    // Preview the current fields immediately. The autosave queue keeps its own
    // debounce; temporary pixels do not need a preceding database write.
    if (!ownsBrief(context) || briefPreviewPaused() || !["brand", "copy"].includes(tab)) return;
    if (context.vm.preview.phase !== "pending" && !(tab === "brand" && currentBriefValue().brand.logo_path && !context.vm.logo)) return;
    clearTimeout(context.timer);
    context.queued = false;
    const body = briefPreviewBody(context);
    identity = { owner: context.owner, generation: context.generation, signature: JSON.stringify(body) };
    return context.view.preview({ identity, body });
  })().finally(() => {
    context.flight = null;
    if (briefPreviewFlight === flight) briefPreviewFlight = null;
    if (ownsBrief(context) && context.metadataChanged && identity?.generation === context.generation) {
      const next = briefViewModel(context);
      next.preview = context.vm.preview;
      if (JSON.stringify(next) !== JSON.stringify(context.vm)) {
        context.vm = next;
        renderBriefModel(context);
      }
      context.metadataChanged = false;
    }
    const latest = briefHost;
    if (latest?.queued && ownsBrief(latest) && !briefPreviewPaused()) {
      latest.queued = false;
      flushBriefPreview(latest);
    }
  });
  context.flight = briefPreviewFlight = flight;
}
async function withBriefPreviewPaused(action, current = () => true) {
  // Drain only this tab's owned background render. Another tab or real operation
  // still receives the server's ordinary concurrency guard; nothing is retried.
  const campaign = draft?.campaign_id;
  briefPreviewActions.set(campaign, (briefPreviewActions.get(campaign) || 0) + 1);
  if (briefHost) {
    clearTimeout(briefHost.timer);
    briefHost.queued = false;
    briefHost.generation++;
    briefHost.previewSignature = null;
  }
  try {
    if (briefPreviewFlight) await briefPreviewFlight.catch(() => {});
    if (current()) return await action();
  } finally {
    const remaining = briefPreviewActions.get(campaign) - 1;
    if (remaining) briefPreviewActions.set(campaign, remaining);
    else briefPreviewActions.delete(campaign);
    if (draft?.campaign_id === campaign && !briefPreviewPaused() && briefHost && ownsBrief(briefHost)) refreshBriefFlow(briefHost);
  }
}
async function briefRequestDigest(body) {
  const canonical = JSON.stringify(body, (_key, value) => value && typeof value === "object" && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value);
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical));
  return [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2, "0")).join("");
}
async function waitForBriefSaveReceipt(context) {
  if (!ownsBrief(context) || getSaveState()?.phase !== "saving") return;
  // A preview can reach the server after our save commits but before its
  // response reaches this browser. Keep the displayed image while that exact
  // save settles; this read must never adopt another window's newer version.
  await new Promise(resolve => {
    let unsubscribe, settled = false;
    unsubscribe = subscribeSaveState(state => {
      if (state.phase === "saving") return;
      settled = true;
      unsubscribe?.();
      resolve();
    });
    if (settled) unsubscribe?.();
  });
}
async function requestBriefPreview(context, request) {
  const current = () => ownsBrief(context) && request.identity.generation === context.generation;
  if (!current()) throw Error("Superseded preview");
  // A decoded-image failure may be an expired URL after our own autosave.
  // Only a newer token already acknowledged by this tab permits another read.
  if (request.expiredVersion !== undefined && request.expiredVersion === model.version) {
    await waitForBriefSaveReceipt(context);
    if (!current()) throw Error("Superseded preview");
    if (request.expiredVersion === model.version) throw Error("Preview image could not be loaded");
  }
  let body = { ...request.body, version: model.version };
  let expected = await briefRequestDigest(body), result;
  try {
    result = await api(`/api/motif/campaigns/${encodeURIComponent(context.owner)}/brief-preview`, body);
  } catch (error) {
    // Only this tab's acknowledged save can advance the token. An external
    // stale save remains an error; a read never adopts another tab's document.
    if (!current() || error.status !== 409) throw error;
    if (body.version === model.version) await waitForBriefSaveReceipt(context);
    if (!current() || body.version === model.version) throw error;
    body = { ...body, version: model.version };
    expected = await briefRequestDigest(body);
    result = await api(`/api/motif/campaigns/${encodeURIComponent(context.owner)}/brief-preview`, body);
  }
  if (!current()) throw Error("Superseded preview");
  if (result.request_digest !== expected || result.target.campaign_id !== context.owner ||
      result.target.product_id !== request.body.product_id || result.target.market !== request.body.market || result.target.ratio !== request.body.ratio)
    throw Error("Preview target mismatch");
  if (result.logo) {
    briefFacts.logos.set(result.logo.reference, { ...result.logo, filename: briefFacts.logos.get(result.logo.reference)?.filename || "Campaign logo" });
    context.metadataChanged = true;
  }
  const local = briefViewModel(context);
  if (["invalid", "color", "empty", "source"].includes(local.preview.phase)) {
    context.vm = local;
    renderBriefModel(context);
    throw Error("Current fields need an explicit correction");
  }
  if (result.status !== "ready" || !result.image?.url) {
    context.vm = briefViewModel(context);
    context.vm.preview = { phase: "invalid", message: result.issues.map(issue => issue.message).join(" ") };
    renderBriefModel(context); // Invalidates the component's pending decode.
    throw Error("Current fields need attention");
  }
  context.fitted = result.fitted_outputs.length ? `Resized to fit · ${result.fitted_outputs.length} ${result.fitted_outputs.length === 1 ? "image" : "images"}` : "";
  return { identity: request.identity, url: result.image.url, kind: result.kind, layers: result.layers,
    layerKey: briefPreviewLayerKey(context), version: body.version,
    alt: `Current Brief · ${briefTargetLabel({ name: request.body.brief.products.find(p => p.id === request.body.product_id)?.name, market: request.body.market, ratio: request.body.ratio })}`,
    note: context.fitted || undefined };
}
function setCampaignWording(field, value, market = MARKET[lang]) {
  draft.markets[market].copy[field] = field === "cta" ? value || null : value;
  for (const product of draft.products) {
    const wording = product.copy_overrides?.[market];
    if (wording) delete wording[field];
  }
}
// A destructive shared wording edit stays local until the producer confirms it.
// Other Campaign fields may still autosave while this decision is pending.
const campaignCopyChanges = new Map();
let campaignCopyTimer = null, campaignCopyDecision = null, campaignCopyReview = null;
function pendingCampaignCopy() {
  // Wording warnings block delivery, not editing or previewing current text.
  return [...campaignCopyChanges.values()].filter(change => change.owner === draft);
}
function requeueRestoredCampaignCopy() {
  // Older saves kept unfinished wording in form fields. Bring that wording
  // through the same replacement decision as typing it in this version.
  const owner = draft;
  let restored = false;
  for (const [language, market] of Object.entries(MARKET)) {
    if (!draft.markets[market]?.copy) continue;
    const fields = Object.assign({}, briefForm("campaign", language), briefForm("brand", language),
      briefForm("copy", language), language === lang ? briefForm(tab, language) : {});
    for (const field of ["message", "cta"]) {
      const control = BRIEF_FIELD[field], value = fields[control], original = draft.markets[market].copy[field];
      const key = `${draft.campaign_id}/${market}/${field}`;
      if (typeof value !== "string" || value === String(original ?? "") ||
          campaignCopyChanges.get(key)?.owner === draft) continue;
      const previousFields = copy(projectBuffers.form_drafts.fields || {});
      for (const [name, values] of Object.entries(previousFields))
        if (name.startsWith("brief/") && name.endsWith(`/${language}`) && control in values) values[control] = original ?? "";
      campaignCopyChanges.set(key, { key, owner, market, field, original,
        displayed: original, previousFields, value });
      restored = true;
    }
  }
  if (!restored) return;
  clearTimeout(campaignCopyTimer);
  campaignCopyTimer = setTimeout(() => {
    if (draft === owner && !modal && pendingCampaignCopy().length) saveProject().catch(() => {});
  }, 1200);
}
function requeueAllowedCampaignCopy(previousRules) {
  const market = MARKET[lang], fields = briefFields();
  for (const field of ["message", "cta"]) {
    const control = BRIEF_FIELD[field], value = fields[control], original = draft.markets[market].copy[field];
    if (!(control in fields) || String(value ?? "") === String(original ?? "") ||
        !MotifWording.matchingTerms(value, previousRules).length ||
        MotifWording.matchingTerms(value, draft.markets[market].phrase_rules).length) continue;
    const key = `${draft.campaign_id}/${market}/${field}`;
    const previousFields = copy(projectBuffers.form_drafts.fields || {});
    // The rule change makes an unfinished proposal eligible, not already applied.
    // Cancel restores applied wording while retaining the producer's rule edit.
    for (const [name, values] of Object.entries(previousFields))
      if (name.startsWith("brief/") && name.endsWith(`/${lang}`) && control in values) values[control] = original ?? "";
    campaignCopyChanges.set(key, { key, owner: draft, market, field, original,
      displayed: original, previousFields, value });
  }
}
function changeCampaignWording(field, value) {
  const market = MARKET[lang], key = `${draft.campaign_id}/${market}/${field}`;
  let change = campaignCopyChanges.get(key);
  const original = draft.markets[market].copy[field];
  const custom = draft.products.filter(product => {
    const wording = product.copy_overrides?.[market];
    return Object.hasOwn(wording || {}, field) && String(wording[field] ?? "") !== String(original ?? "");
  });
  if (!change) {
    change = { key, owner: draft, market, field, original, displayed: briefFields()[BRIEF_FIELD[field]] ?? draft.markets[market].copy[field],
      previousFields: copy(projectBuffers.form_drafts.fields || {}), products: custom.map(product => product.name) };
    campaignCopyChanges.set(key, change);
  }
  change.value = value;
  clearTimeout(campaignCopyTimer);
  campaignCopyTimer = setTimeout(() => {
    if (!modal && pendingCampaignCopy().length) saveProject().catch(() => {});
  }, 1200);
}
function safeCampaignCopyCapture(authored) {
  for (const change of pendingCampaignCopy()) {
    const control = BRIEF_FIELD[change.field], language = change.market.split("-")[0];
    for (const [name, fields] of Object.entries(authored.form_drafts?.fields || {})) {
      if (!name.startsWith("brief/") || !name.endsWith(`/${language}`)) continue;
      if (Object.hasOwn(change.previousFields[name] || {}, control)) fields[control] = change.previousFields[name][control];
      else delete fields[control];
      if (!Object.keys(fields).length) delete authored.form_drafts.fields[name];
    }
  }
  return authored;
}
function affectedCampaignCopy(change) {
  return draft.products.filter(product => {
    const wording = product.copy_overrides?.[change.market];
    return Object.hasOwn(wording || {}, change.field) &&
      String(wording[change.field] ?? "") !== String(change.original ?? "") &&
      String(wording[change.field] ?? "") !== String(change.value ?? "");
  });
}
function campaignCopyView() {
  const change = campaignCopyDecision.change, cta = change.field === "cta";
  const kind = cta ? "calls to action" : "headlines", count = affectedCampaignCopy(change).length;
  const language = change.market === "es-MX" ? "Spanish" : "English";
  return dialog(`Replace custom ${kind}?`,
    `<p>${count} ${count === 1 ? `product has a custom ${cta ? "call to action" : "headline"}` : `products have custom ${kind}`} in ${language}. This change will replace ${count === 1 ? "it" : "them"} with the ${cta ? "call to action" : "headline"} you just entered.</p>`,
    btn("Cancel change", "cancelCampaignCopy") + btn(`Replace ${kind}`, "confirmCampaignCopy", "primary"));
}
function finishCampaignCopy(replace) {
  const decision = campaignCopyDecision;
  if (!decision) return;
  campaignCopyDecision = null;
  const change = decision.change;
  campaignCopyChanges.delete(change.key);
  if (draft === change.owner) {
    if (replace) setCampaignWording(change.field, change.value, change.market);
    const control = BRIEF_FIELD[change.field], language = change.market.split("-")[0];
    for (const [name, fields] of Object.entries(projectBuffers.form_drafts.fields || {})) {
      if (!name.startsWith("brief/") || !name.endsWith(`/${language}`)) continue;
      if (replace) { if (control in fields) fields[control] = change.value; }
      else if (Object.hasOwn(change.previousFields[name] || {}, control)) fields[control] = change.previousFields[name][control];
      else delete fields[control];
    }
    if (page === "campaign" && MARKET[lang] === change.market) {
      const field = $(`[data-bf-field="${change.field}"]`);
      if (field) field.value = replace ? change.value : String(change.displayed ?? "");
    }
  }
  if (modal === "campaign-copy-confirm") closeModal({ retain: false });
  if (draft === change.owner) {
    refreshBriefFlow(); markDirty();
    if (page === "campaign" && MARKET[lang] === change.market) $(`[data-bf-field="${change.field}"]`)?.focus();
  }
  decision.resolve(true);
}
async function confirmCampaignCopyChanges() {
  clearTimeout(campaignCopyTimer);
  if (campaignCopyReview) return campaignCopyReview;
  campaignCopyReview = (async () => {
    while (pendingCampaignCopy().length) {
      if (modal && modal !== "campaign-copy-confirm") return false;
      await new Promise(resolve => {
        campaignCopyDecision = { change: pendingCampaignCopy()[0], resolve };
        const change = campaignCopyDecision.change;
        if (String(change.value ?? "") === String(change.original ?? "")) {
          // Correcting an invalid saved form back to the applied wording is a no-op
          // for product overrides, but must retire the unfinished field value.
          if (MotifWording.matchingTerms(change.displayed, draft.markets[change.market].phrase_rules).length) {
            change.displayed = change.original;
            const control = BRIEF_FIELD[change.field], language = change.market.split("-")[0];
            for (const [name, fields] of Object.entries(change.previousFields))
              if (name.startsWith("brief/") && name.endsWith(`/${language}`) && control in fields) fields[control] = change.original ?? "";
          }
          finishCampaignCopy(false);
        }
        else if (affectedCampaignCopy(change).length) openModal("campaign-copy-confirm");
        else finishCampaignCopy(true);
      });
    }
    return true;
  })();
  try { return await campaignCopyReview; }
  finally { campaignCopyReview = null; }
}
function paintBriefCopyErrors() {
  if (page !== "campaign" || tab !== "copy") return;
  const fields = briefFields(), rules = draft.markets[MARKET[lang]]?.phrase_rules || [];
  for (const field of ["message", "cta"]) {
    const input = $(`[data-bf-field="${field}"]`);
    if (!input?.dataset) continue;
    const terms = MotifWording.matchingTerms(fields[BRIEF_FIELD[field]] ?? draft.markets[MARKET[lang]].copy[field], rules);
    const id = `bf-${field}-wording`;
    let note = $(`#${id}`);
    if (terms.length) {
      if (!note) { input.insertAdjacentHTML("afterend", `<p class="mw-field-error" id="${id}"></p>`); note = $(`#${id}`); }
      note.textContent = `Remove ${terms.map(term => `“${term}”`).join(", ")} or edit the prohibited words.`;
      input.setAttribute("aria-invalid", "true"); input.setAttribute("data-wording-invalid", "");
      const descriptions = new Set((input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
      descriptions.add(id); input.setAttribute("aria-describedby", [...descriptions].join(" "));
    } else {
      note?.remove(); input.removeAttribute("data-wording-invalid");
      if (!input.hasAttribute("data-length-invalid")) input.removeAttribute("aria-invalid");
      const descriptions = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(value => value && value !== id);
      if (descriptions.length) input.setAttribute("aria-describedby", descriptions.join(" ")); else input.removeAttribute("aria-describedby");
    }
  }
}
function changeBriefField(context, key, value, { commit = false } = {}) {
  if (!ownsBrief(context) || context.rendering) return;
  if (typeof brandUpdating !== "undefined" && brandUpdating) return;
  if (key === "market") {
    if (!Object.hasOwn(draft.markets, value)) return;
    captureProject();
    return briefAction(context, `language:${value}`);
  }
  if (key === "previewProduct") { context.productId = value; refreshBriefFlow(context); return; }
  const raw = currentBriefValue(), normalize = MotifBriefFlow.normalizeHex;
  context.paletteMessage = "";
  context.paletteTarget = null;
  if (key === "defaultColor") {
    if (projectBuffers.form_drafts.brief_defaults) delete projectBuffers.form_drafts.brief_defaults[lang];
    if (commit) value = normalize(value) || value;
    draft.brand.text_color = value;
    rememberBriefField("text-color", value);
  } else if (key.startsWith("palette-")) {
    const values = [...raw.brand.palette], index = Number(key.split("-")[1]);
    if (!Number.isInteger(index) || index < 0 || index >= values.length) return;
    values[index] = commit ? normalize(value) || value : value;
    setBriefPalette(commit ? dedupeBriefPalette(values) : values);
  } else {
    const property = { campaignName: "name", audience: "audience", direction: "creative_direction" }[key];
    if (property) draft[property] = value;
    else if (key === "brandName") draft.brand.name = value;
    else if (key === "copyOrigin") draft.copy_provenance.label = value;
    else if (key === "copyRights") draft.copy_provenance.rights = value;
    else if (["message", "cta"].includes(key)) changeCampaignWording(key, value);
    else return;
    rememberBriefField(BRIEF_FIELD[key], value);
  }
  paintBriefCopyErrors();
  markDirty();
  refreshBriefFlow(context);
}
function dedupeBriefPalette(values) {
  const seen = new Set();
  return values.filter(value => {
    const normalized = MotifBriefFlow.normalizeHex(value);
    if (!normalized) return true;
    if (seen.has(normalized)) return false;
    seen.add(normalized); return true;
  }).map(value => MotifBriefFlow.normalizeHex(value) || value);
}
function setBriefPalette(values) {
  draft.brand.palette = values;
  rememberBriefField("brand-palette", values.join(","));
}
async function briefAction(context, action) {
  if (!ownsBrief(context)) return;
  if (typeof brandUpdating !== "undefined" && brandUpdating) return;
  const [verb, ...parts] = action.split(":"), arg = parts.join(":"), normalize = MotifBriefFlow.normalizeHex;
  const value = currentBriefValue(), vm = briefViewModel(context);
  if (verb === "remove-logo") {
    if (!draft.brand.logo_path || briefLogoUploadPending()) return;
    draft.brand.logo_path = "";
    draft.brand.logo_provenance = { label: "Not provided", rights: "Not provided", synthetic: false };
    context.logoStatus = "accepted"; context.logoMessage = "";
    markDirty(); refreshBriefFlow(context); return;
  }
  if (verb === "cancel-logo-upload") { cancelBriefLogoUpload(context); return; }
  if (verb === "open-studio" && briefLogoUploadPending()) {
    toast("Wait for the logo upload to finish, or cancel the upload in Brand."); return;
  }
  if (verb === "tab") {
    if (arg !== "brand" && typeof guardBrandNavigation === "function" && guardBrandNavigation({ kind: "tab", target: arg })) return;
    tab = arg; refreshBriefFlow(context); return;
  }
  if (verb === "language") { lang = arg === "es-MX" ? "es" : "en"; refreshBriefFlow(context); return; }
  if (verb === "format") { context.ratio = arg; refreshBriefFlow(context); return; }
  if (verb === "retry-preview") { context.previewSignature = null; refreshBriefFlow(context); return; }
  if (verb === "update-images") { await updateBrandImages(); return; }
  if (verb === "use-preview-default") {
    const product = draft.products.find(item => item.id === context.productId);
    const placements = product?.framing?.[MARKET[lang]]?.[context.ratio]?.text_placements;
    for (const placement of Object.values(placements || {})) placement.color = null;
    if (model.manifest?.outputs?.some(output => output.product_id === context.productId && output.market === MARKET[lang] && output.ratio === context.ratio && output.url)) {
      const targets = projectBuffers.form_drafts.brand_color_defaults ||= [];
      if (!targets.some(target => target.product_id === context.productId && target.market === MARKET[lang] && target.ratio === context.ratio))
        targets.push({ product_id: context.productId, market: MARKET[lang], ratio: context.ratio });
    }
    markDirty(); refreshBriefFlow(context); return;
  }
  if (["use-default", "add-default", "make-default"].includes(verb)) {
    const next = normalize(verb === "make-default" ? vm.palette[Number(arg)] : vm.defaultDraft);
    if (!next) { toast("Enter six hex digits, with or without #."); return; }
    const present = vm.palette.some(color => normalize(color) === next);
    if (!present && (verb !== "add-default" || vm.palette.length >= 8)) { refreshBriefFlow(context); return; }
    setBriefPalette(dedupeBriefPalette(present ? vm.palette : [...vm.palette, next]));
    draft.brand.text_color = next;
    rememberBriefField("text-color", next);
    if (projectBuffers.form_drafts.brief_defaults) delete projectBuffers.form_drafts.brief_defaults[lang];
  } else if (verb === "add-color") {
    if (vm.palette.length >= 8) return;
    setBriefPalette([...vm.palette, ""]);
  } else if (verb === "restore-default" || verb === "restore-used-color") {
    const color = verb === "restore-default" ? vm.color : context.affectedColor?.color;
    if (!color || vm.palette.length >= 8) return;
    setBriefPalette(dedupeBriefPalette([...vm.palette, color]));
  } else if (verb === "remove-color") {
    const index = Number(arg), color = normalize(vm.palette[index]);
    const use = explicitBriefColors(value).find(item => item.color === color);
    if (vm.palette.length <= 1) context.paletteMessage = "Keep at least one palette color.";
    else if (color === vm.color) context.paletteMessage = "Choose a different default before removing this color.";
    else if (use) { context.paletteMessage = `${briefTargetLabel(use)} uses ${color}. Change that block in Studio before removing its color.`; context.paletteTarget = use; }
    else setBriefPalette(vm.palette.filter((_entry, i) => i !== index));
  } else if (verb === "open-studio") {
    if (!await flushCurrentWork()) { toast("Your changes are still here. Try saving again before opening Studio."); return; }
    const affected = context.affectedColor || context.paletteTarget;
    const target = affected || { product: context.productId, market: MARKET[lang], ratio: context.ratio };
    const index = draft.products.findIndex(p => p.id === target.product);
    if (index < 0 || !model.manifest) { toast("Create this product’s images before opening Studio."); return; }
    selected[0] = currentProduct = index;
    formats[0] = currentFormat = FMT.findIndex(f => f.ratio === target.ratio);
    lang = target.market === "es-MX" ? "es" : "en";
    page = "review"; reviewTab = "product"; updateURL(); render();
    if (affected) {
      openModal("text");
      if (modal === "text" && textEditor) { textEditor.selected = affected.block; paintText(); }
    }
    return;
  } else {
    const mapped = { production: "nav:production", "add-product": "addProduct", "edit-product": `product:${arg}`, "remove-product": `remove:${arg}`,
      source: `productSource:${arg}`, "attach-missing": `attachMissing:${arg}`, "add-rule": "rule", "edit-rule": `editRule:${arg}`, recheck: "recheck" }[verb];
    if (!mapped) return;
    await dispatchAction({ action: mapped }); return;
  }
  markDirty(); refreshBriefFlow(context);
  if (verb === "add-color") $(`#bf-palette-${draft.brand.palette.length - 1}`)?.focus();
}
async function attachBriefLogo(context, file) {
  if (!ownsBrief(context)) return;
  if (typeof brandUpdating !== "undefined" && brandUpdating) return;
  cancelBriefLogoUpload(context);
  const generation = ++context.upload, owner = context.owner;
  const upload = { controller: new AbortController(), timer: null };
  context.logoUpload = upload;
  upload.timer = setTimeout(() => upload.controller.abort(), 30000);
  context.logoStatus = "pending"; context.logoMessage = ""; refreshBriefFlow(context);
  try {
    const asset = await withBriefPreviewPaused(() => api("/api/uploads", undefined, { method: "POST", headers: {
      "Content-Type": file.type || "application/octet-stream", "X-Campaign-Token": $('meta[name="campaign-token"]').content,
      "X-Campaign-Id": owner, "X-Image-Role": "logo",
      "X-Asset-Name": encodeURIComponent((draft.brand?.name || "Brand") + " logo"),
    }, body: file, signal: upload.controller.signal }), () => ownsBrief(context) && generation === context.upload && !upload.controller.signal.aborted);
    if (!asset || !ownsBrief(context) || generation !== context.upload) return;
    draft.brand.logo_path = asset.reference;
    draft.brand.logo_provenance = copy(asset.provenance);
    briefFacts.logos.set(asset.reference, { ...asset, filename: file.name });
    context.logoStatus = "accepted";
    context.logoMessage = "";
    markDirty(); refreshBriefFlow(context);
  } catch (error) {
    if (!ownsBrief(context) || generation !== context.upload) return;
    context.logoStatus = "";
    context.logoMessage = upload.controller.signal.aborted
      ? "The logo upload took too long. Choose the image again." : "";
    refreshBriefFlow(context);
    if (upload.controller.signal.aborted) return;
    imageInputFailure(error, file, true, draft, () =>
      ownsBrief(context) && tab === "brand" ? $('#brief-root [data-bf-action="upload-logo"]') : null);
  } finally {
    clearTimeout(upload.timer);
    if (context.logoUpload === upload) {
      context.logoUpload = null;
      if (context.logoStatus === "pending") {
        context.logoStatus = "";
        context.logoMessage = "The logo upload took too long. Choose the image again.";
        if (ownsBrief(context)) refreshBriefFlow(context);
      }
    }
  }
}
