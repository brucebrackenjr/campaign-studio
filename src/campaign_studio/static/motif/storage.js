"use strict";

// Designer-owned pure views share the application's modal, drafts, and dispatcher.
// Only server-issued identities and inventory tokens authorize file removal.
let storageView = null;
let storageReturn = null;
let storageRequest = 0;
let deletion = null;
let pendingCleanup = [];
let inputRecovery = null;
let deletionSubmitting = false;
let deletionInventory = null;

function rememberStorageReturn() {
  if (storageReturn) return;
  storageReturn = {
    modal,
    nodes: [...$("#overlay").childNodes],
    focus: document.activeElement,
    product: modalProduct,
    campaign: draft?.campaign_id,
  };
}
function showStorage(state, data = {}, { remember = true } = {}) {
  if (remember) rememberStorageReturn();
  storageView = { state, data };
  openModal("storage");
}
function storageDialog() {
  const view = MotifStorage.getView(storageView.state, storageView.data);
  if (!view) throw new Error("This storage action is unavailable.");
  let body = view.body;
  if (storageView.state === "campaign-context") {
    let i = 0;
    body = body.replaceAll('data-sc="open-other"', () => {
      const campaign = storageView.data.otherCampaigns[i++];
      return `data-action="openCampaign" data-campaign-id="${esc(campaign.id)}"`;
    });
  }
  let html = dialog(view.title, body, view.footer, view.wide, storageView.state.startsWith("delete-") ? deletion?.label || deletion?.name : null)
    .replaceAll('data-sc="', 'data-action="storage:')
    .replace(
      'id="dialog-title"',
      `id="dialog-title" tabindex="-1"${view.pending ? ' style="outline:none"' : ""}`,
    );
  if (view.pending || deletionSubmitting)
    html = html
      .replace('aria-modal="true"', 'aria-modal="true" aria-busy="true"')
      .replace('class="close"', 'class="close" disabled');
  if (deletionSubmitting && !view.pending)
    html = html.replaceAll(
      '<button type="button"',
      '<button type="button" disabled',
    );
  return html;
}
function focusStorageDialog() {
  if (modal !== "storage") return false;
  const view = MotifStorage.getView(storageView.state, storageView.data);
  const selector = view.pending
    ? "#dialog-title"
    : view.initialFocus === "dialog-close"
      ? "button.close"
      : `footer [data-action="storage:${view.initialFocus}"]`;
  ($(selector, $("#overlay")) || $("#dialog-title"))?.focus();
  return true;
}
function returnFromStorage() {
  if (deletionSubmitting || storageView?.state === "delete-pending") return;
  deletionInventory?.abort();
  const previous = storageReturn;
  storageView = null;
  storageReturn = null;
  storageRequest++;
  if (previous?.modal && previous.campaign === draft?.campaign_id) {
    modal = previous.modal;
    modalProduct = previous.product;
    $("#overlay").replaceChildren(...previous.nodes);
    previous.focus?.focus();
    if (modal === "text") return previewText();
    if (modal === "frame") return requestFramePreview();
  } else {
    closeModal();
    const restore = () => {
      if (modal || previous?.campaign !== draft?.campaign_id) return;
      const target = previous.resolveFocus ? previous.resolveFocus() : previous.focus;
      if (target?.isConnected) target.focus();
    };
    restore();
    if (previous?.resolveFocus) queueMicrotask(restore);
  }
}
function hasStoredProductDraft() {
  return (
    storageReturn?.modal === "product" &&
    storageReturn.campaign === draft?.campaign_id
  );
}
function clearStorageContext() {
  storageView = null;
  storageReturn = null;
  inputRecovery = null;
  storageRequest++;
}
function showCampaignChooser() {
  if (!draft) {
    openModal("campaigns");
    return;
  }
  showStorage("campaign-context", {
    campaignName: draft.name,
    campaignSummary: `${draft.brand.name ? draft.brand.name + " · " : ""}${draft.products.length} ${draft.products.length === 1 ? "product" : "products"} · ${Object.keys(draft.markets).length} ${Object.keys(draft.markets).length === 1 ? "language" : "languages"}`,
    otherCampaigns: saved.filter((c) => c.id !== draft.campaign_id).map((c) => ({
      id: c.id,
      name: c.name,
      summary: `${c.brand ? c.brand + " · " : ""}${c.products} ${c.products === 1 ? "product" : "products"} · ${workbenchCounts(c)} · ${activityLabel(c.updated)}`,
    })),
  });
}
function deletionData(inventory) {
  return {
    campaignName: inventory.name,
    estimatedReclaimBytes: inventory.removable_bytes,
    sourceFilesToRemove: inventory.source_file_count,
    outputPNGs: inventory.output_png_count,
    packages: inventory.package_count,
    unmanagedFiles: inventory.unmanaged_file_count,
    hasUnsavedEdits:
      deletion?.campaign === draft?.campaign_id && hasPendingNavigationWork(),
  };
}
async function previewDeletion() {
  const campaign = deletion?.campaign || draft?.campaign_id;
  deletion = { campaign, name: deletion?.name || draft?.name, label: deletion?.label };
  const request = ++storageRequest;
  const controller = new AbortController();
  deletionInventory = controller;
  showStorage("delete-loading", { campaignName: deletion.name, hasUnsavedEdits: campaign === draft?.campaign_id && hasPendingNavigationWork() });
  try {
    const inventory = await api(
      `/api/campaigns/${campaign}/cleanup/preview`,
      {},
      { signal: controller.signal },
    );
    if (request !== storageRequest || modal !== "storage") return;
    deletion = { ...deletion, inventory };
    storageView = { state: "delete-confirm", data: deletionData(inventory) };
    const view = MotifStorage.getView(storageView.state, storageView.data);
    // The confirmation keeps its header, buttons, and focus while file counts arrive.
    $("#overlay .modal-body").innerHTML = view.body;
    $('[data-action="storage:confirm-delete"]', $("#overlay")).disabled = false;
  } catch (error) {
    if (controller.signal.aborted) return;
    if (request !== storageRequest || modal !== "storage") return;
    showStorage(error.code === "busy" ? "delete-active" : "delete-failed", {
      campaignName: deletion.name,
      failureMessage: error.message,
      activeOperation: error.message,
    });
  } finally {
    if (deletionInventory === controller) deletionInventory = null;
  }
}
function clearDeletedCampaignDraft(owner) {
  forgetProject(owner);
  if (draft?.campaign_id === owner) {
    dirty = false;
    draft = null;
    model = null;
    baseline = "";
    operation = null;
    if (storageReturn?.modal === "frame") endFraming();
  }
}
async function finishDeletion(result) {
  clearDeletedCampaignDraft(deletion.campaign);
  clearStorageContext();
  closeModal();
  await refreshCatalog();
  page = "library";
  updateURL();
  render();
  const heading = $("#main h1") || $("#main h2");
  if (heading) {
    heading.tabIndex = -1;
    heading.style.outline = "none";
    heading.focus();
  }
  toast(
    Number.isFinite(result.removed_bytes)
      ? `Campaign deleted. ${MotifStorage.mb(result.removed_bytes)} of app-owned files removed.`
      : "Campaign deleted.",
  );
  deletion = null;
}
async function resumeDeletion() {
  const owner = deletion;
  showStorage("delete-pending", { campaignName: owner.name });
  try {
    const result = await api(
      `/api/campaigns/${owner.campaign}/cleanup/${owner.id}/retry`,
      {},
    );
    if (result.state === "complete") await finishDeletion(result);
    else showStorage("delete-cleanup-pending", { campaignName: owner.name });
  } catch (error) {
    if (error.status === 409 || (error.status === 404 && error.code === "not_found")) {
      // Another tab or startup may have completed this same committed intent.
      try {
        await checkDeletion();
        return;
      } catch {
        /* Keep the outcome unknown. */
      }
    }
    showStorage("delete-uncertain", { campaignName: owner.name });
  }
}
async function confirmDeletion() {
  if (!deletion?.inventory || deletionSubmitting) return;
  deletionSubmitting = true;
  openModal("storage");
  try {
    const result = await api(
      `/api/campaigns/${deletion.campaign}/cleanup/confirm`,
      { token: deletion.inventory.token },
    );
    deletion.id = result.cleanup_id;
    deletionSubmitting = false;
    if (result.state === "complete") await finishDeletion(result);
    else await resumeDeletion();
  } catch (error) {
    deletionSubmitting = false;
    // Even a 503 can arrive after the intent committed. Only explicit
    // precommit refusals establish that deletion did not start.
    showStorage(
      error.code === "cleanup_changed"
        ? "delete-changed"
        : error.code === "busy"
          ? "delete-active"
          : "delete-uncertain",
      {
        campaignName: deletion.name,
        failureMessage: error.message,
        activeOperation: error.message,
      },
    );
  }
}
async function checkDeletion() {
  const owner = deletion;
  let status;
  try {
    status = await api(`/api/campaigns/${owner.campaign}/cleanup/status`);
  } catch (error) {
    // A completed deletion removes the registry row. Only that exact confirmed
    // campaign's not-found result resolves a lost deletion acknowledgement.
    if (deletion !== owner) return;
    if (error.status === 404 && error.code === "not_found") {
      await finishDeletion({});
      return;
    }
    throw error;
  }
  if (deletion !== owner) return;
  const intent = status.pending.find((item) => item.kind === "campaign");
  if (intent) {
    deletion.id = intent.id;
    showStorage("delete-cleanup-pending", { campaignName: deletion.name });
  } else showStorage("delete-failed", {
    campaignName: deletion.name,
    failureMessage: "Deletion did not start. Review the current details before confirming again.",
  });
}

function pendingDeletionControl() {
  return pendingCleanup
    .filter((item) => item.kind === "campaign")
    .map(
      (item) =>
        `<button class="btn small" data-action="storage:resume-deletion:${esc(item.id)}" title="${esc(item.name)}">Finish pending deletion</button>`,
    )
    .join("");
}
function showCapacity(error, operationLabel) {
  if (!["workspace_budget", "disk_space"].includes(error.capacity?.kind))
    return false;
  const d = error.capacity;
  showStorage(
    d.kind === "workspace_budget" ? "storage-budget" : "storage-disk",
    {
      operationLabel,
      usedBytes: d.used_bytes,
      budgetBytes: d.budget_bytes,
      projectedBytes: d.projected_bytes,
      freeBytes: d.free_bytes,
      reserveBytes: d.reserve_bytes,
      requiredAdditionalFreeBytes: Math.max(
        0,
        d.projected_bytes + d.reserve_bytes - d.free_bytes,
      ),
    },
  );
  return true;
}
function imageInputFailure(error, file, logo, owner, resolveFocus) {
  if (owner !== draft) {
    toast(error.message);
    return;
  }
  inputRecovery = { logo, owner };
  if (showCapacity(error, "Attaching this image")) {
    if (resolveFocus) storageReturn.resolveFocus = resolveFocus;
    return;
  }
  const kinds = {
    unsupported_type: "image-unsupported",
    encoded_bytes: "image-bytes",
    pixel_count: "image-pixels",
    decode_failure: "image-corrupt",
    color_profile: "image-color-profile",
    no_visible_pixels: "image-empty",
  };
  const d = error.input_image;
  if (!kinds[d?.kind] || (d.kind === "pixel_count" && !(d.width && d.height))) {
    toast(error.message);
    return;
  }
  showStorage(kinds[d.kind], {
    inputRole: logo ? "logo" : "product",
    filename: file.name.split(/[\\/]/).pop(),
    detectedType: d.detected_type || "an unsupported format",
    encodedBytes: d.encoded_bytes || file.size,
    width: d.width,
    height: d.height,
    previousAttachment: (
      logo ? draft.brand.logo_path : modalProduct?.source.path
    )
      ? "The previously attached image"
      : null,
  });
  if (resolveFocus) storageReturn.resolveFocus = resolveFocus;
}
function guardImageCleanup(action) {
  // Project Save preserves pending work; only Apply publishes image results.
  const growth = [
    "recheck",
    "saveRule",
    "start",
    "startRetry",
    "recover",
  ];
  if (!growth.includes(action.split(":")[0]) || !model?.image_cleanup)
    return false;
  const cleanup = model.image_cleanup;
  if (cleanup.pending.some((item) => item.kind === "retirement")) {
    openImageCleanup();
    return true;
  }
  return false;
}
async function dispatchStorage(action, identity) {
  switch (action) {
    case "cancel":
    case "return-product":
    case "keep-product":
      returnFromStorage();
      return;
    case "card-delete": {
      const card = saved.find((item) => item.id === identity);
      if (!card || page !== "library") throw new Error("Refresh the workbench to find this campaign.");
      deletion = { campaign: card.id, name: card.name, label: card.name };
      await previewDeletion();
      return;
    }
    case "preview-delete":
      if (storageView?.state === "campaign-context") deletion = null;
      await previewDeletion();
      return;
    case "confirm-delete":
      await confirmDeletion();
      return;
    case "check-deletion":
      await checkDeletion();
      return;
    case "retry-deletion":
      await resumeDeletion();
      return;
    case "resume-deletion": {
      const item = pendingCleanup.find(
        (item) => item.id === identity && item.kind === "campaign",
      );
      if (!item)
        throw new Error("Refresh the workbench to check this deletion.");
      deletion = { campaign: item.campaign_id, id: item.id, name: item.name };
      showStorage("delete-cleanup-pending", { campaignName: item.name });
      return;
    }
    case "home":
      if (storageView?.state === "delete-cleanup-pending") {
        // Confirmation already included discarding this campaign's drafts.
        clearDeletedCampaignDraft(deletion.campaign);
        clearStorageContext();
        closeModal();
        await refreshCatalog();
      }
      await navigate("library");
      return;
    case "back-to-campaign":
      if (deletion?.campaign && deletion.campaign !== draft?.campaign_id) await openCampaign(deletion.campaign, "production");
      else await navigate("production");
      return;
    case "rename":
      if (hasStoredProductDraft() && guardNavigation({ action: "rename" }))
        return;
      clearStorageContext();
      openModal("rename");
      return;
    case "new":
      if (typeof guardBrandNavigation === "function" && guardBrandNavigation({ action: "new" }))
        return;
      if (!guardNavigation({ action: "new" })) {
        clearStorageContext();
        openModal("new");
      }
      return;
    case "view-campaigns":
      showCampaignChooser();
      return;
    case "workspace":
    case "workspace-refresh":
      await checkReadiness();
      return;
    case "choose-image": {
      const recovery = inputRecovery;
      returnFromStorage();
      picker(recovery?.logo || false);
      return;
    }
    default:
      throw new Error("This storage action is unavailable.");
  }
}

function showPendingImageCleanup() {
  if (
    !model?.image_cleanup?.pending.some((item) => item.kind === "retirement")
  )
    return false;
  openImageCleanup({ committed: true });
  return true;
}
