/* Motif storage addendum. Pure presentation: no network, persistence, or deletion. */
"use strict";
window.MotifStorage = (() => {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const button = (label, action, style = "", extra = "") => `<button type="button" class="btn ${style}" data-sc="${action}" ${extra}>${label}</button>`;
  const row = (label, value) => `<div class="data-row"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
  const note = (title, text, kind = "") => `<div class="notice ${kind}"><span class="icon" aria-hidden="true">${kind === "block" ? "⊗" : kind === "info" ? "↗" : "△"}</span><div><strong>${esc(title)}</strong><p>${esc(text)}</p></div></div>`;
  const mb = bytes => new Intl.NumberFormat("en", {maximumFractionDigits:1}).format(bytes / 1e6) + " MB";
  const gb = bytes => new Intl.NumberFormat("en", {maximumFractionDigits:2}).format(bytes / 1e9) + " GB";
  const count = (n, singular, plural = singular + "s") => `${n} ${n === 1 ? singular : plural}`;
  const modal = (title, body, footer, initialFocus = "cancel", extra = {}) => ({title, body, footer, initialFocus, ...extra});
  const preserved = d => `<div class="rule"></div><p class="small muted">Your typed fields and previous attachment are unchanged.</p>${d.previousAttachment ? row("Previous attachment", d.previousAttachment) : ""}`;
  const inputLimits = "PNG or JPEG · up to 20 MiB (about 21 MB) · up to 40 million pixels";

  function getView(state, d) {
    const logoInput = d.inputRole === "logo";
    const imageFormat = "PNG or JPEG";
    const imageLimits = inputLimits;
    switch (state) {
      case "campaign-context": return modal("Back to the workbench.",
        `<span class="eyebrow">CURRENT CAMPAIGN</span><h3 style="margin-top:8px">${esc(d.campaignName)}</h3><p class="small muted">${esc(d.campaignSummary)}</p><div class="row" style="margin-top:14px">${button("Rename", "rename", "small")}${button("Delete campaign", "preview-delete", "small danger")}</div>${d.otherCampaigns.length ? `<div class="rule"></div><div class="menu-popover">${d.otherCampaigns.map(c => `<button data-sc="open-other"><strong>${esc(c.name)}</strong><br><span class="small muted">${esc(c.summary)}</span></button>`).join("")}</div>` : '<p class="small muted" style="margin-top:18px">No other saved campaigns yet.</p>'}`,
        button("All campaigns", "home") + button("New campaign", "new", "primary"), "dialog-close");
      case "delete-loading": return modal("Checking this campaign’s files…",
        `<p id="storage-description">Checking what belongs to <strong>${esc(d.campaignName)}</strong> and how much disk space can be freed.</p><p class="small muted" style="margin-top:14px">Nothing has been deleted.</p>`, button("Cancel", "cancel") + button("Delete campaign", "confirm-delete", "primary", "disabled"), "cancel", {loading:true});
      case "delete-confirm":
      case "delete-shared":
      case "delete-unsaved": return modal("Delete this campaign?",
        `<p id="storage-description"><strong>${esc(d.campaignName)}</strong> will be removed from this workspace. This cannot be undone.</p>
        ${d.hasUnsavedEdits ? '<p class="small" style="margin-top:10px"><strong>Unsaved changes in this campaign will also be discarded.</strong></p>' : ""}<div class="data-row" style="align-items:center;margin-top:12px"><span>Estimated space freed</span><b class="mini-number">${esc(mb(d.estimatedReclaimBytes))}</b></div>
        ${row("Campaign brief and settings", "This campaign")}${row("Uploaded and generated source copies", count(d.sourceFilesToRemove, "file") + " to remove")}${row("Saved images", count(d.outputPNGs, "PNG"))}${row("App-created packages", count(d.packages, "package"))}
        ${d.unmanagedFiles > 0 ? note(`${count(d.unmanagedFiles, "unmanaged file")} will stay.`, "Motif will leave these files and their containing folders on disk.", "info") : '<div style="height:18px"></div>'}
        <p class="small muted">These app-owned files and records will be deleted from disk. Original source files and downloaded copies outside the application are untouched.</p>`,
        button("Cancel", "cancel") + button("Delete campaign", "confirm-delete", "primary"));
      case "delete-pending": return modal("Deleting this campaign…",
        `<p id="storage-description">Removing <strong>${esc(d.campaignName)}</strong> and its app-owned files.</p><p class="small muted" style="margin-top:14px">Deletion has started. Keep this window open while it finishes.</p>`,
        `<span class="small muted" role="status">Deleting campaign…</span>${button("Deleting…", "none", "primary", "disabled")}`, "dialog-title", {pending:true});
      case "delete-failed": return modal("The campaign is still here.",
        note("Deletion could not start.", d.failureMessage, "block") + `<p id="storage-description">No files were removed. Close anything using this campaign’s files, then review the deletion details again.</p>`,
        button("Cancel", "cancel") + button("Review deletion", "preview-delete", "primary"));
      case "delete-changed": return modal("This campaign has changed.",
        note("Review the updated deletion details.", "The saved work or its file references changed after you opened the confirmation.", "block") + '<p id="storage-description">Nothing was deleted. Check the new file counts and space estimate before deciding.</p>',
        button("Cancel", "cancel") + button("Review updated details", "preview-delete", "primary"));
      case "delete-active": return modal("Let this operation finish first.",
        note("This campaign is still in use.", d.activeOperation, "block") + '<p id="storage-description">Deletion is unavailable while work is running. Return to the campaign to follow its progress. Nothing was deleted.</p>',
        button("Cancel", "cancel") + button("Back to campaign", "back-to-campaign", "primary"));
      case "delete-uncertain": return modal("Deletion needs a check.",
        note("We couldn’t confirm that deletion finished.", "Some files may already have been removed. Check the result before trying to delete again.", "block") + `<p id="storage-description">${esc(d.campaignName)} has not been reported as fully deleted. Your original files and downloaded copies outside the application are untouched.</p>`,
        button("Back to Home", "home") + button("Check deletion status", "check-deletion", "primary"), "check-deletion");
      case "delete-cleanup-pending": return modal("Deletion needs to finish.",
        note("Some campaign files could not be removed.", "Deletion has started. This campaign is unavailable until cleanup finishes.", "block") + `<p id="storage-description">Close any app using <strong>${esc(d.campaignName)}</strong>’s stored files and check that the workspace is writable, then retry deletion.</p><p class="small muted" style="margin-top:12px">Retry continues the deletion already confirmed. Other campaigns, external originals, and downloaded copies are unaffected.</p>`,
        button("Back to Home", "home") + button("Retry deletion", "retry-deletion", "primary"), "home");
      case "storage-budget": return modal("Make room in your workspace.",
        note(`${d.operationLabel} couldn’t start.`, "This would exceed the workspace’s storage budget.", "block") +
        row("Workspace storage", `${gb(d.usedBytes)} of ${gb(d.budgetBytes)}`) + row("Estimated space for this operation", mb(d.projectedBytes)) +
        `<p id="storage-description" style="margin-top:18px">Delete campaigns you no longer need to remove their stored images and app-created packages, then try again.</p><p class="small muted" style="margin-top:12px">Your draft is kept. Saved work and existing downloads remain available. Nothing was deleted.</p>`,
        button("View campaigns", "view-campaigns") + button("Back to my work", "cancel", "primary"));
      case "storage-disk": return modal("Your disk needs more room.",
        note(`${d.operationLabel} couldn’t start.`, "There isn’t enough free disk space for this operation and the free-space reserve.", "block") +
        row("Free on this disk", mb(d.freeBytes)) + row("Estimated operation / free-space reserve", `${mb(d.projectedBytes)} / ${gb(d.reserveBytes)}`) +
        `<p id="storage-description" style="margin-top:18px">Free at least <strong>${esc(mb(d.requiredAdditionalFreeBytes))}</strong> on this disk, then return and try again.</p><p class="small muted" style="margin-top:12px">Your draft is kept. Saved work and existing downloads remain available. Available space can change while other apps are running.</p>`,
        button("Local workspace", "workspace") + button("Back to my work", "cancel", "primary"));
      case "workspace": return modal("Your local workspace.",
        `<p class="muted">Saved work stays on this computer. Image generation is optional.</p><div class="rule"></div><div class="field"><label for="storage-location">Storage location</label><input id="storage-location" readonly value="${esc(d.location)}"><span class="hint">Select and copy this path to find your workspace.</span></div>${row("Workspace storage", `${gb(d.usedBytes)} of ${gb(d.budgetBytes)}`)}${row("Free on this disk", gb(d.freeBytes))}${row("Free-disk reserve", gb(d.reserveBytes))}<p class="small muted" style="margin-top:16px">Storage is checked before substantial new work. Existing images and intact packages remain available if a limit is reached.</p><div class="rule"></div>${row("Local composition", "Available")}${row("Image generation", "Credential configured; provider access not tested")}`,
        button("Check readiness again", "workspace-refresh") + button("Done", "cancel", "primary"));
      case "image-unsupported": return modal("Choose a PNG or JPEG.",
        note("This image format isn’t supported.", `${d.filename} is ${d.detectedType}. Export a ${imageFormat} copy, then choose that file.`, "block") + `<p id="storage-description" class="small muted">${imageLimits}</p>` + preserved(d),
        button("Keep my work", "return-product") + button("Choose another image", "choose-image", "primary"), "return-product");
      case "image-bytes": return modal("A smaller file will fit.",
        note("This image file is too large.", `${d.filename} is ${mb(d.encodedBytes)}. The limit is 20 MiB (about 21 MB).`, "block") + `<p id="storage-description">Export a smaller ${imageFormat}, then choose it again.</p><p class="small muted" style="margin-top:12px">The image must also contain no more than 40 million pixels.</p>` + preserved(d),
        button("Keep my work", "return-product") + button("Choose another image", "choose-image", "primary"), "return-product");
      case "image-pixels": return modal("This image has too many pixels.",
        note("Resize this image before attaching it.", `${d.filename} is ${d.width.toLocaleString()} × ${d.height.toLocaleString()} pixels (${d.width * d.height / 1e6} million). The limit is 40 million pixels.`, "block") + `<p id="storage-description">Reduce its width or height and export a new ${imageFormat}.</p><p class="small muted" style="margin-top:12px">The exported file must also be no larger than 20 MiB (about 21 MB).</p>` + preserved(d),
        button("Keep my work", "return-product") + button("Choose another image", "choose-image", "primary"), "return-product");
      case "image-color-profile": return modal("Use an sRGB copy.",
        note("This image needs a supported color profile.", `${d.filename} has a missing or unsupported color profile.`, "block") + `<p id="storage-description">Open the original in its source editor, convert it to sRGB, and export a new ${imageFormat} with the sRGB profile embedded. Then choose that copy.</p>${logoInput ? '<p class="small muted" style="margin-top:12px">Transparency is optional. Preserve the intended artwork background.</p>' : ""}` + preserved(d),
        button("Keep my work", "return-product") + button("Choose another image", "choose-image", "primary"), "return-product");
      case "image-empty": return modal("Choose visible logo artwork.",
        note("This logo has no visible artwork.", "Choose a visible PNG or JPEG. Transparency is optional.", "block") + preserved(d),
        button("Keep my work", "return-product") + button("Choose another image", "choose-image", "primary"), "return-product");
      case "image-corrupt": return modal("We couldn’t open this image.",
        note("This file isn’t a readable PNG or JPEG.", `${d.filename} may be damaged, incomplete, or saved with the wrong file extension.`, "block") + `<p id="storage-description">Open the original in an image editor and export a fresh ${imageFormat}, then choose that copy.</p>` + preserved(d),
        button("Keep my work", "return-product") + button("Choose another image", "choose-image", "primary"), "return-product");
      default: return null;
    }
  }
  return {esc, button, row, note, mb, gb, getView, inputLimits};
})();
