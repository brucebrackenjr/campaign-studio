"use strict";

// A save owns an exact request until acknowledged. New input stays in the
// editor while that request is in flight, then becomes the next request.
function compositionSaveQueue({ capture, send, accept, changed, prepare, delay = 1000 }) {
  let saved = JSON.stringify(capture()), timer = null, flight = null;
  let pending = null, stopped = false, paused = false, error = null;
  const dirty = () => !!pending || JSON.stringify(capture()) !== saved;
  const publish = phase => changed?.({ phase, error, dirty: dirty() });
  function schedule(wait = delay) {
    clearTimeout(timer);
    if (stopped || !dirty()) return;
    if (paused) return publish("conflict");
    error = null;
    publish("dirty");
    timer = setTimeout(() => flush().catch(() => {}), wait);
  }
  async function flush() {
    clearTimeout(timer);
    if (stopped || paused) return false;
    if (prepare && !await prepare()) return false;
    if (stopped || paused) return false;
    if (flight) { await flight; return dirty() ? flush() : true; }
    if (!dirty()) return true;
    const captured = pending?.captured || structuredClone(capture());
    pending ||= { captured, request: null };
    const attempt = pending;
    error = null;
    publish("saving");
    flight = (async () => {
      try {
        const result = await send(attempt);
        if (stopped) return false;
        const unchanged = JSON.stringify(capture()) === JSON.stringify(captured);
        await accept(result, captured);
        saved = unchanged ? JSON.stringify(capture()) : JSON.stringify(captured);
        pending = null;
        publish(dirty() ? "dirty" : "saved");
        return true;
      } catch (problem) {
        clearTimeout(timer);
        error = problem;
        // Known refusals did not save. Unknown transport failures retain the
        // exact versioned request so Retry cannot duplicate or overwrite work.
        if (problem.status && problem.status < 500) pending = null;
        paused = problem.status === 409;
        publish(paused ? "conflict" : "error");
        throw problem;
      } finally { flight = null; }
    })();
    await flight;
    if (dirty()) return flush();
    return true;
  }
  return { schedule, flush, dirty, busy: () => !!flight,
    stop() { stopped = true; clearTimeout(timer); },
    state: () => ({ error, dirty: dirty(), busy: !!flight }),
    conflict(problem) { paused = true; error = problem; clearTimeout(timer); publish("conflict"); } };
}

// Studio metadata acknowledgements do not reread every source image. Keep the
// known source availability, but move every preview URL to the new document.
function mergeStudioAcknowledgement(previous, acknowledgement) {
  if (!("composition_slots" in acknowledgement)) return acknowledgement;
  const next = { ...previous, ...acknowledgement, composition_projection_stale: true };
  const oldVersion = previous.composition_version || previous.version;
  const version = acknowledgement.composition_version || acknowledgement.version;
  const freshURL = value => typeof value === "string"
    ? value.replace(`/composition/${oldVersion}/`, `/composition/${version}/`) : value;
  for (const key of ["source_urls", "concept_previews"]) {
    next[key] = Object.fromEntries(Object.entries(previous[key] || {}).map(([id, url]) => [id, freshURL(url)]));
  }
  if (previous.manifest) {
    const slots = new Map((acknowledgement.composition_slots || []).map(slot => [slot.id, slot]));
    const findings = acknowledgement.composition_findings
      ? [...(previous.manifest.findings || []).filter(f => !f.context?.phrase && f.rule !== "text_fit"), ...acknowledgement.composition_findings]
      : previous.manifest.findings;
    const outputs = previous.manifest.outputs.map(output => ({ ...output, ...(slots.get(output.id) || {}),
      url: freshURL(output.url), thumbnail_url: freshURL(output.thumbnail_url), download_url: freshURL(output.download_url) }));
    for (const output of outputs) {
      const targets = new Set([output.id, output.product_id, `products/${output.product_id}`,
        `masters/${output.product_id}`, `${output.product_id}/${output.ratio}`, "logo"]);
      output.eligible_for_export = !!output.url && !(findings || []).some(f => f.severity === "blocking" && targets.has(f.item));
    }
    next.manifest = { ...previous.manifest, revision_id: version, findings, outputs,
      complete: !next.composition_issues?.length && outputs.length > 0 && outputs.every(output => output.eligible_for_export) };
  }
  return next;
}

let studioSave = null;
function studioSaveCapture() {
  // A secondary dialog does not change the Studio save queue's editing owner.
  const kind = studioWorkspace?.mode === "framing" ? "frame" : studioWorkspace?.mode || modal;
  if (kind === "inspect") return { kind: "metadata", product: C.products[currentProduct].id,
    market: MARKET[lang], ratio: FMT[currentFormat].ratio,
    description: $("#image-description")?.value || null };
  if (kind === "text" && textEditor) {
    const body = textBody();
    delete body.run_id; delete body.revision_id;
    return { kind: "text", body, values: copy(textEditor.values), all: copy(textEditor.all), pendingStyles: copy(textEditor.pendingStyles || {}) };
  }
  if (kind === "frame" && framingContext) {
    const body = frameBody();
    delete body.run_id; delete body.revision_id; delete body.campaign;
    return { kind: "framing", body, framing: copy(framingDraft) };
  }
  if (kind === "logo" && logoEditor) {
    return { kind: "logo", placement: copy(logoEditor.p),
      asset: copy(logoEditor.asset), replacementFit: copy(logoEditor.replacementFit),
      pendingAsset: logoEditor.pendingAsset };
  }
  return null;
}
function paintStudioSave(state) {
  const owner = studioWorkspace;
  if (!owner) return;
  const controls = $('[data-ws-slot="controls"]', owner.root);
  const failed = ["error", "conflict"].includes(state.phase);
  clearTimeout(owner.savedTimer);
  // Ordinary Studio autosaves are silent; only a real failure needs attention.
  let node = $('[data-studio-recovery]', owner.root);
  if (!failed) {
    node?.remove();
    return;
  }
  if (!node) {
    node = document.createElement("div");
    node.dataset.studioRecovery = "";
    node.className = "ws-save-recovery";
    node.setAttribute("role", "alert");
    controls.prepend(node);
  }
  if (state.phase === "conflict") {
    for (const selector of ["#text-status", "#frame-status", "[data-lg-status]"]) {
      const previewStatus = owner.root.querySelector(selector);
      if (previewStatus) previewStatus.textContent = "";
    }
    for (const selector of ['[data-action="recoverText"]', "#frame-primary"]) {
      const control = owner.root.querySelector(selector);
      if (control) control.hidden = true;
    }
    const campaignNotice = document.querySelector("#project-save-recovery");
    if (campaignNotice) campaignNotice.hidden = true;
    // A focused field can publish the paused conflict during pointerdown.
    // Keep that same Refresh button connected through the eventual click.
    if (node.dataset.savePhase === "conflict") return;
  }
  node.dataset.savePhase = state.phase;
  node.replaceChildren(document.createTextNode(state.phase === "conflict"
    ? MotifSave.conflictMessage
    : "There was a problem saving your changes. Your edits are still here."));
  if (state.phase !== "conflict" && state.error?.message) {
    const detail = document.createElement("p"); detail.textContent = state.error.message; node.append(detail);
  }
  const retry = document.createElement("button"); retry.type = "button";
  retry.className = "btn small"; retry.textContent = state.phase === "conflict" ? "Refresh" : "Try again";
  retry.addEventListener("click", () => state.phase === "conflict" ? MotifSave.refreshPage() : flushStudioSave()); node.append(retry);
}

function beginStudioSave() {
  studioSave?.stop();
  if (!["text", "frame", "logo", "inspect"].includes(modal)) { studioSave = null; return; }
  const owner = studioWorkspace, kind = modal;
  paintStudioSave({ phase: "idle" });
  studioSave = compositionSaveQueue({
    capture: studioSaveCapture,
    prepare: () => kind === "text" ? prepareTextStyleSave() : true,
    changed: paintStudioSave,
    send: async attempt => {
      if (!await saveProject()) {
        if (getSaveState()?.problem?.phase === "conflict")
          throw Object.assign(new Error(MotifSave.conflictMessage), { status: 409 });
        throw new Error("The campaign fields could not be saved. Your image edits are still here.");
      }
      if (studioWorkspace !== owner || (owner.mode === "framing" ? "frame" : owner.mode) !== kind) throw new Error("The editing target changed before saving.");
      if (!attempt.request) {
        const edit = attempt.captured;
        if (edit.kind === "metadata") {
          const id = `${edit.product}/${edit.market}/${edit.ratio.replace(":", "x")}`;
          attempt.request = { description: edit.description, mode: "current", expected_version: model.metadata.creatives[id]?.version || 0 };
          attempt.path = `/api/campaigns/${owner.campaign}/composition/${model.composition_version || model.version}/metadata/${edit.product}/${edit.market}/${edit.ratio.replace(":", "x")}`;
        }
        const body = edit.kind === "logo" ? logoBody({ target: logoSession.target,
          placement: edit.placement, asset: edit.asset, replacementFit: edit.replacementFit,
          mode: edit.pendingAsset ? "replacement" : "placement" }) : copy(edit.body);
        if (edit.kind !== "metadata") attempt.request = { ...body, run_id: model.output_run_id, revision_id: model.composition_version || model.version };
      }
      return api(attempt.path || `/api/campaigns/${owner.campaign}/${attempt.captured.kind}/save`, attempt.request);
    },
    accept: async (data, submitted) => {
      if (studioWorkspace !== owner || (owner.mode === "framing" ? "frame" : owner.mode) !== kind) return;
      // Reconcile campaign fields without restoring an older sent placement
      // over a gesture or sentence entered while the save was running.
      data = mergeStudioAcknowledgement(model, data);
      useData(data, { applied: true });
      if (submitted.kind === "metadata") {
        // Keep a description typed during the save in its existing field.
      } else if (submitted.kind === "text") {
        textEditor.run = data.output_run_id; textEditor.revision = data.composition_version || data.version;
        textEditor.brand = copy((data.output_brief || data.value).brand);
        textEditor.initial = copy(submitted.values);
        for (const field of ["message", "cta"])
          if (submitted.all[field]) textEditor.all[field] = false;
        textEditor.sequence++; textEditor.preview = null; textEditor.recovery = null;
        if (!textEditor.input?.active()) { clearTimeout(textEditor.timer); textEditor.timer = setTimeout(previewText, 0); }
      } else if (submitted.kind === "framing") {
        framingContext.run_id = data.output_run_id; framingContext.revision_id = data.composition_version || data.version;
        framingInitial = JSON.stringify(submitted.framing);
        framePreview = null; framingRecovery = null;
        requestFramePreview(true);
      } else {
        const editor = logoEditor;
        editor.target = logoTarget(data, owner.product, MARKET[lang], FMT[currentFormat].ratio);
        logoSession.target = copy(editor.target);
        editor.initial = copy(submitted.placement);
        editor.savedAsset = copy(submitted.asset);
        if (editor.asset?.reference === submitted.asset?.reference) {
          editor.pendingAsset = false; editor.replacementFit = null;
        }
        editor.valid = null; editor.seq++;
        if (!editor.gesture) editor.validate();
        editor.paint();
      }
      refreshStudioTargets();
      paintWordingAvailability();
    },
  });
}
function scheduleStudioSave(immediate = false) {
  if (!studioSave || !studioWorkspace || studioWorkspace.tearingDown) return;
  if (textEditor?.input?.active() || framingPan?.active() || framingDrawing || logoEditor?.gesture || logoEditor?.uploading) return;
  studioSave.schedule(immediate ? 0 : 1000);
}
async function flushStudioSave() {
  if (!studioSave) return true;
  try { return await studioSave.flush(); } catch { return false; }
}
async function flushCurrentWork() {
  if (typeof flushHandoffDescriptionSave === "function" && !await flushHandoffDescriptionSave()) return false;
  if (!await flushStudioSave()) return false;
  try { return await saveProject(); } catch { return false; }
}

