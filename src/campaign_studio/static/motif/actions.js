"use strict";

let stopTarget = null;
let importFile = null;
let importErrorMessage = "";
let importedCampaignId = null;
let navigationError = "";
let productUploadSequence = 0;
let pendingProductUpload = null;
let productRemovalFromEditor = false;

function hasPendingNavigationWork() {
  return !!projectQueue?.isDirty();
}
function guardNavigation(destination) {
  if (typeof briefLogoUploadPending === "function" && briefLogoUploadPending()) {
    toast("Wait for the logo upload to finish, or cancel the upload in Brand.");
    return true;
  }
  if (!hasPendingNavigationWork()) return false;
  if (getSaveState()?.problem?.phase === "conflict") return true;
  pendingNav = destination;
  navigationError = "";
  openModal("unsaved");
  return true;
}
async function savePendingNavigation() {
  try {
    if (getSaveState()?.problem?.phase === "conflict") { closeModal(); pendingNav = null; return; }
    if (await saveProject()) await finishPendingNavigation();
    else {
      const state = getSaveState();
      navigationError = state?.busy && state.problem?.phase !== "conflict"
        ? "Saving will finish in a moment. Try again."
        : "Your changes are still here. Try saving again.";
    }
  } catch (error) {
    if (getSaveState()?.problem?.phase === "conflict") { closeModal(); pendingNav = null; return; }
    navigationError = error.message;
  }
  if (pendingNav && modal === "unsaved") openModal("unsaved");
}

async function openImportedCampaign() {
  // A returned campaign ID is a committed import. Opening it must never send
  // the file again, even if the following page or Workbench refresh fails.
  try {
    await openCampaign(importedCampaignId, "campaign");
  } catch (error) {
    importErrorMessage = error.message;
    openModal("import-open-failed");
    return;
  }
  try {
    await refreshCatalog();
  } catch {
    toast("Your brief was imported. The Workbench could not refresh; try opening it again later.");
  }
}

async function confirmStop() {
  if (
    !stopTarget ||
    stopTarget.campaign !== draft?.campaign_id ||
    stopTarget.run !== model?.run?.id
  )
    throw new Error(
      "The active run changed. Reopen Production before stopping work.",
    );
  await api(`/api/campaigns/${stopTarget.campaign}/stop`, {
    run_id: stopTarget.run,
  });
  closeModal();
  toast(
    "Stop requested. An in-flight request may still finish or incur a charge.",
  );
}

let editorFormatDialog = null;
function chooseEditorFormat({ product, format, replacement = false, canApply, isCurrent }) {
  if (editorFormatDialog) return Promise.resolve("keep");
  const view = MotifEditorControls.switchPrompt({ product, format, replacement });
  const root = $("#overlay"), previous = [...root.children, ...(studioWorkspace ? [studioWorkspace.root] : [])].map(node => [node, node.inert]), focus = document.activeElement;
  const host = document.createElement("div");
  host.innerHTML = dialog(view.title, `<p>${esc(view.body)}</p>`,
    `<button type="button" class="btn" data-editor-choice="keep">${view.cancel}</button><button type="button" class="btn" data-editor-choice="discard">${view.discard}</button><button type="button" class="btn primary" data-editor-choice="apply" ${canApply() ? "" : "disabled"}>${view.primary}</button>`).replaceAll("dialog-title", "editor-switch-title");
  previous.forEach(([node]) => node.inert = true);
  root.append(host);
  editorFormatDialog = host;
  return new Promise(resolve => {
    const observer = new MutationObserver(refreshReadiness);
    const finish = choice => {
      observer.disconnect();
      if (editorFormatDialog !== host) return;
      editorFormatDialog = null;
      host.remove();
      previous.forEach(([node, inert]) => { if (node.isConnected) node.inert = inert; });
      if (isCurrent() && focus?.isConnected) focus.focus({ preventScroll: true });
      resolve(choice);
    };
    function refreshReadiness() {
      if (!host.isConnected || !isCurrent()) { finish("keep"); return; }
      const apply = $('[data-editor-choice="apply"]', host), disabled = !canApply();
      if (apply.disabled !== disabled) apply.disabled = disabled;
    }
    // Preview completion updates the existing editor controls behind this dialog.
    observer.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "data-action"] });
    if (studioWorkspace) observer.observe(studioWorkspace.root, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "data-action"] });
    refreshReadiness();
    host.addEventListener("click", event => {
      event.stopPropagation();
      refreshReadiness();
      const button = event.target.closest("button");
      if (button && !button.disabled) finish(button.dataset.editorChoice || "keep");
    });
    host.addEventListener("keydown", event => {
      event.stopPropagation();
      if (event.key === "Escape") { event.preventDefault(); finish("keep"); }
      if (event.key === "Tab") {
        event.preventDefault();
        const buttons = $$('button:not(:disabled)', host), index = buttons.indexOf(document.activeElement);
        buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length]?.focus();
      }
    });
    $('[data-editor-choice="keep"]', host).focus();
  });
}
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  setTimeout(() => $("#toast").classList.remove("show"), 7000);
}
function clearToast() {
  $("#toast").classList.remove("show");
}
function render({ preserveModal = false } = {}) {
  if (studioWorkspace) { viewModel(); refreshStudioTargets(); return; }
  // Local setup/cleanup owns its live input and focus until its own close.
  preserveModal ||= !!localDialog;
  captureProductInputs();
  destroyBriefFlow();
  document.body.classList.toggle("mf-brief-page", page === "campaign");
  destroyProjectViews();
  viewModel();
  if (page === "review" && reviewTab === "product") { currentProduct = Math.max(0, Math.min(selected[0], C.products.length - 1)); currentFormat = formats[0]; }
  const content =
    page === "library"
      ? library()
      : page === "campaign"
        ? brief()
        : page === "production"
          ? production()
          : page === "review"
            ? review()
            : handoff();
  $("#app").innerHTML = shell(content);
  if (!preserveModal && modal !== "logo") $("#overlay").innerHTML = modal ? modalView() : "";
  refreshCampaignIdentity();
  bindWorkbench();
  mountBriefFlow();
  restoreProjectControls({ preserveModal });
  mountProjectViews();
  if (page === "review" && reviewTab === "product" && $(".ws-workspace")) mountStudioVisit();
  if (page === "production") queueMicrotask(ensureProductionPlan);
}
function openModal(name) {
  if (page === "review" && ["text", "frame", "inspect"].includes(name))
    return openStudioWorkspace(name === "frame" ? "framing" : name === "inspect" ? "inspect" : "text");
  captureProductInputs();
  if (draft && modal && modal !== "logo") captureProject();
  destroyProjectViews();
  if (!modal) lastFocus = document.activeElement;
  modal = name;
  if (name === "unsaved") projectQueue?.suspendAutosave();
  let content;
  try {
    content = modalView();
  } catch (error) {
    closeModal();
    throw error;
  }
  $("#overlay").innerHTML = content;
  if (modal === "import") {
    const input = $("#json-file");
    if (importFile) {
      const selected = new DataTransfer();
      selected.items.add(importFile);
      input.files = selected.files;
    }
    const showFile = () => {
      const file = input.files[0];
      $("#import-file-label").textContent = file ? "Selected brief" : "JSON brief";
      $("#import-file-name").textContent = file ? file.name : "Choose a JSON brief";
      $("#import-file-change").textContent = file ? "Change file" : "Choose file";
    };
    input.addEventListener("change", showFile);
    showFile();
  }
  $("#app").inert = true;
  $(".skip").inert = true;
  restoreProjectControls();
  mountProjectViews();
  const openedDialog = $('#overlay [role="dialog"]');
  setTimeout(() => {
    // Initial focus belongs only to this dialog and must not steal a later
    // producer choice, including keyboard activation of its Save control.
    if (!openedDialog?.isConnected || $('#overlay [role="dialog"]') !== openedDialog ||
        openedDialog.contains(document.activeElement)) return;
    if (!focusStorageDialog()) (modal === "remove-product" ? $('[data-pr-action="cancel-remove"]') : $("#overlay button.close") || $("#overlay button"))?.focus();
  }, 0);
}
function closeModal({ cancel = false, retain = true, restoreFocus = true } = {}) {
  if (modal === "brand-navigation" && typeof brandUpdating !== "undefined" && brandUpdating) return;
  if (studioWorkspace && ["text", "frame", "logo", "inspect"].includes(modal)) return cancelStudioTool();
  if (modal === "remove-product" && retain) return cancelProductRemoval();
  if (modal === "campaign-copy-confirm" && campaignCopyDecision) return finishCampaignCopy(false);
  const closingKind = modal;
  if (modal === "draft-wording" && draftWording?.saving) return;
  if (modal === "product" && modalProduct?.adding) return;
  const navigationChoice = modal === "unsaved";
  const authoring = ["rule", "rename", "inspect"].includes(modal);
  // Existing product edits save as they are entered; Add remains explicit for a new product.
  if (modal === "product") { captureProductInputs(); projectChanged(); cancelProductUpload(); discardLocalProductPhoto(modalProduct); cancel = false; retain = false; }
  if (
    modal === "storage" &&
    (deletionSubmitting || storageView?.state === "delete-pending")
  )
    return;
  if (cancel) clearProjectTarget();
  else if (retain && draft) captureProject();
  destroyProjectViews();
  clearStorageContext();
  modal = "";
  stopTarget = null;
  modalProduct = null;
  productRemovalFromEditor = false;
  $("#overlay").innerHTML = "";
  $("#app").inert = false;
  $(".skip").inert = false;
  const restore = () => {
    if (modal) return;
    const componentTarget = (lastFocus?.dataset?.prAction && $$("[data-pr-action]").find(el =>
      el.dataset.prAction === lastFocus.dataset.prAction && el.dataset.prId === lastFocus.dataset.prId &&
      el.dataset.prFormat === lastFocus.dataset.prFormat)) ||
      (lastFocus?.dataset?.bfAction && $$("[data-bf-action]").find(el =>
        el.dataset.bfAction === lastFocus.dataset.bfAction && el.className === lastFocus.className));
    const target = lastFocus?.isConnected
      ? lastFocus
      : componentTarget || $$("[data-action]").find(
          (el) => el.dataset.action === lastFocus?.dataset?.action,
        );
    target?.focus();
  };
  if (restoreFocus) {
    restore();
    queueMicrotask(restore);
  }
  mountProjectViews();
  if (navigationChoice) projectQueue?.resumeAutosave();
  if (cancel) projectChanged();
  if (authoring && (retain || cancel)) projectQueue?.retainContext();
  if (closingKind === "draft-wording") draftWording = null;
  returnFromWording(closingKind);
  if (studioWorkspace) modal = studioWorkspace.mode === "framing" ? "frame" : studioWorkspace.mode;
}
async function finishPendingNavigation() {
  const destination = pendingNav;
  pendingNav = "";
  if (typeof destination === "string") {
    await navigate(destination || "library");
    return;
  }
  closeModal();
  if (destination.action === "openCampaign")
    await openCampaign(destination.campaignId);
  else openModal(destination.action);
}

async function navigate(target) {
  if (studioWorkspace && !await leaveStudioWorkspace()) return;
  if (target !== "campaign" && typeof guardBrandNavigation === "function" &&
      guardBrandNavigation({ kind: "page", target })) return;
  if (draft && !await flushCurrentWork()) {
    if (getSaveState()?.problem?.phase !== "conflict")
      toast("Your changes are still here. Finish saving or try again before leaving this page.");
    return;
  }
  if (typeof briefLogoUploadPending === "function" && briefLogoUploadPending()) {
    toast("Wait for the logo upload to finish, or cancel the upload in Brand."); return;
  }
  // Workbench order comes from successful server saves, including image review.
  // Opening a campaign does not write or advance that activity timestamp.
  if (target === "library") await refreshCatalog();
  if (target === "handoff" && model.file_integrity?.verified === false) {
    const campaign = draft.campaign_id;
    const verified = await api(`/api/motif/campaigns/${encodeURIComponent(campaign)}`);
    if (draft?.campaign_id !== campaign) return;
    useData(verified);
  }
  page = target === "products" ? "production" : ["copy", "brand"].includes(target) ? "campaign" : target;
  if (target === "copy" || target === "brand") tab = target;
  if (target === "review") {
    reviewTab = "products";
  }
  closeModal();
  updateURL();
  render();
  scrollTo(0, 0);
}
const originalModalView = modalView;
// Add service-backed recovery dialogs to the existing presentation entry point.
// Keep one modal renderer so shared focus and Save lifecycle hooks apply to both.
modalView = function () {
  if (modal === "brand-navigation") return brandNavigationDialog();
  if (modal === "storage") return storageDialog();
  if (modal === "stop")
    return dialog(
      "Stop the work still in the queue?",
      `<p class="muted">Unsent requests will stay unsent. Completed images are retained.</p>${notice("The active request may still finish and incur a charge.", "Stopping locally cannot guarantee cancellation at the provider. Any result that arrives will be kept.")}`,
      btn("Keep running", "close") +
        btn("Stop unsent work", "confirmStop", "danger"),
    );
  if (modal === "import")
    return dialog(
      "Bring your brief.",
      `<p class="muted" style="margin-bottom:18px">Import a JSON brief. You can attach images later.</p><div class="import-file"><div class="import-file-details" aria-live="polite"><span id="import-file-label" class="small muted">JSON brief</span><strong id="import-file-name">Choose a JSON brief</strong></div><label class="btn import-file-picker"><span id="import-file-change">Choose file</span><input type="file" id="json-file" accept=".json,application/json" aria-label="Choose a JSON brief"></label></div><div id="import-error" role="alert"></div>`,
      `<span class="small muted">English, Spanish, or both · 1–${productLimit} products · all three formats</span>${btn("Import", "doImport", "primary")}`,
    );
  if (modal === "import-open-failed")
    return dialog(
      "Your brief was imported.",
      `<p>Its campaign is saved, but could not open yet.</p>${notice(importErrorMessage, "", "block")}`,
      btn("Close", "close") + btn("Open imported campaign", "openImported", "primary"),
    );
  if (modal === "import-busy")
    return dialog(
      "Another operation is finishing.",
      `<p>Wait for it to finish, then try importing your brief again.</p>${notice(importErrorMessage, "", "block")}`,
      btn("Close", "close") + btn("Return to import", "returnImport", "primary"),
    );
  if (modal === "invalid-import")
    return dialog(
      "This brief needs a small repair.",
      `${notice(importErrorMessage, "Nothing has been replaced, and no image request was made.", "block")}<p class="muted">Correct the named fields in your JSON brief, then return to import and choose the updated file.</p>`,
      btn("Keep my work", "close") +
        btn("Return to import", "returnImport", "primary"),
    );
  if (modal === "campaigns")
    return dialog(
      "Back to the workbench.",
      `<div class="menu-popover">${saved.map((c) => `<button data-action="openCampaign" data-campaign-id="${esc(c.id)}"><strong>${esc(c.name)}</strong><br><span class="small muted">${c.brand ? esc(c.brand) + " · " : ""}${c.products} ${c.products === 1 ? "product" : "products"} · ${workbenchCounts(c)} · ${esc(activityLabel(c.updated))}</span></button>`).join("")}</div>`,
      btn("All campaigns", "library") + btn("New campaign", "new", "primary"),
    );
  if (modal === "remove-product")
    return MotifProducerRepair.removeProductDialog({ name: modalProduct.name });
  if (["setup", "local-diagnostics"].includes(modal)) return setupModal();
  if (
    [
      "missing-key",
      "rejected-key",
      "provider-unavailable",
      "network-unavailable",
      "duplicate-start",
    ].includes(modal)
  )
    return productionRecoveryModal();
  if (modal === "unknown-recovery")
    return dialog(
      "No saved response was found.",
      notice(
        "The request result was not confirmed. It still consumes allowance.",
        "The provider may have completed it and may charge for it. Available images are preserved; nothing was resent.",
      ),
      btn("Keep my work", "close") +
        btn(
          "Review an additional attempt",
          "retryPlan",
          "primary",
          model.retry_plan ? "" : "disabled",
        ),
    );
  if (modal === "rules-recheck-blocked")
    return dialog(
      "Keep these edits separate.",
      notice(
        ruleRecovery,
        "Your rule and other draft edits are preserved. No image request was made.",
        "block",
      ),
      btn("Keep my work", "close") + btn("Return to rules", "rules", "primary"),
    );
  if (modal === "retry-plan") {
    const retry = model.retry_plan;
    if (!retry)
      return dialog(
        "No image retry is needed.",
        "<p>Use local recovery for saved sources, or review the updated brief.</p>",
        btn("Back to work", "close"),
      );
    const names = retry.generation_products
      .map((id) => C.products.find((p) => p.id === id)?.name || "Product")
      .join(", ");
    const unknown = (model.run_attempts || []).some(attempt => ["unknown", "dispatching"].includes(attempt.state));
    return dialog(
      "Resume image generation?",
      `<p>Generate ${retry.generation_products.length === 1 ? "the missing image" : `${retry.generation_products.length} missing images`} for ${esc(names)}. Existing images will be kept.</p>${dataRow("New AI requests", retry.generation_products.length)}${dataRow("Estimated cost", Number.isFinite(retry.cost_estimate.estimated_usd) ? compactUSD(retry.cost_estimate.estimated_usd) : "Not available")}${unknown ? notice("The earlier request’s result is unknown.", "It may still have been charged. Continuing sends a new request.") : ""}<p class="small muted">Actual charges may differ.</p>`,
      btn("Cancel", "close") + btn("Resume generation", "confirmResume", "primary"),
    );
  }

  if (modal === "settings") return settingsModal();
  if (modal === "inspect") return inspectorModal();
  if (modal === "export-failure") return exportFailureModal();
  if (modal === "attempts") return attemptsModal();
  if (modal === "partial-export")
    return dialog(
      "Hand off the available images?",
      `<p>${partialExportOmissions()} The package will identify every omission and include every available file.</p>`,
      btn("Keep reviewing", "close") +
        btn(
          pendingExport === "zip"
            ? "Download partial ZIP"
            : "Preview partial report",
          "confirmPartial",
          "primary",
        ),
    );
  if (modal === "open-report")
    return dialog(
      "Your report is ready.",
      `<a href="${esc(reportPreviewURL)}" target="_blank" rel="noopener">Open report in a new tab ↗</a>`,
      btn("Back to campaign", "close"),
    );
  if (modal === "export-success")
    return dialog(
      "ZIP sent to your browser.",
      `<div class="empty-symbol">↗</div><h3 style="font-size:24px;margin:12px 0;overflow-wrap:anywhere">${esc(exportReceipt?.filename || "Motif.zip")}</h3><p class="muted">${model.manifest.metrics.completed_images} full-size PNGs, the offline overview, and the delivery details were created from your saved campaign settings.</p><div class="rule"></div><p class="small muted">Extract the ZIP anywhere. Open index.html to review without this application, an API key, or a network.</p>`,
      btn("Preview offline overview", "report") +
        btn("Back to campaign", "close", "primary"),
    );
  return originalModalView();
};

function newProduct() {
  return {
    id: `product-${crypto.randomUUID().replaceAll("-", "")}`,
    name: "",
    description: "",
    treatment: "reuse",
    origin: "Not provided",
    rights: "Not provided",
    source: {
      role: "finished_scene",
      path: "",
      provenance: {
        label: "Not provided",
        rights: "Not provided",
        synthetic: false,
      },
    },
  };
}
function productDraft(productId = null) {
  if (operation?.active || model.run?.state === "running")
    throw new Error("Let the current production finish before changing its products.");
  const product = draft.products.find(item => item.id === productId);
  if (productId && !product) throw new Error("This product is no longer in the campaign draft.");
  // Product authoring always uses the current draft. C may describe an older
  // reviewed plan or accepted image set, and must remain bound to that work.
  modalProduct = product ? productDetails(product) : newProduct();
  openModal("product");
}
function retainedProductPhoto(productId) {
  const source = projectBuffers.form_drafts.product_images?.[productId];
  if (source) return { source: copy(source), url: source.path?.includes("/Assets/Products/")
    ? assetURL(source.path) : model.product_images?.[productId]?.url || null };
  return model.product_images?.[productId] || null;
}
function openProductRemoval(productId = null) {
  if (operation?.active || model.run?.state === "running")
    throw new Error("Let the current production finish before changing its products.");
  productRemovalFromEditor = modal === "product" && !productId;
  if (productRemovalFromEditor) captureProductInputs();
  else {
    const product = draft.products.find(item => item.id === productId);
    if (!product) throw new Error("This product is no longer in the campaign draft.");
    modalProduct = productDetails(product);
  }
  openModal("remove-product");
}
function cancelProductRemoval() {
  if (productRemovalFromEditor) {
    openModal("product");
    $('[data-pr-action="remove"]')?.focus();
  }
  else closeModal({ retain: false });
  productRemovalFromEditor = false;
}
async function saveProductDraft() {
  if (modalProduct?.uploading || modalProduct?.adding) return;
  const name = $("#product-name").value,
    description = $("#product-description").value;
  if (!MotifTextLimits.guardFields(["product-name", "product-description", "product-origin", "product-rights"])) return;
  const invalidFields = [];
  if (!name.trim()) invalidFields.push("name");
  if (!description.trim()) invalidFields.push("description");
  if (modalProduct.treatment !== "concept" && !modalProduct.source.path && !modalProduct.localFile)
    invalidFields.push("image");
  if (invalidFields.length) {
    captureProductInputs();
    modalProduct.invalidFields = invalidFields;
    modalProduct.error = [
      invalidFields.includes("name") && invalidFields.includes("description")
        ? "Add a product name and description."
        : invalidFields.includes("name") ? "Add a product name."
          : invalidFields.includes("description") ? "Add a product description." : "",
      invalidFields.includes("image") ? "Choose an image, or select Generate new image." : "",
    ].filter(Boolean).join(" ");
    openModal("product");
    (invalidFields[0] === "image" ? $('[data-pr-action="browse"]') : $(`#product-${invalidFields[0]}`))?.focus();
    return;
  }
  const role = {
    reuse: "finished_scene",
    prepared: "prepared_product",
    concept: "concept",
  }[modalProduct.treatment];
  const provenance = {
    label: $("#product-origin").value || "Not provided",
    rights: $("#product-rights").value || "Not provided",
    synthetic: false,
  };
  const photo = modalProduct.source.path ? modalProduct.source : retainedProductPhoto(modalProduct.id)?.source;
  const source =
    role === "concept"
      ? { role, imagery_absent: true, description, provenance }
      : {
          ...(photo || {}),
          role,
          path: photo?.path || "",
          provenance,
        };
  if (role !== "concept") {
    delete source.imagery_absent;
    delete source.description;
  }
  const index = draft.products.findIndex(item => item.id === modalProduct.id);
  const old = draft.products[index];
  const product = {
    ...old,
    id: old?.id || modalProduct.id,
    name,
    description,
    source,
    copy_overrides: old?.copy_overrides || {},
  };
  if (!old && role !== "concept" && modalProduct.localFile)
    return addProductPhoto(product, modalProduct);
  if (old) draft.products[index] = product;
  else {
    if (draft.products.length >= productLimit)
      throw new Error(`This workspace supports up to ${productLimit} products.`);
    draft.products.push(product);
  }
  if (photo?.path) {
    projectBuffers.form_drafts.product_images ||= {};
    projectBuffers.form_drafts.product_images[product.id] = copy(photo);
  }
  closeModal({ retain: false });
  markDirty();
  render();
}
function discardLocalProductPhoto(target) {
  if (target?.localImage) URL.revokeObjectURL(target.localImage);
  if (target) { delete target.localImage; delete target.localFile; }
}
async function addProductPhoto(product, target) {
  const owner = draft;
  target.adding = true;
  target.error = "";
  openModal("product");
  try {
    await withProjectApply(async () => {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(",", 2)[1]);
        reader.onerror = () => reject(new Error("The selected photo could not be read. Choose it again."));
        reader.readAsDataURL(target.localFile);
      });
      const result = await api(`/api/motif/campaigns/${encodeURIComponent(owner.campaign_id)}/products/photo`, {
        version: projectQueue.version(), base: copy(model.project.base), product, image_base64: encoded,
      });
      if (draft !== owner || modalProduct !== target) return;
      target.adding = false;
      closeModal({ retain: false });
      model = result.campaign;
      model.concept_preview_version = model.version;
      restoreProject(result.project.authored, { restoreContext: false });
      projectQueue.applied(result.project);
      viewModel();
      render();
    });
  } catch (error) {
    if (draft !== owner || modalProduct !== target) return;
    target.error = error.message || "There was an issue adding this product. Your photo and details are still here. Try again.";
  } finally {
    target.adding = false;
    if (modalProduct === target && modal === "product") openModal("product");
  }
}
function openInputField(field) {
  const path = field.path;
  const product = path[0] === "products" ? draft.products[field.product_index ?? path[1]] : null;
  page = path[0] === "products" ? "production" : "campaign";
  if (path[0] === "brand") tab = "brand";
  else if (path[0] === "markets") {
    tab = "copy";
    lang = field.market === "es-MX" ? "es" : "en";
  } else if (page === "campaign") tab = "campaign";
  updateURL();
  render();
  if (product) productDraft(product.id);
  const control = product ? {
    name: "product-name", description: "product-description", label: "product-origin", rights: "product-rights",
  }[path.at(-1)] : {
    name: "bf-campaignName",
    audience: "bf-audience",
    creative_direction: "bf-direction",
    message: "bf-message",
    cta: "bf-cta",
    text_color: "bf-defaultColor",
  }[path.at(-1)];
  if (control) $(`#${control}`)?.focus();
}
function cancelProductUpload() {
  const pending = pendingProductUpload;
  if (!pending) return;
  pendingProductUpload = null;
  pending.target.uploading = false;
  clearTimeout(pending.timer);
  pending.controller.abort();
}
async function upload(file, owner = draft, target = modalProduct) {
  if (!file || !target || target.adding) return;
  cancelProductUpload();
  const localOnly = !owner.products?.some(product => product.id === target.id);
  const sequence = ++productUploadSequence;
  const pending = { target, controller: new AbortController(), timer: null };
  pendingProductUpload = pending;
  target.uploading = true;
  captureProductInputs();
  if (modal === "product") openModal("product");
  pending.timer = setTimeout(() => pending.controller.abort(), 30000);
  const current = () => draft === owner && modalProduct === target &&
    pendingProductUpload === pending && sequence === productUploadSequence;
  try {
    const result = await withBriefPreviewPaused(() => api(localOnly ? "/api/uploads/validate" : "/api/uploads", undefined, {
      method: "POST",
      signal: pending.controller.signal,
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Campaign-Token": $('meta[name="campaign-token"]').content,
        "X-Campaign-Id": owner.campaign_id,
        "X-Asset-Name": encodeURIComponent(target.name || file.name),
        "X-Image-Role": "product",
        "X-Source-Role": target.treatment === "prepared" ? "prepared_product" : "finished_scene",
      },
      body: file,
    }), () => current() && !pending.controller.signal.aborted);
    if (!current()) return;
    if (!result) {
      if (pending.controller.signal.aborted)
        target.error = "The photo upload took too long. Choose the image again.";
      return;
    }
    if (result.opaque === false && target.treatment !== "prepared") {
      throw Object.assign(new Error("This image has transparent areas. Select a product image without transparency."), {
        input_image: { kind: "transparent_scene" },
      });
    }
    if (localOnly) {
      discardLocalProductPhoto(target);
      target.localFile = file;
      target.localImage = URL.createObjectURL(file);
    } else {
      target.source.path = result.reference;
      target.image = result.url;
    }
    target.error = "";
    target.invalidFields = [];
    if (target.treatment === "concept") target.treatment = "reuse";
  } catch (error) {
    if (!current()) return;
    if (pending.controller.signal.aborted) {
      target.error = "The photo upload took too long. Choose the image again.";
      return;
    }
    throw error;
  } finally {
    clearTimeout(pending.timer);
    if (current()) {
      target.uploading = false;
      pendingProductUpload = null;
      if (modal === "product") {
        captureProductInputs();
        projectChanged();
        openModal("product");
      }
    }
  }
}

function picker(logo = false) {
  if (logo) {
    $('#brief-root [data-bf-action="upload-logo"]')?.click();
    return;
  }
  const element = $('#overlay [data-pr-file]') || document.createElement("input");
  element.type = "file";
  element.accept = "image/png,image/jpeg";
  element.value = "";
  const owner = draft, target = modalProduct;
  element.onchange = () => {
    const file = element.files[0];
    if (file && draft === owner && modalProduct === target)
      upload(file, owner, target).catch((error) => {
        if (draft === owner && modalProduct === target)
          imageInputFailure(error, file, false, owner);
      });
  };
  element.click();
}
async function buildPlan() {
  if (page !== "production") await navigate("production");
  if (page !== "production") return;
  return prepareCurrentPlan(true);
}
async function prepareCurrentPlan(retry = false) {
  if (currentPreparation()?.status === "pending") return;
  if (!retry && currentPreparation()?.status === "error") return;
  const owner = draft, campaign = owner.campaign_id;
  const basis = JSON.stringify(currentBriefValue());
  const preparation = productionPreparation = { campaign, basis, status: "pending", error: null };
  blockedProposal = null;
  const owns = () => draft === owner && page === "production" && JSON.stringify(currentBriefValue()) === basis;
  refreshProductionStatus();
  let held = null, before = null, version = null;
  try {
    await saveDraft();
    if (!owns()) return;
    held = projectQueue.hold();
    if (!held) throw new Error("Finish saving your changes before preparing this estimate.");
    before = copy(projectQueue.authored().brief);
    version = model.version;
    const result = await boundedEstimateRequest(signal => withBriefPreviewPaused(
      () => api(`/api/motif/campaigns/${campaign}/plan`, { version }, { signal }),
      () => !signal.aborted && owns(),
    ));
    if (!result || draft !== owner || model.version !== version || projectQueue.version() !== version) return;
    const unchanged = owns();
    if (unchanged) {
      reconcilePreparedSources(before, result.project.authored.brief);
      useData(result, { applied: true });
      retryContext = null;
      blockedProposal = null;
      if (model.unplanned_draft || productionNeedsRun())
        throw Object.assign(new Error("The estimate could not be updated for these products. Try again, or check the campaign details."), { status: 409 });
      preparation.status = "ready";
    } else {
      // This plan owns its attachment normalization, not newer authored input.
      acknowledgePlanSources(before, result);
    }
  } catch (error) {
    let reportError = owns();
    if (held && (!error.status || error.status >= 500)) {
      try {
        // A lost response can hide a completed local snapshot. Read it once;
        // never repeat planning or generation as part of this acknowledgement.
        const current = await boundedEstimateRequest(signal => api(`/api/motif/campaigns/${campaign}`, undefined, { signal }), 5000);
        reportError = owns();
        const base = current.project.base, prior = held.base;
        const normalized = copy(before);
        reconcileAppliedBrief(normalized, before, current.project.authored.brief);
        if (draft === owner && model.version === version && projectQueue.version() === version &&
            current.version === version && base.revision_id === prior.revision_id &&
            (!base.revision_id || base.run_id === prior.run_id) &&
            base.metadata_snapshot_id === prior.metadata_snapshot_id &&
            sameProjectInputs(normalized, current.project.authored.brief)) {
          acknowledgePlanSources(before, current);
        }
      } catch {
        reportError = owns();
      }
    }
    if (!reportError) return;
    preparation.basis = JSON.stringify(currentBriefValue());
    preparation.status = "error";
    preparation.error = error;
    if (error.proposal?.campaign_id === campaign && error.proposal.version === model.version)
      blockedProposal = error.proposal;
  } finally {
    if (held) projectQueue.release(held);
    if (productionPreparation === preparation && preparation.status === "pending") productionPreparation = null;
    if (draft?.campaign_id === campaign && page === "production") refreshProductionStatus();
    queueMicrotask(ensureProductionPlan);
  }
}
async function poll(id = draft.campaign_id, runId = model?.run?.id) {
  if (!runId || !isCurrentRun(id, runId)) return;
  try {
    const status = await api(`/api/campaigns/${id}/status`);
    if (!isCurrentRun(id, runId) || status.run_id !== runId) return;
    if (status.active) {
      operation = status;
      if (status.campaign_metrics?.campaign_id === id)
        model.campaign_metrics = status.campaign_metrics;
      if (page === "production") refreshProductionStatus();
      // Keep the scheduled poll attached to this run, even if a new plan opens.
      setTimeout(() => poll(id, runId), 700);
      return;
    }
    const observedModel = model, observedVersion = model.version;
    const result = await api(`/api/motif/campaigns/${id}`);
    if (!isCurrentRun(id, runId) || result.run?.id !== runId) return;
    if (model !== observedModel || model.version !== observedVersion) {
      // A save finished while the read was in flight. Read current facts again
      // instead of replacing its newer preview with this older response.
      setTimeout(() => poll(id, runId), 700);
      return;
    }
    // Publish terminal status together with its saved result. The prior active
    // presentation must not expose an old planned model's Start in between.
    operation = status;
    // Shared Save owns authored buffers. Refresh result facts without replacing
    // a live editor, its input focus, or its pending draft handlers.
    useData(result, { sourceCompletion: runId });
    if (operation.notice) toast(operation.notice);
    updateURL();
    if (page === "production") refreshProductionStatus();
    else render({ preserveModal: !!modal });
    if (operation.notice_code === "storage_capacity" && showNewProductionFailure()) {
      if (recoveryTarget === runId) recoveryTarget = null;
      return;
    }
    if (showPendingImageCleanup()) return;
    if (recoveryTarget === runId) {
      recoveryTarget = null;
      const attempts = model.run_attempts || [];
      if (
        attempts.some((item) => item.state === "unknown") &&
        !attempts.some(
          (item) => item.source_available || item.response_recoverable,
        )
      ) {
        openModal("unknown-recovery");
        return;
      }
    }
    if (operation.notice) showNewProductionFailure();
  } catch (error) {
    if (isCurrentRun(id, runId)) {
      toast(error.message);
      // Keep the existing read-only polling cadence after a failed result read.
      // No plan or production request is repeated, and each failure is visible.
      if (operation?.run_id === runId && operation.active)
        setTimeout(() => poll(id, runId), 700);
    }
  }
}

document.addEventListener("keydown", (event) => {
  if (modal === "logo" || localDialog) return;
  if (
    ["Enter", " "].includes(event.key) &&
    event.target.matches('[role="button"][data-action]')
  ) {
    event.preventDefault();
    event.target.click();
    return;
  }
  if (!modal) return;
  if (studioWorkspace && !$("#overlay [role=dialog]")) {
    if (event.key === "Escape") { event.preventDefault(); cancelStudioTool(); }
    return;
  }
  if (modal === "frame" && frameSaving && event.key === "Escape") {
    event.preventDefault();
    return;
  }
  if (event.key === "Escape") {
    if (modal === "storage" || storageReturn) {
      event.preventDefault();
      returnFromStorage();
      return;
    }
    closeModal();
    return;
  }
  if (event.key !== "Tab") return;
  const controls = $$(
    "button:not(:disabled),input:not(:disabled):not([type=hidden]),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex]",
    $("#overlay"),
  ).filter(
    (el) =>
      el.tabIndex >= 0 &&
      el.getClientRects().length &&
      !el.closest("[hidden],[inert]"),
  );
  if (!controls.length) {
    event.preventDefault();
    $("#dialog-title")?.focus();
    return;
  }
  event.preventDefault();
  const index = controls.indexOf(document.activeElement);
  const next =
    index < 0
      ? event.shiftKey
        ? controls.length - 1
        : 0
      : (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length;
  controls[next].focus();
});
window.addEventListener("beforeunload", (event) => {
  if (window.MotifSave?.refreshing()) return;
  if (projectQueue?.isDirty() || studioSave?.dirty() || pendingProductUpload ||
      (typeof brandChangesPending === "function" && brandChangesPending()) ||
      (typeof briefLogoUploadPending === "function" && briefLogoUploadPending())) {
    event.preventDefault();
    event.returnValue = "";
  }
});

async function dispatchAction(data) {
  if (localDialog) return;
  if (draft) captureProject();
  // A navigation action can be awaiting this confirmation. Its busy guard
  // must not prevent the producer from answering that exact dialog.
  if (modal === "campaign-copy-confirm" && ["confirmCampaignCopy", "cancelCampaignCopy", "close"].includes(data.action)) {
    finishCampaignCopy(data.action === "confirmCampaignCopy");
    return;
  }
  if (busy) {
    // Inventory is a read-only request. Cancel it without taking ownership of
    // the earlier action's busy flag; its own finally releases that guard.
    if (
      storageView?.state === "delete-loading" &&
      ["close", "storage:cancel"].includes(data.action)
    )
      returnFromStorage();
    return;
  }
  const [action, index, second, third] = data.action.split(":");
  if (["openCampaign", "new", "import"].includes(action) && studioWorkspace && !await leaveStudioWorkspace()) return;
  if (["openCampaign", "new", "import"].includes(action) && draft && !await flushCurrentWork()) {
    if (getSaveState()?.problem?.phase !== "conflict")
      toast("Your changes are still here. Try saving again before switching campaigns.");
    return;
  }
  if (action === "tab" && index !== "brand" && typeof guardBrandNavigation === "function" &&
      guardBrandNavigation({ kind: "tab", target: index })) return;
  const lengthFields = {
    create: ["new-name", "new-brand", "new-audience"],
    saveName: ["rename-name"], saveRule: ["mw-rule-phrase"],
    saveDescription: ["image-description"],
    applyText: ["mf-word-message", "mf-word-cta"],
    applyDraftWording: ["draft-wording-value"],
  }[action];
  if (lengthFields && !MotifTextLimits.guardFields(lengthFields)) return;
  if (["campaigns", "setup", "tab"].includes(action) &&
      typeof briefLogoUploadPending === "function" && briefLogoUploadPending() &&
      guardNavigation(data)) return;
  if (
    ["openCampaign", "new", "import"].includes(action) &&
    guardNavigation({ action, index, campaignId: data.campaignId })
  )
    return;
  if (guardImageCleanup(data.action)) return;
  try {
    busy = true;
    switch (action) {
      case "brandContinue":
        await finishBrandNavigation();
        break;
      case "brandDiscard":
        await finishBrandNavigation(true);
        break;
      case "storage":
        await dispatchStorage(index, second);
        break;
      case "campaigns":
        showCampaignChooser();
        break;
      case "library":
        await navigate("library");
        break;
      case "nav":
        await navigate(index);
        break;
      case "tab":
        tab = index;
        render();
        break;
      case "new":
      case "rename":
      case "settings":
      case "attempts":
        openModal(action);
        break;
      case "setup":
      case "checkReadiness":
        await checkReadiness();
        break;
      case "missingKey":
        showProductionIssue(
          "missing_key",
          "No API key is configured. Local reuse, corrections, and export remain available.",
        );
        break;
      case "updateKey": {
        const code = operation?.notice_code || model?.notice?.code;
        await openKeySetup(true, code === "access_denied" ? operation?.notice || model.notice.message : null);
        break;
      }
      case "addProductImage":
        await navigate("production");
        requestAnimationFrame(() => {
          const button = $(`[data-pr-id="${CSS.escape(index)}"][data-pr-action="choose"]`);
          button?.scrollIntoView({ block: "nearest" });
          button?.focus({ preventScroll: true });
        });
        break;
      case "resumeGeneration": {
        if (!await flushCurrentWork()) break;
        const owner = draft.campaign_id;
        const result = await api(`/api/motif/campaigns/${owner}`);
        if (owner !== draft.campaign_id) break;
        useData(result);
        if (!model.retry_plan) { render(); break; }
        if (!keyAvailable || productionCredentialState().credentialIssue) {
          await openKeySetup(!!keyAvailable);
          break;
        }
        openModal("retry-plan");
        break;
      }
      case "confirmResume": {
        const confirmed = JSON.stringify(model.retry_plan);
        await reviewRetryPlan();
        if (confirmed !== JSON.stringify(model.retry_plan)) {
          openModal("retry-plan");
          toast("The remaining images or estimate changed. Review them before continuing.");
          break;
        }
        await startDisplayedPlan();
        break;
      }
      case "reviewRetryPlan":
        await reviewRetryPlan();
        break;
      case "followRun":
        if (!isCurrentRun(productionRecovery.campaign, productionRecovery.run))
          throw new Error(
            "The displayed run changed. Reopen Production to follow its status.",
          );
        closeModal();
        page = "production";
        await poll();
        break;
      case "import":
        importFile = null;
        importedCampaignId = null;
        importErrorMessage = "";
        openModal("import");
        break;
      case "returnImport":
        openModal("import");
        break;
      case "close":
        if (modal === "storage" || storageReturn) returnFromStorage();
        else closeModal();
        break;
      case "cancelEdits":
        closeModal({ cancel: true });
        break;
      case "cancelCampaignCopy":
        finishCampaignCopy(false);
        break;
      case "confirmCampaignCopy":
        finishCampaignCopy(true);
        break;
      case "applyDraftWording":
        await applyDraftWording();
        break;
      case "create": {
        const created = await api("/api/motif/campaigns", {
          name: $("#new-name").value,
          brand: $("#new-brand").value,
          audience: $("#new-audience").value,
        });
        await refreshCatalog();
        await openCampaign(created.campaign_id);
        break;
      }
      case "openCampaign":
        await openCampaign(data.campaignId);
        break;
      case "workbench":
        moveWorkbench(index === "next" ? 1 : -1);
        break;
      case "openImported":
        await openImportedCampaign();
        break;
      case "doImport": {
        if (importedCampaignId) {
          await openImportedCampaign();
          break;
        }
        const file = $("#json-file").files[0];
        importFile = file || importFile;
        if (!file || file.size > 131072)
          throw new Error("Choose a JSON brief up to 128 KiB.");
        let value;
        try {
          value = JSON.parse(await file.text());
        } catch {
          throw new Error(
            "This file is not valid JSON. Your existing campaign is unchanged.",
          );
        }
        const result = await api("/api/motif/import", value, { body: file });
        importedCampaignId = result.campaign_id;
        importFile = null;
        await openImportedCampaign();
        break;
      }
      case "saveDraft":
        await saveDraft();
        break;
      case "saveName":
        draft.name = $("#rename-name").value;
        clearProjectTarget("rename");
        closeModal({ retain: false });
        markDirty();
        await saveDraft();
        render();
        break;
      case "planInput": {
        const findings = activeBlockedProposal()?.findings || model.plan?.findings || [];
        const field = findings[Number(index)]?.context?.fields?.[0];
        if (field) openInputField(field);
        break;
      }
      case "planDraftInput":
        if (currentPreparation()?.error?.fields?.[0]) openInputField(currentPreparation().error.fields[0]);
        break;
      case "plan":
        await buildPlan();
        break;
      case "addProduct":
        if (draft.products.length >= productLimit) openModal("product-cap");
        else productDraft();
        break;
      case "product":
        productDraft(index);
        break;
      case "productReturn":
        cancelProductRemoval();
        break;
      case "productSource": {
        const product = draft.products.find(item => item.id === index);
        if (!product) throw new Error("This product is no longer in the campaign draft.");
        modalProduct = productDetails(product);
        if (!modalProduct.image) throw new Error("Choose an image before inspecting this product’s source.");
        openModal("source");
        break;
      }
      case "source":
        modalProduct = null;
        currentProduct = Number(index);
        openModal("source");
        break;
      case "attachMissing":
        productDraft(index);
        picker();
        break;
      case "pickProduct":
        picker();
        break;
      case "saveProduct":
        await saveProductDraft();
        break;
      case "remove":
        openProductRemoval(index);
        break;
      case "confirmRemove": {
        const target = draft.products.findIndex(product => product.id === modalProduct.id);
        if (target < 0) throw new Error("This product is no longer in the campaign draft.");
        draft.products.splice(target, 1);
        if (projectBuffers.form_drafts.product_images) delete projectBuffers.form_drafts.product_images[modalProduct.id];
        markDirty();
        closeModal({ retain: false });
        render();
        lastFocus = $('[data-bf-action="add-product"]') || $('[data-pr-action="add"]');
        lastFocus?.focus();
        break;
      }
      case "lang":
        lang = index;
        render();
        break;
      case "rule":
        ruleIndex = -1;
        openModal("rule");
        break;
      case "editRule":
        ruleIndex = Number(index);
        openModal("rule");
        break;
      case "saveRule": {
        const phrase = $("#mw-rule-phrase").value;
        if (!phrase.trim()) {
          $("#mw-rule-error").hidden = false;
          $("#mw-rule-phrase").setAttribute("aria-invalid", "true");
          $("#mw-rule-phrase").focus();
          break;
        }
        const rules = (draft.markets[MARKET[lang]].phrase_rules ||= []);
        const value = {
          ...rules[ruleIndex],
          id: rules[ruleIndex]?.id || `rule-${crypto.randomUUID().replaceAll("-", "")}`,
          phrase,
          match: rules[ruleIndex]?.match || "literal",
          case_sensitive: rules[ruleIndex]?.case_sensitive || false,
        };
        if (ruleIndex < 0) rules.push(value);
        else rules[ruleIndex] = value;
        clearProjectTarget("rule");
        closeModal({ retain: false });
        markDirty();
        await saveProject();
        render();
        break;
      }
      case "deleteRule":
        if (ruleIndex >= 0)
          draft.markets[MARKET[lang]].phrase_rules.splice(ruleIndex, 1);
        clearProjectTarget("rule");
        closeModal({ retain: false });
        markDirty();
        await saveProject();
        render();
        break;
      case "recheck":
        await saveProject();
        useData(await api(`/api/motif/campaigns/${C.id}`));
        render();
        break;
      case "discardLeave":
        clearStorageContext();
        restoreProject(copy(model.project.authored));
        closeModal({ retain: false });
        // Capture must read the restored fields, never the discarded DOM that
        // was still behind the navigation dialog.
        render();
        if (!(await saveProject())) break;
        await finishPendingNavigation();
        break;
      case "saveLeave":
        await savePendingNavigation();
        break;
      case "retryPlan": {
        const owner = draft.campaign_id;
        const result = await api(`/api/motif/campaigns/${owner}`);
        if (owner !== draft.campaign_id) break;
        if (dirty) model = result;
        else useData(result);
        openModal("retry-plan");
        break;
      }
      case "start":
        await startDisplayedPlan();
        break;
      case "reviewTab":
        reviewTab = index;
        pageNum = 0;
        filter = "all";
        render();
        break;
      case "partialReview":
        closeModal();
        page = "review";
        reviewTab = "overview";
        pageNum = 0;
        filter = "all";
        updateURL();
        render();
        break;
      case "format":
        formats[Number(index)] = Number(second);
        render();
        if (reviewTab === "product")
          $(`#product-format-${second}`)?.focus({ preventScroll: true });
        break;
      case "repairLayout":
        if (!await flushCurrentWork()) break;
        currentProduct = Number(index);
        currentFormat = Number(second);
        lang = third;
        page = "review";
        updateURL();
        await openStudioWorkspace(data.action.split(":")[4]);
        break;
      case "inspect":
        currentProduct = Number(index);
        currentFormat = Number(second);
        if (third) lang = third;
        openModal("inspect");
        break;
      case "frame":
        currentProduct = Number(index);
        currentFormat = Number(second);
        openModal("frame");
        break;
      case "text":
        currentProduct = Number(index);
        currentFormat = Number(second);
        openModal("text");
        break;
      case "resetText":
        resetText();
        break;
      case "recoverText":
        await recoverText();
        break;
      case "applyText":
        await withProjectApply(applyText);
        break;
      case "restoreFrame":
        resetFrame();
        break;
      case "markSubject":
        markFrameSubject();
        break;
      case "clearSubject":
        clearFrameSubject();
        break;
      case "recoverFrame":
        // Recovery reads stay editable; a confirmed failed Apply retries explicitly.
        await recoverFraming();
        break;
      case "saveFrame":
        await withProjectApply(saveFraming);
        break;
      case "fullsize": {
        const output = outputFor(currentProduct, lang, currentFormat);
        if (!output?.url) throw new Error("This image is unavailable.");
        window.open(output.url, "_blank", "noopener");
        break;
      }
      case "download":
        await downloadImage(Number(index), second, Number(third));
        break;
      case "saveDescription":
        await saveMetadata(currentProduct, lang, currentFormat, {
          description: $("#image-description").value || null,
        });
        toast("Image details saved.");
        break;
      case "openComparison":
        closeModal();
        page = "review";
        reviewTab = "compare";
        updateURL();
        render();
        break;
      case "brandEdit":
      case "rules":
        page = "campaign";
        tab = action === "rules" ? "copy" : "brand";
        closeModal();
        updateURL();
        render();
        break;
      case "nextPage":
        pageNum++;
        render();
        break;
      case "prevPage":
        pageNum = Math.max(0, pageNum - 1);
        render();
        break;
      case "exportZip":
        requireIntactExport();
        pendingExport = "zip";
        if (!model.manifest.complete) openModal("partial-export");
        else await downloadZIP(false);
        break;
      case "cancelExport":
        await cancelExport();
        break;
      case "retryExport":
        await retryExport();
        break;
      case "exportOverview": {
        closeModal({ restoreFocus: false });
        page = "handoff";
        render();
        const overview = document.querySelector(".overview-grid");
        if (overview) {
          const target = overview.querySelector('button[data-action^="download:"]:not(:disabled)') || overview;
          if (target === overview) overview.tabIndex = -1;
          target.focus({ preventScroll: true });
          overview.scrollIntoView({ block: "start" });
        }
        break;
      }
      case "confirmPartial":
        if (pendingExport === "zip") await downloadZIP(true);
        else await previewReport(true);
        break;
      case "report":
        await previewReport();
        break;
      case "recover":
        recoveryTarget = model.run.id;
        closeModal();
        await api(`/api/campaigns/${C.id}/recover`, { run_id: model.run.id });
        page = "production";
        await poll();
        break;
      case "stop":
        stopTarget = { campaign: draft.campaign_id, run: model.run.id };
        openModal("stop");
        break;
      case "confirmStop":
        await confirmStop();
        break;
      default:
        throw new Error(
          "This action is not available for the current saved campaign.",
        );
    }
  } catch (error) {
    if (error.code === "retired_revision") {
      openImageCleanup({ old: true });
    } else if (showCapacity(error, "This operation")) {
      // Capacity refusal happens before this requested large write starts.
    } else if (error.code === "image_cleanup_pending") {
      model.image_cleanup = await api(
        `/api/campaigns/${draft.campaign_id}/cleanup/status`,
      );
      guardImageCleanup(data.action);
    } else if (action === "doImport") {
      importErrorMessage = error.message;
      openModal(error.code === "busy" ? "import-busy" : "invalid-import");
    } else if (
      ["start", "startRetry"].includes(action) &&
      error.code === "missing_key"
    ) {
      showProductionIssue(error.code, error.message);
    } else {
      if (error.fields?.length && !modal && ["campaign", "production"].includes(page)) {
        openInputField(error.fields[0]);
      }
      toast(error.message);
    }
  } finally {
    busy = false;
    projectChanged();
    if (page === "production") {
      refreshProductionStatus();
      queueMicrotask(ensureProductionPlan);
    }
  }
}
document.addEventListener("click", event => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  event.preventDefault();
  return dispatchAction(button.dataset);
});

(async () => {
  try {
    await refreshCatalog();
    if (document.body.dataset.campaign)
      await openCampaign(
        document.body.dataset.campaign,
        document.body.dataset.page,
      );
    else {
      const intent = new URLSearchParams(location.search).get("action");
      render();
      if (intent === "new") {
        history.replaceState({}, "", "/");
        openModal("new");
      }
    }
  } catch (error) {
    $("#app").innerHTML = shell(
      emptyPage(
        "This campaign could not be opened.",
        error.message,
        "library",
        "Open workbench",
      ),
    );
  }
})();

