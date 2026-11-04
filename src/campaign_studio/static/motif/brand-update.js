"use strict";

// Brand edits are saved drafts until the producer updates the existing images.
// Navigation resolves only these edits; other campaign work remains untouched.
let brandUpdating = false;
let brandDestination = null;
let brandUpdateError = "";

function brandColorTargets() {
  return projectBuffers.form_drafts.brand_color_defaults || [];
}
function brandChangesPending() {
  if (!model?.manifest || !model.output_brief || !draft) return false;
  const color = value => MotifBriefFlow.normalizeHex(value) || value;
  const fields = brand => [brand.name, brand.logo_path || "", brand.logo_provenance,
    (brand.palette || []).map(color), color(brand.text_color), brand.require_logo === true];
  return JSON.stringify(fields(currentBriefValue().brand)) !== JSON.stringify(fields(model.output_brief.brand)) ||
    brandColorTargets().length > 0;
}
function guardBrandNavigation(destination) {
  if (page !== "campaign" || tab !== "brand") return false;
  if (brandUpdating) { toast("Wait for the images to finish updating."); return true; }
  if (!brandChangesPending()) return false;
  brandDestination = destination;
  brandUpdateError = "";
  openModal("brand-navigation");
  return true;
}
function brandNavigationDialog() {
  const colorProblem = briefBrandColorProblem(), message = brandUpdateError || colorProblem;
  return dialog("Update images before leaving Brand?",
    `<p>Your Brand changes have not been applied to the saved images.</p><p>Update them now, discard the Brand changes, or stay here to keep editing.</p>${message ? notice(message, "Your changes are still here.", "block") : ""}`,
    btn("Stay on Brand", "close") + btn("Discard and continue", "brandDiscard") +
      btn(brandUpdating ? "Updating…" : "Update and continue", "brandContinue", "primary", brandUpdating || colorProblem ? "disabled" : ""));
}
async function updateBrandImages() {
  if (brandUpdating || !brandChangesPending()) return !brandUpdating;
  const colorProblem = briefBrandColorProblem();
  if (colorProblem) {
    brandUpdateError = colorProblem;
    if (modal !== "brand-navigation") toast(colorProblem);
    return false;
  }
  if (typeof briefLogoUploadPending === "function" && briefLogoUploadPending()) {
    brandUpdateError = "Wait for the logo upload to finish, or cancel it before updating images.";
    toast(brandUpdateError);
    return false;
  }
  brandUpdating = true;
  brandUpdateError = "";
  const campaign = draft.campaign_id;
  try {
    await withBriefPreviewPaused(() => {
      refreshBriefFlow();
      return withProjectApply(async () => {
      const result = await api(`/api/motif/campaigns/${encodeURIComponent(campaign)}/brand/apply`, {
        version: model.version, run_id: model.output_run_id,
        revision_id: model.manifest.revision_id, default_color_targets: copy(brandColorTargets()),
      });
      if (draft?.campaign_id !== campaign) return;
      delete projectBuffers.form_drafts.brand_color_defaults;
      useData(result, { applied: true });
      });
    });
    toast("Campaign images updated.");
    return true;
  } catch (error) {
    brandUpdateError = error.message;
    if (modal !== "brand-navigation") toast(error.message);
    return false;
  } finally {
    brandUpdating = false;
    if (draft?.campaign_id === campaign) refreshBriefFlow();
  }
}
async function discardBrandChanges() {
  const accepted = model.output_brief;
  draft.brand = copy(accepted.brand);
  for (const target of brandColorTargets()) {
    const current = draft.products.find(p => p.id === target.product_id)?.framing?.[target.market]?.[target.ratio]?.text_placements;
    const previous = accepted.products.find(p => p.id === target.product_id)?.framing?.[target.market]?.[target.ratio]?.text_placements;
    for (const [block, placement] of Object.entries(current || {}))
      placement.color = previous?.[block]?.color ?? null;
  }
  for (const [name, fields] of Object.entries(projectBuffers.form_drafts.fields || {}))
    if (name.startsWith("brief/"))
      for (const field of ["brand-name", "text-color", "brand-palette"]) delete fields[field];
  delete projectBuffers.form_drafts.brief_defaults;
  delete projectBuffers.form_drafts.brand_color_defaults;
  markDirty();
  await saveDraft();
  refreshBriefFlow();
}
async function finishBrandNavigation(discard = false) {
  const destination = brandDestination;
  try {
    if (discard) await discardBrandChanges();
    else if (!await updateBrandImages()) { openModal("brand-navigation"); return; }
    // Applying a replacement logo updates its saved file reference. Acknowledge
    // that draft through the existing Save queue before checking navigation.
    if (!await saveProject()) {
      const state = getSaveState();
      throw new Error(state?.validationMessage || state?.problem?.message ||
        "Finish saving the current work before leaving Brand.");
    }
    brandDestination = null;
    closeModal();
    if (!destination) return;
    if (destination.kind === "page") await navigate(destination.target);
    else if (destination.kind === "tab") { tab = destination.target; refreshBriefFlow(); }
    else if (destination.kind === "studio") await briefAction(briefHost, "open-studio");
    else if (destination.action === "openCampaign") await openCampaign(destination.campaignId);
    else openModal(destination.action);
  } catch (error) {
    brandUpdateError = error.message;
    openModal("brand-navigation");
  }
}
