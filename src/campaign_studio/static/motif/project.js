"use strict";

const PROJECT_PROJECTION_FIELDS = ["composition_version", "composition_issues", "source_urls", "output_brief", "manifest", "output_run_id", "editable_products", "logo_editable_products", "can_edit", "can_describe", "metadata", "wording_issue", "rule_withheld_outputs", "metrics", "file_integrity", "plan", "plan_brief", "run", "unplanned_draft", "retry_plan", "notice", "run_attempts", "concept_previews"];

// Campaign forms save through one version-checked queue. Studio uses the same
// current document through focused metadata patches.
function projectSaveQueue({ capture, restore, send, changed, autosave, acknowledged: onAcknowledged, validate, canAutosave = () => true, delay = 2000 }) {
  let record = null,
    acknowledged = "",
    acknowledgedContext = "",
    contextRequested = false,
    lastSeen = "",
    pending = null,
    flight = null,
    observed = null,
    observedSourceCompletion = false,
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
    // Fresh imports and bundled campaigns can omit empty browser buffers.
    // Opening their controls supplies those empty objects locally; that is not
    // an edit and must not schedule a save during source generation.
    for (const key of ["form_drafts", "review_drafts", "feedback"]) {
      const buffer = work[key];
      if (buffer && typeof buffer === "object" && !Array.isArray(buffer) &&
          Object.keys(buffer).length === 0) delete work[key];
    }
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
    observedSourceCompletion = false;
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
    observedSourceCompletion = false;
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
    if (isDirty() && !held && !autosaveSuspended && !validationMessage && canAutosave())
      timer = setTimeout(() => {
        if (canAutosave()) (autosave || saveCurrent)().catch(() => {});
      }, delay);
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
        const current = observed && (observed.version === ack.version ||
          (observedSourceCompletion && observed.version !== request.version &&
            serialize(observed.authored) === serialize(request.authored)))
          ? observed : null;
        owner.version = current?.version || ack.version;
        owner.saved_at = current?.saved_at || ack.saved_at;
        owner.authored_hash = current?.authored_hash || ack.authored_hash;
        acknowledged = serialize(request.authored);
        acknowledgedContext = contextOf(request.authored);
        if (contextRequested && contextOf(capture()) === acknowledgedContext)
          contextRequested = false;
        pending = null;
        // A source result can finish after this save commits but before its
        // response arrives. Advance only when its authored content is exactly
        // this acknowledged request; a different producer edit stays a conflict.
        if (current) owner.base = structuredClone(current.base);
        owner.authored = structuredClone(current?.authored || request.authored);
        observed = null;
        observedSourceCompletion = false;
        const receipt = current ? { ...ack, version: current.version,
          saved_at: current.saved_at || ack.saved_at, authored_hash: current.authored_hash || ack.authored_hash,
          project: structuredClone(current) } : ack;
        if (current && current.version !== ack.version)
          for (const key of PROJECT_PROJECTION_FIELDS) delete receipt[key];
        onAcknowledged?.(receipt, owner.campaignId, request.authored, current);
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
  function applied(saved) {
    if (!record || flight || paused) return false;
    // Apply has already saved the image and its settings on the server.
    // Acknowledge that state without sending another authored save.
    // An image Apply is not permission to overwrite an observed other-tab Save.
    // Keep that token stale so the ordinary conflict path protects its work.
    if (!observed || observed.version === record.version) record.version = saved.version;
    record.base = structuredClone(saved.base);
    if (record.version === saved.version) {
      record.authored = structuredClone(saved.authored || record.authored);
      acknowledged = serialize(record.authored);
      acknowledgedContext = contextOf(record.authored);
      record.saved_at = saved.saved_at || record.saved_at;
      record.authored_hash = saved.authored_hash || record.authored_hash;
    }
    pending = null;
    paused = false;
    lastSeen = observedWork(capture());
    publish(isDirty() ? "dirty" : "saved");
    return true;
  }
  function observe(saved, { sourceCompletion = false } = {}) {
    if (!record) return false;
    if (flight) {
      observed = structuredClone(saved);
      observedSourceCompletion = sourceCompletion;
      return false;
    }
    if (saved.version !== record.version) {
      // Finishing source generation changes the saved-state guard without
      // changing the producer's settings. Acknowledge that read-only result,
      // preserving any newer local typing. Never adopt different saved edits.
      if (sourceCompletion && !pending && !paused && !held &&
          serialize(saved.authored) === acknowledged) {
        record.version = saved.version;
        record.saved_at = saved.saved_at;
        record.authored_hash = saved.authored_hash;
        record.base = structuredClone(saved.base);
        record.authored = structuredClone(saved.authored);
        observed = null;
        observedSourceCompletion = false;
        publish(isDirty() ? "dirty" : "saved");
        return true;
      }
      observed = structuredClone(saved);
      observedSourceCompletion = sourceCompletion;
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
  if (typeof captureProductInputs === "function") captureProductInputs();
  if (modal === "rule" && ruleIndex >= 0 && $("#mw-rule-phrase")) {
    const rule = draft.markets[MARKET[lang]]?.phrase_rules?.[ruleIndex];
    if (rule && rule.phrase !== $("#mw-rule-phrase").value) {
      const previousRules = copy(draft.markets[MARKET[lang]].phrase_rules);
      rule.phrase = $("#mw-rule-phrase").value;
      if (typeof requeueAllowedCampaignCopy === "function") requeueAllowedCampaignCopy(previousRules);
    }
  }
  if (modal === "rename" && $("#rename-name")) draft.name = $("#rename-name").value;
  if (page === "campaign") {
    captureProjectFields(`brief/${tab}/${lang}`, $("#main"));
  }
  const formKey = projectFormKey();
  if (formKey && modal !== "inspect") captureProjectFields(formKey, imageEditorRoot());
  if (forms.fields && !Object.keys(forms.fields).length) delete forms.fields;
  const captured = {
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
  return typeof safeCampaignCopyCapture === "function" ? safeCampaignCopyCapture(captured) : captured;
}
function isImageEditor(kind = modal) {
  return ["text", "frame", "logo"].includes(kind);
}
function restoreProject(value, { restoreContext = true } = {}) {
  draft = copy(value.brief);
  for (const key of Object.keys(projectBuffers)) projectBuffers[key] = copy(value[key] || {});
  if (!restoreContext) return;
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
  projectViews = [];
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
    changed: publishProjectState,
    autosave: saveProject,
    canAutosave: () => !["text", "frame", "logo", "inspect"].includes(modal),
    validate: authored => MotifTextLimits.authoredIssue(authored),
    acknowledged(ack, id, submitted, current) {
      if (draft?.campaign_id !== id) return;
      if (current) {
        reconcileAppliedBrief(draft, submitted.brief, current.authored.brief);
        reconcileAppliedForms(submitted, current.authored);
        model.project.base = copy(current.base);
      }
      model.version = ack.version;
      Object.assign(model.project, ack.project || Object.fromEntries(
        ["version", "saved_at", "authored_hash", "base"].filter(key => key in ack).map(key => [key, ack[key]])));
      for (const key of PROJECT_PROJECTION_FIELDS)
        if (key in ack) model[key] = ack[key];
      if ("concept_previews" in ack) model.concept_preview_version = ack.version;
      if (typeof viewModel === "function") viewModel();
      model.project.authored = copy(current?.authored || submitted);
      refreshCampaignIdentity(model.project.authored.brief.name, model.project.authored.brief.brand?.name || "");
      // Save changes plan readiness, never the accepted image selection.
      if (!("unplanned_draft" in ack) && model.plan_brief) model.unplanned_draft = !sameProjectInputs(submitted.brief, model.plan_brief)
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
  advance(local, before, after, ["schema_version", "campaign_id", "name", "audience", "creative_direction", "copy_provenance", "markets", "products", "replay"]);
  advance(local.brand, before.brand, after.brand, ["id", "name", "logo_path", "logo_provenance", "palette", "text_color"]);
  for (const [id, source] of Object.entries(local.replay?.sources || {}))
    advance(source, before.replay?.sources?.[id], after.replay?.sources?.[id], ["path"]);
  for (const market of Object.keys(local.markets || {})) {
    advance(local.markets[market], before.markets?.[market], after.markets?.[market], ["phrase_rules"]);
    advance(local.markets[market]?.copy, before.markets?.[market]?.copy, after.markets?.[market]?.copy, ["message", "cta"]);
  }
  for (const product of local.products || []) {
    const old = before.products?.find((p) => p.id === product.id),
      current = after.products?.find((p) => p.id === product.id);
    advance(product, old, current, ["name", "description", "framing"]);
    if (old && current && (old.copy_overrides || current.copy_overrides || product.copy_overrides) && (product.copy_overrides === undefined || product.copy_overrides && typeof product.copy_overrides === "object" && !Array.isArray(product.copy_overrides))) {
      const overrides = product.copy_overrides ??= {};
      for (const market of new Set([...Object.keys(old.copy_overrides || {}), ...Object.keys(current.copy_overrides || {})])) {
        const previous = old.copy_overrides?.[market] || {}, next = current.copy_overrides?.[market] || {}, values = overrides[market] === undefined ? {} : overrides[market];
        if (!values || typeof values !== "object" || Array.isArray(values)) continue;
        advance(values, previous, next, ["message", "cta"]);
        if (Object.keys(values).length || Object.hasOwn(current.copy_overrides || {}, market)) overrides[market] = values;
        else delete overrides[market];
      }
    }
    advance(product.source, old?.source, current?.source, ["role", "path", "provenance", "description", "imagery_absent", "subject_box"]);
  }
}
function receiveProject(data, previous, { applied = false, reset = false, sourceCompletion = null } = {}) {
  if (!data.project) return;
  if (reset || projectOwner !== data.value.campaign_id) {
    draft = copy(data.project.authored.brief);
    initializeProject(data.project);
    return;
  }
  // Only an acknowledged own action or an unchanged authored guard can advance
  // this tab's base. A newly observed other-tab save must still conflict.
  const acknowledged = projectQueue.authored();
  const completedSourceRun = !!sourceCompletion && sourceCompletion === data.run?.id &&
    sourceCompletion === previous?.run?.id;
  if (applied || projectQueue.observe(data.project, { sourceCompletion: completedSourceRun })) {
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
    for (const field of Object.keys(values))
      if (values[field] === before[name]?.[field]) {
        if (field in (after[name] || {})) values[field] = after[name][field];
        else delete values[field];
      }
  }
}
async function saveProject() {
  if (!projectQueue) return false;
  if (typeof confirmCampaignCopyChanges === "function" && !await confirmCampaignCopyChanges()) return false;
  const owner = projectOwner;
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
    onRefresh: MotifSave.refreshPage,
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
    if (root) projectViews.push(MotifSave.mountRecovery(root, host));
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
      if (!["text", "frame", "logo", "inspect"].includes(modal) || !imageEditorRoot().contains(event.target))
        queueMicrotask(() => projectChanged());
    });
}
