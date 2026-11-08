"use strict";

// One temporary modal owner for the designer's key and cleanup views. Credentials
// never enter the authored envelope; cleanup retries only its captured intent.
let localDialog = null;
function localDialogFlow(state, handlers = {}) {
  if (localDialog) return localDialog.finished;
  const overlay = $("#overlay"), app = $("#app"), skip = $(".skip");
  const previous = { modal, nodes: [...overlay.childNodes], focus: document.activeElement,
    appInert: app.inert, skipInert: skip.inert, campaign: draft?.campaign_id };
  previous.focusSelector = previous.focus?.id ? `#${CSS.escape(previous.focus.id)}`
    : previous.focus?.dataset.action ? `[data-action="${CSS.escape(previous.focus.dataset.action)}"]` : null;
  const background = document.createElement("div"), root = document.createElement("div");
  background.append(...previous.nodes);
  overlay.append(background, root);
  app.inert = skip.inert = true;
  let finish;
  const context = { finished: new Promise(resolve => { finish = resolve; }), view: null };
  localDialog = context;
  modal = "local";
  context.view = MotifLocalDialogs.mount(root, {
    state, background,
    // The component releases its local inert wrapper first. The host restores
    // the page/editor before returning focus, so it cannot focus an inert node.
    returnFocus: { focus() {} },
    ...handlers,
    onClose() {
      if (localDialog !== context) return;
      root.remove(); background.replaceWith(...previous.nodes);
      localDialog = null; modal = previous.modal;
      app.inert = previous.appInert; skip.inert = previous.skipInert;
      handlers.onClose?.();
      const focus = previous.focus?.isConnected ? previous.focus : previous.focusSelector ? $(previous.focusSelector) : null;
      focus?.focus({ preventScroll: true });
      finish();
    },
  });
  return context.finished;
}
async function openKeySetup(rejected = false, reason = null) {
  const source = await api("/api/openai-key");
  keyAvailable = source.available;
  keySavedAt = source.saved_at || null;
  let saved = false;
  const phase = source.managed_by_environment ? "environment" : !source.can_save ? "unavailable" : rejected ? "rejected" : source.available ? "configured" : "missing";
  return localDialogFlow({ kind: "key", phase, available: source.available, canRemove: source.available && source.can_save, reason: phase === "rejected" ? reason : null }, {
    async onSubmit(api_key) {
      const result = await api("/api/openai-key", { api_key });
      if (result.saved === true) { keyAvailable = result.available; keySavedAt = result.saved_at || Date.now() / 1000; saved = true; }
      return result;
    },
    async onRemove() {
      const result = await api("/api/openai-key", {}, { method: "DELETE" });
      if (result.removed === true) { keyAvailable = false; keySavedAt = null; saved = true; }
      return result;
    },
    onGuardedRefusal: message => toast(message),
    onHelp() { window.open("/static/local-setup.html", "_blank", "noopener"); },
    onClose() { if (saved && page === "production" && !modal) render(); },
  });
}
function openImageCleanup({ committed = false, old = false, campaign = draft?.campaign_id } = {}) {
  const identity = model?.image_cleanup?.pending.find(item => item.kind === "retirement")?.id;
  if (!old && !identity) return Promise.resolve();
  const name = draft?.name || "Current campaign";
  return localDialogFlow({ kind: "cleanup", phase: old ? "old" : committed ? "committed" : "pending", committed,
    campaign: name, context: old ? "Current images remain available" : "Local image cleanup" }, {
    async onRetry() {
      // The dialog cannot adopt a new cleanup record or restart Apply.
      const result = await api(`/api/campaigns/${encodeURIComponent(campaign)}/cleanup/${identity}/retry`, {});
      const cleanup = await api(`/api/campaigns/${encodeURIComponent(campaign)}/cleanup/status`);
      if (draft?.campaign_id === campaign) model.image_cleanup = cleanup;
      return { complete: result.state === "complete" };
    },
    onCurrentImages() { openCampaign(campaign, "review").catch(error => toast(error.message)); },
  });
}
