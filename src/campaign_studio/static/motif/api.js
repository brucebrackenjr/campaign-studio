"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const copy = (value) => structuredClone(value);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const LANG = { en: "English (United States)", es: "Spanish (Mexico)" };
const MARKET = { en: "en-US", es: "es-MX" };
const FMT = [
  { name: "Square", ratio: "1:1", w: 1080, h: 1080 },
  { name: "Story", ratio: "9:16", w: 1080, h: 1920 },
  { name: "Landscape", ratio: "16:9", w: 1920, h: 1080 },
];
function decodedImage(url) {
  if (!url) return Promise.reject(new Error("Image unavailable"));
  const image = new Image();
  image.src = url;
  return image.decode().then(() => image);
}
let productLimit = Number(document.body.dataset.cap);
let setupNotice = null;
let saved = [],
  model = null,
  draft = null,
  baseline = "",
  C = {},
  page = document.body.dataset.page;
let tab = "campaign",
  lang = "en",
  modal = "",
  modalProduct = null,
  currentProduct = 0,
  ruleIndex = -1;
// Brief comparison for presentation only. The shared project queue owns the
// whole-project dirty state used by Save, autosave, and navigation guards.
let dirty = false,
  pendingNav = "",
  lastFocus = null,
  operation = null,
  keyAvailable = false,
  busy = false;

async function api(path, body, options = {}) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined
        ? {}
        : {
            "Content-Type": "application/json",
            "X-Campaign-Token": $('meta[name="campaign-token"]').content,
          },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    const fields = [...new Set((data.field_errors || []).map(
      (f) => `${f.label}: ${f.message}`,
    ))];
    const error = new Error(
      fields.length
        ? fields.join(" ")
        : data.message || "The action could not finish.",
    );
    error.fields = data.field_errors || [];
    error.status = response.status;
    error.code = data.code;
    error.proposal = data.proposal;
    error.capacity = data.capacity;
    error.input_image = data.input_image;
    error.revision_number = data.revision_number;
    error.project_state = data.project_state;
    throw error;
  }
  return data;
}

function assetURL(reference, productId = null) {
  if (/\/Assets\/(?:Products|Brand)\/[^/]+$/.test(reference || ""))
    return `/uploads/${reference.split("/").pop()}`;
  if (reference && model?.run)
    return productId
      ? `/campaigns/${draft.campaign_id}/sources/${productId}/1x1`
      : `/campaigns/${draft.campaign_id}/logo`;
  return null;
}

function productDetails(product) {
  return {
    id: product.id,
    name: product.name,
    description: product.description,
    treatment: {
      finished_scene: "reuse",
      prepared_product: "prepared",
      concept: "concept",
    }[product.source.role],
    origin: product.source.provenance.label,
    rights: product.source.provenance.rights,
    image: product.source.role === "concept"
      ? model?.concept_previews?.[product.id] || null
      : assetURL(product.source.path, product.id),
    source: copy(product.source),
  };
}

function viewModel() {
  if (!draft) {
    C = { name: "", products: [], markets: ["en", "es"], drafts: {} };
    return;
  }
  const value =
    ["review", "handoff"].includes(page) && model?.output_brief
      ? model.output_brief
      : page === "production" &&
          model?.plan_brief &&
          !(
            typeof activeBlockedProposal === "function" &&
            activeBlockedProposal()
          )
        ? model.plan_brief
        : draft;
  C = {
    id: value.campaign_id,
    name: value.name,
    brand: value.brand.name,
    audience: value.audience,
    scene: value.creative_direction,
    markets: Object.keys(value.markets).map((market) =>
      market === "es-MX" ? "es" : "en",
    ),
    color: value.brand.text_color,
    palette: value.brand.palette,
    logo: assetURL(value.brand.logo_path),
    drafts: {},
    revision: model?.revision_number || 0,
    descriptions: {},
    overrides: {},
    layout: {},
    products: value.products.map((p) => {
      const product = productDetails(p);
      if (["review", "handoff"].includes(page) && model?.manifest)
        product.image = `/campaigns/${value.campaign_id}/runs/${model.output_run_id}/revisions/${model.manifest.revision_id}/sources/${p.id}`;
      return product;
    }),
    copy: {},
    rules: {},
  };
  if (!C.markets.includes(lang)) lang = C.markets[0] || "en";
  for (const [short, market] of Object.entries(MARKET)) {
    const content = value.markets[market] || {
      copy: { message: "", cta: null },
      phrase_rules: null,
    };
    C.copy[short] = {
      headline: content.copy.message,
      cta: content.copy.cta || "",
    };
    C.rules[short] = content.phrase_rules || [];
    for (const product of value.products) {
      const override = product.copy_overrides?.[market];
      if (override) {
        C.overrides[product.id] ||= {};
        C.overrides[product.id][short] = Object.fromEntries(
          Object.entries(override).map(([key, text]) => [
            key === "message" ? "headline" : key,
            text || "",
          ]),
        );
      }
      for (let format = 0; format < FMT.length; format++) {
        const row =
          model?.metadata?.creatives?.[
            `${product.id}/${market}/${FMT[format].ratio.replace(":", "x")}`
          ];
        if (row) {
          C.descriptions[`${product.id}-${short}-${format}`] =
            row.description || "";
        }
      }
    }
  }
}

function useData(data, projectOptions = {}) {
  const previous = model,
    previousDraft = draft;
  if (
    draft?.campaign_id !== data.value.campaign_id ||
    operation?.run_id !== data.run?.id
  ) {
    operation = null;
  }
  if (draft?.campaign_id !== data.value.campaign_id) {
    selected = [0, 1];
    imageMarket = "all";
    pageNum = 0;
    filter = "all";
  }
  model = data;
  model.concept_preview_version = data.version;
  draft = previousDraft?.campaign_id === data.value.campaign_id && data.project
    ? previousDraft : copy(data.value);
  baseline = JSON.stringify(draft);
  dirty = false;
  if (typeof receiveProject === "function") receiveProject(data, previous, projectOptions);
  viewModel();
}
function isCurrentRun(campaignId, runId) {
  return draft?.campaign_id === campaignId && model?.run?.id === runId;
}
async function refreshCatalog() {
  const result = await api("/api/motif/workspace");
  saved = result.campaigns;
  setupNotice = result.setup_notice || null;
  pendingCleanup = result.pending_cleanup || [];
  productLimit = result.product_limit;
  keyAvailable = result.key_available;
}
async function openCampaign(id, target = null) {
  closeModal();
  try {
    useData(
      await api(
        `/api/motif/campaigns/${encodeURIComponent(id)}`,
      ),
    );
  } catch (error) {
    if (error.code === "not_found") {
      await refreshCatalog();
      if (page === "library") render();
      throw error;
    }
    throw error;
  }
  const runId = model.run?.id;
  if (runId) {
    const status = await api(`/api/campaigns/${encodeURIComponent(id)}/status`);
    if (!isCurrentRun(id, runId)) return;
    if (status.run_id === runId) operation = status;
  }
  const savedPage = model.project?.authored.ui_context?.page;
  page = target || (["campaign", "production", "review", "handoff"].includes(savedPage)
    ? savedPage : model.manifest ? "review" : "campaign");
  if (page === "library") page = model.manifest ? "review" : "campaign";
  if (page === "review" && model.project?.authored.ui_context?.page !== "review") {
    reviewTab = "products";
  }
  if (!model.project?.authored.ui_context?.tab) tab = "campaign";
  closeModal({ retain: false });
  clearToast();
  updateURL();
  render();
  await reopenProjectEditor(model.project?.authored.ui_context);
  if (!operation?.active && operation?.notice_code === "storage_capacity")
    showNewProductionFailure();
  if (operation?.active) setTimeout(() => poll(id, runId), 700);
}
function updateURL() {
  const route = page === "review" ? "studio" : page;
  history.replaceState(
    {},
    "",
    page === "library"
      ? "/"
      : `/campaigns/${draft.campaign_id}/${route}`,
  );
}
function campaignIdentity(name = draft?.name, brand = draft?.brand?.name || "") {
  const saved = model?.project?.authored?.brief;
  const acknowledged = saved?.campaign_id === draft?.campaign_id ? saved : null;
  const valid = (value, purpose, fallback = "") => {
    if (typeof value === "string" && !MotifTextLimits.purposeIssue(purpose, value)) return value;
    return typeof fallback === "string" && !MotifTextLimits.purposeIssue(purpose, fallback) ? fallback : "";
  };
  // Invalid drafts remain fully editable. The header keeps this campaign's
  // acknowledged identity until its name fits; it never slices the text.
  return {
    name: valid(name, "campaign_name", acknowledged?.name) || "Untitled campaign",
    brand: valid(brand, "brand_name", acknowledged?.brand?.name),
  };
}
function refreshCampaignIdentity(name = draft?.name, brand = draft?.brand?.name || "") {
  const current = campaignIdentity(name, brand), label = current.name;
  brand = current.brand;
  const identity = [brand, label].filter(Boolean).join(" · ");
  const selector = $('.top-context [data-action="campaigns"]');
  if (selector) {
    selector.title = identity;
    selector.querySelector(".campaign-heading-name").textContent = label;
    const brandRow = selector.querySelector(".campaign-heading-brand");
    brandRow.textContent = brand;
    brandRow.hidden = !brand.trim();
  }
  MotifIdentityLayout.fit(selector);
  document.title = `Motif – Campaign Studio${page === "library" ? "" : ` · ${identity}`}`;
}
async function saveDraft() {
  if (!(await saveProject()))
    throw new Error(getSaveState()?.validationMessage || "Your work is still here. Finish saving before reviewing its production plan.");
}

function markDirty() {
  dirty = JSON.stringify(draft) !== baseline;
  viewModel();
  if (typeof projectChanged === "function") projectChanged();
}
function damagedFiles() {
  return model?.file_integrity?.damaged_outputs?.length || 0;
}
function total() {
  return (C.products?.length || 0) * (C.markets?.length || 0) * FMT.length;
}
function compactUSD(value) {
  return value > 0 && value < 0.01
    ? "< $0.01"
    : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
}
function incompleteCostReason(cost, detail = false) {
  const reasons = cost.incomplete_reasons || [];
  if (reasons.length !== 1) return "Usage or pricing could not be verified.";
  const { code, count } = reasons[0];
  if (detail && count === 1 && ["connection", "timeout"].includes(code))
    return `No usable usage record was saved ${code === "connection" ? "after the connection was lost" : "before the request timed out"}. This request’s cost could not be estimated.`;
  return ({ missing_usage: "Usage was not recorded.", connection: "Usage was not recorded.", timeout: "Usage was not recorded.", pricing: "Saved pricing is unavailable.", request: "Saved request settings are unavailable.", usage: "Saved usage could not be read." })[code] || "Usage or pricing could not be verified.";
}
function campaignMetricDisplay(summary = model?.campaign_metrics, campaignId = C.id, loading = !model) {
  if (!summary || summary.campaign_id !== campaignId) {
    const value = loading ? "Loading…" : "Unavailable";
    return { attempts: value, attempts_helper: "", spend: value, spend_helper: loading ? "" : "Saved totals could not be loaded.", known_spend: false, detail: "" };
  }
  const complete = summary.coverage === "complete",
    count = summary.attempted_requests,
    knownCount = Number.isInteger(count) && count >= 0,
    cost = summary.usage_cost_estimate || {},
    estimate = complete ? cost.estimated_usd : null,
    knownSpend = Number.isFinite(estimate) && estimate >= 0,
    subtotal = cost.known_usage_subtotal_usd,
    zero = complete && knownCount && count === 0 && estimate === 0;
  return {
    attempts: complete && knownCount ? String(count) : knownCount && count > 0 ? `${count}+` : "Not recorded",
    attempts_helper: zero ? "No generation attempts in this campaign." : complete ? "" : knownCount && count > 0 ? "Recorded attempts · history incomplete." : "Earlier records are incomplete.",
    spend: knownSpend ? compactUSD(estimate) : "Incomplete",
    known_spend: knownSpend,
    has_known_portion: !knownSpend && Number.isFinite(subtotal) && subtotal > 0,
    spend_helper: zero ? "No generation attempts in this campaign." : knownSpend ? "USD · usage-based · not a bill" : Number.isFinite(subtotal) && subtotal > 0 ? `Known portion: ${compactUSD(subtotal)} USD.` : complete ? incompleteCostReason(cost) : "Earlier records are incomplete.",
    detail: knownSpend ? "" : `${cost.unreported_attempts ? `${cost.unreported_attempts} ${cost.unreported_attempts === 1 ? "request" : "requests"} could not be estimated. ` : ""}${complete ? incompleteCostReason(cost, true) : knownCount && count > 0 ? "Earlier request records are incomplete. The recorded count is a minimum." : "Earlier records are incomplete."}`,
  };
}
function sourceArt(index) {
  return C.products[index]?.image || "/static/motif/empty.svg";
}
const art = sourceArt;
const treatment = (role) =>
  ({
    reuse: "Choose existing image",
    concept: "Generate new image",
    prepared: "Prepared product + saved setting",
  })[role];
const badge = (text, kind = "") =>
  `<span class="tag ${kind}">${esc(text)}</span>`;
const btn = (text, action, kind = "", extra = "") =>
  `<button class="btn ${kind}" data-action="${action}" ${extra}>${text}</button>`;
const input = (label, id, value, type = "text", hint = "") =>
  `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="${type}" value="${esc(value)}">${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;
const area = (label, id, value, hint = "") =>
  `<div class="field"><label for="${id}">${label}</label><textarea id="${id}">${esc(value)}</textarea>${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;
const notice = (title, body = "", kind = "", action = "", text = "") =>
  `<div class="notice ${kind}" role="${kind === "block" ? "alert" : "status"}"><span class="icon">${kind === "block" ? "⊗" : kind === "info" ? "↗" : "△"}</span><div class="grow"><strong>${esc(title)}</strong>${body ? `<p>${esc(body)}</p>` : ""}</div>${action ? btn(text, action, "small") : ""}</div>`;

let reviewTab = "products",
  selected = [0, 1],
  formats = [0, 0],
  pageNum = 0,
  imageMarket = "all",
  filter = "all";
let currentFormat = 0;
function outputFor(product, language, format) {
  return model?.manifest?.outputs.find(
    (item) =>
      item.product_id === C.products[product]?.id &&
      item.market === MARKET[language] &&
      item.ratio === FMT[format].ratio,
  );
}
function creative(product = 0, language = lang, format = 0, extra = "") {
  const output = outputFor(product, language, format);
  if (!output?.url) {
    const withheld = model?.rule_withheld_outputs?.includes(output?.id);
    return `<div class="missing-tile"><strong>${withheld ? "Image withheld by current rules." : "Image not available"}</strong><p class="small">${esc(withheld ? "Correct the prohibited wording above to update this image." : operation?.notice_code === "source_unavailable" ? "Replace the unavailable source in Production before rebuilding this image." : output?.omission || "Create campaign images before reviewing this format.")}</p></div>`;
  }
  const description =
    C.descriptions[`${C.products[product].id}-${language}-${format}`];
  return `<img class="creative-image" src="${esc(output.url)}" alt="${esc(description || `Image description not supplied. ${C.products[product].name}, ${LANG[language]}, ${FMT[format].name}.`)}" ${extra}>`;
}
function valueFor(product, language, field) {
  const id = C.products[product]?.id;
  if (field in (C.overrides[id]?.[language] || {})) return C.overrides[id][language][field];
  return C.copy[language]?.[field] || "";
}
function descriptionCount() {
  return Object.values(C.descriptions).filter(Boolean).length;
}
// Metadata depends on a saved image; recomposition additionally needs its verified inputs.
function canDescribeCreative(product, language, format) {
  return (
    model?.can_describe &&
    !!outputFor(product, language, format)?.url
  );
}
function canEditProduct(product) {
  return (
    model?.can_edit &&
    !!model.editable_products?.[C.products[product].id]
  );
}
function canEditLogo(product) {
  return !!model?.logo_editable_products?.[C.products[product]?.id];
}
