"use strict";

// A product visit owns one shell and one existing editor controller at a time.
// Requests, geometry, and publication stay in those controllers.
let studioWorkspace = null;
function imageEditorRoot() { return studioWorkspace?.root || $("#overlay"); }
function studioTextAvailable() {
  const output = outputFor(currentProduct, lang, currentFormat);
  return canEditProduct(currentProduct) && !!(output?.url || model.rule_withheld_outputs?.includes(output?.id));
}
function studioBusy() {
  return !!(studioWorkspace?.loading || textEditor?.saving || frameSaving || logoEditor?.busy || logoEditor?.uploading || logoEditor?.reviewing);
}
function studioDirty() {
  if (!studioWorkspace) return false;
  if (modal === "text") return Object.keys(textEditor?.placements || {}).length > 0 || wordingIntent(textEditor).fields.length > 0;
  if (modal === "frame") return JSON.stringify(framingDraft) !== framingInitial;
  if (modal === "logo") return logoEditor?.dirty();
  return false;
}
function studioApplyButton() {
  return $(modal === "logo" ? '[data-lg="save"]' : modal === "frame" ? '#frame-primary[data-action="saveFrame"]' : '[data-action="applyText"]', studioWorkspace?.root);
}
async function resolveStudioChanges() {
  const owner = studioWorkspace;
  if (!owner || studioBusy()) return false;
  if (!studioDirty()) return true;
  const mode = modal;
  const choice = await chooseEditorFormat({
    product: C.products[currentProduct].name, format: FMT[currentFormat].ratio,
    replacement: mode === "logo" && logoEditor.pendingAsset,
    isCurrent: () => studioWorkspace === owner && modal === mode,
    canApply: () => !!studioApplyButton() && !studioApplyButton().disabled,
  });
  if (choice === "keep" || studioWorkspace !== owner || modal !== mode) return false;
  if (choice === "apply") {
    owner.continuing = true;
    try {
      const applied = await (mode === "logo" ? logoEditor.applyForSwitch()
        : withProjectApply(mode === "text" ? applyText : saveFraming));
      return applied && studioWorkspace === owner;
    } finally { owner.continuing = false; }
  }
  return true;
}
function teardownStudioTool() {
  const owner = studioWorkspace;
  if (!owner) return;
  owner.tearingDown = true;
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
      url: outputFor(currentProduct, lang, index)?.url, available: !!outputFor(currentProduct, lang, index)?.url })),
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
  const owner = studioWorkspace = { campaign: C.id, product: C.products[currentProduct].id, mode, sequence: 0, loading: false };
  document.body.classList.add("ws-active");
  owner.root = $(".ws-workspace");
  owner.root.classList.add("mf-editor", "is-direct");
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
    const available = !!outputFor(currentProduct, lang, currentFormat)?.url;
    button.disabled = button.dataset.wsMode === "text" ? !studioTextAvailable()
      : button.dataset.wsMode === "inspect" ? !available
      : button.dataset.wsMode === "logo" ? !canEditLogo(currentProduct) || !available
      : !canEditProduct(currentProduct) || !available;
  }
  $('[data-ws-target="market"]', root).value = MARKET[lang];
  for (const button of $$('[data-ws-target="format"]', root)) {
    const index = FMT.findIndex(format => format.ratio === button.dataset.wsFormat);
    const output = outputFor(currentProduct, lang, index);
    button.disabled = !output?.url && !(studioWorkspace.mode === "text" && model.rule_withheld_outputs?.includes(output?.id));
    button.setAttribute("aria-pressed", String(index === currentFormat));
    button.setAttribute("aria-label", `${FMT[index].name} · ${FMT[index].ratio}${output?.url ? "" : " · Image unavailable"}`);
    $(".ws-thumb", button).innerHTML = output?.url ? `<img src="${esc(output.url)}" alt="">` : '<span aria-hidden="true">–</span>';
  }
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
  $('[data-ws-slot="surface"]', root).replaceChildren(canvas);
  const foot = $('[data-ws-slot="commit"]', root);
  foot.replaceChildren(source.querySelector(status), source.querySelector(footer));
  foot.hidden = false;
  const apply = foot.querySelector('[data-action="applyText"],#frame-primary,[data-lg="save"]');
  if (apply) apply.dataset.wsAction = "apply";
  const cancel = foot.querySelector('[data-action="close"],[data-lg="cancel"]');
  if (cancel) cancel.dataset.wsAction = "cancel";
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
  const sequence = ++owner.sequence;
  const current = () => studioWorkspace === owner && owner.sequence === sequence;
  const output = outputFor(currentProduct, lang, currentFormat);
  if (!output?.url && !(mode === "text" && studioTextAvailable())) {
    owner.mode = "inspect"; modal = "inspect";
    MotifStudioShell.setMode(owner.root, { mode: "inspect", title: "Inspect", target: `${LANG[lang]} · ${FMT[currentFormat].name}`, controlsHtml: "", inspect: true });
    $('[data-ws-slot="surface"]', owner.root).innerHTML = notice("This image is unavailable.", "Open Production to review the next action for this image.", "info", "nav:production", "Open Production");
    refreshStudioTargets();
    return;
  }
  if (mode === "logo") {
    owner.loading = true;
    $('[data-ws-slot="status"]', owner.root)?.replaceChildren("Preparing logo preview…");
    try { await openLogo(currentProduct, currentFormat, { isCurrent: current }); }
    finally { if (current()) owner.loading = false; }
    return;
  }
  if (mode === "text") {
    beginText(); modal = "text";
    mountStudioParts(textParts(), { tools: ".mf-editor-tools", surface: "#text-stage", status: "#text-status", footer: ".ws-text-footer" });
    if (output.url) { $("#text-image").src = output.url; $("#text-image").hidden = false; }
    bindText();
  } else if (mode === "framing") {
    modal = "frame";
    mountStudioParts(framingParts(), { tools: ".framing-tools", surface: ".framing-visual", status: "#frame-status" });
    $("#frame-image").src = output.url; $("#frame-image").hidden = false;
    bindFraming();
  } else {
    modal = "inspect";
    const template = document.createElement("template"); template.innerHTML = inspectorModal();
    const details = template.content.querySelector(".large-inspector > div:last-child");
    MotifStudioShell.setMode(owner.root, {mode, title:"Inspect", target:`${LANG[lang]} · ${FMT[currentFormat].name}`, controlsHtml:details.innerHTML, inspect:true});
    $('[data-ws-slot="surface"]', owner.root).replaceChildren(template.content.querySelector(".lightbox").firstElementChild);
    restoreProjectControls();
    refreshStudioTargets();
  }
}
async function switchStudioTarget({ mode = studioWorkspace?.mode, format = currentFormat, language = lang } = {}) {
  if (!studioWorkspace || (mode === studioWorkspace.mode && format === currentFormat && language === lang)) return;
  if (!await resolveStudioChanges()) return;
  teardownStudioTool();
  currentFormat = format; formats[0] = format; lang = language;
  await mountStudioTool(mode);
}
async function cancelStudioTool() {
  if (!studioWorkspace || studioBusy()) return;
  if (wordingReturn?.kind === "text") { finishStudioVisit(); returnFromWording("text"); return; }
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
  if (!studioWorkspace) return true;
  if (!await resolveStudioChanges()) return false;
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
