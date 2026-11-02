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
  if (wording) for (const [key, property] of [["copy-headline", "message"], ["copy-cta", "cta"]])
    if (key in fields) wording[property] = property === "cta" ? fields[key] || null : fields[key];
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
  const accepted = projectBuffers.form_drafts.brief_defaults?.[lang] ?? draft.brand.text_color;
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
  else if (!normalized.includes(color)) {
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
  if (selected && !value.brand.logo_path) preview = { phase: "invalid", message: "Attach a visible PNG or JPEG logo to preview this brief." };
  const override = selected?.copy_overrides?.[MARKET[lang]] || {};
  return {
    id: value.campaign_id, name: value.name, tab, language: MARKET[lang],
    ratio: context.ratio, productId: context.productId, brandName: value.brand.name,
    audience: value.audience, direction: value.creative_direction,
    copyOrigin: value.copy_provenance.label, copyRights: value.copy_provenance.rights,
    markets: Object.keys(value.markets), productLimit, mutationsBlocked: !!operation?.active || model.run?.state === "running",
    products: value.products.map(p => ({
      id: p.id, name: p.name, description: p.description,
      mode: p.source?.role === "concept" ? "concept" : "photo",
      image: p.source?.role === "concept" ? model.concept_previews?.[p.id] || null : assetURL(p.source?.path, p.id),
      attachmentHint: p.source?.role !== "concept" && !p.source?.path
        ? model.attachment_hints?.[p.id] || null : null,
    })),
    logo: info ? { ...info, url: assetURL(value.brand.logo_path), filename: info.filename || "Campaign logo" } : null,
    logoStatus: context.logoStatus || "accepted", logoMessage: context.logoMessage || "",
    fitted: context.fitted || "", color, defaultDraft: rawDefault, palette,
    colorMessage, colorAction, paletteMessage, paletteAction,
    copy: Object.fromEntries(Object.entries(value.markets).map(([m, data]) => [m, data.copy])),
    rules: Object.fromEntries(Object.entries(value.markets).map(([m, data]) => [m, data.phrase_rules || []])),
    hasSavedImages: !!model.manifest,
    overrideNotice: Object.keys(override).length ? `${selected.name} has custom wording for this language. These fields edit campaign wording; the preview includes that product’s overrides.` : null,
    preview,
  };
}
function destroyBriefFlow() {
  if (!briefHost) return;
  briefHost.dead = true;
  clearTimeout(briefHost.timer);
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
  try { context.view.render(context.vm); }
  finally { context.rendering = false; }
}
function refreshBriefFlow(context = briefHost) {
  if (!context || !ownsBrief(context)) return;
  context.generation++;
  clearTimeout(context.timer);
  context.vm = briefViewModel(context);
  renderBriefModel(context);
  if (!briefPreviewPaused() && ["brand", "copy"].includes(tab) && (context.vm.preview.phase === "pending" ||
      (tab === "brand" && currentBriefValue().brand.logo_path && !context.vm.logo)))
    context.timer = setTimeout(() => flushBriefPreview(context), 300);
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
    const result = await api(`/api/motif/campaigns/${encodeURIComponent(authored.campaign_id)}`);
    if (!current() || result.version !== version || result.value.campaign_id !== authored.campaign_id ||
        result.run?.id !== owner.run?.id) return;
    owner.concept_previews = result.concept_previews || {};
    owner.concept_preview_version = version;
    owner.wording_issue = result.wording_issue;
    wordingCheckPhase = null;
    paintWordingAvailability();
    viewModel();
    refreshBriefFlow();
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
  const body = { version: model.version, brief: currentBriefValue(), product_id: context.productId, market: MARKET[lang], ratio: context.ratio };
  const identity = { owner: context.owner, generation: context.generation, signature: JSON.stringify(body) };
  const flight = context.view.preview({ identity, body }).finally(() => {
    context.flight = null;
    if (briefPreviewFlight === flight) briefPreviewFlight = null;
    if (ownsBrief(context) && context.metadataChanged && identity.generation === context.generation) {
      const preview = context.vm.preview;
      context.vm = briefViewModel(context);
      context.vm.preview = preview;
      renderBriefModel(context);
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
async function requestBriefPreview(context, request) {
  const current = () => ownsBrief(context) && request.identity.generation === context.generation;
  const expected = await briefRequestDigest(request.body);
  const result = await api(`/api/motif/campaigns/${encodeURIComponent(context.owner)}/brief-preview`, request.body);
  if (!current()) throw Error("Superseded preview");
  if (result.request_digest !== expected || result.target.campaign_id !== context.owner ||
      result.target.product_id !== request.body.product_id || result.target.market !== request.body.market || result.target.ratio !== request.body.ratio)
    throw Error("Preview target mismatch");
  if (result.logo) {
    briefFacts.logos.set(result.logo.reference, { ...result.logo, filename: briefFacts.logos.get(result.logo.reference)?.filename || "Campaign logo" });
    context.metadataChanged = true;
  }
  const local = briefViewModel(context);
  if (["invalid", "color", "empty"].includes(local.preview.phase)) {
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
  return { identity: request.identity, url: result.image.url, kind: result.kind,
    alt: `Current Brief · ${briefTargetLabel({ name: request.body.brief.products.find(p => p.id === request.body.product_id)?.name, market: request.body.market, ratio: request.body.ratio })}`,
    note: context.fitted || undefined };
}
function changeBriefField(context, key, value, { commit = false } = {}) {
  if (!ownsBrief(context) || context.rendering) return;
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
    const defaults = projectBuffers.form_drafts.brief_defaults ||= {};
    if (!(lang in defaults)) defaults[lang] = draft.brand.text_color;
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
    else if (["message", "cta"].includes(key)) draft.markets[MARKET[lang]].copy[key] = key === "cta" ? value || null : value;
    else return;
    rememberBriefField(BRIEF_FIELD[key], value);
  }
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
  const [verb, ...parts] = action.split(":"), arg = parts.join(":"), normalize = MotifBriefFlow.normalizeHex;
  const value = currentBriefValue(), vm = briefViewModel(context);
  if (verb === "tab") { tab = arg; refreshBriefFlow(context); return; }
  if (verb === "language") { lang = arg === "es-MX" ? "es" : "en"; refreshBriefFlow(context); return; }
  if (verb === "format") { context.ratio = arg; refreshBriefFlow(context); return; }
  if (verb === "retry-preview") { refreshBriefFlow(context); return; }
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
  const generation = ++context.upload, owner = context.owner;
  context.logoStatus = "pending"; context.logoMessage = ""; refreshBriefFlow(context);
  try {
    const asset = await withBriefPreviewPaused(() => api("/api/uploads", undefined, { method: "POST", headers: {
      "Content-Type": file.type || "application/octet-stream", "X-Campaign-Token": $('meta[name="campaign-token"]').content,
      "X-Campaign-Id": owner, "X-Image-Role": "logo",
    }, body: file }), () => ownsBrief(context) && generation === context.upload);
    if (!ownsBrief(context) || generation !== context.upload) return;
    draft.brand.logo_path = asset.reference;
    draft.brand.logo_provenance = copy(asset.provenance);
    briefFacts.logos.set(asset.reference, { ...asset, filename: file.name });
    context.logoStatus = "accepted";
    context.logoMessage = "Logo attached.";
    markDirty(); refreshBriefFlow(context);
  } catch (error) {
    if (!ownsBrief(context) || generation !== context.upload) return;
    context.logoStatus = "";
    context.logoMessage = "";
    refreshBriefFlow(context);
    imageInputFailure(error, file, true, draft, () =>
      ownsBrief(context) && tab === "brand" ? $('#brief-root [data-bf-action="upload-logo"]') : null);
  }
}
