/* Small Motif local-setup and cleanup dialogs. Host owns safe persistence and exact-ID cleanup. */
(function (scope) {
  "use strict";
  const { esc, button } = scope.MotifBriefFlow;
  const copy = {
    missing: {
      title: "Add your OpenAI API key.",
      intro:
        "Use your own OpenAI account when this campaign needs new imagery. Saved images, local editing, and export work without a key.",
    },
    rejected: {
      title: "Update your OpenAI API key.",
      intro:
        "OpenAI rejected the configured key. Replace it, then return to Production to review the unsent work.",
    },
    invalid: {
      title: "Add your OpenAI API key.",
      intro:
        "Use your own OpenAI account when this campaign needs new imagery.",
      error: "Enter one complete key, with no spaces or line breaks.",
    },
    failed: {
      title: "The key could not be saved.",
      intro:
        "Local setup could not safely update this installation. The running key is unchanged.",
      error:
        "Check local file permissions and available disk space, then enter the key again. Nothing was generated.",
    },
    uncertain: {
      title: "The local save is not confirmed.",
      intro:
        "Motif could not confirm the final local file state. The running key is unchanged.",
      error:
        "Check the installation’s .env file using the setup instructions before trying again. No image request was made.",
    },
    unknown: {
      title: "The save response did not arrive.",
      intro:
        "Motif could not confirm whether this key was saved. The connection ended before a result arrived.",
      error:
        "Check local setup before trying again. A key being available does not confirm that this submission was saved. No image request was made.",
    },
    saving: {
      title: "Saving your key…",
      intro:
        "Storing the key locally for this installation. This does not start production.",
    },
    saved: {
      title: "Your key is saved.",
      intro:
        "It is available for your next deliberate production action. No image request was made.",
      status: "Saved locally, not verified",
      detail:
        "Return to Production to review the plan or unsent work before starting.",
    },
    unavailable: {
      title: "Local key entry is unavailable.",
      intro:
        "This running installation cannot save a key through the app. Use the documented local setup, then restart Motif.",
      detail: "Saved imagery, local editing, and export remain available.",
    },
    busy: {
      title: "Key setup is busy.",
      intro:
        "Another local key update is in progress. Wait for it to finish, then enter the key again.",
    },
    localfile: {
      title: "Check your local setup.",
      intro: "The installation’s .env file has a syntax problem.",
      error:
        "Use the setup instructions to correct the file, then enter the key again. No image request was made.",
    },
    environment: {
      title: "This key is managed outside Motif.",
      intro:
        "OPENAI_API_KEY was supplied when this app started. Update it in the launch environment, then restart Motif.",
      detail:
        "Motif will not save a lower-priority key that would stop being active after a restart. Local editing and export remain available.",
    },
  };
  // Adapter passes sanitized API results/errors. Availability alone never proves this submission saved.
  function keyPhase(result, error) {
    const value = error || result || {},
      code = value.code || value.error?.code;
    const errors = {
      credential_input: "invalid",
      credential_environment_managed: "environment",
      credential_setup_unavailable: "unavailable",
      credential_busy: "busy",
      credential_file_invalid: "localfile",
      credential_save_failed: "failed",
      credential_save_uncertain: "uncertain",
    };
    if (code) return errors[code] || "unknown";
    if (error) return "unknown";
    if (value.saved === true) return "saved";
    if (value.managed_by_environment === true) return "environment";
    if (value.can_save === false) return "unavailable";
    return "unknown";
  }
  // Parsed guarded refusals are known non-saves, unlike a lost response.
  function guardedKeyRefusal(error) {
    if (error?.status === 403 && ["origin", "session"].includes(error.code)) {
      return "Key setup was refused by this local session. No key was saved. Keep this page open and retry in a new tab at Motif’s local address.";
    }
    if (error?.status === 413 && error.code === "action_size") {
      return "Key setup was refused because the request was too large. No key was saved. Reopen key setup and paste only the key.";
    }
    if (error?.status === 415 && error.code === "content_type") {
      return "Key setup was refused because the request format was unsupported. No key was saved. Reopen key setup and try again.";
    }
    return null;
  }
  function credential(v) {
    const c = copy[v.phase] || copy.missing,
      input = ![
        "saved",
        "environment",
        "uncertain",
        "unknown",
        "unavailable",
      ].includes(v.phase),
      busy = v.phase === "saving";
    return {
      title: c.title,
      body: `${c.status ? `<div class="bf-key-status" role="status"><strong>${c.status}</strong>${esc(c.detail)}</div>` : ""}<p>${esc(v.phase === "rejected" && v.reason ? v.reason : c.intro)}</p>${c.error ? `<div class="bf-key-status ${["uncertain", "unknown"].includes(v.phase) ? "warning" : "error"}" role="alert">${esc(c.error)}</div>` : ""}${input ? `<div class="bf-field"><label for="bf-local-key">OpenAI API key</label><input id="bf-local-key" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="4096" aria-describedby="bf-key-local-note" ${busy ? "disabled" : ""}></div>` : ""}${["environment", "unavailable"].includes(v.phase) ? `<p class="bf-hint">${esc(c.detail)}</p>` : ""}<p id="bf-key-local-note" class="bf-key-helper">Stored locally for this installation and excluded from campaign exports. Requests authenticate with OpenAI and are billed to the OpenAI account that owns the key.</p>`,
      foot: [
        "saved",
        "environment",
        "uncertain",
        "unknown",
        "unavailable",
      ].includes(v.phase)
        ? `${["environment", "uncertain", "unknown", "unavailable", "localfile"].includes(v.phase) ? button("Setup instructions", "setup-help", "bf-quiet") : ""}${button("Close", "dialog-close", "bf-primary")}`
        : `${v.phase === "localfile" ? button("Setup instructions", "setup-help", "bf-quiet") : ""}${button("Cancel", "dialog-close", "", busy ? "disabled" : "")}${button(busy ? "Saving…" : "Save key", "key-submit", "bf-primary", busy ? "disabled" : "")}`,
      busy,
    };
  }
  const cleanupCopy = {
    pending: {
      title: "Finish local cleanup.",
      intro:
        "Some superseded image files could not be removed. Finish cleanup before applying more image changes.",
      detail:
        "Your pending edits are still here. Current images, original files, and existing exports are available.",
      primary: "Retry cleanup",
      cancel: "Keep editing",
    },
    committed: {
      title: "Your images were updated.",
      intro:
        "The update is saved, but cleanup of superseded image files needs attention.",
      detail:
        "Your current images, original files, and existing exports are available. Finish cleanup before making more image changes.",
      primary: "Retry cleanup",
      cancel: "Close",
      success: "Image update saved",
    },
    retrying: {
      title: "Finishing local cleanup…",
      intro: "Retrying the known files from the interrupted cleanup.",
      detail:
        "Your current images and original files stay protected. This does not apply edits or create imagery.",
      primary: "Cleaning up…",
      cancel: "Close",
      busy: true,
    },
    failed: {
      title: "Cleanup is still blocked.",
      intro:
        "Some files could not be safely removed. Check local file access, then retry.",
      detail:
        "Current images and existing exports remain available. No image update was repeated.",
      primary: "Retry cleanup",
      cancel: "Close",
    },
    complete: {
      title: "Local cleanup is complete.",
      intro: "You can continue working with the current images.",
      detail:
        "If you kept pending edits open, apply them when you are ready. Cleanup never applies them automatically.",
      primary: "Return to work",
      cancel: null,
      success: "Cleanup complete",
    },
    old: {
      title: "This image is no longer available.",
      intro: "Open this campaign’s current images to continue working.",
      detail: "Original files and existing downloaded packages are unchanged.",
      primary: "Open current images",
      cancel: "Close",
    },
  };
  function cleanup(v) {
    const c = { ...(cleanupCopy[v.phase] || cleanupCopy.pending) };
    if (v.committed && v.phase === "failed") c.success = "Image update saved";
    return {
      title: c.title,
      body: `${c.success ? `<div class="bf-key-status" role="status"><strong>${esc(c.success)}</strong></div>` : ""}<p>${esc(c.intro)}</p><p>${esc(c.detail)}</p><div class="bf-cleanup-context">${esc(v.campaign || "Current campaign")}${v.context ? " · " + esc(v.context) : ""}</div>`,
      foot: `${c.cancel ? button(c.cancel, "dialog-close", "", c.busy ? "disabled" : "") : ""}${button(c.primary, v.phase === "old" ? "current-images" : v.phase === "complete" ? "dialog-close" : "cleanup-retry", "bf-primary", c.busy ? "disabled" : "")}`,
      busy: !!c.busy,
    };
  }
  function dialog(v) {
    const d = v.kind === "key" ? credential(v) : cleanup(v);
    return `<div class="hlb bf-overlay"><section class="bf-dialog" tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="bf-dialog-title"><header class="bf-dialog-head"><div><span class="bf-kicker">${v.kind === "key" ? "LOCAL SETUP" : esc(v.campaign || "MOTIF / CAMPAIGN STUDIO")}</span><h2 id="bf-dialog-title">${esc(d.title)}</h2></div><button type="button" class="bf-dialog-close" data-bf-action="dialog-close" aria-label="Close dialog" ${d.busy ? "disabled" : ""}>×</button></header><div class="bf-dialog-body">${d.body}</div><footer class="bf-dialog-foot">${d.foot}</footer></section></div>`;
  }
  function mount(
    root,
    {
      state,
      background,
      returnFocus = document.activeElement,
      onSubmit,
    onGuardedRefusal,
      onRetry,
      onCurrentImages,
      onHelp,
      onClose,
    },
  ) {
    let v = { committed: state.phase === "committed", ...state },
      dead = false,
      request = 0;
    const previousInert = background?.inert || false;
    if (background) background.inert = true;
    const ac = new AbortController();
    function render(next, focus = true) {
      v = { ...v, ...next };
      root.innerHTML = dialog(v);
      if (focus)
        (
          root.querySelector("#bf-local-key") ||
          root.querySelector('[data-bf-action="dialog-close"]:not(:disabled)')
        )?.focus();
      else root.querySelector(".bf-dialog")?.focus();
    }
    function close() {
      if (v.phase === "saving" || v.phase === "retrying") return;
      dead = true;
      request++;
      const input = root.querySelector("#bf-local-key");
      if (input) input.value = "";
      ac.abort();
      root.innerHTML = "";
      if (background) background.inert = previousInert;
      returnFocus?.focus({ preventScroll: true });
      onClose?.();
    }
    root.addEventListener(
      "click",
      async (e) => {
        const b = e.target.closest("[data-bf-action]");
        if (!b || b.disabled) return;
        e.preventDefault();
        e.stopPropagation();
        const action = b.dataset.bfAction;
        if (action === "dialog-close") return close();
        if (action === "setup-help") return onHelp?.();
        if (action === "current-images") {
          close();
          return onCurrentImages?.();
        }
        if (action === "key-submit") {
          const input = root.querySelector("#bf-local-key");
          let token = input?.value || "";
          if (!token || token.length > 4096 || !/^[\x21-\x7e]+$/.test(token)) {
            if (input) input.value = "";
            render({ phase: "invalid" });
            return;
          }
          if (input) input.value = "";
          const current = ++request;
          render({ phase: "saving" }, false);
          try {
            const result = await onSubmit(token);
            token = "";
            if (dead || current !== request) return;
            render({ phase: keyPhase(result) });
          } catch (error) {
            token = "";
            if (dead || current !== request) return;
            const refusal = guardedKeyRefusal(error);
            if (refusal !== null) {
              v = { ...v, phase: "missing" }; // Settled; no intermediate render.
              close();
              onGuardedRefusal(refusal);
              return;
            }
            render({ phase: keyPhase(null, error) });
          }
          return;
        }
        if (action === "cleanup-retry") {
          const current = ++request;
          render({ phase: "retrying" }, false);
          try {
            const result = await onRetry();
            if (dead || current !== request) return;
            render({ phase: result.complete ? "complete" : "failed" });
          } catch {
            if (!dead && current === request) render({ phase: "failed" });
          }
        }
      },
      { signal: ac.signal },
    );
    root.addEventListener(
      "keydown",
      (e) => {
        if (e.key === "Enter" && e.target.id === "bf-local-key") {
          e.preventDefault();
          root
            .querySelector('[data-bf-action="key-submit"]:not(:disabled)')
            ?.click();
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          close();
          return;
        }
        if (e.key !== "Tab") return;
        const items = [
          ...root.querySelectorAll(
            "button:not(:disabled),input:not(:disabled),a[href]",
          ),
        ].filter((el) => el.getClientRects().length);
        if (!items.length) {
          e.preventDefault();
          return;
        }
        if (e.shiftKey && document.activeElement === items[0]) {
          e.preventDefault();
          items.at(-1).focus();
        } else if (!e.shiftKey && document.activeElement === items.at(-1)) {
          e.preventDefault();
          items[0].focus();
        }
      },
      { signal: ac.signal },
    );
    render(v);
    return {
      close,
      render,
      get phase() {
        return v.phase;
      },
    };
  }
  scope.MotifLocalDialogs = {
    dialog,
    mount,
    keyPhase,
    credentialCopy: copy,
    cleanupCopy,
  };
})(typeof window !== "undefined" ? window : globalThis);
