"use strict";
/* Local framing uses one crop calculation for the controls, preview, and saved PNG. */
let framingDraft = null,
  framingSource = null,
  framingSequence = 0,
  framingReady = Promise.resolve(false),
  framingMarking = false,
  framingDrawing = false,
  framingObserver = null,
  framingPan = null,
  framingShowSafety = false,
  framingInheritedSubject = null;
let framingOverlay = null,
  framingRaf = 0,
  framingTimer = 0,
  framingDelayResolve = null;
let framingRecovery = null, framingSession = 0, framingCustomText = false, framingAttempt = null;
const frameClamp = (v, min, max) => Math.min(max, Math.max(min, v));
function frameCrop(width, height, format, settings = {}) {
  const zoom = frameClamp(Number(settings.zoom) || 1, 1, 2),
    scale = Math.max(format.w / width, format.h / height) * zoom;
  const sw = format.w / scale,
    sh = format.h / scale,
    rangeX = Math.max(0, width - sw),
    rangeY = Math.max(0, height - sh);
  const focal =
      rangeX > 0.5 ? frameClamp(Number(settings.focal ?? 50), 0, 100) : 50,
    vertical =
      rangeY > 0.5 ? frameClamp(Number(settings.vertical ?? 50), 0, 100) : 50;
  return {
    sx: (rangeX * focal) / 100,
    sy: (rangeY * vertical) / 100,
    sw,
    sh,
    rangeX,
    rangeY,
    focal,
    vertical,
    zoom,
  };
}
function frameSubjectFits(crop, subject, width, height) {
  return (
    !subject ||
    (subject.x * width >= crop.sx - 0.5 &&
      subject.y * height >= crop.sy - 0.5 &&
      (subject.x + subject.w) * width <= crop.sx + crop.sw + 0.5 &&
      (subject.y + subject.h) * height <= crop.sy + crop.sh + 0.5)
  );
}
function beginFraming() {
  endFraming();
  const product = model.output_brief.products[currentProduct];
  const saved =
    product.framing?.[MARKET[lang]]?.[FMT[currentFormat].ratio] || {};
  framingCustomText = !!Object.keys(saved.text_placements || {}).length;
  const box =
    saved.subject_box ||
    (saved.inherit_subject !== false ? product.source.subject_box : null);
  framingContext = {
    campaign: C.id,
    run_id: model.output_run_id,
    revision_id: model.manifest.revision_id,
    product_id: product.id,
    market: MARKET[lang],
    ratio: FMT[currentFormat].ratio,
  };
  framingDraft = {
    zoom: saved.zoom ?? 1,
    focal: (saved.focal_x ?? 0.5) * 100,
    vertical: (saved.focal_y ?? 0.5) * 100,
    place: saved.copy_placement || "top",
    subject: box
      ? {
          x: box.left,
          y: box.top,
          w: box.right - box.left,
          h: box.bottom - box.top,
        }
      : null,
  };
  framingInheritedSubject =
    !saved.subject_box &&
    saved.inherit_subject !== false &&
    product.source.subject_box
      ? JSON.stringify(framingDraft.subject)
      : null;
  const size = model.source_sizes?.[product.id];
  if (size) {
    const crop = frameCrop(size[0], size[1], FMT[currentFormat], framingDraft);
    framingDraft.focal = crop.focal;
    framingDraft.vertical = crop.vertical;
  }
  framingSourceURL = C.products[currentProduct].image;
  framingInitial = JSON.stringify(framingDraft);
  framingMarking = false;
  framingShowSafety = false;

}
function framingControl(label, id, value, min, max, output, hint = "") {
  return `<div class="framing-control"><div class="framing-label"><label for="${id}">${label}</label><output id="${id}-value" for="${id}">${output}</output></div><input id="${id}" type="range" min="${min}" max="${max}" step="0.1" value="${value}" aria-describedby="${id}-hint"><p id="${id}-hint" class="framing-hint">${hint}</p></div>`;
}
function framingParts() {
  beginFraming();
  return editorParts(
    `<div class="framing-editor mf-editor-grid"><div class="framing-tools mf-editor-tools"><div class="framing-product"><h3>${esc(C.products[currentProduct].name)}</h3><p class="small muted">${LANG[lang]} · ${FMT[currentFormat].name} ${FMT[currentFormat].ratio}</p></div>${framingControl("Zoom", "frame-zoom", Math.round(framingDraft.zoom * 1000) / 10, 100, 200, Math.round(framingDraft.zoom * 1000) / 10 + "%", "100% fills this format. Zoom in to reframe.")}${framingControl("Horizontal position", "frame-focal", framingDraft.focal, 0, 100, "Centered")}${framingControl("Vertical position", "frame-vertical", framingDraft.vertical, 0, 100, "Centered")}<div class="framing-placement"><label for="frame-layout">Copy placement</label><select id="frame-layout" ${framingCustomText ? "disabled hidden" : ""}><option value="top" ${framingDraft.place === "top" ? "selected" : ""}>Top left</option><option value="bottom" ${framingDraft.place === "bottom" ? "selected" : ""}>Bottom left</option></select><p id="frame-custom-text" class="small muted" ${framingCustomText ? "" : "hidden"}>Custom text layout · Adjust text</p></div><section class="framing-protection"><div class="framing-label"><strong>Product area</strong><span id="frame-subject-state">No area marked</span></div><div class="framing-safety-row"><label class="framing-safety-toggle"><input type="checkbox" id="frame-show-safety" aria-describedby="frame-subject-help" ${framingShowSafety ? "checked" : ""}>Show safety area</label>${btn("Clear", "clearSubject", "small quiet", "disabled")}</div><p id="frame-subject-help"></p>${btn("Mark area", "markSubject", "small")}</section></div><section class="framing-preview mf-editor-preview"><div class="framing-preview-label"><span class="eyebrow" id="frame-preview-label">LIVE FRAMING PREVIEW</span></div><div class="framing-visual"><img id="frame-image" class="creative-image" hidden alt="Draft framing preview for ${esc(C.products[currentProduct].name)}"><canvas id="frame-live" hidden aria-hidden="true"></canvas><div id="frame-pan" hidden tabindex="0" role="group" aria-label="Move photo. Drag or use arrow keys to pan."></div><div id="frame-safety-unset" class="framing-empty-safety" role="status" hidden><strong>No safety area set</strong><span>Choose Mark area to create one.</span></div><div id="frame-safety-overlay" hidden aria-hidden="true"><div id="frame-safety-outline"></div><span id="frame-safety-label">SAFETY AREA</span></div><div id="frame-source" hidden><img alt="Original source; mark the product area here"><div id="frame-crop-window"><span>VISIBLE CROP</span></div><div id="frame-subject-box" tabindex="0" role="group" aria-label="Marked product area. Arrow keys move it. Shift and arrow keys resize it."><span>PRODUCT AREA</span></div></div></div><div id="frame-status" class="framing-status" role="status">Loading saved source…</div></section></div>`,
    `<div class="row ws-framing-actions">` +
    btn("Reset", "restoreFrame", "quiet", 'aria-label="Reset framing to fit"') +
    btn("Cancel", "close", "", 'aria-label="Cancel framing changes"') +
    btn("Apply", "saveFrame", "primary", 'id="frame-primary" aria-label="Apply framing" disabled') + "</div>",
  );
}
function framePositionLabel(value, axis) {
  return value === 50
    ? "Centered"
    : Math.round(Math.abs((value - 50) * 2) * 10) / 10 +
        "% " +
        (axis === "horizontal"
          ? value < 50
            ? "left"
            : "right"
          : value < 50
            ? "up"
            : "down");
}
function frameSettingsFromControls(id) {
  framingDraft.zoom = Number($("#frame-zoom").value) / 100;
  framingDraft.focal = Number($("#frame-focal").value);
  framingDraft.vertical = Number($("#frame-vertical").value);
  framingDraft.place = $("#frame-layout").value;
  if (id === "frame-zoom" && framingDraft.zoom === 1) {
    framingDraft.focal = 50;
    framingDraft.vertical = 50;
  }
  requestFramePreview(id !== "frame-layout");
}
function cancelFrameWork(cancelPaint = true) {
  if (cancelPaint) {
    cancelAnimationFrame(framingRaf);
    framingRaf = 0;
  }
  clearTimeout(framingTimer);
  framingTimer = 0;
  if (framingDelayResolve) {
    framingDelayResolve(false);
    framingDelayResolve = null;
  }
}
function queueFramePaint() {
  if (framingRaf || !framingSource || framingMarking) return;
  framingRaf = requestAnimationFrame(() => {
    framingRaf = 0;
    if (modal === "frame" && framingDraft && !framingMarking)
      paintLiveFraming();
  });
}
function requestFramePreview(live = false) {
  cancelFrameWork(!live);
  if (framingRecovery?.startsWith("latest") || ["missing", "unsupported", "uncertain", "checking"].includes(framingRecovery)) {
    showFrameRecovery();
    return Promise.resolve(false);
  }
  if (live) queueFramePaint();
  framingReady = refreshFramePreview(live);
  return framingReady;
}
function waitForFrameIdle() {
  return new Promise((resolve) => {
    framingDelayResolve = resolve;
    framingTimer = setTimeout(() => {
      framingTimer = 0;
      framingDelayResolve = null;
      resolve(true);
    }, 160);
  });
}
function paintLiveFraming() {
  const canvas = $("#frame-live"),
    image = $("#frame-image");
  if (!canvas || !framingSource || framingMarking) return;
  if (!framingOverlay || framingOverlayPlace !== framingDraft.place) {
    canvas.hidden = true;
    image.style.visibility = "";
    return;
  }
  const fm = FMT[currentFormat],
    scale =
      Math.min(image.clientWidth / fm.w, image.clientHeight / fm.h) *
      Math.min(devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(fm.w * scale)),
    height = Math.max(1, Math.round(fm.h * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(width / fm.w, 0, 0, height / fm.h, 0, 0);
  ctx.clearRect(0, 0, fm.w, fm.h);
  const crop = frameCrop(
    framingSource.width,
    framingSource.height,
    fm,
    framingDraft,
  );
  ctx.drawImage(
    framingSource,
    crop.sx,
    crop.sy,
    crop.sw,
    crop.sh,
    0,
    0,
    fm.w,
    fm.h,
  );
  ctx.drawImage(framingOverlay, 0, 0, fm.w, fm.h);
  canvas.hidden = false;
  // Retain the image's measurement box, but let only the live canvas paint.
  image.style.visibility = "hidden";
  drawFrameSafety();
}
async function refreshFramePreview(live = false) {
  const token = ++framingSequence,
    save = $("#frame-primary") || $("[data-action=saveFrame]");
  if (!framingDraft || !save) return false;
  save.disabled = true;
  save.dataset.action = "saveFrame";
  save.textContent = "Apply";
  save.setAttribute("aria-label", "Apply framing");
  framePreview = null;
  try {
    if (!framingSource) {
      const source = await decodedImage(framingSourceURL);
      if (token !== framingSequence || modal !== "frame") return false;
      framingSource = source;
    }
    const source = framingSource;
    const crop = frameCrop(
      source.width,
      source.height,
      FMT[currentFormat],
      framingDraft,
    );
    framingDraft.focal = crop.focal;
    framingDraft.vertical = crop.vertical;
    for (const [id, available, value, axis] of [
      ["frame-focal", crop.rangeX > 0.5, crop.focal, "horizontal"],
      ["frame-vertical", crop.rangeY > 0.5, crop.vertical, "vertical"],
    ]) {
      const el = $("#" + id);
      el.disabled = !available;
      el.value = value;
      el.closest(".framing-control").classList.toggle("is-locked", !available);
      $("#" + id + "-value").textContent = available
        ? framePositionLabel(value, axis)
        : "At fit";
      $("#" + id + "-hint").textContent = available
        ? "Move within the available crop."
        : `No ${axis} movement at this zoom.`;
    }
    $("#frame-zoom").value = Math.round(crop.zoom * 1000) / 10;
    $("#frame-zoom-value").textContent =
      Math.round(crop.zoom * 1000) / 10 + "%";
    $("#frame-layout").value = framingDraft.place;
    const safe = frameSubjectFits(
      crop,
      framingDraft.subject,
      source.width,
      source.height,
    );
    $("#frame-subject-state").textContent = framingDraft.subject
      ? inheritedFrameSubject()
        ? "Source area retained"
        : "Area marked"
      : "No area marked";
    const markButton = $("[data-action=markSubject]");
    markButton.textContent = framingMarking
      ? "Preview crop"
      : framingDraft.subject
        ? "Edit marked area"
        : "Mark area";
    markButton.setAttribute("aria-pressed", String(framingMarking));
    $("[data-action=clearSubject]").disabled = !framingDraft.subject;
    syncFrameSafetyControl();
    $("#frame-preview-label").textContent = framingMarking
      ? "MARK ON THE ORIGINAL IMAGE"
      : "LIVE FRAMING PREVIEW";
    $("#frame-source").hidden = !framingMarking;
    $("#frame-image").hidden = framingMarking || (!framingOverlay && !studioWorkspace);
    if (framingMarking) $("#frame-live").hidden = true;
    const sourceImg = $("#frame-source img");
    if (sourceImg.src !== source.src) sourceImg.src = source.src;
    drawFrameSource();
    const status = $("#frame-status");
    status.classList.toggle("is-invalid", !safe);
    const readyStatus = !safe
      ? "Crop cuts the marked product area. Zoom out or reposition before applying."
      : "";
    status.textContent = !safe ? readyStatus : "Preparing preview…";
    if (live) {
      queueFramePaint();
      if (!(await waitForFrameIdle())) return false;
    }
    if (token !== framingSequence || modal !== "frame") return false;
    const body = frameBody(),
      signature = JSON.stringify(body);
    const result = await api(
      `/api/campaigns/${framingContext.campaign}/framing/preview`,
      body,
    );
    if (token !== framingSequence || modal !== "frame") return false;
    const item = result.images.find(
      (item) =>
        item.id ===
        `${body.product_id}/${body.market}/${body.ratio.replace(":", "x")}`,
    );
    if (!item) throw new Error("No composed preview");
    const [finalImage, overlay] = await Promise.all([
      decodedImage(item.url),
      decodedImage(item.overlay_url),
    ]);
    if (
      token !== framingSequence ||
      modal !== "frame" ||
      JSON.stringify(frameBody()) !== signature
    )
      return false;
    // Never enable Apply from a JSON response alone. Both the exact final image and
    // the reusable Pillow foreground layer have decoded for this draft signature.
    const image = $("#frame-image");
    image.src = finalImage.src;
    await image.decode();
    if (
      token !== framingSequence ||
      modal !== "frame" ||
      JSON.stringify(frameBody()) !== signature
    )
      return false;
    framingOverlay = overlay;
    framingOverlayPlace = framingDraft.place;
    image.hidden = framingMarking;
    image.style.visibility = "";
    status.textContent = readyStatus;
    $("#frame-live").hidden = true;
    drawFrameSafety();
    framingRecovery = null;
    save.textContent = "Apply";
    framePreview = { body, signature, result };
    const unchanged = JSON.stringify(framingDraft) === framingInitial;
    save.disabled = result.blocked || !safe || framingDrawing || unchanged;
    return !save.disabled;
  } catch (error) {
    if (token !== framingSequence || modal !== "frame") return false;
    let changed = false;
    if (error.status === 409 && ["state", "stale"].includes(error.code)) {
      const target = framingContext;
      const current = await api(`/api/motif/campaigns/${target.campaign}`).catch(() => null);
      if (token !== framingSequence || modal !== "frame" || framingContext !== target) return false;
      changed = !!current && (current.output_run_id !== target.run_id || current.manifest?.revision_id !== target.revision_id);
    }
    framingRecovery = changed ? "latest" : "preview";
    if (changed) framingInitial = null;
    showFrameRecovery();
    return false;
  }
}
// Recovery changes the editing base only after the producer asks. It never
// applies pixels, clears a retained buffer, or follows a second revision silently.
function showFrameRecovery() {
  const status = $("#frame-status"), button = $("#frame-primary") || $("[data-action=saveFrame]");
  if (!status || !button) return;
  // These values remain editable even when the old base cannot be previewed.
  $("#frame-zoom").value = framingDraft.zoom * 100;
  $("#frame-zoom-value").textContent = Math.round(framingDraft.zoom * 1000) / 10 + "%";
  for (const [id, value, axis] of [["frame-focal", framingDraft.focal, "horizontal"], ["frame-vertical", framingDraft.vertical, "vertical"]]) {
    $("#" + id).value = value;
    $("#" + id + "-value").textContent = framePositionLabel(value, axis);
  }
  $("#frame-layout").value = framingDraft.place;
  $("#frame-subject-state").textContent = framingDraft.subject ? "Area marked" : "No area marked";
  $("[data-action=clearSubject]").disabled = !framingDraft.subject;
  syncFrameSafetyControl();
  const messages = {
    latest: MotifEditorControls.copy.stale,
    uncertain: MotifEditorControls.copy.uncertain,
    checking: "Checking the current image…",
    "latest-loading": "Loading the latest image. Your pending framing edits are kept.",
    "latest-error": "The latest image could not be loaded. Your pending framing edits are kept. Try again.",
    missing: MotifEditorControls.copy.missing,
    unsupported: "This image does not support framing adjustments. Cancel or close to discard these edits.",
    preview: "Preview unavailable. Your pending framing edits are kept. Retry the preview.",
    "preview-loading": "Preparing local preview…",
  };
  status.textContent = messages[framingRecovery];
  status.classList.add("is-invalid");
  button.dataset.action = "recoverFrame";
  button.textContent = framingRecovery === "uncertain" ? "Check current image" : framingRecovery === "checking" ? "Checking…" : framingRecovery === "preview" ? "Retry preview" : framingRecovery === "preview-loading" ? "Preparing preview…" : framingRecovery === "latest-loading" ? "Loading latest image…" : "Use latest image";
  button.setAttribute("aria-label", button.textContent);
  button.disabled = ["latest-loading", "preview-loading", "missing", "unsupported", "checking"].includes(framingRecovery);
  if (!framingRecovery.startsWith("preview"))
    $("#frame-preview-label").textContent = "LOCAL DRAFT · REVIEW REQUIRED";
}
async function recoverFraming() {
  if (!framingDraft || !["latest", "latest-error", "preview", "uncertain"].includes(framingRecovery)) return;
  if (framingRecovery === "uncertain") return checkFramingApply();
  if (framingRecovery === "preview") {
    framingRecovery = "preview-loading";
    showFrameRecovery();
    await refreshFramePreview();
    return;
  }
  const session = framingSession, target = framingContext;
  const owns = () => modal === "frame" && framingSession === session && framingContext === target && C.id === target.campaign;
  framingRecovery = "latest-loading";
  showFrameRecovery();
  try {
    const current = await api(`/api/motif/campaigns/${target.campaign}`);
    if (!owns()) return;
    const product = current.output_brief?.products.find(p => p.id === target.product_id);
    const output = current.manifest?.outputs.find(o => o.product_id === target.product_id && o.market === target.market && o.ratio === target.ratio);
    if (!product || !output?.url) {
      framingRecovery = "missing";
      showFrameRecovery();
      return;
    }
    if (product.source.role === "prepared_product") {
      framingRecovery = "unsupported";
      showFrameRecovery();
      return;
    }
    if (!current.editable_products?.[target.product_id]) throw new Error("Source unavailable");
    const sourceURL = `/campaigns/${target.campaign}/runs/${current.output_run_id}/revisions/${current.manifest.revision_id}/sources/${target.product_id}`;
    const source = await decodedImage(sourceURL);
    if (!owns()) return;
    // Preserve the live values, including typing during the read. Only this
    // editor's explicitly selected base and decoded source are replaced.
    framingContext = { ...target, run_id: current.output_run_id, revision_id: current.manifest.revision_id };
    framingSourceURL = sourceURL;
    framingSource = source;
    framingOverlay = null;
    framingInheritedSubject = null;
    framingCustomText = !!Object.keys(product.framing?.[target.market]?.[target.ratio]?.text_placements || {}).length;
    // Preset availability belongs to the explicitly adopted image, while the
    // producer's pending crop values keep their existing local ownership.
    $("#frame-layout").disabled = framingCustomText;
    $("#frame-layout").hidden = framingCustomText;
    $("#frame-custom-text").hidden = !framingCustomText;
    framingRecovery = null;
    framingInitial = null;
    projectChanged();
    await requestFramePreview();
    if (modal === "frame" && framingSession === session && framePreview && !framingRecovery) {
      $("#frame-status").textContent = "Latest image loaded with your pending framing. Check the crop and safety area before applying.";
      $("#frame-zoom").focus({ preventScroll: true });
    }
  } catch {
    if (!owns()) return;
    framingRecovery = "latest-error";
    showFrameRecovery();
  }
}
function inheritedFrameSubject() {
  return (
    !!framingInheritedSubject &&
    JSON.stringify(framingDraft.subject) === framingInheritedSubject
  );
}
function syncFrameSafetyControl() {
  $("#frame-show-safety").checked = framingShowSafety;
  $("#frame-subject-help").textContent = framingMarking
    ? "Drag to mark. Arrows move; Shift + arrows resize."
    : "";
}
function drawFrameSource() {
  drawFrameSafety();
  if (!framingSource || !$("#frame-source")) return;
  const outer = $(".framing-visual"),
    stage = $("#frame-source"),
    source = framingSource,
    scale = Math.min(
      (outer.clientWidth - 24) / source.width,
      (outer.clientHeight - 24) / source.height,
    );
  stage.style.width = source.width * scale + "px";
  stage.style.height = source.height * scale + "px";
  const crop = frameCrop(
    source.width,
    source.height,
    FMT[currentFormat],
    framingDraft,
  );
  const rect = (el, r) =>
    Object.assign(el.style, {
      left: r.x * 100 + "%",
      top: r.y * 100 + "%",
      width: r.w * 100 + "%",
      height: r.h * 100 + "%",
    });
  rect($("#frame-crop-window"), {
    x: crop.sx / source.width,
    y: crop.sy / source.height,
    w: crop.sw / source.width,
    h: crop.sh / source.height,
  });
  const box = $("#frame-subject-box");
  box.hidden = !framingDraft.subject || !framingShowSafety;
  if (framingDraft.subject) rect(box, framingDraft.subject);
  stage.classList.toggle(
    "is-invalid",
    !frameSubjectFits(crop, framingDraft.subject, source.width, source.height),
  );
}
function drawFrameSafety() {
  const unset = $("#frame-safety-unset");
  if (unset) unset.hidden = !framingShowSafety || !!framingDraft?.subject;
  const overlay = $("#frame-safety-overlay"),
    image = $("#frame-image");
  if (!overlay || !image) return;
  overlay.hidden =
    !framingShowSafety ||
    framingMarking ||
    !framingSource ||
    !framingDraft?.subject;
  const pan = $("#frame-pan");
  const format = FMT[currentFormat],
    outer = $(".framing-visual"),
    rect = image.getBoundingClientRect(),
    host = outer.getBoundingClientRect();
  const scale = Math.min(rect.width / format.w, rect.height / format.h),
    width = format.w * scale,
    height = format.h * scale;
  const bounds = {
    left:
      rect.left -
      host.left -
      outer.clientLeft +
      (rect.width - width) / 2 +
      "px",
    top:
      rect.top - host.top - outer.clientTop + (rect.height - height) / 2 + "px",
    width: width + "px",
    height: height + "px",
  };
  pan.hidden = !framingSource || framingMarking || !framingOverlay ||
    ($("#frame-focal").disabled && $("#frame-vertical").disabled);
  if (!pan.hidden) Object.assign(pan.style, bounds);
  if (overlay.hidden) return;
  Object.assign(overlay.style, bounds);
  const crop = frameCrop(
      framingSource.width,
      framingSource.height,
      format,
      framingDraft,
    ),
    r = framingDraft.subject;
  const left = (r.x * framingSource.width - crop.sx) / crop.sw,
    top = (r.y * framingSource.height - crop.sy) / crop.sh,
    w = (r.w * framingSource.width) / crop.sw,
    h = (r.h * framingSource.height) / crop.sh;
  Object.assign($("#frame-safety-outline").style, {
    left: left * 100 + "%",
    top: top * 100 + "%",
    width: w * 100 + "%",
    height: h * 100 + "%",
  });
  const safe = frameSubjectFits(
      crop,
      r,
      framingSource.width,
      framingSource.height,
    ),
    visible = left < 1 && top < 1 && left + w > 0 && top + h > 0;
  overlay.classList.toggle("is-invalid", !safe);
  const label = $("#frame-safety-label");
  label.textContent = safe
    ? "SAFETY AREA"
    : visible
      ? "SAFETY AREA · CROPPED"
      : "SAFETY AREA OUTSIDE CROP";
  label.style.left =
    frameClamp(left * width + 4, 4, Math.max(4, width - 180)) + "px";
  label.style.top =
    frameClamp(top * height + 4, 4, Math.max(4, height - 24)) + "px";
}
function markFrameSubject() {
  if (!framingSource || frameSaving) return;
  framingPan?.cancel();
  framingMarking = !framingMarking;
  if (framingMarking) framingShowSafety = true;
  if (framingMarking && !framingDraft.subject) {
    const c = frameCrop(
      framingSource.width,
      framingSource.height,
      FMT[currentFormat],
      framingDraft,
    );
    framingDraft.subject = {
      x: (c.sx + c.sw * 0.25) / framingSource.width,
      y: (c.sy + c.sh * 0.25) / framingSource.height,
      w: (c.sw * 0.5) / framingSource.width,
      h: (c.sh * 0.5) / framingSource.height,
    };
  }
  requestFramePreview().then(() => {
    if (framingMarking) $("#frame-subject-box")?.focus({ preventScroll: true });
  });
}
function bindFramePan() {
  const pan = $("#frame-pan");
  const crop = () => frameCrop(framingSource.width, framingSource.height, FMT[currentFormat], framingDraft);
  return MotifManipulation.bind({
    element: pan, canvas: pan,
    outputSize: () => ({ width: FMT[currentFormat].w, height: FMT[currentFormat].h }),
    enabled: () => !pan.hidden && !framingMarking && !frameSaving && !framingRecovery,
    read: () => ({ focal: framingDraft.focal, vertical: framingDraft.vertical }),
    move: (start, dx, dy) => {
      const c = crop(), format = FMT[currentFormat];
      // Moving the photo right moves the crop window left across the source.
      return {
        focal: c.rangeX > 0.5 ? frameClamp(start.focal - dx * c.sw * 100 / (format.w * c.rangeX), 0, 100) : 50,
        vertical: c.rangeY > 0.5 ? frameClamp(start.vertical - dy * c.sh * 100 / (format.h * c.rangeY), 0, 100) : 50,
      };
    },
    write: position => {
      if (position.focal === framingDraft.focal && position.vertical === framingDraft.vertical) return;
      Object.assign(framingDraft, position);
      requestFramePreview(true);
    },
  });
}
function bindFraming() {
  const stage = $("#frame-source");
  if (!stage) return;
  framingPan = bindFramePan();
  let start = null;
  for (const control of $$(".framing-control input,#frame-layout"))
    control.addEventListener("input", (event) => {
      if (!frameSaving) frameSettingsFromControls(event.target.id);
    });
  for (const slider of $$(".framing-control input"))
    slider.addEventListener("change", () => requestFramePreview(true));
  $("#frame-show-safety").addEventListener("change", (e) => {
    framingShowSafety = e.target.checked;
    syncFrameSafetyControl();
    drawFrameSource();
  });
  const point = (e) => {
    const r = stage.getBoundingClientRect();
    return {
      x: frameClamp((e.clientX - r.left) / r.width, 0, 1),
      y: frameClamp((e.clientY - r.top) / r.height, 0, 1),
    };
  };
  stage.addEventListener("pointerdown", (e) => {
    if (frameSaving || !framingMarking || start || e.button !== 0 || e.isPrimary === false) return;
    e.preventDefault();
    framingShowSafety = true;
    syncFrameSafetyControl();
    drawFrameSource();
    framingDrawing = true;
    framePreview = null;
    ++framingSequence;
    $("#frame-primary").disabled = true;
    start = {
      ...point(e),
      pointerId: e.pointerId,
      subject: framingDraft.subject && { ...framingDraft.subject },
    };
    $("#frame-subject-box").focus({ preventScroll: true });
    stage.setPointerCapture(e.pointerId);
  });
  const finishMark = (pointerId, cancelled) => {
    if (!start || start.pointerId !== pointerId) return;
    if (cancelled) framingDraft.subject = start.subject;
    start = null;
    framingDrawing = false;
    if (stage.hasPointerCapture(pointerId)) stage.releasePointerCapture(pointerId);
    requestFramePreview();
  };
  stage.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !start) return;
    e.preventDefault();
    e.stopPropagation();
    finishMark(start.pointerId, true);
  });
  stage.addEventListener("pointermove", (e) => {
    if (!start || e.pointerId !== start.pointerId) return;
    const end = point(e);
    framingDraft.subject = {
      x: Math.min(start.x, end.x, 0.99),
      y: Math.min(start.y, end.y, 0.99),
      w: Math.max(0.01, Math.abs(end.x - start.x)),
      h: Math.max(0.01, Math.abs(end.y - start.y)),
    };
    framingDraft.subject.w = Math.min(
      framingDraft.subject.w,
      1 - framingDraft.subject.x,
    );
    framingDraft.subject.h = Math.min(
      framingDraft.subject.h,
      1 - framingDraft.subject.y,
    );
    drawFrameSource();
  });
  stage.addEventListener("pointerup", e => finishMark(e.pointerId, false));
  stage.addEventListener("pointercancel", e => finishMark(e.pointerId, true));
  stage.addEventListener("lostpointercapture", e => finishMark(e.pointerId, true));
  $("#frame-subject-box").addEventListener("keydown", (e) => {
    if (
      frameSaving ||
      !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key) ||
      !framingDraft.subject
    )
      return;
    e.preventDefault();
    const r = framingDraft.subject,
      delta = e.key === "ArrowRight" || e.key === "ArrowDown" ? 0.01 : -0.01;
    if (e.shiftKey) {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight")
        r.w = frameClamp(r.w + delta, 0.01, 1 - r.x);
      else r.h = frameClamp(r.h + delta, 0.01, 1 - r.y);
    } else {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight")
        r.x = frameClamp(r.x + delta, 0, 1 - r.w);
      else r.y = frameClamp(r.y + delta, 0, 1 - r.h);
    }
    requestFramePreview();
  });
  framingObserver = new ResizeObserver(() => {
    drawFrameSource();
    if ($("#frame-live") && !$("#frame-live").hidden) paintLiveFraming();
  });
  framingObserver.observe($(".framing-visual"));
  requestFramePreview();
}
function endFraming() {
  framingDrawing = false;
  framingPan?.destroy();
  framingPan = null;
  cancelFrameWork();
  framingSequence++;
  framingSession++;
  framingRecovery = null;
  framingAttempt = null;
  framingObserver?.disconnect();
  framingObserver = null;
  framingDraft = null;
  framingSource = null;
  framingOverlay = null;
  framePreview = null;
  framingContext = null;
}

let frameSaving = false,
  framePreview = null,
  framingContext = null,
  framingInitial = "",
  framingSourceURL = "",
  framingOverlayPlace = null;
function frameBody() {
  const r = framingDraft.subject;
  return {
    ...framingContext,
    campaign: undefined,
    framing: {
      zoom: framingDraft.zoom,
      focal_x: framingDraft.focal / 100,
      focal_y: framingDraft.vertical / 100,
      copy_placement: framingDraft.place,
      inherit_subject: false,
      subject_box: r
        ? {
            left: r.x,
            top: r.y,
            right: Math.min(1, r.x + r.w),
            bottom: Math.min(1, r.y + r.h),
          }
        : null,
    },
  };
}
function resetFrame() {
  framingDraft.zoom = 1;
  framingDraft.focal = 50;
  framingDraft.vertical = 50;
  requestFramePreview();
}
function clearFrameSubject() {
  framingDraft.subject = null;
  framingMarking = false;
  framingShowSafety = false;
  requestFramePreview();
}
// A receipt identifies this exact submitted command. A failed transport cannot
// justify resending it until a read confirms whether publication happened.
async function finishFraming(current) {
  const target = framingContext;
  const controls = $$("input,select,button", imageEditorRoot());
  const disabled = controls.map(control => control.disabled);
  frameSaving = true;
  controls.forEach(control => control.disabled = true);
  try { return await studioApplied(current, output => {
    framingContext.run_id = current.output_run_id;
    framingContext.revision_id = current.manifest.revision_id;
    framingInitial = JSON.stringify(framingDraft);
    framingAttempt = null;
    framingRecovery = null;
    framePreview = null;
    frameSaving = false;
    framingMarking = false;
    $("#frame-preview-label").textContent = "LIVE FRAMING PREVIEW";
    $("#frame-source").hidden = true;
    $("#frame-image").src = output.url;
    $("#frame-image").hidden = false;
    $("[data-action=markSubject]").textContent = framingDraft.subject ? "Edit marked area" : "Mark area";
    $("[data-action=markSubject]").setAttribute("aria-pressed", "false");
    syncFrameSafetyControl();
    drawFrameSafety();
    $("#frame-status").textContent = "";
    $("#frame-primary").dataset.action = "saveFrame";
    $("#frame-primary").textContent = "Apply";
    $("#frame-primary").setAttribute("aria-label", "Apply framing");
    $("#frame-primary").disabled = true;
  }); } finally {
    if (framingContext === target) frameSaving = false;
    controls.forEach((control, index) => { if (control.isConnected) control.disabled = disabled[index]; });
    if (modal === "frame" && framingContext === target && !framePreview) $("#frame-primary").disabled = true;
  }
}
async function checkFramingApply() {
  const target = framingContext, attempt = framingAttempt, session = framingSession;
  framingRecovery = "checking"; showFrameRecovery();
  try {
    const current = await api(`/api/motif/campaigns/${target.campaign}`);
    if (modal !== "frame" || framingSession !== session) return false;
    const receipt = current.manifest?.edit_receipt;
    if (receipt?.kind === "framing" && receipt.base_revision === target.revision_id && receipt.preview_digest === attempt?.result.digest)
      return await finishFraming(current);
    framingRecovery = current.output_run_id === target.run_id && current.manifest?.revision_id === target.revision_id ? "preview" : "latest";
    showFrameRecovery();
  } catch {
    if (modal === "frame" && framingSession === session) { framingRecovery = "uncertain"; showFrameRecovery(); }
  }
  return false;
}
async function saveFraming() {
  if (framingPan?.active() || framingDrawing) return false;
  if (!framePreview || framePreview.result.blocked || framingRecovery ||
      $("[data-action=saveFrame]").disabled || JSON.stringify(frameBody()) !== framePreview.signature)
    throw new Error("Wait for the current safe framing preview before applying.");
  const captured = framePreview, target = framingContext, session = framingSession;
  const controls = $$("input:not(#frame-show-safety),select,button", imageEditorRoot());
  const disabled = controls.map(control => control.disabled);
  const status = $("#frame-status"), previousStatus = status.textContent;
  frameSaving = true; framingAttempt = captured;
  controls.forEach(control => control.disabled = true);
  status.textContent = "Applying…";
  try {
    if (!(await imageCleanupReady(target.campaign, () => modal === "frame" && framingSession === session && framePreview === captured && JSON.stringify(frameBody()) === captured.signature))) {
      frameSaving = false;
      controls.forEach((control, index) => control.disabled = disabled[index]);
      status.textContent = previousStatus;
      return false;
    }
    const applied = await api(`/api/campaigns/${target.campaign}/framing/apply`, {
      ...captured.body, preview_digest: captured.result.digest,
    });
    const current = await api(`/api/motif/campaigns/${target.campaign}`);
    if (modal !== "frame" || framingSession !== session) return false;
    if (current.manifest?.revision_id !== applied.revision_id) throw { status: 409 };
    return await finishFraming(current);
  } catch (error) {
    if (modal !== "frame" || framingSession !== session) return false;
    controls.forEach((control, index) => control.disabled = disabled[index]);
    frameSaving = false;
    if (await imageCapacity(error, "Applying this framing")) {
      status.textContent = previousStatus;
      return false;
    }
    framingRecovery = error.code === "apply_not_accepted" ? "preview" : !error.status || error.status >= 500 ? "uncertain" : error.status === 409 ? "latest" : "preview";
    showFrameRecovery();
    return false;
  } finally {
    frameSaving = false;
    // The same controls survive Apply. Restore their prior axis/scope guards.
    controls.forEach((control, index) => {
      if (control.isConnected) control.disabled = disabled[index];
    });
    if (modal === "frame" && framingSession === session && !framePreview)
      $("#frame-primary").disabled = true;
  }
}
