"use strict";

let stopTarget = null;
let importFile = null;
let importErrorMessage = "";
let navigationError = "";
let productUploadSequence = 0;
let productRemovalFromEditor = false;

function hasPendingNavigationWork() {
  return !!projectQueue?.isDirty();
}
function guardNavigation(destination) {
  if (!hasPendingNavigationWork()) return false;
  pendingNav = destination;
  navigationError = "";
  openModal("unsaved");
  return true;
}
async function savePendingNavigation() {
  try {
    if (await saveProject()) await finishPendingNavigation();
    else {
      const state = getSaveState();
      navigationError = state?.busy && state.problem?.phase !== "conflict"
        ? "Saving will finish in a moment. Try again."
        : "Saving is paused. Keep editing, then review the update.";
    }
  } catch (error) {
    navigationError = error.message;
  }
  if (pendingNav && modal === "unsaved") openModal("unsaved");
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
  if (modal === "import" && importFile) {
    const selected = new DataTransfer();
    selected.items.add(importFile);
    $("#json-file").files = selected.files;
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
function closeModal({ cancel = false, retain = true } = {}) {
  if (studioWorkspace && ["text", "frame", "logo", "inspect"].includes(modal)) return cancelStudioTool();
  if (modal === "remove-product" && retain) return cancelProductRemoval();
  const closingKind = modal;
  if (modal === "draft-wording" && draftWording?.saving) return;
  const navigationChoice = modal === "unsaved";
  const authoring = ["rule", "rename", "inspect"].includes(modal);
  // Product edits are local until Add/Update. Every dismissal discards them.
  if (modal === "product") { cancel = false; retain = false; }
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
  restore();
  queueMicrotask(restore);
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
  if (guardNavigation(target)) return;
  // Workbench order comes from successful server saves, including image review.
  // Opening a campaign does not write or advance that activity timestamp.
  if (target === "library") await refreshCatalog();
  page = target === "products" ? "production" : target === "copy" ? "campaign" : target;
  if (target === "copy") tab = "copy";
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
      `<p class="muted" style="margin-bottom:18px">Import a documented JSON brief into the same editable fields. You can attach images later.</p><div class="upload"><strong>Choose a JSON brief</strong><input type="file" id="json-file" accept=".json,application/json" aria-label="Choose a JSON brief"></div><div id="import-error" role="alert"></div>`,
      `<span class="small muted">English, Spanish, or both · 1–${productLimit} products · all three formats</span>${btn("Import into a new draft", "doImport", "primary")}`,
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
      `<div class="menu-popover">${saved.map((c) => `<button data-action="openCampaign" data-campaign-id="${esc(c.id)}"><strong>${esc(c.name)}</strong><br><span class="small muted">${campaignContext(c) ? esc(campaignContext(c)) + " · " : ""}${c.products} ${c.products === 1 ? "product" : "products"} · ${workbenchCounts(c)} · ${esc(activityLabel(c.updated))}</span></button>`).join("")}</div>`,
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
    const additional = Math.max(0, retry.allowance - model.run.allowance);
    return dialog(
      "A new attempt is a new decision.",
      `${notice("The previous attempts remain in the record.", `${retry.previous_attempts} used of ${model.run.allowance} request slots. Charges are not confirmed. No automatic retry has occurred.`)}${dataRow("New work", `${names} · ${retry.generation_products.length} additional image ${retry.generation_products.length === 1 ? "request" : "requests"}`)}${dataRow("Additional allowance", `${additional} ${additional === 1 ? "slot" : "slots"} · ${retry.allowance} cumulative`)}${dataRow("Provider charges", "Check your provider account.")}${dataRow("New-request estimate", Number.isFinite(retry.cost_estimate.estimated_usd) ? compactUSD(retry.cost_estimate.estimated_usd) : "Not estimated")}<p class="small muted" style="margin-top:20px">${Number.isFinite(retry.cost_estimate.estimated_usd) ? "" : "No verified estimate is available for these model settings. "}First look for saved imagery, attach a suitable replacement, or hand off available files. Reviewing the revised plan does not dispatch a request.</p>`,
      btn("Look for saved result", "recover") +
        btn("Review the revised plan", "reviewRetryPlan", "primary"),
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
            : "Create partial package",
          "confirmPartial",
          "primary",
        ),
    );
  if (modal === "export-success")
    return dialog(
      "The work is ready to travel.",
      `<div class="empty-symbol">↗</div><h3 style="font-size:24px;margin:12px 0;overflow-wrap:anywhere">${esc(exportReceipt?.filename || "Saved campaign folder")}</h3><p class="muted">${model.manifest.metrics.completed_images} full-size PNGs, the offline overview, manifest, and provenance belong to this saved revision and description snapshot.</p><div class="rule"></div><p class="small muted">${exportReceipt?.kind === "zip" ? "Extract the ZIP anywhere." : "Take the complete saved folder with you."} Open index.html to review without this application, an API key, or a network.</p>`,
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
function saveProductDraft() {
  const name = $("#product-name").value,
    description = $("#product-description").value;
  const invalidFields = [];
  if (!name.trim()) invalidFields.push("name");
  if (!description.trim()) invalidFields.push("description");
  if (modalProduct.treatment !== "concept" && !modalProduct.source.path)
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
async function upload(file, owner = draft, target = modalProduct) {
  if (!file) return;
  const sequence = ++productUploadSequence;
  const result = await withBriefPreviewPaused(() => api("/api/uploads", undefined, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Campaign-Token": $('meta[name="campaign-token"]').content,
      "X-Campaign-Id": owner.campaign_id,
    },
    body: file,
  }), () => draft === owner && modalProduct === target);
  if (!result || draft !== owner || sequence !== productUploadSequence) return;
  if (!target || target !== modalProduct) {
    toast("Image uploaded. The closed product draft was not changed.");
    return;
  }
  target.source.path = result.reference;
  target.image = result.url;
  if (target.treatment !== "concept") target.treatment = "reuse";
  if (modal === "product") {
    captureProductInputs();
    openModal("product");
  }
  if ($("#asset-result"))
    $("#asset-result").innerHTML = notice(
      file.name,
      `${result.width} × ${result.height} · save this product to attach the image`,
      "info",
    );
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
    const result = await withBriefPreviewPaused(() => api(`/api/motif/campaigns/${campaign}/plan`, { version }), owns);
    if (!result || draft !== owner || model.version !== version || projectQueue.version() !== version) return;
    const unchanged = owns();
    if (unchanged) {
      reconcilePreparedSources(before, result.project.authored.brief);
      useData(result, { applied: true });
      retryContext = null;
      blockedProposal = null;
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
        const current = await api(`/api/motif/campaigns/${campaign}`);
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
    const result = await api(`/api/motif/campaigns/${id}`);
    if (!isCurrentRun(id, runId) || result.run?.id !== runId) return;
    // Publish terminal status together with its saved result. The prior active
    // presentation must not expose an old planned model's Start in between.
    operation = status;
    // Shared Save owns authored buffers. Refresh result facts without replacing
    // a live editor, its input focus, or its pending draft handlers.
    useData(result);
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
  if (projectQueue?.isDirty()) {
    event.preventDefault();
    event.returnValue = "";
  }
});

async function dispatchAction(data) {
  if (localDialog) return;
  if (draft) captureProject();
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
  if (
    ["openCampaign", "new", "import"].includes(action) &&
    guardNavigation({ action, index, campaignId: data.campaignId })
  )
    return;
  if (guardImageCleanup(data.action)) return;
  try {
    busy = true;
    switch (action) {
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
      case "doImport": {
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
        await refreshCatalog();
        await openCampaign(result.campaign_id, "campaign");
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
        saveProductDraft();
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
        if (model?.manifest) await withProjectApply(recheckRules);
        else {
          closeModal();
          render();
        }
        break;
      }
      case "deleteRule":
        if (ruleIndex >= 0)
          draft.markets[MARKET[lang]].phrase_rules.splice(ruleIndex, 1);
        clearProjectTarget("rule");
        closeModal({ retain: false });
        markDirty();
        if (model?.manifest) await withProjectApply(recheckRules);
        else render();
        break;
      case "recheck":
        await withProjectApply(recheckRules);
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
        // The editor owns this read and permits Close while it is pending.
        recoverFraming();
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
      case "retryExport":
        await retryExport();
        break;
      case "exportOverview":
        closeModal();
        page = "handoff";
        render();
        document
          .querySelector(".handoff-grid, .overview-table")
          ?.scrollIntoView({ block: "start" });
        break;
      case "confirmPartial":
        if (pendingExport === "zip") await downloadZIP(true);
        else await createFolder(pendingExport === "report", true);
        break;
      case "exportFolder":
        await createFolder(false);
        break;
      case "report":
        await createFolder(true);
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
      openModal("invalid-import");
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

