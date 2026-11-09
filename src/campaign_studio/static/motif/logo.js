"use strict";

// The designer component owns gestures and presentation. This adapter owns only
// current-target service calls, decoded previews, and the surrounding app draft.
let logoEditor = null;
let logoSession = null;

function logoTarget(data, productId, market, ratio) {
  const product = data.output_brief?.products.find(
    (item) => item.id === productId,
  );
  const format = FMT.find((item) => item.ratio === ratio);
  const output = data.manifest?.outputs.find(
    (item) =>
      item.product_id === productId &&
      item.market === market &&
      item.ratio === ratio,
  );
  if (
    !product ||
    !output ||
    !data.logo_editable_products?.[productId]
  )
    throw new Error("This saved image is not available for logo editing.");
  return {
    campaignId: data.value.campaign_id,
    campaignName: data.output_brief.name,
    runId: data.output_run_id,
    revisionId: data.manifest.revision_id,
    revisionNumber: data.revision_number,
    draftVersion: data.version,
    productId,
    productName: product.name,
    market,
    marketLabel: LANG[market === "es-MX" ? "es" : "en"],
    formatName: format.name,
    ratio,
    width: format.w,
    height: format.h,
  };
}

function logoBody(body) {
  const t = body.target;
  return {
    run_id: t.runId,
    revision_id: t.revisionId,
    product_id: t.productId,
    market: t.market,
    ratio: t.ratio,
    ...(body.placement === undefined
      ? {}
      : {
          placement: body.placement
            ? {
                left: body.placement.x,
                top: body.placement.y,
                width: body.placement.width,
              }
            : null,
        }),
    ...(body.mode === "replacement"
      ? {
          replacement: body.asset.reference,
          ...(body.replacementFit ? {pre_fit_placement: {left: body.replacementFit.x, top: body.replacementFit.y, width: body.replacementFit.width}} : {}),
          provenance: body.asset.provenance || {
            label: "User-supplied logo",
            rights: "Not provided",
            synthetic: false,
          },
          draft_version: logoSession?.target.campaignId === t.campaignId &&
            logoSession.target.revisionId === t.revisionId
            ? logoSession.target.draftVersion : t.draftVersion,
        }
      : {}),
  };
}

function logoPlacement(bounds, target) {
  return {
    x: bounds[0] / target.width,
    y: bounds[1] / target.height,
    width: (bounds[2] - bounds[0]) / target.width,
  };
}

async function logoPreview(body, signal) {
  const result = await api(
    `/api/campaigns/${body.target.campaignId}/logo/preview`,
    logoBody(body), { signal },
  );
  const image = result.images[0];
  if (!image) {
    const error = new Error(
      result.findings.find((f) => f.severity === "blocking")?.evidence ||
        "The logo preview is unavailable.",
    );
    error.code = "preview_blocked";
    throw error;
  }
  const decoded = await decodedImage(image.url);
  if (
    decoded.naturalWidth !== body.target.width ||
    decoded.naturalHeight !== body.target.height
  )
    throw new Error("The logo preview does not match this image's dimensions.");
  const bounds = image.layers.bounds;
  return {
    ...result,
    digest: result.digest,
    compositeUrl: image.url,
    resolvedRect: {
      left: bounds[0],
      top: bounds[1],
      width: bounds[2] - bounds[0],
      height: bounds[3] - bounds[1],
    },
    defaultPlacement: logoPlacement(image.layers.default_bounds, body.target),
    availableOutputs: result.impact.available_outputs,
    expectedOutputs: result.impact.expected_outputs,
    missingOutputs:
      result.impact.expected_outputs - result.impact.available_outputs,
    fittedOutputCount: result.impact.fitted_count,
    message: result.findings
      .filter((f) => f.severity === "blocking")
      .map((f) => f.evidence)
      .join(" "),
  };
}

async function logoContext(data, target, signal) {
  const result = await logoPreview({ target, mode: "placement" }, signal);
  const layer = result.images[0].layers;
  await Promise.all([
    decodedImage(layer.base_url),
    decodedImage(layer.copy_url),
  ]);
  const asset = result.logo_asset
    ? {
        ...result.logo_asset,
        url: layer.logo_url,
        filename: "Campaign logo",
        provenance: copy(data.output_brief.brand.logo_provenance),
      }
    : null;
  const placement = layer.placement
    ? {
        x: layer.placement.left,
        y: layer.placement.top,
        width: layer.placement.width,
      }
    : result.defaultPlacement;
  return {
    target,
    asset,
    placement,
    defaultPlacement: result.defaultPlacement,
    savedDisplay: { compositeUrl: result.compositeUrl, resolvedRect: result.resolvedRect },
    previewUrl: layer.base_url,
    copyOverlayUrl: layer.copy_url,
    copyDescription: "Exact supplied campaign wording, printed above the logo",
  };
}

async function selectLogoFile(file, target) {
  const owner = logoSession;
  const response = await fetch("/api/uploads", {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Campaign-Token": $('meta[name="campaign-token"]').content,
      "X-Campaign-ID": target.campaignId,
      "X-Image-Role": "logo",
      "X-Asset-Name": encodeURIComponent((draft.brand?.name || "Brand") + " logo"),
    },
    body: file,
  });
  const result = await response.json();
  if (!response.ok) {
    if (
      result.code === "storage_capacity" &&
      owner === logoSession &&
      modal === "logo" &&
      (await imageCapacity(result, "Attaching this logo"))
    )
      throw { handled: true, commitState: "not_started" };
    // A parsed action refusal did not stage a replacement in this editor.
    // Network/JSON failures never enter this branch; image findings retain their
    // cause-specific overlay, and a retired upload owner cannot toast later.
    if (!result.input_image && typeof result.code === "string" && typeof result.message === "string") {
      if (owner === logoSession && modal === "logo") toast(result.message);
      throw { handled: true, commitState: "not_started" };
    }
    const input = result.input_image || {};
    throw {
      kind:
        { encoded_bytes: "size", pixel_count: "pixels" }[input.kind] ||
        input.kind ||
        "decode_failure",
      detectedType: input.detected_type,
      encodedBytes: input.encoded_bytes || file.size,
      width: input.width,
      height: input.height,
      message: result.message,
    };
  }
  return {
    ...result,
    filename: file.name.split(/[\\/]/).pop(),
    provenance: copy(result.provenance),
  };
}

function refreshAfterLogo(data) {
  // Rebase only logo fields changed by this operation. Other live Brief typing
  // and keyed wording drafts remain in memory and are never committed here.
  const previous = draft,
    previousBaseline = JSON.parse(baseline),
    wasDirty = dirty;
  useData(data, { applied: true });
  if (wasDirty && previous.campaign_id === data.value.campaign_id) {
    for (const field of ["logo_path", "logo_provenance"])
      if (
        JSON.stringify(previous.brand[field]) ===
        JSON.stringify(previousBaseline.brand[field])
      )
        previous.brand[field] = copy(data.value.brand[field]);
    for (const product of previous.products) {
      const before = previousBaseline.products.find((p) => p.id === product.id);
      const after = data.value.products.find((p) => p.id === product.id);
      if (!before || !after) continue;
      for (const [market, formats] of Object.entries(after.framing || {}))
        for (const [ratio, frame] of Object.entries(formats)) {
          const oldLogo = before.framing?.[market]?.[ratio]?.logo_placement;
          const typedLogo = product.framing?.[market]?.[ratio]?.logo_placement;
          if (JSON.stringify(oldLogo) !== JSON.stringify(typedLogo)) continue;
          product.framing ||= {};
          product.framing[market] ||= {};
          product.framing[market][ratio] ||= {};
          product.framing[market][ratio].logo_placement = copy(
            frame.logo_placement,
          );
        }
    }
    draft = previous;
    dirty = JSON.stringify(draft) !== baseline;
    viewModel();
  }
  render({ preserveModal: true });
}

function logoHostDialog(title, body, buttons, handleAction = null) {
  // Apply can temporarily disable its button and leave focus on the body.
  const campaignName = C.name,
    root = $("#overlay"),
    focused = document.activeElement === document.body
      ? root.querySelector('[data-action="saveCorrection"], #frame-primary, [data-action="applyText"], [data-lg-box]')
      : document.activeElement,
    previous = [...root.children, ...(studioWorkspace ? [studioWorkspace.root] : [])].map((node) => [node, node.inert]);
  previous.forEach(([node]) => {
    node.inert = true;
  });
  const overlay = document.createElement("div");
  overlay.className = "overlay mf-logo-sub";
  const show = (title, body, buttons, focus = '[data-logo-host="cancel"]') => {
    overlay.innerHTML = `<section class="modal" role="dialog" aria-modal="true" aria-labelledby="logo-host-title"><header class="modal-head"><div><div class="eyebrow">${esc(campaignName)}</div><h2 id="logo-host-title" tabindex="-1">${esc(title)}</h2></div><button type="button" class="close" data-logo-host="cancel" aria-label="Close dialog">×</button></header><div class="modal-body">${body}</div><footer class="modal-foot">${buttons}</footer></section>`;
    queueMicrotask(() => overlay.querySelector(focus)?.focus());
  };
  show(title, body, buttons);
  root.append(overlay);
  return new Promise((resolve) => {
    const done = (action) => {
      overlay.remove();
      previous.forEach(([node, inert]) => {
        node.inert = inert;
      });
      focused?.isConnected && focused.focus();
      resolve(action);
    };
    let changing = 0,
      actionSequence = 0;
    const choose = async (action) => {
      if (changing && action !== "cancel") return;
      const sequence = ++actionSequence;
      changing = sequence;
      try {
        if (handleAction && (await handleAction(action, show)) === false)
          return;
        done(action);
      } finally {
        if (changing === sequence) changing = 0;
      }
    };
    overlay.addEventListener("click", (event) => {
      const button = event.target.closest("[data-logo-host]");
      if (button) {
        event.stopPropagation();
        choose(button.dataset.logoHost);
      }
    });
    overlay.addEventListener("keydown", (event) => {
      event.stopPropagation();
      if (event.key === "Escape") {
        event.preventDefault();
        choose("cancel");
        return;
      }
      if (event.key !== "Tab") return;
      const controls = [
        ...overlay.querySelectorAll("button:not(:disabled),a[href]"),
      ];
      event.preventDefault();
      const index = controls.indexOf(document.activeElement);
      controls[
        (index + (event.shiftKey ? -1 : 1) + controls.length) % controls.length
      ]?.focus();
    });
  });
}
function logoHostButton(text, action, primary = false) {
  return `<button type="button" class="btn ${primary ? "primary" : ""}" data-logo-host="${action}">${text}</button>`;
}

async function imageCleanupReady(campaign, current) {
  if (model.image_cleanup?.pending.some(item => item.kind === "retirement")) {
    await openImageCleanup({ campaign });
    // Cleanup and Apply are separate deliberate actions, even after success.
    return false;
  }
  return current();
}

function logoPreviewKey(body) {
  return JSON.stringify({
    target: body.target,
    asset: body.asset,
    placement: body.placement,
    replacementFit: body.replacementFit,
    mode: body.mode,
  });
}

async function imageCapacity(error, operationLabel = "Applying this logo") {
  const d = error.capacity;
  if (!["workspace_budget", "disk_space"].includes(d?.kind)) return false;
  const view = MotifStorage.getView(
    d.kind === "workspace_budget" ? "storage-budget" : "storage-disk",
    {
      operationLabel,
      usedBytes: d.used_bytes,
      budgetBytes: d.budget_bytes,
      projectedBytes: d.projected_bytes,
      freeBytes: d.free_bytes,
      reserveBytes: d.reserve_bytes,
      requiredAdditionalFreeBytes: Math.max(
        0,
        d.projected_bytes + d.reserve_bytes - d.free_bytes,
      ),
    },
  );
  const footer = view.footer
    .replaceAll('data-sc="', 'data-logo-host="')
    .replace(
      /<button[^>]*data-logo-host="view-campaigns"[^>]*>.*?<\/button>/,
      '<a class="btn" href="/" target="_blank" rel="noopener">Open campaigns in new tab</a>',
    );
  const body =
    view.body +
    (d.kind === "workspace_budget"
      ? '<p class="small muted">Your edits stay in this tab.</p>'
      : "");
  let workspace = false,
    workspaceRequest = 0;
  await logoHostDialog(view.title, body, footer, async (action, show) => {
    if (workspace && action === "cancel") {
      workspace = false;
      workspaceRequest++;
      show(view.title, body, footer, '[data-logo-host="workspace"]');
      return false;
    }
    if (!["workspace", "workspace-refresh"].includes(action)) return;
    workspace = true;
    const request = ++workspaceRequest;
    readiness = null;
    const display = () => {
      const template = document.createElement("template");
      template.innerHTML = setupModal();
      show(
        template.content.querySelector("h2").textContent,
        template.content.querySelector(".modal-body").innerHTML,
        logoHostButton("Check readiness again", "workspace-refresh") +
          logoHostButton("Back to storage notice", "cancel", true),
        "#logo-host-title",
      );
    };
    display();
    try {
      readiness = await api("/api/motif/readiness", {});
      keyAvailable = readiness.key_available;
    } catch {
      readiness = {
        ready: false,
        checks: [],
        message: "The local readiness check could not finish. Try again.",
      };
    }
    if (workspace && request === workspaceRequest) display();
    return false;
  });
  return true;
}

function logoReceiptMatches(data, body) {
  const receipt = data?.manifest?.edit_receipt;
  return (
    data?.output_run_id === body.target.runId &&
    data?.manifest?.revision_id !== body.target.revisionId &&
    receipt?.kind === "logo" &&
    receipt.base_revision === body.target.revisionId &&
    receipt.preview_digest === body.previewDigest
  );
}

async function previewLogoEditor(body) {
  const owner = logoSession,
    sequence = ++owner.previewSequence;
  owner.previewDigest = null;
  let result;
  try {
    result = await logoPreview(body);
  } catch (error) {
    if (!error.status && error.code !== "preview_blocked") throw error;
    // Known service refusals carry named affected inputs. Present that exact
    // sanitized guidance, with no invented final PNG and no enabled Save.
    result = {
      blocked: true,
      digest: null,
      message: error.message,
      reviewRequired: ["logo_stale_revision", "stale_brief"].includes(
        error.code,
      ),
    };
  }
  if (logoSession === owner && owner.previewSequence === sequence) {
    owner.mode = body.mode;
    owner.previewKey = logoPreviewKey(body);
    owner.previewDigest = result.digest;
  }
  return result;
}

async function applyLogo(body) {
  const owner = logoSession;
  if (!(await imageCleanupReady(body.target.campaignId, () =>
    logoSession?.target.revisionId === body.target.revisionId &&
    logoSession.previewDigest === body.previewDigest &&
    logoSession.previewKey === logoPreviewKey(body))))
    throw { handled: true, commitState: "not_started" };
  let result, current;
  try {
    result = await api(`/api/campaigns/${body.target.campaignId}/logo/apply`, {
      ...logoBody(body),
      preview_digest: body.previewDigest,
    });
  } catch (error) {
    if (error.status === 409 && ["no_changes", "apply_not_accepted"].includes(error.code)) throw error;
    if (error.code === "storage_capacity" && (await imageCapacity(error)))
      throw { ...error, handled: true, commitState: "not_started" };
    current = await api(
      `/api/motif/campaigns/${body.target.campaignId}`,
    ).catch(() => null);
    if (logoReceiptMatches(current, body))
      result = { revision_id: current.manifest.revision_id };
    else {
      if (
        error.status === 409 &&
        current?.manifest &&
        (current.output_run_id !== body.target.runId ||
          current.manifest.revision_id !== body.target.revisionId ||
          error.code === "stale_brief")
      ) {
        error.code = "conflict";
        error.latestRevisionNumber = current.revision_number;
      }
      throw error;
    }
  }
  current ||= await api(`/api/motif/campaigns/${body.target.campaignId}?after_edit=true`);
  if (current.manifest?.revision_id !== result.revision_id)
    throw { code: "conflict", latestRevisionNumber: current.revision_number };
  const target = logoTarget(
    current,
    body.target.productId,
    body.target.market,
    body.target.ratio,
  );
  const image = current.manifest.outputs.find(output => output.product_id === target.productId && output.market === target.market && output.ratio === target.ratio);
  await decodedImage(image.url);
  if (logoSession !== owner) throw { code: "conflict" };
  refreshAfterLogo(current);
  logoSession.target = target;
  logoSession.previewKey = null;
  logoSession.previewDigest = null;
  return {
    target,
    imageUrl: image.url,
    availableOutputs: current.manifest.metrics.completed_images,
    missingOutputs:
      current.manifest.outputs.length -
      current.manifest.metrics.completed_images,
  };
}

async function openLogo(product, format, { isCurrent = () => true, signal } = {}) {
  if (!canEditLogo(product))
    throw new Error("Choose an available current image.");
  const authored = JSON.stringify(model.project?.authored.brief || model.value);
  let target = logoTarget(
    model,
    C.products[product].id,
    MARKET[lang],
    FMT[format].ratio,
  );
  lastFocus = document.activeElement;
  modal = "logo";
  logoSession = {
    target,
    mode: "placement",
    previewKey: null,
    previewDigest: null,
    previewSequence: 0,
  };
  const workspaceOwner = studioWorkspace;
  const missingLogo = !!model.output_brief?.brand && !model.output_brief.brand.logo_path;
  logoEditor = MotifLogo.mount({
    target,
    loading: !missingLogo,
    embedded: true,
    logoUrl: C.logo,
    savedDisplay: { compositeUrl: (typeof studioRetainedImage === "function" && studioRetainedImage()?.src) || outputFor(product, lang, format)?.url },
    root: studioWorkspace.root,
    onMount: markup => {
      studioWorkspace.root.classList.add("mf-logo-modal");
      mountStudioParts(markup, { tools: ".mf-logo-tools", surface: ".mf-logo-stage", status: ".mf-logo-preview-note" });
    },
    onUnmount: () => {},
    onSelectFile: (file) => selectLogoFile(file, logoSession.target),
    onPreview: previewLogoEditor,
    onChange: immediate => scheduleStudioSave(immediate),
    onSave: (body) => withProjectApply(() => applyLogo(body)),
    onConflict: () => studioSave?.conflict(Object.assign(new Error(MotifSave.conflictMessage), { status: 409 })),
    onSaved: () => {
      refreshStudioTargets();
    },
    onClose: () => {
      if (studioWorkspace !== workspaceOwner) return;
      logoEditor = null; logoSession = null; modal = "";
      if (!workspaceOwner.tearingDown && !workspaceOwner.continuing)
        mountStudioTool("inspect").catch(error => toast(error.message));
    },
    onReturnFocus: () =>
      $('[data-ws-mode="logo"]')?.focus({
        preventScroll: true,
      }),
  });
  mountProjectViews();
  // The saved campaign already tells us whether there is artwork to position.
  // A first attachment obtains its own checked preview after the file is chosen.
  if (missingLogo) {
    if (isCurrent() && typeof studioPreviewReady === "function") studioPreviewReady();
    return;
  }
  const openingEditor = logoEditor;
  const context = await logoContext(model, target, signal);
  if (!isCurrent() || studioWorkspace !== workspaceOwner || logoEditor !== openingEditor) return;
  if (C.id !== target.campaignId || model.manifest.revision_id !== target.revisionId)
    throw new Error("The campaign changed. Reopen Edit logo.");
  // Viewing-context acknowledgements may advance while verified layers decode.
  if (JSON.stringify(model.project?.authored.brief || model.value) === authored)
    target.draftVersion = model.version;
  logoSession.target = target;
  openingEditor.acceptContext?.({ ...context, target });
  const displayed = openingEditor.root?.querySelector(".mf-logo-composite");
  if (displayed && !displayed.hidden) await displayed.decode();
  if (isCurrent() && logoEditor === openingEditor && typeof studioPreviewReady === "function") studioPreviewReady();
}
