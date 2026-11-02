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
  requireIntactExport();
  const output = outputFor(product, language, format), owner = model;
  if (!output?.url) throw new Error("This image is unavailable. Recover it before downloading.");
  const response = await fetch(`${output.url}?download=true`);
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
function requireIntactExport() {
  if (wordingExportBlocked()) throw new Error("Correct or apply the prohibited wording changes before exporting.");
  if (!model?.manifest?.metrics.completed_images)
    throw new Error(
      "Nothing to export yet. Open Production to recover or create images.",
    );
  if (damagedFiles())
    throw new Error(
      "Saved files are missing or changed. Use local recovery before exporting this version.",
    );
}
let exportRecovery = null;
let exportReceipt = null;
let pendingExport = "zip";
async function exportFailure(error, target, kind, allowPartial) {
  if (await wordingExportRefused(error)) return;
  if (showCapacity(error, "Creating this package")) return;
  if (!model?.manifest || metadataPath() !== target) return;
  exportRecovery = { target, kind, allowPartial, message: error.message };
  openModal("export-failure");
}
function exportFailureModal() {
  return dialog(
    "Your images are safe. The package could not be saved.",
    `${notice(exportRecovery?.message || "The local package operation could not finish.", "Your saved images and revision are unchanged.", "block")}<p class="muted">You can still retrieve individual PNGs from the handoff overview. A missing response does not change your saved campaign.</p>`,
    btn("Individual PNGs", "exportOverview") +
      btn(
        exportRecovery?.kind === "zip"
          ? "Retry ZIP download"
          : "Retry local package",
        "retryExport",
        "primary",
      ),
  );
}
async function retryExport() {
  const previous = exportRecovery;
  if (!previous || metadataPath() !== previous.target) {
    closeModal();
    return;
  }
  if (previous.kind === "zip") await downloadZIP(previous.allowPartial);
  else await createFolder(previous.kind === "report", previous.allowPartial);
}
async function downloadZIP(allowPartial) {
  requireIntactExport();
  const target = metadataPath();
  let result;
  try {
    result = await api(`${target}/zip`, {
      snapshot_id: model.metadata.snapshot_id,
      allow_partial: allowPartial,
    });
  } catch (error) {
    await exportFailure(error, target, "zip", allowPartial);
    return;
  }
  if (!model?.manifest || metadataPath() !== target) return;
  const anchor = document.createElement("a");
  anchor.href = result.url;
  anchor.download = "";
  anchor.click();
  exportReceipt = { kind: "zip", filename: result.filename };
  openModal("export-success");
}
async function createFolder(openReport, allowPartial = false) {
  requireIntactExport();
  if (!model.manifest.complete && !allowPartial) {
    pendingExport = openReport ? "report" : "folder";
    openModal("partial-export");
    return;
  }
  const target = metadataPath();
  let result;
  try {
    result = await api(`/api/campaigns/${C.id}/export`, {
      run_id: model.output_run_id,
      revision_id: model.manifest.revision_id,
      snapshot_id: model.metadata.snapshot_id,
      allow_partial: allowPartial,
    });
  } catch (error) {
    await exportFailure(
      error,
      target,
      openReport ? "report" : "folder",
      allowPartial,
    );
    return;
  }
  if (!model?.manifest || metadataPath() !== target) return;
  exportReceipt = { kind: "folder" };
  // Opening a returned same-origin report is separate from producing the package.
  openModal("export-success");
  $("#overlay .modal-body").insertAdjacentHTML(
    "beforeend",
    `<div class="field"><label for="export-location">Saved folder</label><input id="export-location" type="text" readonly value="${esc(result.location)}"><span class="hint">Select and copy this path to open the folder on this computer.</span></div><p><a href="${esc(result.url)}" target="_blank" rel="noopener">Open offline report ↗</a></p>`,
  );
  if (openReport) window.open(result.url, "_blank", "noopener");
}
document.addEventListener("change", event => {
  if (event.target.id === "overview-filter") {
    filter = event.target.value;
    pageNum = 0;
    render();
  }
});
