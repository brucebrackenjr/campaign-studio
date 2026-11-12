"use strict";

// A page-local correction and return target. Accepted rules and export admission
// come from the server; this controller never matches text or persists clearance.
let wordingCaseId = null;
let wordingReturn = null;
let wordingCheckPhase = null;
let draftWording = null;
function currentWordingView() {
  const proposal = page === "production" ? activeBlockedProposal() : null;
  const findings = proposal?.findings || (!model?.manifest ? model?.plan?.findings : null);
  const matches = findings?.filter(f => f.severity === "blocking" && f.context?.phrase?.matches?.length) || [];
  const issue = proposal || !model?.manifest
    ? matches.length ? { code: "prohibited_phrase", matches } : null
    : model.wording_issue;
  const view = MotifWording.fromIssue(issue);
  if (wordingCheckPhase?.campaign === C.id && wordingCheckPhase.owner === model) view.phase = wordingCheckPhase.phase;
  if (view.phase === "rules-pending" && pendingRuleForm()) view.phase = "rules-invalid";
  const editing = page === "review" && reviewTab === "product";
  const local = editing ? view.cases.findIndex(item => item.target.productId === C.products[currentProduct]?.id && item.target.market === MARKET[lang]) : -1;
  let index = local >= 0 ? local : view.cases.findIndex(item => item.id === wordingCaseId);
  if (index < 0) index = 0;
  wordingCaseId = view.cases[index]?.id || null;
  return { ...view, index, surface: page === "review" && reviewTab === "product" ? "studio-editor" : page, hasImages: !!model?.manifest && !proposal };
}
function pendingRuleForm() {
  const target = model?.wording_issue?.pending_rule_targets?.find(item => Number.isInteger(item.rule_index));
  return target || null;
}
function wordingNotice() {
  return `<div data-wording-slot>${MotifWording.notice(currentWordingView())}</div>`;
}
function wordingExportBlocked() {
  return currentWordingView().phase !== "clear";
}
function currentLayoutFindings() {
  return (model?.manifest?.findings || []).filter(finding => finding.severity === "blocking" &&
    finding.rule === "text_fit");
}
function layoutFindingsFor(output) {
  if (!output) return [];
  return currentLayoutFindings().filter(finding => finding.item === output.id ||
    finding.context?.product_id && finding.context.product_id === output.product_id && finding.context.market === output.market &&
    finding.context?.ratio === output.ratio);
}
function exportBlocked() {
  return !!model?.logo_issue || wordingExportBlocked() || currentLayoutFindings().length > 0;
}
function paintWordingAvailability() {
  const content = MotifWording.notice(currentWordingView());
  for (const slot of $$('[data-logo-notice]')) slot.innerHTML = handoffLogoNotice();
  const slots = $$('[data-wording-slot]');
  const focused = slots.some(slot => slot.contains(document.activeElement));
  for (const slot of slots) slot.innerHTML = content;
  for (const slot of $$('[data-layout-slot]')) slot.innerHTML = layoutNotice();
  if (typeof textEditor !== "undefined" && textEditor && typeof paintText === "function") paintText();
  for (const button of $$('[data-wording-export]')) button.disabled = exportBlocked() || button.dataset.wordingExport === "unavailable";
  if (focused) focusWordingControl();
}
function focusWordingControl(action) {
  const control = $$('[data-mw]').find(el => el.dataset.mw === action)
    || $('[data-wording-slot] [data-mw]');
  const destination = control || $('[data-action="plan"]:not(:disabled)') || $('#main h1');
  if (!control && destination?.tagName === "H1") destination.setAttribute("tabindex", "-1");
  destination?.focus({ preventScroll: true });
}
function keepWordingOrigin(kind, control) {
  wordingReturn = {
    campaign: C.id, kind, page, tab, reviewTab, selected: [...selected], formats: [...formats],
    currentProduct, currentFormat, lang, filter, pageNum, scroll: window.scrollY,
    caseId: wordingCaseId, control,
  };
}
function returnFromWording(kind) {
  const origin = wordingReturn;
  if (!origin || origin.kind !== kind) return;
  wordingReturn = null;
  if (C.id !== origin.campaign) return;
  ({ page, tab, reviewTab, currentProduct, currentFormat, lang, filter, pageNum } = origin);
  selected = origin.selected; formats = origin.formats; wordingCaseId = origin.caseId;
  updateURL(); render();
  window.scrollTo(0, origin.scroll);
  queueMicrotask(() => {
    if (C.id !== origin.campaign || page !== origin.page || modal) return;
    focusWordingControl(origin.control);
  });
}
function focusMatchedWording(control, terms) {
  if (!control) return;
  control.focus();
  // Selection is a convenience only. The server's Unicode/case matcher remains
  // authoritative when a normalized match has no exact display-text offset.
  const value = control.value.toLocaleLowerCase();
  const term = terms.find(item => value.includes(item.toLocaleLowerCase()));
  if (term) { const start = value.indexOf(term.toLocaleLowerCase()); control.setSelectionRange(start, start + term.length); }
}
function draftWordingTarget(session) {
  const { productId, market, field, origin } = session.item.target;
  const product = draft.products.find(p => p.id === productId);
  if (!product || !["message", "cta"].includes(field)) return null;
  const override = product.copy_overrides?.[market];
  const overridden = Object.prototype.hasOwnProperty.call(override || {}, field);
  if (origin === "product_override" && overridden) return override;
  if (origin === "market_shared" && !overridden) return draft.markets[market]?.copy;
  return null;
}
function draftWordingView() {
  const session = draftWording, item = session.item, cta = item.target.field === "cta";
  const scope = item.target.origin === "product_override"
    ? "This product’s wording." : "Campaign wording. Updates every product for this language.";
  return dialog(cta ? "Edit the call to action." : "Edit the headline.",
    `<h3>${esc(item.product)}</h3><p class="small muted">${esc(item.language)}</p>` +
    (cta ? input("Call to action", "draft-wording-value", session.value)
      : area("Headline", "draft-wording-value", session.value)).replace('id="draft-wording-value"', `id="draft-wording-value" data-length-purpose="${cta ? "cta" : "headline"}"`) +
    MotifWording.fieldMessage(item.terms) + `<p class="small muted">${scope}</p>`,
    `<span class="small muted">Updates the saved wording and previews.</span>` +
    btn("Cancel", "close") + btn("Save wording", "applyDraftWording", "primary"));
}
function openDraftWording(item, control) {
  const session = { campaign: C.id, owner: draft, item, value: item.value, saving: false };
  const target = draftWordingTarget(session);
  if (!target || target[item.target.field] !== item.value)
    throw new Error("This wording changed. Review the updated plan before correcting it.");
  session.expected = item.value;
  keepWordingOrigin("draft-wording", control);
  page = "campaign"; tab = "copy";
  lang = item.target.market === "es-MX" ? "es" : "en";
  updateURL(); render();
  draftWording = session;
  openModal("draft-wording");
  focusMatchedWording($('#draft-wording-value'), item.terms);
}
async function applyDraftWording() {
  const session = draftWording;
  if (!session || session.saving || modal !== "draft-wording" ||
      draft !== session.owner || C.id !== session.campaign) return;
  if (!MotifTextLimits.guardFields(["draft-wording-value"])) return;
  const target = draftWordingTarget(session), { field, market, origin } = session.item.target;
  if (!target || target[field] !== session.expected)
    throw new Error("This wording changed. Review the updated plan before correcting it.");
  session.saving = true;
  const controls = $$('button,input,textarea', $('#overlay'));
  controls.forEach(control => control.disabled = true);
  try {
    target[field] = field === "cta" ? session.value || null : session.value;
    if (origin === "market_shared") {
      setCampaignWording(field, session.value, market);
      const language = market === "es-MX" ? "es" : "en";
      const key = field === "message" ? "copy-headline" : "copy-cta";
      for (const [name, values] of Object.entries(projectBuffers.form_drafts.fields || {}))
        if (name.startsWith("brief/") && name.endsWith(`/${language}`) && Object.prototype.hasOwnProperty.call(values, key))
          values[key] = session.value;
    }
    session.expected = target[field];
    markDirty();
    if (!(await saveProject()))
      throw new Error("There was a problem saving your wording. Your edit is still here. Try again.");
    if (draft !== session.owner || draftWording !== session || C.id !== session.campaign) return;
    blockedProposal = null;
  } finally {
    session.saving = false;
    if (draftWording === session && modal === "draft-wording") {
      controls.forEach(control => { if (control.isConnected) control.disabled = false; });
      $('#draft-wording-value')?.focus();
    }
  }
  if (draftWording === session && modal === "draft-wording") closeModal({ retain: false });
}
function openCampaignWording(item) {
  page = "campaign"; tab = "copy";
  lang = item.target.market === "es-MX" ? "es" : "en";
  updateURL(); render();
  focusMatchedWording($(`[data-bf-field="${item.target.field}"]`), item.terms);
}
async function openWordingCase(item, control) {
  if (!item) return;
  const index = C.products.findIndex(p => p.id === item.target.productId);
  if (index < 0) return;
  const beforeImages = !model?.manifest || (page === "production" && !!activeBlockedProposal());
  if (beforeImages) return item.target.origin === "market_shared" ? openCampaignWording(item) : openDraftWording(item, control);
  if (studioWorkspace) {
    if (!await resolveStudioChanges()) return;
    const cases = currentWordingView().cases;
    item = cases.find(candidate => candidate.id === item.id) || cases[0];
    if (!item) { paintWordingAvailability(); return; }
    if (item.target.origin === "market_shared") {
      if (!await leaveStudioWorkspace()) return;
      return openCampaignWording(item);
    }
    if (studioWorkspace.product === item.target.productId && MARKET[lang] === item.target.market) {
      if (studioWorkspace.mode !== "text") await switchStudioTarget({ mode: "text" });
      if (studioWorkspace.scene) {
        studioWorkspace.scene.setMode('text', item.target.field);
        focusMatchedWording($(`#mf-word-${item.target.field}`), item.terms);
        return;
      }
      if (!textEditor) return;
      textEditor.selected = item.target.field;
      paintText();
      focusMatchedWording($(`#mf-word-${item.target.field}`), item.terms);
      return;
    }
    if (!await leaveStudioWorkspace()) return;
  }
  if (item.target.origin === "market_shared") return openCampaignWording(item);
  const latestIndex = C.products.findIndex(p => p.id === item.target.productId);
  keepWordingOrigin("text", control);
  currentProduct = selected[0] = latestIndex;
  lang = item.target.market === "es-MX" ? "es" : "en";
  page = "review"; reviewTab = "product"; updateURL(); render();
  await openStudioWorkspace("text");
  if (studioWorkspace?.scene) {
    studioWorkspace.scene.setMode('text', item.target.field);
    focusMatchedWording($(`#mf-word-${item.target.field}`), item.terms);
    return;
  }
  if (!textEditor || C.id !== wordingReturn?.campaign) return;
  textEditor.selected = item.target.field;
  paintText();
  const field = $(`#mf-word-${item.target.field}`);
  focusMatchedWording(field, item.terms);
}
function openProhibitedRule(item, invalid = false, control = "edit-invalid-rule") {
  keepWordingOrigin("rule", control);
  const target = invalid ? pendingRuleForm() : null;
  const market = target?.market || item?.target.market || model.wording_issue?.pending_rule_targets?.[0]?.market || MARKET[lang];
  lang = market === "es-MX" ? "es" : "en";
  page = "campaign"; tab = "copy"; updateURL(); render();
  const rules = draft.markets[market]?.phrase_rules || [];
  ruleIndex = target ? target.rule_index : item ? rules.findIndex(rule => item.ruleIds.includes(rule.id)) : -1;
  openModal("rule");
  $('#mw-rule-phrase')?.focus();
}
async function refreshWordingStatus() {
  const owner = model, campaign = C.id, version = model.version;
  wordingCheckPhase = { campaign, owner, phase: "pending" }; paintWordingAvailability();
  try {
    const result = await api(`/api/motif/campaigns/${encodeURIComponent(campaign)}`);
    if (model !== owner || C.id !== campaign) return;
    if (result.version !== version || result.output_run_id !== owner.output_run_id || result.manifest?.revision_id !== owner.manifest?.revision_id) {
      wordingCheckPhase = { campaign, owner, phase: "stale" };
    } else { owner.wording_issue = result.wording_issue; owner.logo_issue = result.logo_issue; wordingCheckPhase = null; }
  } catch (_) {
    if (model === owner && C.id === campaign) wordingCheckPhase = { campaign, owner, phase: "failed" };
  }
  if (model === owner && C.id === campaign) paintWordingAvailability();
}
async function wordingExportRefused(error) {
  if (!["prohibited_phrase", "pending_rules", "conflict"].includes(error.code)) return false;
  closeModal();
  wordingCheckPhase = { campaign: C.id, owner: model, phase: error.code === "conflict" ? "stale" : "pending" };
  if (error.code !== "conflict") await refreshWordingStatus();
  render();
  $('[data-wording-slot] [data-mw]')?.focus();
  return true;
}
async function refreshSavedWording() {
  const owner = model, campaign = C.id, version = projectQueue?.version();
  const canAdopt = () => projectQueue?.getSaveState().phase === "saved" &&
    !projectQueue.isDirty() && projectQueue.version() === version;
  if (!canAdopt())
    throw new Error("Save or discard this tab’s changes before refreshing current wording.");
  const data = await api(`/api/motif/campaigns/${encodeURIComponent(campaign)}`);
  if (model !== owner || C.id !== campaign) return;
  // A completed local autosave is intervening work too, even when the queue
  // has returned to Saved. Adopt only the unchanged acknowledged record.
  if (!canAdopt())
    throw new Error("This tab changed while refreshing. Refresh again after saving your work.");
  const location = { page, tab, reviewTab, selected: [...selected], formats: [...formats],
    currentProduct, currentFormat, lang, filter, pageNum };
  useData(data, { reset: true });
  ({ page, tab, reviewTab, selected, formats, currentProduct, currentFormat, lang, filter, pageNum } = location);
  wordingCheckPhase = null;
  render();
}
async function dispatchWording(action, control) {
  const view = currentWordingView();
  if (action === "previous" || action === "next") {
    const next = Math.max(0, Math.min(view.cases.length - 1, view.index + (action === "next" ? 1 : -1)));
    wordingCaseId = view.cases[next]?.id; paintWordingAvailability();
    $(`[data-mw="${action}"]:not(:disabled)`)?.focus(); return;
  }
  if (action.startsWith("edit:")) return openWordingCase(view.cases.find(c => c.id === action.slice(5)), action);
  if (action.startsWith("rules:")) return openProhibitedRule(view.cases.find(c => c.id === action.slice(6)), false, action);
  if (action === "edit-invalid-rule") return openProhibitedRule(null, true);
  if (action === "refresh") {
    if (view.phase === "stale") await refreshSavedWording();
    else await refreshWordingStatus();
    return;
  }
  const mapped = { "apply-rules": "recheck", "add-rule": "rule", "close-rule": "close", "cancel-rule": "cancelEdits", "save-rule": "saveRule", "remove-rule": "deleteRule" }[action] || (action.startsWith("rule:") ? `editRule:${action.slice(5)}` : null);
  if (mapped) {
    await dispatchAction({ action: mapped });
    if (modal === "rule") $("#mw-rule-phrase")?.focus();
  }
}
document.addEventListener("click", event => {
  const button = event.target.closest('[data-mw]');
  if (!button || button.disabled || busy) return;
  event.preventDefault();
  dispatchWording(button.dataset.mw, button).catch(error => toast(error.message));
});
document.addEventListener("input", event => {
  if (modal !== "draft-wording" || !draftWording || draftWording.saving ||
      event.target.id !== "draft-wording-value") return;
  draftWording.value = event.target.value;
  $('#mw-field-error')?.remove();
});
