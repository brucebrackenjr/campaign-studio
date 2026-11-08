/* Shared server contract; never use UTF-16 maxlength or truncate authored text. */
(function (scope) {
  "use strict";
  const contract = JSON.parse(document.getElementById("motif-text-limits").textContent);
  const count = value => Array.from(String(value ?? "")).length;
  const entries = value => value && typeof value === "object" && !Array.isArray(value) ? Object.entries(value) : [];
  const list = value => Array.isArray(value) ? value : [];
  function purposeIssue(purpose, value) {
    const limit = contract.limits[purpose];
    return typeof value === "string" && limit && count(value) > limit
      ? `${contract.labels[purpose]}: Use no more than ${limit.toLocaleString()} characters.` : null;
  }
  const issue = (id, value) => purposeIssue(contract.fields[id], value);
  function authoredIssue(authored = {}) {
    const candidates = [], add = (object, fields) => {
      for (const [field, purpose] of Object.entries(fields)) candidates.push([purpose, object?.[field]]);
    };
    const origin = object => add(object, {label: "origin_label", rights: "rights"});
    const wording = object => add(object, {message: "headline", cta: "cta"});
    const brief = authored.brief;
    add(brief, {name: "campaign_name", audience: "audience", creative_direction: "direction"});
    add(brief?.brand, {name: "brand_name", logo_path: "file_reference"});
    origin(brief?.brand?.logo_provenance); origin(brief?.copy_provenance);
    for (const [, market] of entries(brief?.markets)) {
      wording(market?.copy);
      for (const rule of list(market?.phrase_rules)) add(rule, {phrase: "phrase"});
    }
    for (const product of list(brief?.products)) {
      add(product, {name: "product_name", description: "description"});
      add(product?.source, {path: "file_reference", description: "description"});
      origin(product?.source?.provenance);
      for (const [, copy] of entries(product?.copy_overrides)) wording(copy);
    }
    add(brief?.replay, {history: "replay_history"});
    for (const [, source] of entries(brief?.replay?.sources)) {
      add(source, {path: "file_reference", model: "replay_model"}); origin(source?.provenance);
    }
    // Pending fields can outlive their visible tab. Validate the captured work,
    // not only the inputs currently mounted in the document.
    const forms = entries(authored.form_drafts?.fields).map(([, value]) => value);
    forms.push(...entries(authored.review_drafts).map(([, value]) => value?.values));
    for (const form of forms)
      for (const [id, value] of entries(form)) candidates.push([contract.fields[id], value]);
    return candidates.map(([purpose, value]) => purposeIssue(purpose, value)).find(Boolean) || null;
  }
  const ownedInvalid = new WeakMap();
  let live = null;
  function announce(message) {
    if (!live) {
      live = document.createElement("span");
      live.className = "mf-text-limit-live";
      live.setAttribute("role", "status");
      live.setAttribute("aria-live", "polite");
      live.setAttribute("aria-atomic", "true");
      document.body.append(live);
    }
    if (live.textContent !== message) live.textContent = message;
  }
  function update(input, spoken = false) {
    const purpose = input.dataset?.lengthPurpose || contract.fields[input.id];
    if (!purpose) return;
    const limit = contract.limits[purpose], length = count(input.value), over = length > limit;
    const id = input.id + "-length";
    let note = document.getElementById(id);
    if (!note) {
      note = document.createElement("small");
      note.id = id;
      note.className = "mf-text-limit";
      // A wrapping label must keep its original name. The separate status
      // announces edits; aria-describedby exposes the count on focus.
      note.setAttribute("aria-hidden", "true");
      const focused = document.activeElement === input;
      const selection = focused ? [input.selectionStart, input.selectionEnd] : null;
      const holder = document.createElement("span");
      holder.className = "mf-limited-input";
      input.before(holder);
      holder.append(input, note);
      if (focused) {
        input.focus({preventScroll: true});
        input.setSelectionRange(...selection);
      }
    }
    const message = over ? `${length.toLocaleString()} / ${limit.toLocaleString()}` : "";
    if (note.textContent !== message) note.textContent = message;
    note.hidden = !over;
    input.toggleAttribute("data-length-invalid", over);
    if (over) input.parentElement.style.setProperty("--mf-limit-width", `${note.getBoundingClientRect().width}px`);
    const wasInvalid = ownedInvalid.has(input);
    if (over) {
      if (!wasInvalid) ownedInvalid.set(input, { value: input.getAttribute("aria-invalid"), wording: input.getAttribute("data-wording-invalid") !== null });
      input.setAttribute("aria-invalid", "true");
      input.setCustomValidity(purposeIssue(purpose, input.value));
    } else if (wasInvalid) {
      const prior = ownedInvalid.get(input);
      const previous = prior.wording && input.getAttribute("data-wording-invalid") === null ? null : prior.value;
      if (previous === null) input.removeAttribute("aria-invalid");
      else input.setAttribute("aria-invalid", previous);
      input.setCustomValidity("");
      ownedInvalid.delete(input);
    }
    const descriptions = new Set((input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
    descriptions.add(id);
    input.setAttribute("aria-describedby", [...descriptions].join(" "));
    if (spoken && (over || wasInvalid))
      announce(over ? `${contract.labels[purpose]}: ${length.toLocaleString()} of ${limit.toLocaleString()} characters. Shorten this field.` : `${contract.labels[purpose]} is within the limit.`);
  }
  function guardFields(ids) {
    for (const id of ids) {
      const input = document.getElementById(id);
      if (!input) continue;
      update(input);
      const purpose = input.dataset?.lengthPurpose || contract.fields[id];
      if (purposeIssue(purpose, input.value)) {
        input.focus(); input.reportValidity(); return false;
      }
    }
    return true;
  }
  function scan() {
    document.querySelectorAll("input[id],textarea[id]").forEach(input => update(input));
  }
  document.addEventListener("input", event => update(event.target, true));
  new MutationObserver(scan).observe(document.body, {childList: true, subtree: true});
  scan();
  scope.MotifTextLimits = {count, contract, issue, purposeIssue, authoredIssue, guardFields, update};
})(window);
