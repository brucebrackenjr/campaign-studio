"use strict";

// A product visit owns one shell and one existing editor controller at a time.
// Requests, geometry, and publication stay in those controllers.
let studioWorkspace = null;
// One target's verified preview work lives only for this Studio visit. A new
// document or target releases every old image reference and preview response.
function studioPreviewCache() {
  const owner = studioWorkspace;
  if (!owner) return null;
  const key = JSON.stringify([C.id, model?.composition_version || model?.version,
    model?.manifest?.revision_id, C.products[currentProduct]?.id, lang, currentFormat]);
  if (owner.previewCache?.key !== key) owner.previewCache = { key, requests: new Map(), images: new Map(), readyImages: new Set() };
  return owner.previewCache;
}
function studioRetainImage() {
  const owner = studioWorkspace;
  if (!owner?.root?.querySelector || !modal) return;
  const cache = studioPreviewCache();
  const image = owner.root.querySelector('#text-image:not([hidden]),#frame-image:not([hidden]),.mf-logo-composite:not([hidden]),[data-ws-slot="surface"] > img');
  if (image?.complete && image.naturalWidth && image.style.visibility !== "hidden" &&
      (owner.surfaceKey === cache.key || cache.readyImages.has(image.currentSrc || image.src)))
    owner.retainedImage = { image, url: image.currentSrc || image.src, key: studioPreviewCache().key };
}
function studioRetainedImage() {
  return studioWorkspace?.retainedImage?.key === studioPreviewCache()?.key ? studioWorkspace.retainedImage.image : null;
}
// A visible surface is presentation only: it never authorizes a save or a
// preview cache hit. Keep one same-target surface while the next tool decodes.
function studioVisualTarget() {
  return JSON.stringify([C.id, C.products[currentProduct]?.id, lang, currentFormat]);
}
function studioRetainSurface() {
  const owner = studioWorkspace;
  if (!owner?.root?.querySelector || !modal || owner.visualTarget !== studioVisualTarget()) return;
  const slot = owner.root.querySelector('[data-ws-slot="surface"]');
  if (!slot || slot.querySelector('.ws-preview-continuity')) return;
  const visible = node => node && !node.hidden && node.style.visibility !== 'hidden' &&
    (node.tagName === 'CANVAS' || (node.complete && node.naturalWidth));
  const canvas = document.createElement('canvas');
  const format = FMT[currentFormat];
  canvas.width = format.w || format.width; canvas.height = format.h || format.height;
  const paint = canvas.getContext('2d');
  if (!paint) return;
  const image = modal === 'text'
    ? [slot.querySelector('#text-canvas'), slot.querySelector('#text-image')].find(visible)
    : modal === 'frame'
      ? [slot.querySelector('#frame-live'), slot.querySelector('#frame-image')].find(visible)
      : modal === 'logo'
        ? [slot.querySelector('.mf-logo-composite')].find(visible)
        : [...slot.querySelectorAll('img')].find(visible);
  if (image) paint.drawImage(image, 0, 0, canvas.width, canvas.height);
  else if (modal === 'logo' && !logoEditor?.loading) {
    const base = slot.querySelector('.mf-logo-photo'), copy = slot.querySelector('.mf-logo-copy');
    if (!visible(base) || !visible(copy)) return;
    paint.drawImage(base, 0, 0, canvas.width, canvas.height);
    const art = slot.querySelector('[data-lg-art]'), logo = art?.querySelector('img');
    if (visible(logo) && !art.hidden) paint.drawImage(logo,
      parseFloat(art.style.left) / 100 * canvas.width, parseFloat(art.style.top) / 100 * canvas.height,
      parseFloat(art.style.width) / 100 * canvas.width, parseFloat(art.style.height) / 100 * canvas.height);
    paint.drawImage(copy, 0, 0, canvas.width, canvas.height);
  } else return;
  canvas.className = 'ws-preview-continuity'; canvas.setAttribute('aria-hidden', 'true');
  owner.retainedSurface = { node: canvas, target: owner.visualTarget };
}
function studioShowRetainedSurface() {
  const owner = studioWorkspace, held = owner?.retainedSurface;
  if (!held || held.target !== studioVisualTarget()) { if (owner) owner.retainedSurface = null; return; }
  owner.root.querySelector('[data-ws-slot="surface"]')?.append(held.node);
}
function studioFormatSurface(format) {
  const owner = studioWorkspace;
  if (!owner?.thumbnails) return null;
  const output = outputFor(currentProduct, lang, format);
  const signature = studioThumbnailSignature(output, format);
  const target = JSON.stringify([C.id, C.products[currentProduct]?.id, lang, format]);
  for (const [button, state] of owner.thumbnails) {
    if (state.target !== target || state.signature !== signature) continue;
    const image = button.querySelector('.ws-thumb img');
    if (!image?.complete || !image.naturalWidth) return null;
    // This thumbnail already shows this format's current settings, even when
    // another format's edit changed the campaign's preview URL. Copy its pixels
    // without moving or reloading the thumbnail while the new tool loads.
    const canvas = document.createElement('canvas');
    canvas.width = FMT[format].w; canvas.height = FMT[format].h;
    const paint = canvas.getContext('2d');
    if (!paint) return null;
    paint.drawImage(image, 0, 0, canvas.width, canvas.height);
    canvas.className = 'ws-preview-continuity'; canvas.setAttribute('aria-hidden', 'true');
    return { node: canvas, target };
  }
  return null;
}
function studioPreviewReady() {
  const owner = studioWorkspace;
  if (!owner) return;
  owner.root?.querySelector?.('.ws-preview-continuity')?.remove();
  owner.retainedSurface = null;
}
function imageEditorRoot() { return studioWorkspace?.root || $("#overlay"); }
function studioTextAvailable() {
  const output = outputFor(currentProduct, lang, currentFormat);
  return canEditProduct(currentProduct) && !!output;
}
function studioBusy() {
  return !!(textEditor?.saving || frameSaving || logoEditor?.busy || logoEditor?.uploading || logoEditor?.reviewing);
}
function studioDirty() {
  if (!studioWorkspace) return false;
  if (modal === "text") return Object.keys(textEditor?.placements || {}).length > 0 || wordingIntent(textEditor).fields.length > 0;
  if (modal === "frame") return JSON.stringify(framingDraft) !== framingInitial;
  if (modal === "logo") return logoEditor?.dirty();
  return false;
}
function studioApplyButton() {
  const selector = modal === "logo" ? '[data-lg="save"]' : modal === "frame"
    ? framingRecovery === "apply" ? '#frame-primary' : '#frame-primary[data-action="saveFrame"]'
    : textEditor?.recovery === "apply" ? '[data-action="recoverText"]' : '[data-action="applyText"]';
  return $(selector, studioWorkspace?.root);
}
async function resolveStudioChanges() {
  const owner = studioWorkspace;
  if (!owner) return true;
  if (logoEditor?.uploading || framingPan?.active() || framingDrawing || textEditor?.input?.active()) return false;
  return await flushStudioSave() && studioWorkspace === owner;
}
function teardownStudioTool() {
  const owner = studioWorkspace;
  if (!owner) return;
  studioRetainImage();
  studioRetainSurface();
  owner.tearingDown = true;
  studioSave?.stop(); studioSave = null;
  clearTimeout(owner.savedTimer);
  owner.logoLoad?.abort();
  owner.logoLoad = null;
  owner.observer?.disconnect();
  if (modal === "text") endText();
  if (modal === "frame") endFraming();
  if (modal === "logo") logoEditor?.dismiss();
  if (modal === "inspect") captureProject();
  modal = "";
  owner.root.classList.remove("mf-logo-modal");
  owner.tearingDown = false;
}
function studioShellView(mode) {
  const product = C.products[currentProduct];
  return {
    productName: product.name, productNumber: currentProduct + 1, productCount: C.products.length,
    markets: C.markets.map(language => ({ id: MARKET[language], label: LANG[language] })),
    market: MARKET[lang], marketLabel: LANG[lang], mode,
    formats: FMT.map((format, index) => ({ id: format.ratio, name: format.name,
      url: outputFor(currentProduct, lang, index)?.thumbnail_url || outputFor(currentProduct, lang, index)?.url, available: !!outputFor(currentProduct, lang, index) && (!!outputFor(currentProduct, lang, index)?.url || canEditProduct(currentProduct)) })),
    format: FMT[currentFormat].ratio, formatLabel: FMT[currentFormat].name,
  };
}
async function openStudioWorkspace(mode = "text") {
  if (studioWorkspace) return switchStudioTarget({ mode });
  selected[0] = currentProduct;
  formats[0] = currentFormat;
  reviewTab = "product";
  render();
  if (mode !== "inspect") await switchStudioTarget({ mode });
}
function mountStudioVisit(mode = "inspect") {
  if (!outputFor(currentProduct, lang, currentFormat)?.url && studioTextAvailable()) mode = "text";
  const owner = studioWorkspace = { campaign: C.id, product: C.products[currentProduct].id, mode, sequence: 0 };
  document.body.classList.add("ws-active");
  owner.root = $(".ws-workspace");
  owner.root.classList.add("mf-editor", "is-direct");
  for (const type of ["input", "change", "pointerup", "keyup", "click"]) owner.root.addEventListener(type, event => {
    if (event.target.closest("[data-studio-save],[data-studio-recovery]")) return;
    queueMicrotask(() => { if (studioWorkspace === owner) scheduleStudioSave(type === "change" || type === "pointerup"); });
  });
  owner.root.addEventListener("click", async event => {
    const control = event.target.closest("[data-ws-mode],[data-ws-target],[data-ws-action]");
    if (!control || control.disabled || control.dataset.action || control.dataset.lg) return;
    event.stopPropagation();
    try {
      if (control.dataset.wsMode) await switchStudioTarget({ mode: control.dataset.wsMode });
      else if (control.dataset.wsTarget === "format") await switchStudioTarget({ format: FMT.findIndex(f => f.ratio === control.dataset.wsFormat) });
      else if (control.dataset.wsAction === "all-products" && await leaveStudioWorkspace()) {
        reviewTab = "products"; render();
      }
    } catch (error) { toast(error.message); }
  });
  owner.root.addEventListener("change", async event => {
    if (!event.target.matches('[data-ws-target="market"]')) return;
    const next = event.target.value === "es-MX" ? "es" : "en";
    event.target.value = MARKET[lang];
    try { await switchStudioTarget({ language: next }); } catch (error) { toast(error.message); }
  });
  mountStudioTool(mode).catch(error => toast(error.message));
}
function refreshStudioTargets() {
  if (!studioWorkspace) return;
  const root = studioWorkspace.root;
  for (const button of $$('[data-ws-mode]', root)) {
    const output = outputFor(currentProduct, lang, currentFormat);
    const available = !!output && (!!output.url || canEditProduct(currentProduct));
    button.disabled = button.dataset.wsMode === "text" ? !studioTextAvailable()
      : button.dataset.wsMode === "inspect" ? !available
      : button.dataset.wsMode === "logo" ? !canEditLogo(currentProduct) || !available
      : !canEditProduct(currentProduct) || !available;
  }
  $('[data-ws-target="market"]', root).value = MARKET[lang];
  for (const button of $$('[data-ws-target="format"]', root)) {
    const index = FMT.findIndex(format => format.ratio === button.dataset.wsFormat);
    const output = outputFor(currentProduct, lang, index);
    button.disabled = !output || !(output.url || canEditProduct(currentProduct));
    button.setAttribute("aria-pressed", String(index === currentFormat));
    button.setAttribute("aria-label", `${FMT[index].name} · ${FMT[index].ratio}${output?.url ? "" : " · Image unavailable"}`);
    const thumbnail = $(".ws-thumb", button);
    const url = output?.thumbnail_url || output?.url;
    refreshStudioThumbnail(button, thumbnail, output, index, url);
  }
}
function studioThumbnailSignature(output, format) {
  const product = model.output_brief?.products?.find(item => item.id === C.products[currentProduct]?.id);
  const brand = model.output_brief?.brand;
  // A document version changes for every autosave. The visible pixels only
  // change when this product/market/format's actual composition changes.
  return JSON.stringify([C.id, C.products[currentProduct]?.id, lang, format,
    !!output?.url, output?.copy, product?.source, product?.framing?.[MARKET[lang]]?.[FMT[format].ratio],
    brand?.logo_path, brand?.text_color,
    model.source_urls?.[product?.id]?.replace(/\/composition\/[^/]+\//, "/composition/current/")]);
}
function refreshStudioThumbnail(button, thumbnail, output, format, url) {
  const owner = studioWorkspace;
  owner.thumbnails ||= new Map();
  const signature = studioThumbnailSignature(output, format);
  const target = JSON.stringify([C.id, C.products[currentProduct]?.id, lang, format]);
  let state = owner.thumbnails.get(button);
  let shown = thumbnail.querySelector?.("img");
  if (!state) {
    state = { signature, target, url, sequence: 0 };
    owner.thumbnails.set(button, state);
    // The shell has already requested this exact target. Retain that node.
    if (shown && url && shown.getAttribute("src") === url) return;
  } else if (state.target !== target) {
    // An old market or product must never appear under the new target label.
    state.target = target; state.signature = null; state.pending = null; state.sequence++;
    thumbnail.innerHTML = '<span aria-hidden="true">–</span>';
    shown = null;
  } else if (state.signature === signature && shown?.complete && shown.naturalWidth) {
    // An unchanged, decoded thumbnail is presentation only. Never use its old
    // URL to authorize a new preview request or save.
    return;
  }
  if (!url) {
    state.signature = signature; state.url = null; state.sequence++;
    thumbnail.innerHTML = '<span aria-hidden="true">–</span>';
    return;
  }
  if (state.pending === url) return;
  const sequence = ++state.sequence;
  state.pending = url;
  const image = new Image(); image.alt = ""; image.src = url;
  state.ready = image.decode().then(() => {
    if (studioWorkspace !== owner || state.sequence !== sequence ||
        studioThumbnailSignature(outputFor(currentProduct, lang, format), format) !== signature) return;
    thumbnail.replaceChildren(image);
    state.signature = signature; state.url = url; state.pending = null;
    button.removeAttribute("title");
  }).catch(() => {
    if (studioWorkspace !== owner || state.sequence !== sequence) return;
    state.pending = null;
    button.title = "Preview could not be updated. Select this format to retry.";
  });
}
function mountStudioParts(markup, { tools, surface, status, footer = ".modal-foot .row" }) {
  const root = studioWorkspace.root, template = document.createElement("template");
  template.innerHTML = markup;
  const source = template.content, controls = source.querySelector(tools), canvas = source.querySelector(surface);
  controls.querySelector(".mf-editor-target,.framing-product,.mf-logo-target")?.remove();
  const previewLabel = source.querySelector("#frame-preview-label,[data-lg-preview-label]");
  if (previewLabel) controls.prepend(previewLabel);
  MotifStudioShell.setMode(root, { mode: studioWorkspace.mode, title: {text:"Text",framing:"Framing",logo:"Logo",inspect:"Inspect"}[studioWorkspace.mode],
    target: `${LANG[lang]} · ${FMT[currentFormat].name}`, controlsHtml: "", applyLabel: "Apply changes", canApply: false, status: "" });
  $('[data-ws-slot="controls"]', root).replaceChildren(controls);
  // Move the already decoded image into the next tool instead of clearing it.
  const retained = studioRetainedImage();
  const nextImage = canvas.querySelector('#text-image,#frame-image,.mf-logo-composite');
  // Adopt the new stage into the live document first. Moving a decoded image
  // through template.content's inert document would discard its decoded pixels.
  $('[data-ws-slot="surface"]', root).replaceChildren(canvas);
  if (retained && nextImage) {
    for (const name of [...retained.attributes].map(attribute => attribute.name)) if (name !== "src") retained.removeAttribute(name);
    for (const attribute of [...nextImage.attributes]) if (attribute.name !== "src") retained.setAttribute(attribute.name, attribute.value);
    retained.hidden = false;
    nextImage.replaceWith(retained);
  }
  studioShowRetainedSurface();
  const foot = $('[data-ws-slot="commit"]', root);
  foot.replaceChildren(source.querySelector(status), source.querySelector(footer));
  foot.hidden = false;
  const apply = foot.querySelector('[data-action="applyText"],#frame-primary,[data-lg="save"]');
  if (apply) { apply.dataset.wsAction = "apply"; apply.hidden = true; }
  const cancel = foot.querySelector('[data-action="close"],[data-lg="cancel"]');
  if (cancel) { cancel.dataset.wsAction = "cancel"; cancel.hidden = true; }
  const reset = root.querySelector('[data-action="resetText"],[data-action="restoreFrame"],[data-lg="reset"]');
  if (reset) {
    const resetSlot = document.createElement("div");
    resetSlot.className = "ws-reset-control";
    reset.classList.remove("quiet", "small");
    reset.textContent = "Reset";
    reset.setAttribute("aria-label", { text: "Reset text layout", framing: "Reset framing", logo: "Reset logo placement" }[studioWorkspace.mode]);
    resetSlot.append(reset);
    controls.querySelector(".mf-logo-reset")?.remove();
    $('[data-ws-slot="controls"]', root).append(resetSlot);
  }
  const statusNode = foot.querySelector("[role=status]") || foot.firstElementChild;
  statusNode.dataset.wsSlot = "status";
  const target = $('[data-ws-slot="target"]', root);
  target.toggleAttribute("data-lg-context", studioWorkspace.mode === "logo");
  refreshStudioTargets();
}
async function mountStudioTool(mode) {
  const owner = studioWorkspace;
  if (!owner) return;
  if (mode === "framing" && C.products[currentProduct].treatment === "prepared") {
    if (owner.mode !== "inspect") await mountStudioTool("inspect");
    toast("This saved source uses a separate prepared product and background. Product placement is not adjustable in this version. Your saved image remains unchanged.");
    return;
  }
  teardownStudioTool();
  owner.mode = mode;
  owner.surfaceKey = studioPreviewCache()?.key;
  owner.visualTarget = studioVisualTarget();
  const sequence = ++owner.sequence;
  const current = () => studioWorkspace === owner && owner.sequence === sequence;
  const output = outputFor(currentProduct, lang, currentFormat);
  if (!output || !(output.url || canEditProduct(currentProduct))) {
    owner.mode = "inspect"; modal = "inspect";
    MotifStudioShell.setMode(owner.root, { mode: "inspect", title: "Inspect", target: `${LANG[lang]} · ${FMT[currentFormat].name}`, controlsHtml: "", inspect: true });
    $('[data-ws-slot="surface"]', owner.root).innerHTML = notice("This image is unavailable.", "Open Production to review the next action for this image.", "info", "nav:production", "Open Production");
    refreshStudioTargets();
    return;
  }
  if (mode === "logo") {
    const load = owner.logoLoad = new AbortController();
    try {
      await openLogo(currentProduct, currentFormat, { isCurrent: current, signal: load.signal });
      if (current()) beginStudioSave();
    } catch (error) {
      if (!current() || load.signal.aborted) return;
      const controls = $('[data-ws-slot="controls"]', owner.root);
      const status = owner.root.querySelector('[data-lg-status]');
      if (status) status.textContent = error.message;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "btn";
      retry.textContent = "Retry logo controls";
      retry.addEventListener("click", () => mountStudioTool("logo"), { once: true });
      controls.append(retry);
    } finally {
      if (owner.logoLoad === load) owner.logoLoad = null;
    }
    return;
  }
  if (mode === "text") {
    beginText(); modal = "text";
    mountStudioParts(textParts(), { tools: ".mf-editor-tools", surface: "#text-stage", status: "#text-status", footer: ".ws-text-footer" });
    if (output.url && !$("#text-image").src) { $("#text-image").src = output.url; $("#text-image").hidden = false; }
    bindText();
    beginStudioSave();
  } else if (mode === "framing") {
    modal = "frame";
    mountStudioParts(framingParts(), { tools: ".framing-tools", surface: ".framing-visual", status: "#frame-status" });
    if (output.url && !$("#frame-image").src) { $("#frame-image").src = output.url; $("#frame-image").hidden = false; }
    bindFraming();
    beginStudioSave();
  } else {
    modal = "inspect";
    const template = document.createElement("template"); template.innerHTML = inspectorModal();
    const details = template.content.querySelector(".large-inspector > div:last-child");
    MotifStudioShell.setMode(owner.root, {mode, title:"Inspect", target:`${LANG[lang]} · ${FMT[currentFormat].name}`, controlsHtml:details.innerHTML, inspect:true});
    $('[data-ws-slot="surface"]', owner.root).replaceChildren(template.content.querySelector(".lightbox").firstElementChild);
    studioShowRetainedSurface();
    const image = $('[data-ws-slot="surface"] > img', owner.root);
    if (image) image.decode().then(() => { if (current()) studioPreviewReady(); }).catch(() => {});
    restoreProjectControls();
    refreshStudioTargets();
    beginStudioSave();
  }
}
async function switchStudioTarget({ mode = studioWorkspace?.mode, format = currentFormat, language = lang } = {}) {
  const owner = studioWorkspace;
  if (!owner) return;
  const sequence = owner.switchSequence = (owner.switchSequence || 0) + 1;
  const current = () => studioWorkspace === owner && owner.switchSequence === sequence;
  if (mode === owner.mode && format === currentFormat && language === lang) return;
  if (!await resolveStudioChanges() || !current()) return;
  let targetSurface = language === lang && format !== currentFormat ? studioFormatSurface(format) : null;
  if (!targetSurface && language === lang && format !== currentFormat) {
    const target = JSON.stringify([C.id, C.products[currentProduct]?.id, lang, format]);
    const pending = [...(owner.thumbnails?.values() || [])].find(state => state.target === target && state.pending);
    if (pending?.ready) {
      // A quick return to the just-edited format can beat its thumbnail decode.
      // Keep the current canvas mounted until that existing request finishes.
      await pending.ready;
      if (!current() || !await resolveStudioChanges() || !current()) return;
      targetSurface = studioFormatSurface(format);
    }
  }
  teardownStudioTool();
  currentFormat = format; formats[0] = format; lang = language;
  if (targetSurface) studioWorkspace.retainedSurface = targetSurface;
  await mountStudioTool(mode);
}
async function cancelStudioTool() {
  if (!studioWorkspace || !await resolveStudioChanges()) return;
  if (wordingReturn?.kind === "text") {
    const origin = wordingReturn;
    if (!await leaveStudioWorkspace()) return;
    wordingReturn = origin;
    returnFromWording("text");
    return;
  }
  await mountStudioTool("inspect");
}
async function studioApplied(data, accept) {
  const owner = studioWorkspace;
  const sequence = owner.sequence;
  const output = data.manifest.outputs.find(item => item.product_id === owner.product && item.market === MARKET[lang] && item.ratio === FMT[currentFormat].ratio);
  await decodedImage(output.url);
  if (studioWorkspace !== owner || owner.sequence !== sequence) return false;
  useData(data, { applied: true });
  currentProduct = C.products.findIndex(p => p.id === owner.product);
  // Adopt the saved baseline and release the controller before any return teardown.
  accept(output);
  if (wordingReturn?.kind === "text" && !owner.continuing) { finishStudioVisit(); returnFromWording("text"); return true; }
  refreshStudioTargets();
  return true;
}
async function leaveStudioWorkspace() {
  const owner = studioWorkspace;
  if (!owner) return true;
  if (!await resolveStudioChanges()) return false;
  // Metadata acknowledgements intentionally skip source checks and layout planning.
  // Refresh that read-only projection once before showing the gallery or Handoff.
  const previous = model;
  if (previous?.composition_projection_stale) {
    const version = previous.composition_version || previous.version;
    const latest = await api(`/api/motif/campaigns/${encodeURIComponent(owner.campaign)}`);
    if (studioWorkspace !== owner || model !== previous || studioSave?.dirty() || projectQueue?.isDirty()) return false;
    if (latest.value?.campaign_id !== owner.campaign || (latest.composition_version || latest.version) !== version) {
      studioSave?.conflict(Object.assign(new Error(MotifSave.conflictMessage), { status: 409 }));
      return false;
    }
    useData(latest);
  }
  finishStudioVisit();
  wordingReturn = null;
  return true;
}
function finishStudioVisit() {
  if (!studioWorkspace) return;
  teardownStudioTool();
  studioWorkspace.sequence++;
  studioWorkspace = null;
  document.body.classList.remove("ws-active");
}
