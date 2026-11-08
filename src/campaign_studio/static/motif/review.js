"use strict";

// Shared text intent, precise blocked-copy guidance, and current image metadata/export.
function wordingIntent(editor) {
  return MotifEditorControls.changes({
    initial: editor.initial, values: editor.values, all: editor.all,
    openingProductId: editor.product, productIds: C.products.map(p => p.id),
    market: MARKET[editor.language],
  });
}
function wordingPatch(editor) {
  const intent = wordingIntent(editor);
  return {
    values: Object.fromEntries(intent.fields.map(f => [f.field, f.field === "cta" ? f.value || null : f.value])),
    field_scopes: Object.fromEntries(intent.fields.map(f => [f.field, f.scope === "all" ? "all_products" : "product"])),
  };
}
function wordingBlockDetails(findings, localTarget = null) {
  const view = MotifWording.fromIssue({ matches: findings.filter(f => f.severity === "blocking") });
  const wording = view.cases.map(item => `${item.product} · ${item.language} · ${item.field}: remove ${item.terms.map(term => `“${term}”`).join(", ")}.`);
  const errors = findings.filter(f => f.severity === "blocking" && !f.context?.phrase).map(f => {
    const [productId, market, ratio] = (f.item || "").split("/");
    const context = f.context || {}, product = C.products.find(p => p.id === (context.product_id || productId));
    const language = Object.keys(MARKET).find(key => MARKET[key] === (context.market || market));
    const format = context.ratio || FMT.find(item => item.ratio.replace(":", "x") === ratio)?.ratio;
    const evidence = f.evidence === "An unbroken copy segment is too wide for the output."
      ? "A word is too wide for its text area. Shorten the word or reset the text layout." : f.evidence;
    return f.item === localTarget ? evidence
      : [context.product_name || product?.name, LANG[language], format, evidence].filter(Boolean).join(" · ");
  });
  return [...new Set([...wording, ...errors])].join("\n");
}
function metadataPath() {
  return `/api/campaigns/${C.id}/runs/${model.output_run_id}/revisions/${model.manifest.revision_id}`;
}
async function saveMetadata(product, language, format, values) {
  return withProjectApply(() => applyMetadata(product, language, format, values));
}
async function applyMetadata(product, language, format, values) {
  if (!model.can_describe)
    throw new Error(
      "Return to the current version before changing its image description.",
    );
  const creativeId = `${C.products[product].id}/${MARKET[language]}/${FMT[format].ratio.replace(":", "x")}`;
  const row = model.metadata.creatives[creativeId];
  const target = metadataPath();
  const formKey = `review/${C.products[product].id}/${language}/${format}`;
  const pending = projectBuffers.form_drafts.fields?.[formKey];
  const submittedFields = pending && JSON.stringify(pending);
  const result = await api(`${target}/metadata/${creativeId}`, {
    mode: "current",
    expected_version: row.version,
    ...values,
  });
  if (!model?.manifest || metadataPath() !== target) return;
  // A delayed snapshot must not replace newer image details from another save.
  if (
    Object.entries(model.metadata.creatives).some(
      ([id, value]) => (result.creatives[id]?.version ?? -1) < value.version,
    )
  )
    return;
  model.metadata = result;
  // Updating image details clears only the fields this request submitted. A
  // newer description typed while it was in flight remains pending and saved.
  if ("description" in values && submittedFields && modal === "inspect" &&
      projectFormKey() === formKey && JSON.stringify(projectFormValues(imageEditorRoot())) === submittedFields) {
    delete projectBuffers.form_drafts.fields[formKey];
    delete projectBuffers.review_drafts[creativeId];
    projectFieldDefaults[formKey] = projectFormValues(imageEditorRoot());
  }
  model.project.base.metadata_snapshot_id = result.snapshot_id;
  projectQueue.applied(model.project);
  // Refresh descriptions under the dialog without recreating its draft or event bindings.
  render({ preserveModal: true });
  if (modal === "inspect" && currentProduct === product && lang === language && currentFormat === format) {
    const image = $('img[data-action="fullsize"]', imageEditorRoot());
    if (image) image.alt = result.creatives[creativeId].description ||
      `Image description not supplied. ${C.products[product].name}, ${LANG[language]}, ${FMT[format].name}.`;
  }
}
async function downloadImage(product, language, format) {
  if (!await flushCurrentWork()) throw new Error("Your changes are still here. Save them before downloading.");
  requireIntactExport();
  const output = outputFor(product, language, format), owner = model;
  if (!output?.url) throw new Error("This image is unavailable. Recover it before downloading.");
  const downloadURL = output.download_url || `/api/campaigns/${C.id}/composition/${deliveryVersion()}/download/${C.products[product].id}/${MARKET[language]}/${FMT[format].ratio.replace(":", "x")}`;
  const response = await fetch(downloadURL);
  if (model !== owner) return;
  if (!response.ok) {
    const body = await response.json();
    const error = Object.assign(new Error(body.message || "The image could not be downloaded."), { code: body.code, status: response.status });
    if (await wordingExportRefused(error)) return;
    throw error;
  }
  const blob = await response.blob();
  if (model !== owner) return;
  const anchor = document.createElement("a"), url = URL.createObjectURL(blob);
  anchor.href = url;
  anchor.download = decodeURIComponent(response.headers.get("Content-Disposition").split("UTF-8''")[1]);
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
function deliveryPath() { return `/api/campaigns/${C.id}/delivery`; }
function deliveryVersion() { return model.composition_version || model.version; }
function requireIntactExport() {
  if (model.composition_issues?.length) throw new Error("Finish the incomplete campaign fields before exporting.");
  if (wordingExportBlocked()) throw new Error("Correct the prohibited wording before exporting.");
  if (currentLayoutFindings().length) throw new Error("Correct the listed image layouts in Studio before exporting.");
  if (!model?.manifest?.metrics.completed_images)
    throw new Error(
      "Nothing to export yet. Open Production to recover or create images.",
    );
  if (!model.composition_version && damagedFiles())
    throw new Error(
      "Saved files are missing or changed. Use local recovery before exporting this version.",
    );
}
let exportRecovery = null;
let exportReceipt = null;
let pendingExport = "zip";
let reportPreviewURL = null;
async function exportFailure(error, target, snapshot, kind, allowPartial, operationId = null) {
  if (await wordingExportRefused(error)) return;
  if (!model?.manifest || deliveryPath() !== target) return;
  let progress = null;
  if (operationId) {
    try { progress = await api(`${target}/progress/${operationId}`); } catch {}
  }
  exportRecovery = { target, snapshot, kind, allowPartial, operationId, progress, code: error.code, message: error.message };
  openModal("export-failure");
}
function exportRequiresRefresh(recovery) {
  // Another tab's save is not reflected in this tab's unchanged model version.
  return ["composition_conflict", "stale_snapshot", "stale_report"].includes(recovery?.code);
}
function exportFailureModal() {
  const recovery = exportRecovery, progress = recovery?.progress;
  if (recovery?.code === "font_unavailable") {
    return dialog(
      "A required font is unavailable.",
      notice(recovery.message, "", "block"),
      btn("Close", "cancelExport", "primary"),
    );
  }
  if (exportRequiresRefresh(recovery)) {
    const next = recovery.kind === "report"
      ? "After refreshing, open the report preview again."
      : "After refreshing, choose Download ZIP again.";
    return dialog(
      "The campaign has changed.",
      `${notice(window.MotifSave.conflictMessage, "", "block")}<p>${next}</p>`,
      btn("Refresh", "retryExport", "primary"),
    );
  }
  const changed = recovery && deliveryVersion() !== recovery.snapshot;
  const failures = progress?.failed_outputs?.length ? progress.failed_outputs : progress?.failed_output ? [progress.failed_output] : [];
  const labels = failures.map(failed => {
    const language = Object.keys(MARKET).find(key => MARKET[key] === failed.market);
    const format = FMT.find(item => item.ratio === failed.ratio || item.ratio.replace(":", "x") === failed.ratio);
    return [failed.product_name, format?.name || failed.ratio, LANG[language] || failed.market].filter(Boolean).join(" · ");
  });
  const detail = labels.length ? labels.length === 1 ? `We couldn’t create ${labels[0]}.` : "We couldn’t create these images:" : recovery?.message || "Try again.";
  const failedList = labels.length > 1 ? `<ul>${labels.map(label => `<li>${esc(label)}</li>`).join("")}</ul>` : "";
  const created = Number.isInteger(progress?.completed) ? `<p>${progress.completed} of ${progress.total} images have been created. Retrying keeps those images and creates the failed ones.</p>` : "";
  return dialog(
    recovery?.kind === "report" ? "The report could not be opened." : "The download is not ready yet.",
    `${notice(detail, "", "block")}${failedList}${created}${changed ? '<p>Your campaign has changed. Start a new export to include the latest saved edits.</p>' : ''}`,
    btn("Cancel export", "cancelExport") + btn(changed ? "Start new export" : recovery?.kind === "zip" ? progress?.status === "failed" ? "Retry failed images" : "Retry ZIP download" : "Retry report preview", "retryExport", "primary"),
  );
}
async function cancelExport() {
  const previous = exportRecovery;
  if (previous?.operationId) {
    await api(`${previous.target}/cancel`, { operation_id: previous.operationId });
  }
  exportRecovery = null;
  closeModal();
}
async function retryExport() {
  const previous = exportRecovery;
  if (!previous || !model?.manifest || deliveryPath() !== previous.target) { closeModal(); return; }
  if (exportRequiresRefresh(previous)) { window.MotifSave.refreshPage(); return; }
  if (!await flushCurrentWork()) return;
  const changed = deliveryVersion() !== previous.snapshot;
  if (changed && previous.operationId) await api(`${previous.target}/cancel`, { operation_id: previous.operationId });
  if (previous.kind === "zip") await downloadZIP(previous.allowPartial, changed ? null : previous.operationId);
  else await previewReport(previous.allowPartial);
}
function watchDeliveryProgress(campaign, operationId, button) {
  let stopped = false, timer;
  const stages = new Set(["Creating finished images", "Retrying image", "Preparing ZIP", "Checking download", "Download ready"]);
  let detail = null;
  if (button?.parentElement && typeof document.createElement === "function") {
    detail = document.createElement("p"); detail.className = "small muted";
    detail.setAttribute("role", "status"); detail.setAttribute("aria-live", "polite");
    button.parentElement.append(detail);
  }
  async function poll() {
    try {
      const progress = await api(`/api/campaigns/${campaign}/delivery/progress/${operationId}`);
      if (!stopped && detail) detail.textContent = progress.message || "";
      if (!stopped && button && stages.has(progress.stage)) {
        button.textContent = progress.stage === "Creating finished images" && Number.isInteger(progress.completed)
          ? `Creating images: ${progress.completed} of ${progress.total}.`
          : progress.stage + (progress.stage === "Download ready" ? "" : "…");
      }
    } catch { /* The operation may not yet be registered, or its response is closing. */ }
    if (!stopped) timer = setTimeout(poll, 350);
  }
  timer = setTimeout(poll, 200);
  return () => { stopped = true; clearTimeout(timer); detail?.remove(); };
}
async function downloadZIP(allowPartial, retryOperationId = null) {
  if (!await flushCurrentWork()) throw new Error("Your changes are still here. Save them before exporting.");
  requireIntactExport();
  const target = deliveryPath(), snapshot = deliveryVersion();
  const button = $("#overlay [data-action='confirmPartial'], #overlay [data-action='retryExport']") ||
    $("[data-action='exportZip']");
  const label = button?.textContent;
  if (button) {
    const completed = retryOperationId && exportRecovery?.operationId === retryOperationId ? exportRecovery.progress?.completed || 0 : 0;
    button.textContent = `Creating images: ${completed} of ${model.manifest.metrics.completed_images}.`;
    button.setAttribute("aria-busy", "true");
  }
  const operationId = retryOperationId || crypto.randomUUID().replaceAll("-", "");
  const stopProgress = watchDeliveryProgress(C.id, operationId, button);
  try {
    const response = await apiResponse(`${target}/zip`, {
      operation_id: operationId,
      version: snapshot,
      allow_partial: allowPartial,
    });
    if (response.headers.get("Content-Type")?.split(";")[0] !== "application/zip")
      throw new Error("Motif did not receive the ZIP. Try the download again.");
    const blob = await response.blob();
    if (!model?.manifest || deliveryPath() !== target) return;
    const disposition = response.headers.get("Content-Disposition") || "";
    const encoded = disposition.match(/(?:^|;)\s*filename\*=UTF-8''([^;]+)/i);
    const filename = encoded ? decodeURIComponent(encoded[1]) : "Motif.zip";
    const anchor = document.createElement("a");
    const url = URL.createObjectURL(blob);
    try {
      anchor.href = url;
      anchor.download = filename;
      document.body.append(anchor);
      anchor.click();
    } finally {
      anchor.remove();
      // Give the browser's download manager time to take ownership of a large ZIP.
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    exportReceipt = { filename };
    openModal("export-success");
  } catch (error) {
    await exportFailure(error, target, snapshot, "zip", allowPartial, operationId);
  } finally {
    stopProgress();
    if (button) {
      button.textContent = label;
      button.removeAttribute("aria-busy");
    }
  }
}
async function previewReport(allowPartial = false) {
  requireIntactExport();
  if (!model.manifest.complete && !allowPartial) {
    pendingExport = "report";
    openModal("partial-export");
    return;
  }
  const target = deliveryPath();
  let snapshot = deliveryVersion();
  // Open in response to the click, before the asynchronous validation, so
  // browsers that require an active user gesture can still show the report.
  const tab = window.open("about:blank", "_blank");
  try {
    if (tab) {
      tab.opener = null;
      tab.document.title = "Motif – Report preview";
      tab.document.body.textContent = "Preparing report…";
    }
    if (!await flushCurrentWork()) throw new Error("Your changes are still here. Save them before opening the report.");
    snapshot = deliveryVersion();
    const result = await api(`${target}/report-preview`, {
      version: snapshot,
      allow_partial: allowPartial,
    });
    if (!model?.manifest || deliveryPath() !== target) {
      tab?.close();
      return;
    }
    if (tab) {
      if (!tab.closed) tab.location.replace(result.url);
      if (modal) closeModal();
    } else {
      reportPreviewURL = result.url;
      openModal("open-report");
    }
  } catch (error) {
    tab?.close();
    await exportFailure(error, target, snapshot, "report", allowPartial);
  }
}
document.addEventListener("change", event => {
  if (event.target.id === "overview-filter") {
    filter = event.target.value;
    pageNum = 0;
    render();
  }
});
