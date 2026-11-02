/* Motif E108 presentation. Host owns transactions, identity, validation, and I/O. */
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
  const fields = [
    ["message", "Headline"],
    ["cta", "Call to action"],
  ];
  function wordingFields({ values, all = {} }) {
    return fields
      .map(
        ([id, label]) =>
          `<div class="mf-word-field" data-mf-field="${id}"><div class="mf-word-label"><label for="mf-word-${id}">${label}</label><label class="mf-all-products"><input type="checkbox" data-mf-all="${id}" aria-label="All products for ${esc(label)}" ${all[id] ? "checked" : ""}>All products</label></div>${id === "cta" ? `<input id="mf-word-${id}" data-mf-value="${id}" value="${esc(values[id])}" autocomplete="off">` : `<textarea id="mf-word-${id}" data-mf-value="${id}" rows="3">${esc(values[id])}</textarea>`}<span class="mf-field-scope" data-mf-scope="${id}" hidden></span></div>`,
      )
      .join("");
  }
  function readFields(root) {
    const values = {},
      all = {};
    root
      .querySelectorAll("[data-mf-value]")
      .forEach((el) => (values[el.dataset.mfValue] = el.value));
    root
      .querySelectorAll("[data-mf-all]")
      .forEach((el) => (all[el.dataset.mfAll] = el.checked));
    return { values, all };
  }
  function changes({
    initial,
    values,
    all,
    openingProductId,
    productIds,
    market,
  }) {
    const unique = [...new Set(productIds)];
    if (!unique.includes(openingProductId))
      throw Error("Opening product is outside the campaign");
    return {
      market,
      openingProductId,
      fields: fields
        .filter(
          ([id]) =>
            Object.hasOwn(values, id) &&
            (all[id] || values[id] !== String(initial[id] ?? "")),
        )
        .map(([id]) => ({
          field: id,
          value: values[id],
          scope: all[id] ? "all" : "product",
          productIds: all[id] ? [...unique] : [openingProductId],
        })),
    };
  }
  function paintScopes(root, patch, openingProductName) {
    root.querySelectorAll("[data-mf-scope]").forEach((el) => {
      const item = patch.fields.find((f) => f.field === el.dataset.mfScope);
      el.hidden = !item;
      el.textContent = item
        ? item.scope === "all"
          ? `${item.productIds.length} products`
          : `${openingProductName} only`
        : "";
    });
  }
  function textStyleControls({
    fontId = "noto_sans",
    color = null,
    campaignColor,
    palette,
  }) {
    const colors = [...new Set(palette.map((v) => v.toUpperCase()))],
      effective = (color || campaignColor).toUpperCase();
    if (!["noto_sans", "barlow_condensed"].includes(fontId))
      throw Error("Unsupported bundled font");
    if (
      colors.some((v) => !/^#[0-9A-F]{6}$/.test(v)) ||
      !/^#[0-9A-F]{6}$/.test(effective)
    )
      throw Error("Invalid brand palette");
    if (color !== null && !colors.includes(color.toUpperCase()))
      throw Error("Color is outside the brand palette");
    return `<section class="mf-text-style" aria-label="Selected text style"><label for="mf-text-font">Font</label><select id="mf-text-font" data-mf-font><option value="noto_sans" ${fontId === "noto_sans" ? "selected" : ""}>Noto Sans</option><option value="barlow_condensed" ${fontId === "barlow_condensed" ? "selected" : ""}>Barlow Condensed</option></select><div class="mf-color-label">Brand color</div><div class="mf-swatches" role="group" aria-label="Brand palette">${colors.map((value) => `<button type="button" class="mf-swatch" data-mf-color="${value}" style="--swatch:${value}" aria-label="Brand color ${value}" aria-pressed="${value === effective}" title="${value}"></button>`).join("")}<button type="button" class="mf-color-default" data-mf-color-default aria-label="Use campaign text color" aria-pressed="${color === null}">Default</button></div></section>`;
  }
  function selection(label) {
    return `<div class="mf-direct-selection" tabindex="0" role="group" aria-label="${esc(label)}. Drag inside to move; corners resize. Arrow keys move; Shift moves ten pixels."><span class="mf-direct-label" aria-hidden="true">${esc(label)}</span>${["nw", "ne", "sw", "se"].map((c) => `<button type="button" class="mf-direct-handle ${c}" data-mf-corner="${c}" aria-label="Resize ${esc(label)} from ${{ nw: "top left", ne: "top right", sw: "bottom left", se: "bottom right" }[c]}. Arrow keys resize. Shift uses ten pixels."></button>`).join("")}</div>`;
  }
  function switchPrompt({ product, format, replacement = false }) {
    return {
      title: `${replacement ? "Review" : "Apply"} changes before switching?`,
      body: `${product} · ${format}`,
      primary: replacement ? "Review and switch" : "Apply and switch",
      discard: "Discard and switch",
      cancel: "Keep editing",
    };
  }
  const copy = {
    unchanged: "",
    previewing: "Preparing preview…",
    applyFailed: "Could not apply. Your edits are still here.",
    uncertain:
      "The update could not be confirmed. Check the current image before trying again.",
    stale:
      "This image changed. Use the latest image to preview your edits again.",
    missing:
      "This image is unavailable. Cancel or close to discard these edits.",
    noFit:
      "This text does not fit. Reduce its size, move it, or edit the wording.",
    noReference: "No visible reference pixels to verify.",
  };
  const api = {
    esc,
    textStyleControls,
    wordingFields,
    readFields,
    changes,
    paintScopes,
    selection,
    switchPrompt,
    copy,
  };
  scope.MotifEditorControls = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
