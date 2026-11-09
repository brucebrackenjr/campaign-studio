/* Motif Brief presentation adapter. No storage, network, rendering engine, or credentials. */
(function (scope) {
  "use strict";
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const languages = { "en-US": "English (United States)", "es-MX": "Spanish (Mexico)" },
    tabs = [
      ["campaign", "Brief"],
      ["products", "Products"],
      ["brand", "Brand"],
      ["copy", "Copy & Rules"],
    ];
  const button = (label, action, kind = "", extra = "") =>
    `<button type="button" class="bf-button ${kind}" data-bf-action="${esc(action)}" ${extra}>${label}</button>`;
  const field = (
    label,
    key,
    value,
    { area = false, hint = "", type = "text", errors = [] } = {},
  ) =>
    `<div class="bf-field"><label for="bf-${key}">${esc(label)}</label>${area ? `<textarea id="bf-${key}" data-bf-field="${esc(key)}" ${errors.length ? `aria-invalid="true" aria-describedby="bf-${key}-wording"` : ""}>${esc(value)}</textarea>` : `<input id="bf-${key}" data-bf-field="${esc(key)}" type="${type}" value="${esc(value)}" ${errors.length ? `aria-invalid="true" aria-describedby="bf-${key}-wording"` : ""}>`}${errors.length ? `<p class="mw-field-error" id="bf-${key}-wording">Remove ${errors.map(term => `“${esc(term)}”`).join(", ")} or edit the prohibited words.</p>` : ""}${hint ? `<p class="bf-hint">${esc(hint)}</p>` : ""}</div>`;
  function normalizeHex(raw) {
    const text = String(raw).trim();
    return /^#?[0-9a-f]{6}$/i.test(text)
      ? "#" + text.replace(/^#/, "").toUpperCase()
      : null;
  }
  function colorControl(
    key,
    label,
    value,
    { defaultAction = false, compact = false } = {},
  ) {
    const normalized = normalizeHex(value);
    return `<div class="bf-color-control"><input type="color" value="${normalized || "#FFFFFF"}" data-bf-picker="${esc(key)}" aria-label="Choose ${esc(label)}"><input type="text" id="bf-${esc(key)}" data-bf-field="${esc(key)}" value="${esc(value)}" aria-label="${esc(label)} hex value" maxlength="24" spellcheck="false" autocapitalize="characters" aria-invalid="${!normalized}">${defaultAction ? button("Use color", "use-default", "bf-small") : ""}</div>`;
  }
  function campaign(v) {
    return `<div class="bf-layout"><section class="bf-panel"><h2>The brief</h2><div class="bf-pair">${field("Campaign name", "campaignName", v.name)}${field("Brand name", "brandName", v.brandName)}${field("Who is this for?", "audience", v.audience, { area: true })}${field("What should the photography feel like?", "direction", v.direction, { area: true })}${field("Copy origin", "copyOrigin", v.copyOrigin)}${field("Copy usage rights", "copyRights", v.copyRights, { hint: "Record only rights that are known." })}</div></section><aside class="bf-panel"><div class="bf-kicker">THE DELIVERABLES</div><div class="bf-stat">${v.products.length * v.markets.length * 3} <small>planned images</small></div><p class="bf-hint">Every selected product and market gets all three formats.</p><div class="bf-rule"></div><h3>Markets</h3><div class="bf-language-facts">${v.markets.map((m) => `<span class="bf-chip">✓ ${languages[m]}</span>`).join("")}</div><div class="bf-rule"></div><div class="bf-deliverables">${[
      ["Instagram post", "1:1", "1080 × 1080"],
      ["Story", "9:16", "1080 × 1920"],
      ["Facebook post", "16:9", "1920 × 1080"],
    ]
      .map(
        ([name, ratio, size]) =>
          `<div class="bf-deliverable"><i aria-hidden="true"></i><div><b>${name} · ${ratio}</b><span>${size} PNG</span></div></div>`,
      )
      .join("")}</div></aside></div>`;
  }
  function productImage(p, blocked) {
    const disabled = blocked ? "disabled" : "";
    const action = `data-bf-action="edit-product:${esc(p.id)}"`;
    if (p.image) return `<button type="button" class="bf-product-photo-button" ${action} aria-label="Edit image for ${esc(p.name)}" ${disabled}><img src="${esc(p.image)}" alt="${esc(p.name)} source"><span class="bf-photo-edit" aria-hidden="true">Edit image</span></button>`;
    if (p.mode === "concept") return `<button type="button" class="bf-generation-tile" ${action} aria-label="${esc(p.name)}: Image will be generated. Edit product." ${disabled}><span class="bf-generation-symbol" aria-hidden="true">✳</span><span>Image will be generated.</span><span class="bf-photo-edit" aria-hidden="true">Edit image</span></button>`;
    return `<button type="button" class="bf-add-image" ${action} aria-label="Add image for ${esc(p.name)}" ${disabled}><span class="bf-add-image-plus" aria-hidden="true">+</span><span>Add image</span></button>`;
  }
  function products(v) {
    return `<div class="bf-products-top"><p>Give each product a source and a clear direction.</p><div class="pr-add-group">${button("＋ Add product", "add-product", "bf-primary bf-add-product", v.mutationsBlocked ? "disabled" : "")}<span class="pr-cap">${v.products.length} of ${v.productLimit} products</span></div></div>${v.products.length ? `<div class="bf-products">${v.products.map((p) => `<article class="bf-product"><div class="bf-product-art">${productImage(p,v.mutationsBlocked)}</div><div class="bf-product-info"><h3>${esc(p.name)}</h3><p>${esc(p.description)}</p>${p.attachmentHint ? `${button("Attach " + esc(p.attachmentHint), "attach-missing:" + p.id, "bf-small bf-attach-source")}` : ""}<div class="bf-row">${button("Edit product", "edit-product:" + p.id, "bf-small")}${p.image ? button("Inspect source", "source:" + p.id, "bf-small bf-quiet") : ""}</div></div><button type="button" class="bf-remove-product" data-bf-action="remove-product:${esc(p.id)}" aria-label="Remove ${esc(p.name)}" title="Remove product" ${v.mutationsBlocked ? "disabled" : ""}><span aria-hidden="true">×</span></button></article>`).join("")}</div>` : `<section class="bf-empty"><div class="bf-empty-symbol" aria-hidden="true">＋</div><div><h2>Bring the products.</h2><p>Choose an existing image or describe a new image to generate. Begin with one product and build the campaign from there.</p></div>${button("Add your first product", "add-product", "bf-primary")}</section>`}`;
  }
  function marketSelect(v) {
    return `<label class="pr-select-label"><span>Market</span><select data-bf-field="market">${v.markets.map(m => `<option value="${esc(m)}" ${v.language === m ? "selected" : ""}>${languages[m]}</option>`).join("")}</select></label>`;
  }
  function preview(v) {
    const p = v.products.find((p) => p.id === v.productId);
    return `<aside class="bf-preview ${v.preview?.note ? "bf-preview-noted" : ""}" aria-label="Product Preview"><div class="bf-preview-label"><strong>Product Preview</strong>${v.tab === "brand" ? marketSelect(v) : `<span>${languages[v.language]}</span>`}</div><div class="bf-preview-target"><select data-bf-field="previewProduct" aria-label="Preview product">${v.products.map((p) => `<option value="${esc(p.id)}" ${p.id === v.productId ? "selected" : ""}>${esc(p.name)}</option>`).join("") || '<option value="">No product selected</option>'}</select><div class="bf-formats" role="group" aria-label="Preview format">${["1:1", "9:16", "16:9"].map((r) => `<button type="button" data-bf-action="format:${r}" aria-label="Preview ${r}" aria-pressed="${v.ratio === r}">${r}</button>`).join("")}</div></div><div class="bf-preview-stage" data-bf-preview-stage>${previewStage(v.preview)}</div><div class="bf-preview-caption" data-bf-preview-caption>${previewCaption(v)}</div>${v.tab === "brand" && v.customPreviewColors ? `<p class="bf-hint">This image uses custom text colors. ${button("Use default for this image", "use-preview-default", "bf-small bf-quiet")}</p>` : ""}</aside>`;
  }
  function previewStage(p) {
    if (["ready", "source"].includes(p?.phase) && p.url)
      return `<img${p.kind === "typography" ? ' class="bf-typography-preview"' : ""} src="${esc(p.url)}" alt="${esc(p.alt || "Representative composition using the current Brief")}">`;
    if (p?.phase === "pending")
      return '<div class="bf-preview-busy" role="status"><i aria-hidden="true"></i>Updating preview…</div>';
    const messages = {
      empty: [
        "Choose a product to preview.",
        "Add a product and its imagery to see this brief take shape.",
      ],
      invalid: [p?.message || "Check the preview settings.", ""],
      failed: [
        "Preview unavailable.",
        "Your brief is still here. Try loading this preview again.",
      ],
      color: [
        "Choose how to use this color.",
        "Use a palette color, or add the new color before making it the default.",
      ],
    };
    const [title, body] = messages[p?.phase] || messages.empty;
    return `<div class="bf-preview-empty" role="status"><strong>${esc(title)}</strong>${body ? `<p>${esc(body)}</p>` : ""}${p?.phase === "failed" ? button("Retry preview", "retry-preview", "bf-small") : ""}</div>`;
  }
  function previewCaption(v) {
    const p = v.products.find((p) => p.id === v.productId),
      pv = v.preview || {};
    if (pv.kind === "source") return `<strong>Product photo</strong><br>${esc(pv.note)}`;
    return `${pv.kind === "typography" ? "No image exists for this product yet." : p ? `${esc(languages[v.language])} · ${esc(v.ratio)}` : "Select a product, language, and format."}${pv.note ? `<br>${esc(pv.note)}` : ""}`;
  }
  function inlineMessage(message, kind = "error", action) {
    return message
      ? `<div class="bf-inline-message ${kind === "info" ? "info" : ""}" role="status">${esc(message)}${action ? button(esc(action.label), action.action, "bf-small") : ""}</div>`
      : "";
  }
  function brand(v) {
    return `<div class="bf-layout bf-brand-layout"><section class="bf-panel"><div class="bf-brand-heading"><h2>One recognizable signature.</h2></div><div class="bf-brand-grid"><div>${field("Brand name", "brandName", v.brandName)}<div class="bf-field"><span class="bf-label">Brand logo</span><div class="bf-logo-card"><div class="bf-logo-art">${v.logo?.url ? `<img src="${esc(v.logo.url)}" alt="Selected logo">` : v.hasLogo ? '<span class="bf-hint">Logo attached</span>' : '<span class="bf-hint">No logo attached</span>'}</div>${button(v.logoStatus === "pending" ? "Checking logo…" : v.hasLogo ? "Replace logo" : "Attach logo", "upload-logo", "bf-small", v.logoStatus === "pending" ? "disabled" : "")}${v.logoStatus === "pending" ? button("Cancel upload", "cancel-logo-upload", "bf-small bf-quiet") : v.hasLogo ? button("Remove logo", "remove-logo", "bf-small bf-quiet") : ""}<input type="file" accept="image/png,image/jpeg" data-bf-upload hidden><p class="bf-hint">PNG or JPEG · up to 20 MiB / 40 MP<br>Transparency is optional.</p></div>${v.logo ? `<p class="bf-logo-facts">${esc(v.logo.filename)}<br>${esc(v.logo.type)} · ${v.logo.width} × ${v.logo.height}${v.logo.opaque ? " · original background kept" : ""}</p>` : ""}</div></div><div><div class="bf-field"><label for="bf-defaultColor">Default text color</label>${colorControl("defaultColor", "default text color", v.defaultDraft ?? v.color)}<p class="bf-hint">Applies across the campaign. Colors chosen for individual images in Studio stay unchanged.</p></div>${inlineMessage(v.colorMessage, v.colorAction ? "info" : "error", v.colorAction)}<div class="bf-palette-head"><h3>Brand palette</h3><div class="bf-row"><span>${v.palette.length} / 8 colors</span>${v.palette.length < 8 ? button("＋ Add color", "add-color", "bf-small bf-quiet") : ""}</div></div><div class="bf-palette">${v.palette.map((c, i) => `<div class="bf-palette-cell">${colorControl("palette-" + i, "palette color " + (i + 1), c, { compact: true })}<div class="bf-palette-meta">${normalizeHex(c) === v.color ? '<span class="bf-default">● Default</span>' : `<button type="button" data-bf-action="make-default:${i}">Make default</button>`}<button type="button" data-bf-action="remove-color:${i}" aria-label="Remove palette color ${i + 1}">×</button></div></div>`).join("")}</div><p class="bf-palette-hint">Six hex digits, with or without #. Matching colors are kept once.</p>${inlineMessage(v.paletteMessage, "error", v.paletteAction)}<p class="bf-font-note"><strong>Noto Sans by default.</strong> Choose Noto Sans or Barlow Condensed per Message and CTA in Studio. Existing custom styles stay.</p>${inlineMessage(v.logoMessage, v.logoStatus === "accepted" ? "info" : "error")}${v.fitted ? `<div class="bf-inline-message info">${esc(v.fitted)}</div>` : ""}</div></div></section>${preview(v)}</div>`;
  }
  function copyView(v) {
    const cc = v.copy[v.language],
      rules = v.rules[v.language] || [];
    return `<div class="bf-layout"><section class="bf-panel bf-copy-panel"><div class="bf-copy-title"><h2>Find the words.</h2>${marketSelect(v)}</div><div class="bf-copy-fields">${field("Headline", "message", cc.message, { area: true, errors: v.copyErrors?.message })}${field("Call to action · optional", "cta", cc.cta || "", { errors: v.copyErrors?.cta })}<p class="bf-hint">Updates every product for this language.</p></div><div class="bf-rule"></div>${scope.MotifWording.rules({language: v.language === "es-MX" ? "Spanish (Mexico)" : "English (United States)", words: rules.map(r => r.phrase)})}</section>${preview(v)}</div>`;
  }
  function renderBrief(v) {
    const next = {
      campaign: ["Next: Products", "tab:products"],
      products: ["Next: Brand", "tab:brand"],
      brand: ["Next: Copy & Rules", "tab:copy"],
    }[v.tab];
    const previous = { products: ['Brief','campaign'], brand: ['Products','products'], copy: ['Brand','brand'] }[v.tab];
    return `<div class="hlb"><header class="bf-head wf-stage-header"><div class="mf-page-title-group"><h1>Set the direction.</h1><p>Your products, your audience, and your exact words.</p></div>${button("Next: Production →", "production", "bf-primary", v.logoStatus === "pending" ? "disabled" : "")}</header><div class="bf-tab-row"><nav class="bf-tabs" role="tablist" aria-label="Campaign sections">${tabs.map(([id, label]) => `<button type="button" role="tab" id="bf-tab-${id}" aria-controls="bf-tab-panel" aria-selected="${v.tab === id}" tabindex="${v.tab === id ? 0 : -1}" data-bf-action="tab:${id}">${label}</button>`).join("")}</nav>${`<div class="wf-sequence-actions">${previous ? button("← Previous: " + previous[0], "tab:" + previous[1], "bf-quiet wf-previous") : ""}${next ? button(esc(next[0]) + " →", next[1], "bf-quiet") : ""}</div>`}</div><div id="bf-tab-panel" role="tabpanel" aria-labelledby="bf-tab-${v.tab}">${{ campaign, products, brand, copy: copyView }[v.tab](v)}</div></div>`;
  }
  function mount(root, { onField, onAction, onFile, onPreview }) {
    let value = null,
      sequence = 0,
      dead = false,
      pointerTarget = null,
      blurDestination = null,
      fileSelection = null,
      renderPending = false,
      renderTimer = null,
      paintedLayers = null;
    const controller = new AbortController(),
      opts = { signal: controller.signal };
    function clearFileSelection() {
      if (!fileSelection) return;
      const { input, finish } = fileSelection;
      input.removeEventListener("change", finish);
      input.removeEventListener("cancel", finish);
      fileSelection = null;
    }
    function selectFile() {
      clearFileSelection();
      const input = root.querySelector("[data-bf-upload]");
      // Preview/autosave renders may detach this input while the OS chooser is
      // open. Its owned listener must survive; root delegation cannot see it.
      const finish = (event) => {
        event.stopPropagation();
        if (dead || fileSelection?.finish !== finish) return;
        const file = event.type === "change" ? input.files?.[0] : null;
        input.value = "";
        clearFileSelection();
        if (file) onFile?.(file);
      };
      fileSelection = { input, finish };
      input.addEventListener("change", finish);
      input.addEventListener("cancel", finish);
      input.click();
    }
    function render(v) {
      sequence++;
      const previous = value;
      const target = x => x && JSON.stringify([x.id, x.productId, x.language, x.ratio]);
      const oldStage = root.querySelector("[data-bf-preview-stage]");
      const oldVisual = oldStage?.firstElementChild;
      const keepVisual = target(previous) === target(v) &&
        ["pending", "ready"].includes(v.preview?.phase) &&
        ["IMG", "CANVAS"].includes(oldVisual?.tagName);
      value = v;
      // Detaching a pressed checkbox cancels its native click, even if the
      // same node is restored. Keep it mounted until activation or release.
      if (pointerTarget?.matches?.('[data-bf-field][type="checkbox"]')) {
        renderPending = true;
        return;
      }
      clearTimeout(renderTimer);
      renderTimer = null;
      renderPending = false;
      pointerTarget = null;
      const destination = blurDestination,
        active = document.activeElement,
        key = !destination && root.contains(active) ? active?.id : null,
        activeField = !destination && root.contains(active) ? active?.dataset?.bfField : null,
        activeAction = !destination && root.contains(active) ? active?.dataset?.bfAction : null,
        selection = active?.selectionStart;
      root.innerHTML = renderBrief(v);
      const stage = root.querySelector("[data-bf-preview-stage]");
      if (keepVisual && stage?.replaceChildren) stage.replaceChildren(oldVisual);
      if (stage && v.preview?.phase === "pending" && paintedLayers?.key === v.previewLayerKey)
        paintLocalColor(stage, v.color);
      if (destination) {
        const attribute = destination.dataset.bfField != null ? "bfField" : "bfPicker",
          selector = attribute === "bfField" ? "data-bf-field" : "data-bf-picker",
          replacement = root.querySelector(`[${selector}="${CSS.escape(destination.dataset[attribute])}"]`);
        // Keep the unchanged incoming field alive so the browser can finish
        // its original focus and caret operation after the color blur.
        if (
          replacement?.tagName === destination.tagName &&
          replacement.type === destination.type &&
          replacement.value === destination.value &&
          replacement.checked === destination.checked
        ) replacement.replaceWith(destination);
      }
      root
        .querySelectorAll("[data-bf-mixed]")
        .forEach((el) => (el.indeterminate = true));
      if (v.brandUpdating)
        root.querySelectorAll("input, textarea, select, button").forEach(el => { el.disabled = true; });
      if (key || activeField || activeAction) {
        const el = root.querySelector(key ? "#" + CSS.escape(key)
          : activeField ? `[data-bf-field="${CSS.escape(activeField)}"]`
            : `[data-bf-action="${CSS.escape(activeAction)}"]`);
        if (el) {
          el.focus({ preventScroll: true });
          try {
            if (selection !== null) el.setSelectionRange(selection, selection);
          } catch {}
        }
      }
    }
    function releasePointer() {
      pointerTarget = null;
      if (!renderPending) return;
      clearTimeout(renderTimer);
      // The real change event can render first. Otherwise refresh after a
      // canceled/outside release, without synthesizing or accepting a click.
      renderTimer = setTimeout(() => {
        if (!dead && renderPending) render(value);
      }, 0);
    }
    const pointerRoot = root.ownerDocument || root;
    pointerRoot.addEventListener(
      "pointerdown",
      (e) => {
        pointerTarget = null;
        if (e.button !== 0 || e.isPrimary === false) return;
        const target = e.target.closest?.("[data-bf-action], [data-bf-field], [data-bf-picker]") ||
          e.target.closest?.("label")?.control;
        if (target && !target.disabled && root.contains(target))
          pointerTarget = target;
      },
      { ...opts, capture: true },
    );
    for (const event of ["pointerup", "pointercancel"])
      pointerRoot.addEventListener(
        event,
        releasePointer,
        opts,
      );
    root.addEventListener(
      "click",
      (e) => {
        pointerTarget = null;
        const el = e.target.closest("[data-bf-action]");
        if (!el || el.disabled) return;
        e.preventDefault();
        e.stopPropagation();
        if (el.dataset.bfAction === "upload-logo") {
          selectFile();
          return;
        }
        onAction?.(el.dataset.bfAction);
      },
      opts,
    );
    root.addEventListener(
      "input",
      (e) => {
        const el = e.target;
        if (el.matches("[data-bf-picker]")) {
          e.stopPropagation();
          onField?.(el.dataset.bfPicker, el.value, {
            commit: true,
            picker: true,
          });
        } else if (
          el.matches("[data-bf-field]:not([type=checkbox]):not(select)")
        ) {
          e.stopPropagation();
          onField?.(el.dataset.bfField, el.value, { commit: false });
        }
      },
      opts,
    );
    root.addEventListener(
      "change",
      (e) => {
        const el = e.target;
        if (el.matches("[data-bf-upload]")) {
          e.stopPropagation();
          const file = el.files?.[0];
          el.value = "";
          if (!dead && !fileSelection && root.contains(el) && file) onFile?.(file);
          return;
        }
        if (
          el.matches("[data-bf-field][type=checkbox],select[data-bf-field]")
        ) {
          e.stopPropagation();
          onField?.(
            el.dataset.bfField,
            el.type === "checkbox" ? el.checked : el.value,
            { commit: true },
          );
        }
      },
      opts,
    );
    root.addEventListener(
      "focusout",
      (e) => {
        const el = e.target;
        if (
          el.matches("[data-bf-field]") &&
          /^(defaultColor|palette-)/.test(el.dataset.bfField)
        ) {
          e.stopPropagation();
          // Safari pointer buttons need not take focus. Keep their intended
          // action mounted; only the later click/keyboard activation accepts it.
          const action = e.relatedTarget?.closest?.("[data-bf-action]") ||
            pointerTarget?.closest?.("[data-bf-action]");
          if (action && !action.disabled && root.contains(action)) return;
          const destination = e.relatedTarget?.matches?.("[data-bf-field], [data-bf-picker]")
            ? e.relatedTarget : pointerTarget;
          blurDestination = root.contains(destination) && destination?.matches?.(
            "[data-bf-field], [data-bf-picker]",
          ) ? destination : null;
          try {
            onField?.(el.dataset.bfField, el.value, { commit: true });
          } finally {
            blurDestination = null;
          }
        }
      },
      opts,
    );
    root.addEventListener(
      "keydown",
      (e) => {
        releasePointer();
        const tab = e.target.closest("[role=tab]");
        if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key))
          return;
        e.preventDefault();
        e.stopPropagation();
        const current = tabs.findIndex((x) => x[0] === value.tab);
        const i =
          e.key === "Home"
            ? 0
            : e.key === "End"
              ? 3
              : (current + (e.key === "ArrowRight" ? 1 : 3)) % 4;
        onAction?.("tab:" + tabs[i][0]);
        root.querySelector("#bf-tab-" + tabs[i][0])?.focus();
      },
      opts,
    );
    function paintLocalColor(stage, color) {
      if (!paintedLayers || !/^#[0-9A-F]{6}$/i.test(color || "")) return;
      const canvas = document.createElement("canvas"), mask = document.createElement("canvas");
      canvas.width = mask.width = paintedLayers.base.naturalWidth;
      canvas.height = mask.height = paintedLayers.base.naturalHeight;
      const context = canvas.getContext("2d"), tint = mask.getContext("2d");
      context.drawImage(paintedLayers.base, 0, 0);
      for (const block of paintedLayers.blocks) {
        tint.clearRect(0, 0, mask.width, mask.height);
        tint.globalCompositeOperation = "source-over";
        tint.drawImage(block.image, 0, 0);
        tint.globalCompositeOperation = "source-in";
        tint.fillStyle = block.color || color;
        tint.fillRect(0, 0, mask.width, mask.height);
        context.drawImage(mask, 0, 0);
      }
      canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", "Current campaign preview");
      stage.replaceChildren(canvas);
    }
    async function decodePreview(url) {
      const image = new Image(); image.src = url; await image.decode(); return image;
    }
    async function previewRequest(request) {
      const mine = ++sequence,
        identity = request.identity;
      const stage = root.querySelector("[data-bf-preview-stage]");
      if (!stage || !onPreview) return;
      if (!["IMG", "CANVAS"].includes(stage.firstElementChild?.tagName)) stage.innerHTML = previewStage({ phase: "pending" });
      try {
        let result = await onPreview(request), im, layers;
        for (let attempt = 0; ; attempt++) {
          if (dead || mine !== sequence) return;
          if (result.identity !== identity) throw Error("Preview identity mismatch");
          try {
            im = await decodePreview(result.url);
            layers = null;
            if (result.layers) {
              const [base, blocks] = await Promise.all([
                decodePreview(result.layers.base_url),
                Promise.all(Object.values(result.layers.text_blocks).map(async block =>
                  ({ color: block.color, image: await decodePreview(block.url) }))),
              ]);
              layers = { key: result.layerKey, base, blocks };
            }
            break;
          } catch (error) {
            if (dead || mine !== sequence) return;
            if (attempt || result.version === undefined) throw error;
            // Keep the current visual mounted. The host permits this single
            // retry only if our own save expired the first image's URL.
            result = await onPreview({ ...request, expiredVersion: result.version });
          }
        }
        if (dead || mine !== sequence) return;
        value.preview = { ...result, phase: "ready" };
        paintedLayers = layers;
        if (stage.replaceChildren) {
          im.alt = result.alt || "Current campaign preview";
          if (result.kind === "typography") im.className = "bf-typography-preview";
          stage.replaceChildren(im);
        } else stage.innerHTML = previewStage(value.preview);
        root.querySelector("[data-bf-preview-caption]").innerHTML =
          previewCaption(value);
      } catch {
        if (dead || mine !== sequence) return;
        value.preview = { phase: "failed" };
        stage.innerHTML = previewStage(value.preview);
      }
    }
    return {
      render,
      preview: previewRequest,
      destroy() {
        dead = true;
        clearFileSelection();
        pointerTarget = null;
        blurDestination = null;
        renderPending = false;
        clearTimeout(renderTimer);
        sequence++;
        controller.abort();
        root.innerHTML = "";
      },
    };
  }
  scope.MotifBriefFlow = {
    renderBrief,
    mount,
    normalizeHex,
    colorControl,
    previewStage,
    previewCaption,
    esc,
    button,
  };
})(typeof window !== "undefined" ? window : globalThis);
