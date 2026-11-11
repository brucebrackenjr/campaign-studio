"use strict";

// The reusable view has no persistence or request path. These bindings reuse
// the campaign queue, product dialog, current-output editors, and plan actions.
function productPhotoView(product) {
  const current = product.source.path ? product.source : null;
  const retained = current ? { source: current, url: assetURL(current.path, product.id) }
    : retainedProductPhoto(product.id);
  const concept = product.treatment === "concept";
  return {
    ...product,
    mode: concept ? "concept" : "photo",
    description: concept ? product.source.description || product.description : product.description,
    image: concept ? null : product.localImage || (current ? product.image || retained?.url : null),
    filename: product.localFile?.name || current?.path?.split("/").pop() || "",
    previousImage: retained?.url || null,
    previousFilename: retained?.source.path?.split("/").pop() || "",
  };
}

function productEditorView() {
  return MotifProducerRepair.productEditor({
    product: productPhotoView(modalProduct),
    existing: draft.products.some(product => product.id === modalProduct.id),
    origin: page,
    busy: operation?.active || !!modalProduct.uploading || !!modalProduct.adding,
    adding: !!modalProduct.adding,
    uploading: !!modalProduct.uploading,
    error: modalProduct.error,
    invalidFields: modalProduct.invalidFields,
  }).replace('class="pr-button pr-close"', 'class="pr-button pr-close close"');
}

function captureProductInputs() {
  if (modal !== "product" || !modalProduct || !$("#product-name")) return;
  for (const field of ["name", "description", "origin", "rights"])
    modalProduct[field] = $(`#product-${field}`).value;
  if (modalProduct.source.role === "concept")
    modalProduct.source.description = modalProduct.description;
  const existing = draft.products.find(product => product.id === modalProduct.id);
  if (existing) {
    existing.name = modalProduct.name;
    existing.description = modalProduct.description;
    const role = { reuse: "finished_scene", prepared: "prepared_product", concept: "concept" }[modalProduct.treatment];
    const provenance = { label: modalProduct.origin || "Not provided", rights: modalProduct.rights || "Not provided", synthetic: false };
    const photoSource = { ...copy(modalProduct.source) };
    delete photoSource.description;
    delete photoSource.imagery_absent;
    existing.source = role === "concept"
      ? { role, imagery_absent: true, description: modalProduct.description, provenance }
      : { ...photoSource, role, provenance };
  }
}

function setProductMode(concept, restore = false) {
  if (modal !== "product" || !modalProduct || operation?.active || modalProduct.adding) return;
  cancelProductUpload();
  captureProductInputs();
  if (restore || (!concept && !modalProduct.source.path)) {
    const retained = modalProduct.source.path
      ? { source: modalProduct.source, url: modalProduct.image }
      : retainedProductPhoto(modalProduct.id);
    if (retained?.source.path) {
      modalProduct.source = copy(retained.source);
      modalProduct.image = retained.url;
    } else if (restore) return;
  }
  modalProduct.treatment = concept ? "concept"
    : modalProduct.source.role === "prepared_product" ? "prepared" : "reuse";
  delete modalProduct.error;
  delete modalProduct.invalidFields;
  captureProductInputs();
  projectChanged();
  openModal("product");
}

function productionView(context, operationHtml = "", notices = "") {
  const requests = context.plan?.generation_products.length || 0;
  const estimate = context.plan?.cost_estimate?.estimated_usd;
  const view = {
    ...context,
    productLimit,
    mutationsBlocked: context.active,
    savedCount: (model.manifest?.outputs || []).filter(output => output.url).length,
    planState: context.active ? "running" : context.preparation?.status === "error" || context.blocked ? "error"
      : context.preparation?.status === "pending" ? "preparing" : context.pending ? "pending" : context.complete ? "complete" : "ready",
    planError: context.preparation?.error || (context.proposal ? { proposal: context.proposal } : null),
    plan: { requests, estimate: Number.isFinite(estimate) ? compactUSD(estimate) : "Not estimated",
      busy: busy || context.blocked, keyMissing: requests > 0 && !keyAvailable },
    operationHtml,
  };
  let html = MotifProducerRepair.production(view);
  if (model.regeneration && model.regeneration.outcome !== "pending")
    notices += `<p role="status">${esc(model.regeneration.message)}</p>`;
  const changedSources = context.plan?.changed_source_products || [];
  if (changedSources.length && !context.active) {
    const names = changedSources.map(product => product.name).join(", ");
    notices += `<div class="pr-inline-error" role="status"><div><strong>The image instructions changed.</strong><p>${esc(names)}: the previous generated image no longer matches the current instructions. Review the estimate before generating a replacement. You can also restore the previous instructions to use that image again.</p></div></div>`;
  }
  if (context.active)
    for (const action of ["edit", "choose", "choose-concept"])
      html = html.replaceAll(`data-pr-action="${action}"`, `data-pr-action="${action}" disabled`);
  // Current validation and failure explanations stay beside the existing
  // action, with their actual remedy. They do not replace the product board.
  return notices ? html.replace('<aside class="pr-action-panel">', `<aside class="pr-action-panel">${notices}`) : html;
}

function refreshProductionStatus() {
  if (page !== "production" || studioWorkspace || !$(".pr-workspace")) return;
  viewModel();
  const template = document.createElement("template");
  template.innerHTML = production();
  const panel = $(".pr-action-panel"), next = $(".pr-action-panel", template.content);
  if (!panel || !next) return;
  const focused = panel.contains(document.activeElement) ? document.activeElement : null;
  const focus = focused?.matches("button[data-action],button[data-pr-action]") ? { ...focused.dataset } : null;
  panel.replaceWith(next);
  // Keep unchanged product cards and the user's coverage expansion in place.
  const cards = $$(".pr-product-card"), updated = $$(".pr-product-card", template.content);
  cards.forEach((card, index) => { if (updated[index] && card.outerHTML !== updated[index].outerHTML) card.replaceWith(updated[index]); });
  const add = $('.wf-product-management [data-pr-action="add"]'), nextAdd = $('.wf-product-management [data-pr-action="add"]', template.content);
  if (add && nextAdd) add.disabled = nextAdd.disabled;
  const keyAccess = $(".pr-key-access"), nextKeyAccess = $(".pr-key-access", template.content);
  if (keyAccess && nextKeyAccess) keyAccess.replaceWith(nextKeyAccess);
  else if (keyAccess) keyAccess.remove();
  else if (nextKeyAccess) next.after(nextKeyAccess);
  const availability = $(".wf-studio-availability"), nextAvailability = $(".wf-studio-availability", template.content);
  if (availability && !nextAvailability) availability.remove();
  else if (!availability && nextAvailability) $(".wf-production-sidebar").append(nextAvailability);
  const studio = $('[data-pr-action="studio"]'), nextStudio = $('[data-pr-action="studio"]', template.content);
  if (studio && nextStudio) {
    studio.disabled = nextStudio.disabled;
    const description = nextStudio.getAttribute("aria-describedby");
    if (description) studio.setAttribute("aria-describedby", description);
    else studio.removeAttribute("aria-describedby");
  }
  if (focused) {
    const target = focus && $$("button", next).find(button => Object.entries(focus).every(([key, value]) => button.dataset[key] === value));
    const heading = $("h2", next);
    if (!target && heading) heading.tabIndex = -1;
    (target || heading)?.focus({ preventScroll: true });
  }
}

function studioView() {
  if (reviewTab === "product") return MotifStudioShell.shell(studioShellView("inspect"));
  const products = C.products.map((product, index) => ({
    id: product.id, name: product.name,
    sourceAvailable: !!model.editable_products?.[product.id] || !!model.source_urls?.[product.id] ||
      (model.concept_preview_version === model.version && !!model.concept_previews?.[product.id]),
    outputs: Object.fromEntries(C.markets.flatMap(language => FMT.map((format, f) =>
      [`${MARKET[language]}|${format.ratio}`, outputFor(index, language, f)?.url || null]))),
  }));
  const shown = products.flatMap(product => Object.entries(product.outputs))
    .filter(([key, url]) => (imageMarket === "all" || key.startsWith(imageMarket + "|"))
      && (filter !== "missing" || !url)).length;
  pageNum = Math.min(Math.max(0, pageNum), Math.max(0, Math.ceil(shown / 12) - 1));
  const view = {
    products,
    markets: C.markets.map(language => ({ id: MARKET[language], label: LANG[language] })),
    savedCount: model.manifest?.outputs.filter(output => output.url).length || 0,
    studioView: reviewTab === "overview" ? "images" : reviewTab === "activity" ? "products" : reviewTab,
    market: MARKET[lang], imageMarket, imagePage: pageNum, onlyUnavailable: filter === "missing",
    format: FMT[formats[0]].ratio,
    compareIds: selected.map(index => products[Math.min(index, products.length - 1)]?.id),
    actionAvailability: Object.fromEntries(products.map((product, index) => [product.id,
      Object.fromEntries(C.markets.flatMap(language => FMT.map((format, f) => {
        const output = outputFor(index, language, f);
        const available = !!output && (!!output.url || canEditProduct(index));
        return [`${MARKET[language]}|${format.ratio}`, {
          text: canEditProduct(index) && available,
          framing: canEditProduct(index) && available, logo: canEditLogo(index) && available,
          inspect: available,
        }];
      }))),
    ])),
  };
  return MotifProducerRepair.studio(view);
}

async function producerAction(button) {
  const action = button.dataset.prAction;
  if (modal === "product" && ["dismiss", "cancel", "save"].includes(action) && draft.products.some(product => product.id === modalProduct.id)) {
    captureProductInputs(); projectChanged();
    if (!await saveProject()) return;
    closeModal({ retain: false }); render(); return;
  }
  const id = button.dataset.prId;
  const p = C.products.findIndex(product => product.id === id);
  const f = FMT.findIndex(format => format.ratio === button.dataset.prFormat);
  if (action === "mode-photo" || action === "mode-concept" || action === "restore") {
    setProductMode(action === "mode-concept", action === "restore");
    return;
  }
  if (action === "choose" || action === "choose-concept") {
    productDraft(id);
    setProductMode(action === "choose-concept");
    return;
  }
  if (action === "image-prev" || action === "image-next") {
    pageNum = Math.max(0, pageNum + (action === "image-next" ? 1 : -1));
    render();
    return;
  }
  if (["open-product", "open-format", "open-output"].includes(action)) {
    if (p < 0) return;
    if (!model.editable_products?.[id] && !model.source_urls?.[id] &&
        !(model.concept_preview_version === model.version && model.concept_previews?.[id]))
      return dispatchAction({ action: `addProductImage:${id}` });
    if (button.dataset.prMarket) lang = button.dataset.prMarket === "es-MX" ? "es" : "en";
    if (action === "open-output" && outputFor(p, lang, f)?.url)
      return dispatchAction({ action: `inspect:${p}:${f}:${lang}` });
    retainStudioGallery();
    selected[0] = p;
    formats[0] = f < 0 ? 0 : f;
    reviewTab = "product";
    render();
    return;
  }
  const focused = p < 0 ? Math.max(0, Math.min(selected[0], C.products.length - 1)) : p;
  const format = f < 0 ? formats[0] : f;
  if (button.dataset.prMarket) lang = button.dataset.prMarket === "es-MX" ? "es" : "en";
  const mapped = {
    "add-image": `addProductImage:${id}`, "update-key": "updateKey", resumeGeneration: "resumeGeneration",
    regenerate: `regenerate:${id}`,
    add: "addProduct", "close-cap": "close", edit: `product:${id}`, source: `productSource:${id}`,
    "review-plan": "plan", "plan-input": "planDraftInput", settings: "settings", start: "start", key: "missingKey", stop: "stop",
    studio: "nav:review", browse: "pickProduct", save: "saveProduct", cancel: "cancelEdits",
    "cancel-remove": "productReturn", "confirm-remove": "confirmRemove",
    dismiss: "close", remove: "remove", "studio-products": "reviewTab:products",
    "studio-images": "reviewTab:overview", "studio-compare": "reviewTab:compare",
    record: "reviewTab:activity", handoff: "nav:handoff",
    "previous-copy": "nav:copy", "previous-production": "nav:production",
    text: `text:${focused}:${format}`,
    framing: `frame:${focused}:${format}`,
    inspect: `inspect:${focused}:${format}`,
  }[action];
  if (mapped) await dispatchAction({ action: mapped });
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-pr-action]");
  if (!button || button.disabled) return;
  event.preventDefault();
  producerAction(button).catch(error => toast(error.message));
});
document.addEventListener("change", event => {
  const input = event.target;
  if (input.dataset.prInput === "only-unavailable") {
    filter = input.checked ? "missing" : "all";
    pageNum = 0;
    render();
    return;
  }
  const kind = input.dataset.prSelect;
  if (!kind) return;
  if (kind === "market") lang = input.value === "es-MX" ? "es" : "en";
  else if (kind === "image-market") { imageMarket = input.value; pageNum = 0; }
  else if (kind === "compare-format") formats[0] = formats[1] = FMT.findIndex(format => format.ratio === input.value);
  else if (kind.startsWith("compare-")) selected[Number(kind.slice(-1))] = C.products.findIndex(product => product.id === input.value);
  render();
});
