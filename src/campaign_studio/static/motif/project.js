"use strict";

// One queue for both manual and automatic project saves. Image Apply operations
// remain separate; this controller only persists authored state.
function projectSaveQueue({ capture, restore, send, read, changed, autosave, acknowledged: onAcknowledged, validate, delay = 1200 }) {
  let record = null,
    acknowledged = "",
    acknowledgedContext = "",
    contextRequested = false,
    lastSeen = "",
    pending = null,
    flight = null,
    observed = null,
    timer = null,
    paused = false,
    held = false,
    autosaveSuspended = false,
    phase = "saved",
    message = "",
    problem = null,
    validationMessage = "";
  const listeners = new Set();
  const serialize = (value) => {
    const { ui_context, ...work } = value;
    return JSON.stringify(work);
  };
  const contextOf = value => JSON.stringify(value.ui_context || {});
  const observedWork = value => serialize(value) + (contextRequested ? contextOf(value) : "");
  const snapshot = () => ({
    phase,
    savedAt: record?.saved_at || null,
    message,
    busy: !!held,
    problem,
    validationMessage,
  });
  function validateCurrent() {
    validationMessage = validate?.(capture()) || "";
    return validationMessage;
  }
  function publish(next, text = "") {
    phase = next;
    message = text;
    if (["error", "conflict"].includes(next)) problem = { phase: next, message: text };
    // An older successful payload may leave newer work waiting. Keep its
    // unresolved notice until the acknowledged state covers current work.
    if (next === "saved") problem = null;
    for (const listener of listeners) listener(snapshot());
    changed?.(snapshot());
  }
  function cancelTimer() {
    clearTimeout(timer);
    timer = null;
  }
  function open(campaignId, saved, { restoreState = true } = {}) {
    cancelTimer();
    record = { ...structuredClone(saved), campaignId };
    acknowledged = serialize(saved.authored);
    acknowledgedContext = contextOf(saved.authored);
    contextRequested = false;
    lastSeen = acknowledged;
    pending = null;
    observed = null;
    paused = false;
    held = false;
    autosaveSuspended = false;
    validationMessage = "";
    if (restoreState) restore(structuredClone(saved.authored));
    publish("saved");
  }
  function forget(campaignId) {
    if (record?.campaignId !== campaignId) return false;
    // Confirmed deletion ends this queue's ownership. A late acknowledgement
    // still belongs to the old record and cannot capture or restore Home/new work.
    cancelTimer();
    record = pending = observed = null;
    contextRequested = false;
    held = paused = autosaveSuspended = false;
    validationMessage = "";
    publish("saved");
    return true;
  }
  function isDirty() {
    if (!record) return false;
    const current = capture();
    // The server may have committed a pending payload whose response was lost.
    // Reverting local fields does not acknowledge or undo that unknown commit.
    return !!pending || serialize(current) !== acknowledged ||
      (contextRequested && contextOf(current) !== acknowledgedContext);
  }
  function markChanged(reschedule = false) {
    if (!record) return;
    const current = observedWork(capture());
    // Navigation and dialog clicks report capture opportunities too. They are
    // not new edits and must not retry a failed Save behind a recovery choice.
    if (current === lastSeen && !reschedule) return;
    lastSeen = current;
    validateCurrent();
    cancelTimer();
    if (paused) return publish("conflict", message);
    if (!flight) publish(isDirty() ? "dirty" : "saved");
    if (isDirty() && !held && !autosaveSuspended && !validationMessage)
      timer = setTimeout(() => (autosave || saveCurrent)().catch(() => {}), delay);
  }
  function retainContext() {
    if (!record) return;
    // Close a saved or in-flight editor with its pending work, but do not make
    // a newly inspected image's page/format selection into an authored edit.
    const current = capture(), previous = pending?.authored || record.authored;
    contextRequested = (serialize(current) !== acknowledged || !!previous.ui_context?.modal) &&
      contextOf(current) !== contextOf(previous);
    if (phase === "error" || paused) {
      lastSeen = observedWork(capture());
      return;
    }
    markChanged(true);
  }
  async function save() {
    cancelTimer();
    if (!record) return false;
    if (flight) return flight;
    if (paused || held) return false;
    if (validateCurrent()) {
      if (!paused && !flight) publish("dirty");
      return false;
    }
    if (!pending && !isDirty()) return true;
    const owner = record;
    lastSeen = observedWork(capture());
    pending ||= {
      version: owner.version,
      base: structuredClone(owner.base),
      authored: structuredClone(capture()),
    };
    const request = pending;
    let succeeded = false, rejected = false;
    publish("saving");
    flight = (async () => {
      try {
        const ack = await send(owner.campaignId, request);
        succeeded = true;
        if (record !== owner) return false;
        owner.version = ack.version;
        owner.saved_at = ack.saved_at;
        owner.authored_hash = ack.authored_hash;
        acknowledged = serialize(request.authored);
        acknowledgedContext = contextOf(request.authored);
        if (contextRequested && contextOf(capture()) === acknowledgedContext)
          contextRequested = false;
        pending = null;
        // A completed run may be observed before this Save response arrives.
        // Only the exact acknowledged authored version can advance its base;
        // a competing tab's different version remains a real conflict.
        const current = observed?.version === ack.version ? observed : null;
        if (current) owner.base = structuredClone(current.base);
        owner.authored = structuredClone(current?.authored || request.authored);
        observed = null;
        onAcknowledged?.(ack, owner.campaignId, request.authored, current);
        // Acknowledgement never restores sent values over newer local typing.
        publish(isDirty() ? "dirty" : "saved");
        return !isDirty();
      } catch (error) {
        if (record !== owner) return false;
        if (error.code === "authored_conflict") {
          pending = null;
          paused = true;
          publish("conflict", error.message);
        } else {
          // A transport/lost-response failure retains the exact last request for
          // its bounded acknowledgement retry. Known refusals did not commit.
          if (error.status && error.status < 500) { pending = null; rejected = true; }
          publish("error", error.status ? error.message : "");
        }
        throw error;
      } finally {
        flight = null;
        // A debounce may have fired while this request owned the queue. Drain
        // newer work after success, including a newly opened campaign, but do
        // not turn a failed request into an automatic retry loop.
        // A known refusal did not commit. If the user corrected its fields
        // while it was in flight, save that newer work without replaying the
        // rejected payload. Unchanged invalid input never starts a retry loop.
        const corrected = rejected && record === owner && serialize(capture()) !== serialize(request.authored);
        if ((succeeded || corrected || record !== owner) && record && !paused && isDirty())
          markChanged(true);
      }
    })();
    return flight;
  }
  async function saveCurrent() {
    const owner = record;
    // Save-and-leave must acknowledge the current captured state, rather than
    // treating an older in-flight acknowledgement as the latest Saved state.
    do {
      if (!owner || record !== owner || paused || held) return false;
      if (validateCurrent()) { publish("dirty"); return false; }
      if (await save()) return record === owner && !isDirty();
    } while (record === owner && !paused && !held && isDirty());
    return false;
  }
  function hold() {
    // An image Apply or plan may advance owned source references. Hold this
    // record until its result is reconciled; a new campaign owns a new record.
    if (flight || held) return false;
    held = record;
    cancelTimer();
    return held;
  }
  function release(owner = held) {
    if (held !== owner) return;
    held = false;
    markChanged(true);
  }
  async function loadSaved() {
    if (!record || flight) return false;
    const owner = record;
    const saved = await read(owner.campaignId);
    if (record !== owner) return false;
    open(owner.campaignId, saved);
    return true;
  }
  async function replaceSaved(reviewed) {
    if (!record || flight || !paused) return false;
    // The host presents the fetched saved state first. The same ordinary Save
    // uses that exact reviewed token; any further competing save still conflicts.
    record.version = reviewed.version;
    record.base = structuredClone(reviewed.base);
    pending = null;
    paused = false;
    publish("dirty");
    return save();
  }
  function applied(saved) {
    if (!record || flight) return false;
    // The host has reconciled only the applied target and cleared its matching
    // buffer. Reuse the same queue to persist that new authored state.
    // An image Apply is not permission to overwrite an observed other-tab Save.
    // Keep that token stale so the ordinary conflict path protects its work.
    if (!observed || observed.version === record.version) record.version = saved.version;
    record.base = structuredClone(saved.base);
    if (record.version === saved.version) record.authored = structuredClone(saved.authored || record.authored);
    pending = null;
    paused = false;
    markChanged(true);
    return true;
  }
  function observe(saved) {
    if (!record) return false;
    if (flight) {
      observed = structuredClone(saved);
      return false;
    }
    if (saved.version !== record.version) {
      observed = structuredClone(saved);
      return false;
    }
    record.base = structuredClone(saved.base);
    record.authored = structuredClone(saved.authored || record.authored);
    return true;
  }
  return {
    open,
    forget,
    save,
    saveCurrent,
    hold,
    release,
    markChanged,
    retainContext,
    isDirty,
    loadSaved,
    replaceSaved,
    applied,
    observe,
    authored: () => structuredClone(record?.authored),
    version: () => record?.version,
    suspendAutosave() {
      autosaveSuspended = true;
      cancelTimer();
    },
    resumeAutosave() {
      autosaveSuspended = false;
      if (phase !== "error") markChanged(true);
    },
    getSaveState: snapshot,
    subscribeSaveState(listener) {
      listeners.add(listener);
      listener(snapshot());
      return () => listeners.delete(listener);
    },
    stop: cancelTimer,
  };
}

let projectBuffers = {
  form_drafts: {},
  review_drafts: {},
  feedback: {},
};
let projectFieldDefaults = {};
const PROJECT_FIELDS = new Set([
  "campaign-name", "brand-name", "audience", "scene", "copy-origin", "copy-rights",
  "text-color", "brand-palette",
  "copy-headline", "copy-cta",
  "rename-name", "mw-rule-phrase",
  "image-description",
]);
function projectFormValues(root) {
  const values = {};
  for (const input of root?.querySelectorAll("input[id],textarea[id],select[id]") || []) {
    if (!PROJECT_FIELDS.has(input.id) || input.type === "file") continue;
    values[input.id] = input.type === "checkbox" ? input.checked : input.value;
  }
  return values;
}
function restoreProjectFormValues(root, values = {}) {
  for (const input of root?.querySelectorAll("input[id],textarea[id],select[id]") || []) {
    if (!PROJECT_FIELDS.has(input.id) || !(input.id in values)) continue;
    if (input.type === "checkbox") input.checked = values[input.id];
    else input.value = values[input.id];
  }
}
function projectFormKey() {
  if (modal === "rule") return `rule/${lang}/${ruleIndex}`;
  if (modal === "inspect") return `review/${C.products[currentProduct]?.id}/${lang}/${currentFormat}`;
  if (modal === "rename") return "rename";
  return null;
}
function captureProjectFields(key, root) {
  const values = projectFormValues(root);
  if (!Object.keys(values).length) return;
  const defaults = projectFieldDefaults[key];
  if (defaults && JSON.stringify(values) === JSON.stringify(defaults)) {
    // Reopened raw values may now also be the canonical field defaults. Retain
    // that already-saved record; browsing alone must not create a dirty change.
    if (projectBuffers.form_drafts.fields?.[key] &&
        JSON.stringify(projectBuffers.form_drafts.fields[key]) !== JSON.stringify(values))
      delete projectBuffers.form_drafts.fields[key];
  } else {
    projectBuffers.form_drafts.fields ||= {};
    projectBuffers.form_drafts.fields[key] = values;
  }
}
function restoreProjectControls({ preserveModal = false } = {}) {
  const contexts = [];
  if (page === "campaign") contexts.push([`brief/${tab}/${lang}`, $("#main")]);
  const key = projectFormKey();
  if (key && !preserveModal) contexts.push([key, imageEditorRoot()]);
  for (const [name, root] of contexts) {
    projectFieldDefaults[name] = projectFormValues(root);
    restoreProjectFormValues(root, projectBuffers.form_drafts.fields?.[name]);
  }
}
function captureProject() {
  const forms = projectBuffers.form_drafts;
  if (page === "campaign") {
    captureProjectFields(`brief/${tab}/${lang}`, $("#main"));
  }
  const formKey = projectFormKey();
  if (formKey) captureProjectFields(formKey, imageEditorRoot());
  if (modal === "inspect" && forms.fields?.[formKey]) {
    const product = C.products[currentProduct]?.id;
    if (product) {
      const key = `${product}/${MARKET[lang]}/${FMT[currentFormat].ratio.replace(":", "x")}`;
      projectBuffers.review_drafts[key] = {
        revision_id: model.manifest?.revision_id,
        metadata_snapshot_id: model.metadata?.snapshot_id,
        values: projectFormValues(imageEditorRoot()),
      };
    }
  }
  if (forms.fields && !Object.keys(forms.fields).length) delete forms.fields;
  return {
    brief: copy(draft),
    ...copy(projectBuffers),
    ui_context: {
      page, tab, language: lang, currentFormat,
      reviewTab: typeof reviewTab === "undefined" ? "products" : reviewTab,
      productId: C.products[currentProduct]?.id || null,
      modal: ["rule", "rename", "inspect"].includes(modal) ? modal : null,
      ruleIndex,
    },
  };
}
function isImageEditor(kind = modal) {
  return ["text", "frame", "logo"].includes(kind);
}
function restoreProject(value) {
  draft = copy(value.brief);
  for (const key of Object.keys(projectBuffers)) projectBuffers[key] = copy(value[key] || {});
  const context = value.ui_context || {};
  if (context.tab) tab = context.tab;
  if (context.language) lang = context.language;
  if (Number.isInteger(context.currentFormat)) currentFormat = context.currentFormat;
  if (typeof reviewTab !== "undefined" && ["products", "product", "compare", "overview", "activity"].includes(context.reviewTab))
    reviewTab = context.reviewTab;
  const index = draft.products?.findIndex?.((p) => p.id === context.productId);
  if (index >= 0) currentProduct = index;
  if (context.reviewTab === "product" && index >= 0 && typeof selected !== "undefined") {
    selected[0] = index;
    if (Number.isInteger(context.currentFormat)) formats[0] = context.currentFormat;
  }
}

// The page, authoring dialogs, and autosave all use this one controller.
let projectQueue = null,
  projectOwner = null,
  projectViews = [],
  projectConflict = null;
const projectListeners = new Set();
function getSaveState() {
  return projectQueue && page !== "library" && draft?.campaign_id === projectOwner
    ? projectQueue.getSaveState() : null;
}
function publishProjectState() {
  for (const listener of projectListeners) listener(getSaveState());
  if (page === "production" && typeof ensureProductionPlan === "function") queueMicrotask(ensureProductionPlan);
}
function subscribeSaveState(listener) {
  projectListeners.add(listener);
  listener(getSaveState());
  return () => projectListeners.delete(listener);
}
function sameProjectInputs(authored, planned) {
  const color = value => typeof value === "string" ? MotifBriefFlow.normalizeHex(value) || value : value;
  const clean = value => {
    const brief = copy(value);
    // Completed hex shorthand has the same meaning as the server's normalized
    // color. Compare copies only; unfinished raw work stays exactly as entered.
    if (brief.brand && typeof brief.brand === "object") {
      if (Array.isArray(brief.brand.palette)) brief.brand.palette = brief.brand.palette.map(color);
      brief.brand.text_color = color(brief.brand.text_color);
    }
    for (const product of Array.isArray(brief.products) ? brief.products : []) {
      // New products omit framing; the schema's empty default adds no edits.
      if (product.framing === undefined) product.framing = {};
      // Uploaded finished images may omit this optional box. Planning writes
      // null; both mean no supplied subject bounds, not a source change.
      if (product.source?.role === "finished_scene" && product.source.subject_box === undefined)
        product.source.subject_box = null;
      for (const formats of Object.values(product?.framing || {}))
        for (const framing of Object.values(formats || {}))
          for (const placement of Object.values(framing?.text_placements || {}))
            if (placement && typeof placement === "object" && "color" in placement) placement.color = color(placement.color);
    }
    // Planning adds verified source-reuse metadata. Its absence from raw
    // authored work is not a request to change the produced imagery.
    if (authored.replay == null) brief.replay = null;
    return JSON.stringify(brief, (_key, item) => item && !Array.isArray(item) && typeof item === "object"
      ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
  };
  return clean(authored) === clean(planned);
}
function initializeProject(saved) {
  projectOwner = draft.campaign_id;
  projectFieldDefaults = {};
  projectQueue ||= projectSaveQueue({
    capture: captureProject,
    restore: restoreProject,
    send: (id, body) => api(`/api/motif/campaigns/${encodeURIComponent(id)}/authored`, body),
    read: (id) => api(`/api/motif/campaigns/${encodeURIComponent(id)}/authored`),
    changed: publishProjectState,
    autosave: saveProject,
    validate: authored => MotifTextLimits.authoredIssue(authored),
    acknowledged(ack, id, submitted, current) {
      if (draft?.campaign_id !== id) return;
      if (current) {
        reconcileAppliedBrief(draft, submitted.brief, current.authored.brief);
        reconcileAppliedForms(submitted, current.authored);
        model.project.base = copy(current.base);
      }
      model.version = ack.version;
      Object.assign(model.project, ack);
      model.project.authored = copy(current?.authored || submitted);
      refreshCampaignIdentity(model.project.authored.brief.name, model.project.authored.brief.brand?.name || "");
      // Save changes plan readiness, never the accepted image selection.
      if (model.plan_brief) model.unplanned_draft = !sameProjectInputs(submitted.brief, model.plan_brief)
        // A Save receipt does not re-admit a pending request. Only the server's
        // plan refresh may clear an existing strict admission refusal there.
        || (model.run?.state !== "complete" && !!model.unplanned_draft);
      // This tab's own file acknowledgement advances only the authored guard.
      // It never adopts another image revision or reuses an old preview token.
      if (logoSession?.target.campaignId === id) logoSession.target.draftVersion = ack.version;
      dirty = JSON.stringify(draft) !== JSON.stringify(submitted.brief);
      if (typeof refreshBriefFlow === "function") refreshBriefFlow();
      if (typeof refreshBriefSources === "function")
        refreshBriefSources(ack.version, current?.authored.brief || submitted.brief);
    },
  });
  projectQueue.open(projectOwner, saved);
}
function forgetProject(owner) {
  if (projectOwner !== owner) return;
  projectOwner = null;
  projectConflict = null;
  projectFieldDefaults = {};
  for (const key of Object.keys(projectBuffers)) projectBuffers[key] = {};
  projectQueue?.forget(owner);
}
function reconcileAppliedBrief(local, before, after) {
  const advance = (target, old, current, fields) => {
    if (!target || !old || !current) return;
    for (const field of fields)
      if (JSON.stringify(target[field]) === JSON.stringify(old[field])) {
        if (field in current) target[field] = copy(current[field]);
        else delete target[field];
      }
  };
  advance(local.brand, before.brand, after.brand, ["logo_path", "logo_provenance"]);
  for (const [id, source] of Object.entries(local.replay?.sources || {}))
    advance(source, before.replay?.sources?.[id], after.replay?.sources?.[id], ["path"]);
  for (const market of Object.keys(local.markets || {})) {
    advance(local.markets[market], before.markets?.[market], after.markets?.[market], ["phrase_rules"]);
    advance(local.markets[market]?.copy, before.markets?.[market]?.copy, after.markets?.[market]?.copy, ["message", "cta"]);
  }
  for (const product of local.products || []) {
    const old = before.products?.find((p) => p.id === product.id),
      current = after.products?.find((p) => p.id === product.id);
    advance(product, old, current, ["framing"]);
    if (old && current && (product.copy_overrides === undefined || product.copy_overrides && typeof product.copy_overrides === "object" && !Array.isArray(product.copy_overrides))) {
      const overrides = product.copy_overrides ??= {};
      for (const market of new Set([...Object.keys(old.copy_overrides || {}), ...Object.keys(current.copy_overrides || {})])) {
        const previous = old.copy_overrides?.[market] || {}, next = current.copy_overrides?.[market] || {}, values = overrides[market] === undefined ? {} : overrides[market];
        if (!values || typeof values !== "object" || Array.isArray(values)) continue;
        advance(values, previous, next, ["message", "cta"]);
        if (Object.keys(values).length) overrides[market] = values;
        else delete overrides[market];
      }
    }
    advance(product.source, old?.source, current?.source, ["path"]);
  }
}
function receiveProject(data, previous, { applied = false, reset = false } = {}) {
  if (!data.project) return;
  if (reset || projectOwner !== data.value.campaign_id) {
    draft = copy(data.project.authored.brief);
    initializeProject(data.project);
    return;
  }
  // Only an acknowledged own action or an unchanged authored guard can advance
  // this tab's base. A newly observed other-tab save must still conflict.
  const acknowledged = projectQueue.authored();
  if (applied || projectQueue.observe(data.project)) {
    reconcileAppliedBrief(draft, acknowledged.brief, data.project.authored.brief);
    reconcileAppliedForms(acknowledged, data.project.authored);
    if (applied) projectQueue.applied(data.project);
  }
}
function reconcileAppliedForms(previous, current) {
  const resets = projectBuffers.form_drafts.brand_color_defaults;
  if (Array.isArray(resets)) {
    const identity = target => target && ["product_id", "market", "ratio"].every(key => typeof target[key] === "string")
      ? JSON.stringify([target.product_id, target.market, target.ratio]) : null;
    const targets = authored => Array.isArray(authored.form_drafts?.brand_color_defaults)
      ? authored.form_drafts.brand_color_defaults : [];
    const retained = new Set(targets(current).map(identity));
    const retired = new Set(targets(previous).map(identity).filter(key => key !== null && !retained.has(key)));
    // An accepted Studio color can retire an earlier Brand reset. Remove that
    // old intent from this tab too, so its next Save cannot bring it back.
    projectBuffers.form_drafts.brand_color_defaults = resets.filter(target => !retired.has(identity(target)));
  }
  const before = previous.form_drafts?.fields || {}, after = current.form_drafts?.fields || {};
  for (const [name, values] of Object.entries(projectBuffers.form_drafts.fields || {})) {
    if (!name.startsWith("brief/")) continue;
    for (const field of ["copy-headline", "copy-cta"])
      if (field in values && values[field] === before[name]?.[field] && field in (after[name] || {}))
        values[field] = after[name][field];
  }
}
async function saveProject({ reviewed } = {}) {
  if (!projectQueue) return false;
  const owner = projectOwner;
  if (reviewed && !(await projectQueue.replaceSaved(reviewed))) return false;
  const saved = await projectQueue.saveCurrent();
  if (saved && owner === projectOwner) dirty = false;
  return saved;
}
function projectChanged() {
  if (!projectQueue || draft?.campaign_id !== projectOwner) return;
  projectQueue.markChanged();
}
function destroyProjectViews() {
  for (const view of projectViews) view.destroy();
  projectViews = [];
}
function mountProjectViews() {
  destroyProjectViews();
  const host = {
    getSaveState, subscribeSaveState, onSave: saveProject,
    onReviewConflict: reviewProjectConflict,
  };
  const recovery = $("#project-save-recovery");
  if (recovery) projectViews.push(MotifSave.mountRecovery(recovery, {
    ...host,
    onResolved() {
      const target = $("#main h1") || $("#main");
      target?.setAttribute("tabindex", "-1");
      target?.focus({ preventScroll: true });
    },
  }));
  const mount = (root) => {
    if (root) projectViews.push(MotifSave.mount(root, host));
  };
  if (["rule", "rename", "inspect"].includes(modal)) {
    const header = $("#overlay .modal-head"), close = header?.querySelector("button.close");
    if (close) {
      let actions = header.querySelector(".mf-save-modal-actions");
      if (!actions) {
        actions = document.createElement("div");
        actions.className = "mf-save-modal-actions";
        const root = document.createElement("div");
        root.dataset.projectSave = "modal";
        header.insertBefore(actions, close);
        actions.append(root, close);
      }
      mount(actions.querySelector("[data-project-save]"));
    }
  }
  publishProjectState();
}
function clearProjectTarget(kind = modal) {
  const key = projectFormKey();
  if (key && projectBuffers.form_drafts.fields) delete projectBuffers.form_drafts.fields[key];
  if (kind === "inspect")
    delete projectBuffers.review_drafts[`${C.products[currentProduct]?.id}/${MARKET[lang]}/${FMT[currentFormat].ratio.replace(":", "x")}`];
}
async function withProjectApply(operation) {
  const owner = await saveProject() && projectQueue.hold();
  if (!owner) throw new Error(projectQueue?.getSaveState().validationMessage || "Keep this tab open and finish saving the current work before applying images.");
  try { return await operation(); }
  finally { projectQueue.release(owner); }
}

async function reviewProjectConflict() {
  if (projectConflict || !projectQueue) return;
  captureProject();
  const owner = projectOwner, overlay = $("#overlay"),
    previous = [...overlay.children].map((node) => [node, node.inert]),
    focused = document.activeElement,
    appInert = $("#app").inert, skipInert = $(".skip").inert;
  previous.forEach(([node]) => { node.inert = true; });
  $("#app").inert = true;
  $(".skip").inert = true;
  const host = document.createElement("div");
  overlay.append(host);
  projectConflict = host;
  let reviewed = null, working = false;
  const button = (label, action, kind = "") =>
    `<button type="button" class="btn ${kind}" data-project-choice="${action}">${label}</button>`;
  function close() {
    host.remove();
    projectConflict = null;
    previous.forEach(([node, inert]) => { node.inert = inert; });
    $("#app").inert = appInert;
    $(".skip").inert = skipInert;
    focused?.isConnected && focused.focus();
  }
  function show(step = "review") {
    let title = "Choose which work to keep.",
      body = `<p>Your edits are still here. Another tab saved changes to this campaign. Automatic saving is paused.</p><div class="rule"></div><div class="data-row"><span>Current saved campaign</span><b>${esc(reviewed?.project.authored.brief.name || "Untitled campaign")}</b></div><div class="data-row"><span>Saved</span><b>${esc(reviewed?.project.saved_at ? new Date(reviewed.project.saved_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "Not recorded")}</b></div><p style="margin-top:16px">Keep editing here, load the current saved work, or deliberately replace it with this tab’s current work.</p>`,
      footer = button("Keep editing here", "keep") + button("Use saved work", "load-confirm") + button("Save this tab’s work", "replace-confirm", "primary");
    if (step === "load-confirm") {
      title = "Use the current saved work?";
      body = "<p>This replaces this tab’s current fields and pending edits with the work you just reviewed. Unsaved changes in this tab will be discarded.</p>";
      footer = button("Back", "review") + button("Use saved work", "load", "primary");
    } else if (step === "replace-confirm") {
      title = "Save this tab’s work?";
      body = "<p>This replaces the current saved authored work with this tab’s fields and pending edits. It does not apply image edits or change production records.</p><p style=\"margin-top:14px\">If the saved work changes again, saving will pause so you can review it.</p>";
      footer = button("Back", "review") + button("Replace saved work", "replace", "primary");
    } else if (step === "unavailable") {
      title = "The saved work is unavailable.";
      body = "<p>Your current edits are still here. Keep editing, or try checking the saved work again. Nothing has been replaced.</p>";
      footer = button("Keep editing here", "keep") + button("Try again", "read", "primary");
    }
    host.innerHTML = dialog(title, body, footer).replace('data-action="close"', 'data-project-choice="keep"');
    host.querySelector(".modal-foot button")?.focus();
  }
  async function read() {
    working = true;
    try {
      reviewed = await api(`/api/motif/campaigns/${encodeURIComponent(owner)}`);
      if (owner === projectOwner) show();
    } catch { show("unavailable"); }
    finally { working = false; }
  }
  host.addEventListener("click", async (event) => {
    const control = event.target.closest("[data-project-choice]");
    if (!control) return;
    event.stopPropagation();
    if (working) return;
    const choice = control.dataset.projectChoice;
    if (choice === "keep") return close();
    if (choice === "read") return read();
    if (!["load", "replace"].includes(choice)) return show(choice);
    working = true;
    try {
      if (choice === "replace") {
        if (await saveProject({ reviewed: reviewed.project })) close();
        else show("unavailable");
      } else {
        const fresh = await api(`/api/motif/campaigns/${encodeURIComponent(owner)}`);
        const identity = (data) => JSON.stringify([data.project.version, data.project.authored_hash, data.project.base]);
        if (identity(fresh) !== identity(reviewed)) { reviewed = fresh; show(); return; }
        close();
        closeModal();
        useData(fresh, { reset: true });
        render();
        await reopenProjectEditor(fresh.project.authored.ui_context);
      }
    } catch { show("unavailable"); }
    finally { working = false; }
  });
  host.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (!working) close();
    } else if (event.key === "Tab") {
      event.preventDefault(); event.stopPropagation();
      const controls = [...host.querySelectorAll("button:not(:disabled)")];
      const index = controls.indexOf(document.activeElement);
      controls[(index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length]?.focus();
    }
  });
  await read();
}

async function reopenProjectEditor(context = {}) {
  if (!context || !draft || context.page !== page) return;
  const product = draft.products?.findIndex((p) => p.id === context.productId);
  if (product >= 0) currentProduct = product;
  if (context.language) lang = context.language;
  if (Number.isInteger(context.currentFormat)) currentFormat = context.currentFormat;
  viewModel();
  const kind = context.modal;
  if (["rule", "rename", "inspect"].includes(kind)) {
    ruleIndex = context.ruleIndex ?? -1;
    const key = kind === "rule" ? `rule/${lang}/${ruleIndex}` : kind === "rename" ? "rename" : `review/${C.products[currentProduct]?.id}/${lang}/${currentFormat}`;
    if (projectBuffers.form_drafts.fields?.[key]) openModal(kind);
  }
}

// Capture after the ordinary field handler has updated its live canonical value.
// The Save component itself contains no timer or persistence implementation.
if (typeof document !== "undefined") {
  for (const type of ["input", "change"])
    document.addEventListener(type, event => {
      if (!(isImageEditor() || modal === "product") || !imageEditorRoot().contains(event.target))
        queueMicrotask(() => projectChanged());
    });
}
