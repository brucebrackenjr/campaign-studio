"use strict";

// One transient target. Geometry and glyph layers always come from the renderer;
// the shared input helper supplies only pointer/keyboard ownership and snapshots.
let textEditor = null;
function beginText() {
  const product = C.products[currentProduct], format = FMT[currentFormat];
  textEditor = {
    campaign: C.id, product: product.id, language: lang, format: currentFormat,
    run: model.output_run_id, revision: model.manifest.revision_id,
    brand: copy(model.output_brief.brand), placements: {}, selected: "message",
    initial: { message: valueFor(currentProduct, lang, "headline"), cta: valueFor(currentProduct, lang, "cta") },
    values: { message: valueFor(currentProduct, lang, "headline"), cta: valueFor(currentProduct, lang, "cta") },
    all: { message: false, cta: false },
    geometry: null, geometrySequence: -1, layers: null, preview: null, sequence: 0, timer: null,
    pending: false, queued: false, saving: false, recovery: null, attempt: null, gestureBlock: null,
    anchor: null, anchorBlocked: false, message: MotifEditorControls.copy.previewing,
    width: format.w, height: format.h, input: null, observer: null,
  };
}
function textCurrent(editor) {
  return textEditor === editor && !editor.closing && C.id === editor.campaign && modal === "text";
}
function textBody(editor = textEditor) {
  return {
    run_id: editor.run, revision_id: editor.revision, product_id: editor.product,
    market: MARKET[editor.language], ratio: FMT[editor.format].ratio,
    placements: copy(editor.placements), ...wordingPatch(editor),
  };
}
function textParts() {
  const editor = textEditor, product = C.products.find(p => p.id === editor.product);
  return editorParts(
    `<div class="mf-editor-grid"><aside class="mf-editor-tools"><div class="mf-editor-target"><h3>${esc(product.name)}</h3><p>${LANG[editor.language]} · ${FMT[editor.format].name}</p></div>
    <div class="mf-blocks ws-blocks" role="group" aria-label="Text block">${[["message", "Headline"], ["cta", "Call to action"]].map(([id, label]) => `<button type="button" data-text-block="${id}" aria-pressed="${id === editor.selected}"><strong>${label}</strong><small hidden>No wording supplied</small></button>`).join("")}</div><div class="mf-combined-wording">${MotifEditorControls.wordingFields(editor)}</div>
    <div id="text-style"></div><div class="mf-instructions"><strong>Drag to move. Pull a corner to resize.</strong><p>Arrows move; Shift moves ten pixels.</p><p>Tab to a corner; arrows resize.</p></div>
    </aside>
    <section class="mf-editor-preview"><div class="mf-preview-head"><span class="eyebrow">${esc(product.name)}</span></div>
    <div class="mf-stage" id="text-stage"><div class="mf-output" id="text-output"><canvas id="text-canvas" width="${editor.width}" height="${editor.height}" hidden></canvas><img id="text-image" hidden alt="Proposed text layout for ${esc(product.name)}">${MotifEditorControls.selection("Message")}</div></div>
    <p class="mf-preview-status" id="text-status" role="status">Preparing preview…</p></section></div>`,
    `<div class="ws-text-footer">${btn("Retry preview", "recoverText", "", "hidden")}<div class="row ws-text-actions">${btn("Reset text layout", "resetText", "quiet")}${btn("Cancel", "close")}${btn("Apply text", "applyText", "primary", "disabled")}</div></div>`);
}
function textBlock(editor = textEditor) {
  return editor?.geometry?.text_blocks[editor.selected];
}
function textPlacement(editor = textEditor) {
  return editor.placements[editor.selected] || textBlock(editor)?.placement;
}
function textStyle(editor) {
  const block = textBlock(editor);
  if (!block) { $("#text-style").innerHTML = ""; delete $("#text-style").dataset.signature; return; }
  const placement = textPlacement(editor);
  const style = $("#text-style"), signature = JSON.stringify([editor.selected, placement.font_id, placement.color, editor.brand]);
  if (style.dataset.signature === signature) return;
  const focused = document.activeElement, key = focused?.dataset?.mfColor;
  const fontFocused = focused?.matches?.("[data-mf-font]"), defaultFocused = focused?.hasAttribute?.("data-mf-color-default");
  style.innerHTML = MotifEditorControls.textStyleControls({
    fontId: placement.font_id, color: placement.color,
    campaignColor: editor.brand.text_color, palette: editor.brand.palette,
  });
  style.dataset.signature = signature;
  if (fontFocused) $("[data-mf-font]", style)?.focus({ preventScroll: true });
  else if (key) $$('[data-mf-color]', style).find(el => el.dataset.mfColor === key)?.focus({ preventScroll: true });
  else if (defaultFocused) $("[data-mf-color-default]", style)?.focus({ preventScroll: true });
}
function paintText() {
  const editor = textEditor;
  if (!editor || !textCurrent(editor)) return;
  $("#text-status").textContent = editor.message;
  $("#text-status").classList.toggle("is-error", !!editor.recovery || !!editor.preview?.result.blocked || editor.anchorBlocked);
  const apply = $('[data-action="applyText"]');
  apply.textContent = editor.saving ? "Applying…" : "Apply text";
  apply.disabled = editor.saving || editor.pending || !!editor.recovery || editor.anchorBlocked || !editor.preview?.displayed || editor.preview.result.blocked || !editor.preview.result.impact.render_outputs.length;
  const recovery = $('[data-action="recoverText"]');
  recovery.hidden = !editor.recovery;
  recovery.textContent = editor.recovery === "production" ? "Open recovery" : editor.recovery === "stale" ? "Use latest image" : editor.recovery === "uncertain" ? "Check current image" : "Retry preview";
  for (const el of $$('[data-text-block]')) {
    const block = editor.geometry?.text_blocks[el.dataset.textBlock];
    el.disabled = editor.saving;
    el.setAttribute("aria-pressed", String(editor.selected === el.dataset.textBlock));
    $("small", el).hidden = !editor.geometry || editor.pending || editor.geometrySequence !== editor.sequence || !!block;
  }
  MotifEditorControls.paintScopes($(".mf-combined-wording"), wordingIntent(editor), C.products.find(p => p.id === editor.product).name);
  for (const field of $$("[data-mf-field]", $(".mf-combined-wording"))) field.hidden = field.dataset.mfField !== editor.selected;
  textStyle(editor);
  for (const el of $$('button.close,[data-action="close"],[data-action="resetText"],[data-mf-font],[data-mf-color],[data-mf-color-default],[data-mf-value],[data-mf-all]')) el.disabled = editor.saving;
  positionTextSelection();
}
function positionTextSelection() {
  const editor = textEditor, block = textBlock(editor);
  if (!editor || !textCurrent(editor)) return;
  const frame = $(".mf-direct-selection"), canvas = $("#text-output");
  frame.hidden = !block;
  if (!block) return;
  const rect = canvas.getBoundingClientRect(), [left, top, right, bottom] = block.bounds;
  const bounds = MotifManipulation.selectionFrame({
    left: left / editor.width * rect.width, top: top / editor.height * rect.height,
    width: (right - left) / editor.width * rect.width, height: (bottom - top) / editor.height * rect.height,
  }, { width: rect.width, height: rect.height });
  Object.assign(frame.style, Object.fromEntries(["left", "top", "width", "height"].map(key => [key, `${bounds[key]}px`])));
  frame.classList.toggle("is-expanded", bounds.expanded);
  frame.classList.toggle("is-top-edge", bounds.top < 24);
  const label = editor.selected === "message" ? "Headline" : "Call to action";
  $(".mf-direct-label", frame).textContent = label;
  frame.setAttribute("aria-label", `${label}. Drag inside to move; corners resize. Arrow keys move; Shift moves ten pixels.`);
  for (const handle of $$("[data-mf-corner]", frame)) handle.setAttribute("aria-label", `Resize ${label} from ${{nw:"top left",ne:"top right",sw:"bottom left",se:"bottom right"}[handle.dataset.mfCorner]}. Arrow keys resize. Shift uses ten pixels.`);
}
function sizeTextCanvas() {
  const editor = textEditor;
  if (!editor || !textCurrent(editor)) return;
  editor.input?.cancel();
  const stage = $("#text-stage"), output = $("#text-output");
  const width = Math.max(1, Math.min(stage.clientWidth, stage.clientHeight * editor.width / editor.height));
  output.style.width = `${width}px`;
  output.style.height = `${width * editor.height / editor.width}px`;
  positionTextSelection();
}
function readTextGesture() {
  const block = textBlock();
  return { id: textEditor.selected, placement: copy(textPlacement()), bounds: [...block.bounds], patch: copy(textEditor.placements), anchor: null };
}
function moveTextGesture(start, dx, dy) {
  if (!dx && !dy) return start;
  const editor = textEditor, [left, top, right, bottom] = start.bounds;
  const x = start.placement.left * editor.width, y = start.placement.top * editor.height;
  // Measured glyphs use rounded raster coordinates; the saved origin can be
  // fractional. Keep both within the canvas using the same translation.
  dx = Math.max(-left, -x, Math.min(editor.width - right, editor.width - x, dx));
  dy = Math.max(-top, -y, Math.min(editor.height - bottom, editor.height - y, dy));
  const placement = { ...start.placement, left: (x + dx) / editor.width, top: (y + dy) / editor.height };
  return { ...start, placement, bounds: [left + dx, top + dy, right + dx, bottom + dy], patch: { ...start.patch, [start.id]: placement }, anchor: null };
}
function resizeTextGesture(start, corner, dx, dy, keyboard = false) {
  if (!dx && !dy) return start;
  const editor = textEditor;
  const [left, top, right, bottom] = start.bounds, width = right - left, height = bottom - top;
  const directionX = corner.endsWith("e") ? 1 : -1, directionY = corner.startsWith("s") ? 1 : -1;
  const delta = keyboard ? directionX * dx + directionY * dy
    : start.placement.font_size * (directionX * dx * width + directionY * dy * height) / Math.max(1, width * width + height * height);
  const x = directionX > 0 ? left : right, y = directionY > 0 ? top : bottom;
  // Scale the wrapping column with the font. Holding it fixed turns a corner
  // drag into repeated reflow, changing the next gesture's scale and anchor.
  // One raster pixel leaves room for the renderer's integer glyph rounding.
  const maximum = Math.min(
    (directionX > 0 ? editor.width - x - 1 : x - 1) / Math.max(1, width),
    (directionY > 0 ? editor.height - y - 1 : y - 1) / Math.max(1, height),
  );
  const minimumFont = Math.min(24, start.placement.font_size);
  const fontSize = Math.max(minimumFont, Math.min(65535, Math.floor(start.placement.font_size * maximum), Math.round(start.placement.font_size + delta)));
  const scale = fontSize / start.placement.font_size;
  const placement = {
    ...start.placement, font_size: fontSize,
    width: Math.min(1, start.placement.width * scale),
    left: Math.max(0, (x + (start.placement.left * editor.width - x) * scale) / editor.width),
    top: Math.max(0, (y + (start.placement.top * editor.height - y) * scale) / editor.height),
  };
  if (placement.font_size === start.placement.font_size) return start;
  return { ...start, placement, bounds: [x + (left - x) * scale, y + (top - y) * scale, x + (right - x) * scale, y + (bottom - y) * scale], patch: { ...start.patch, [start.id]: placement }, anchor: {
    id: start.id, corner, x, y,
  } };
}
function writeTextGesture(value) {
  const editor = textEditor;
  if (!editor || editor.closing || editor.saving) return;
  const block = editor.geometry.text_blocks[value.id];
  if (JSON.stringify(editor.placements) === JSON.stringify(value.patch) &&
      JSON.stringify(block.bounds) === JSON.stringify(value.bounds)) return;
  editor.placements = copy(value.patch);
  editor.anchor = value.anchor;
  editor.anchorBlocked = false;
  block.placement = copy(value.placement);
  block.bounds = [...value.bounds];
  if (editor.layers) {
    const canvas = $("#text-canvas"), ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, editor.width, editor.height);
    ctx.drawImage(editor.layers.base, 0, 0);
    for (const id of ["message", "cta"]) {
      const layer = editor.layers.blocks[id], geometry = editor.geometry.text_blocks[id];
      if (layer && geometry) {
        const scale = geometry.placement.font_size / layer.fontSize;
        ctx.drawImage(layer.image, geometry.bounds[0] - layer.bounds[0] * scale, geometry.bounds[1] - layer.bounds[1] * scale, editor.width * scale, editor.height * scale);
      }
    }
    canvas.hidden = false;
    $("#text-image").hidden = true;
  }
  editor.gestureBlock = value.id;
  scheduleTextPreview(true);
  positionTextSelection();
}
function scheduleTextPreview(preserveGesture = false) {
  const editor = textEditor;
  if (!editor || editor.saving) return;
  editor.sequence++;
  if (!preserveGesture) editor.gestureBlock = null;
  editor.preview = null;
  clearTimeout(editor.timer);
  if (!["stale", "uncertain"].includes(editor.recovery)) {
    editor.message = MotifEditorControls.copy.previewing;
    editor.recovery = null;
    if (!editor.input?.active()) editor.timer = setTimeout(previewText, 180);
  }
  paintText();
}
async function previewText() {
  const editor = textEditor;
  if (!editor || !textCurrent(editor) || editor.saving || editor.input?.active() || ["stale", "uncertain"].includes(editor.recovery)) return;
  if (editor.pending) { editor.queued = true; return; }
  const sequence = editor.sequence, current = () => textCurrent(editor) && editor.sequence === sequence;
  editor.pending = true;
  editor.recovery = null;
  paintText();
  try {
    let body = textBody(editor), result = await api(`/api/campaigns/${editor.campaign}/text/preview`, body);
    if (!current()) return;
    if (editor.anchor) {
      const anchor = editor.anchor, block = result.geometry.text_blocks[anchor.id];
      const x = anchor.corner.endsWith("e") ? block.bounds[0] : block.bounds[2];
      const y = anchor.corner.startsWith("s") ? block.bounds[1] : block.bounds[3];
      const placement = { ...body.placements[anchor.id], left: block.placement.left + (anchor.x - x) / editor.width, top: block.placement.top + (anchor.y - y) / editor.height };
      editor.anchorBlocked = placement.left < 0 || placement.top < 0 || placement.left > 1 || placement.top > 1;
      if (!editor.anchorBlocked && (Math.abs(x - anchor.x) > 0.5 || Math.abs(y - anchor.y) > 0.5)) {
        body.placements[anchor.id] = placement;
        result = await api(`/api/campaigns/${editor.campaign}/text/preview`, body);
        if (!current()) return;
      }
    }
    // Selection belongs to the producer, including an optional block just cleared.
    const image = result.images[0];
    if (image && !result.blocked) {
      const [final, base, entries] = await Promise.all([
        decodedImage(image.url), decodedImage(image.layers.base_url),
        Promise.all(Object.entries(image.layers.text_blocks).map(async ([id, block]) => [id, { image: await decodedImage(block.url), bounds: block.bounds, fontSize: result.geometry.text_blocks[id].placement.font_size }])),
      ]);
      if (!current()) return;
      const displayed = $("#text-image");
      displayed.src = final.src;
      await displayed.decode();
      if (!current()) return;
      displayed.hidden = false;
      $("#text-canvas").hidden = true;
      editor.layers = { base, blocks: Object.fromEntries(entries) };
    } else editor.layers = null;
    editor.placements = copy(body.placements);
    editor.geometry = result.geometry;
    editor.geometrySequence = sequence;
    editor.gestureBlock = !result.blocked && !editor.anchorBlocked ? editor.selected : null;
    editor.preview = { body, result, displayed: !!image && !result.blocked };
    editor.message = result.blocked ? wordingBlockDetails(result.findings, `${editor.product}/${MARKET[editor.language]}/${FMT[editor.format].ratio.replace(":", "x")}`) || MotifEditorControls.copy.noFit
      : editor.anchorBlocked ? MotifEditorControls.copy.noFit
      : Object.keys(editor.placements).length || wordingIntent(editor).fields.length ? "Preview only. Apply text saves your changes." : "";
    if (!image && !result.blocked) { editor.message = "Preview unavailable."; editor.recovery = "retry"; }
  } catch (error) {
    if (!current()) return;
    editor.gestureBlock = null;
    editor.recovery = error.code === "saved_images_unavailable" ? "production" : "retry";
    editor.message = error.message || "Preview unavailable.";
    if (error.status === 409) {
      const latest = await api(`/api/motif/campaigns/${editor.campaign}`).catch(() => null);
      if (!current()) return;
      if (latest && (latest.output_run_id !== editor.run || latest.manifest?.revision_id !== editor.revision)) {
        editor.recovery = "stale";
        editor.message = MotifEditorControls.copy.stale;
      }
    }
  } finally {
    editor.pending = false;
    if (textCurrent(editor)) {
      paintText();
      if (editor.queued) { editor.queued = false; previewText(); }
    }
  }
}
function selectTextAtPointer(event, editor) {
  if (!textCurrent(editor) || editor.saving || editor.pending ||
      editor.geometrySequence !== editor.sequence || !editor.geometry ||
      event.button !== 0 || event.isPrimary === false ||
      event.target.closest("[data-mf-corner]")) return;
  const rect = $("#text-output").getBoundingClientRect();
  const x = (event.clientX - rect.left) * editor.width / rect.width;
  const y = (event.clientY - rect.top) * editor.height / rect.height;
  const selected = ["cta", "message"].find(id => {
    const bounds = editor.geometry.text_blocks[id]?.bounds;
    return bounds && x >= bounds[0] && x <= bounds[2] && y >= bounds[1] && y <= bounds[3];
  });
  if (selected && selected !== editor.selected) {
    editor.input?.cancel();
    editor.selected = selected;
    paintText();
  }
}
function bindText() {
  const editor = textEditor;
  // Glyph selection precedes the enlarged frame's move handler. Explicit resize
  // handles retain priority, and overlapping blocks follow their final draw order.
  $("#text-output").addEventListener("pointerdown", event => selectTextAtPointer(event, editor), { capture: true });
  editor.input = MotifManipulation.bind({
    element: $(".mf-direct-selection"), canvas: $("#text-output"),
    outputSize: () => ({ width: editor.width, height: editor.height }),
    // Direct gestures transform the last measured layers locally. Wording or
    // font changes clear this permission until their new geometry arrives.
    enabled: () => textCurrent(editor) && !editor.saving && !!textBlock(editor) &&
      (editor.input?.active() || (!editor.pending && editor.geometrySequence === editor.sequence) ||
        editor.gestureBlock === editor.selected),
    read: readTextGesture, write: writeTextGesture, move: moveTextGesture,
    resize: resizeTextGesture,
    resizeKey: (start, corner, delta) => resizeTextGesture(start, corner, delta.dx, delta.dy, true),
    onStart: () => {
      clearTimeout(editor.timer);
      // A response already in flight must not replace a held pointer's basis.
      if (editor.pending && editor.input?.active()) editor.sequence++;
    },
    onFinish: ({ keyboard }) => {
      if (!keyboard && editor.geometrySequence !== editor.sequence) {
        clearTimeout(editor.timer);
        previewText();
      }
    },
  });
  editor.observer = new ResizeObserver(sizeTextCanvas);
  editor.observer.observe($("#text-stage"));
  sizeTextCanvas();
  previewText();
}
function endText() {
  const editor = textEditor;
  if (!editor || editor.saving) return false;
  // Destroy can cancel an active gesture. Keep its owner readable during that
  // callback, while preventing a cancelled editor from queuing another preview.
  editor.closing = true;
  clearTimeout(editor.timer);
  editor.observer?.disconnect();
  editor.input?.destroy();
  textEditor = null;
  return true;
}
function resetText() {
  if (!textEditor || textEditor.saving) return;
  textEditor.placements = { message: null, cta: null };
  textEditor.anchor = null;
  textEditor.anchorBlocked = false;
  scheduleTextPreview();
}
async function finishText(editor, latest) {
  if (!textCurrent(editor)) return false;
  editor.saving = true;
  paintText();
  try { return await studioApplied(latest, output => {
    editor.run = latest.output_run_id;
    editor.revision = latest.manifest.revision_id;
    editor.brand = copy(latest.output_brief.brand);
    editor.initial = { message: String(output.copy.message ?? ""), cta: String(output.copy.cta ?? "") };
    editor.values = copy(editor.initial);
    editor.all = { message: false, cta: false };
    editor.placements = {};
    editor.preview = null;
    editor.attempt = null;
    editor.anchor = null;
    editor.anchorBlocked = false;
    editor.recovery = null;
    editor.saving = false;
    editor.message = "";
    for (const input of $$('[data-mf-all]')) input.checked = false;
    $("#text-image").src = output.url;
    paintText();
  }); } finally {
    if (textCurrent(editor) && editor.saving) { editor.saving = false; paintText(); }
  }
}
async function applyText() {
  const editor = textEditor, preview = editor?.preview;
  if (!editor || editor.saving || editor.pending || editor.recovery || editor.anchorBlocked || !preview?.displayed || preview.result.blocked || !preview.result.impact.render_outputs.length) return false;
  editor.saving = true;
  paintText();
  try {
    if (!(await imageCleanupReady(editor.campaign, () => textCurrent(editor) && editor.preview === preview && JSON.stringify(textBody(editor)) === JSON.stringify(preview.body)))) {
      editor.saving = false; paintText(); return false;
    }
    editor.attempt = { body: copy(preview.body), digest: preview.result.digest, sequence: editor.sequence,
      values: copy(editor.values), all: copy(editor.all) };
    const applied = await api(`/api/campaigns/${editor.campaign}/text/apply`, { ...preview.body, preview_digest: preview.result.digest });
    const latest = await api(`/api/motif/campaigns/${editor.campaign}`);
    if (latest.manifest?.revision_id !== applied.revision_id) throw { status: 409, message: MotifEditorControls.copy.stale };
    return await finishText(editor, latest);
  } catch (error) {
    if (!textCurrent(editor)) return false;
    editor.saving = false;
    paintText();
    if (await imageCapacity(error, "Applying this text layout")) { paintText(); return false; }
    editor.recovery = error.code === "apply_not_accepted" ? "retry" : !error.status || error.status >= 500 ? "uncertain" : error.status === 409 ? "stale" : "retry";
    editor.message = editor.recovery === "uncertain" ? MotifEditorControls.copy.uncertain : error.message || MotifEditorControls.copy.applyFailed;
    paintText();
    return false;
  }
}
async function recoverText() {
  const editor = textEditor;
  if (!editor || editor.saving) return;
  if (editor.recovery === "production") {
    const latest = await api(`/api/motif/campaigns/${editor.campaign}`);
    if (!textCurrent(editor) || editor.recovery !== "production") return;
    useData(latest);
    return navigate("production");
  }
  if (editor.recovery === "retry") return previewText();
  const recovery = editor.recovery, sequence = editor.sequence, attempt = editor.attempt;
  const latest = await api(`/api/motif/campaigns/${editor.campaign}`);
  if (!textCurrent(editor) || sequence !== editor.sequence) return;
  let acknowledged = false;
  if (recovery === "uncertain") {
    const receipt = latest.manifest?.edit_receipt;
    acknowledged = !!attempt && receipt?.kind === "text" && receipt.base_revision === attempt.body.revision_id && receipt.preview_digest === attempt.digest;
    if (acknowledged && sequence === attempt.sequence) return finishText(editor, latest);
    if (!acknowledged && latest.manifest?.revision_id !== editor.revision) {
      editor.recovery = "stale"; editor.message = MotifEditorControls.copy.stale; paintText(); return;
    }
  }
  const output = latest.manifest?.outputs.find(o => o.product_id === editor.product && o.market === MARKET[editor.language] && o.ratio === FMT[editor.format].ratio);
  if (!output?.url && !latest.rule_withheld_outputs?.includes(output?.id)) { editor.message = MotifEditorControls.copy.missing; paintText(); return; }
  useData(latest, { applied: acknowledged });
  editor.run = latest.output_run_id;
  editor.revision = latest.manifest.revision_id;
  editor.brand = copy(latest.output_brief.brand);
  if (acknowledged) {
    // The submitted command is saved. Keep only the producer's later intent
    // against that accepted image, including a revert to the original wording.
    for (const field of ["message", "cta"]) {
      editor.initial[field] = String(output.copy[field] ?? "");
      if (editor.values[field] === attempt.values[field] && editor.all[field] === attempt.all[field]) editor.all[field] = false;
      if (JSON.stringify(editor.placements[field]) === JSON.stringify(attempt.body.placements[field])) {
        delete editor.placements[field];
        if (editor.anchor?.id === field) { editor.anchor = null; editor.anchorBlocked = false; }
      }
    }
    for (const input of $$('[data-mf-all]')) input.checked = editor.all[input.dataset.mfAll];
  }
  editor.attempt = null;
  editor.recovery = null;
  scheduleTextPreview();
}
document.addEventListener("click", event => {
  if (modal !== "text" || !textEditor || textEditor.saving) return;
  const block = event.target.closest("[data-text-block]"), color = event.target.closest("[data-mf-color],[data-mf-color-default]");
  if (block) {
    textEditor.input.cancel();
    textEditor.selected = block.dataset.textBlock;
    paintText();
  } else if (color && textBlock()) {
    textEditor.placements[textEditor.selected] = { ...textPlacement(), color: color.hasAttribute("data-mf-color-default") ? null : color.dataset.mfColor };
    textEditor.anchor = null;
    scheduleTextPreview();
  }
});
document.addEventListener("change", event => {
  if (modal !== "text" || !textEditor || textEditor.saving || !event.target.matches("[data-mf-font]") || !textBlock()) return;
  textEditor.placements[textEditor.selected] = { ...textPlacement(), font_id: event.target.value };
  textEditor.anchor = null;
  scheduleTextPreview();
});

function recordCombinedText(event) {
  const editor = textEditor;
  if (modal !== "text" || !editor || editor.saving || !event.target.matches("[data-mf-value],[data-mf-all]")) return;
  Object.assign(editor, MotifEditorControls.readFields($(".mf-combined-wording")));
  if (event.target.dataset.mfValue) {
    editor.selected = event.target.dataset.mfValue;
    event.target.parentElement.querySelector(".mw-field-message")?.remove();
  }
  editor.anchor = null;
  scheduleTextPreview();
}
document.addEventListener("input", recordCombinedText);
document.addEventListener("change", event => {
  if (event.target.matches("[data-mf-all]")) recordCombinedText(event);
});
