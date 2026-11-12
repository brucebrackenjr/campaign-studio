const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function cache() {
  const requests = [],
    revoked = [];
  const context = vm.createContext({
    C: { id: "campaign" },
    model: {
      output_brief: {
        products: [{ id: "p", source: { path: "source.png" }, framing: {} }],
        brand: { logo_path: "logo.png", text_color: "#000000" },
      },
      visual_sources: { p: "source-sha" },
      visual_logo: "logo-sha",
      manifest: {
        outputs: [
          {
            product_id: "p",
            market: "en-US",
            ratio: "1:1",
            copy: { message: "Hello" },
          },
        ],
      },
    },
    Image: class {
      constructor() {
        this.naturalWidth = 640;
        this.naturalHeight = 640;
      }
      async decode() {}
    },
    URL: {
      createObjectURL: () => `blob:${requests.length}`,
      revokeObjectURL: (url) => revoked.push(url),
    },
    fetch: async (url) => {
      requests.push(url);
      return {
        ok: !url.includes("failure"),
        blob: async () => ({ type: "image/png", size: 64 }),
      };
    },
    window: {},
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(
    fs.readFileSync(
      require.resolve("../src/campaign_studio/static/motif/visuals.js"),
      "utf8",
    ),
    context,
  );
  return { visuals: context.window.MotifVisuals, context, requests, revoked };
}
const visual = (version = "v1", format = "1x1", campaign = "campaign") =>
  `/api/campaigns/${campaign}/composition/${version}/thumbnails/p/en-US/${format}?edge=640`;

test("visual reuse survives save-version changes but distinguishes changed content and campaign", async () => {
  const { visuals, context, requests } = cache();
  const square = visuals.load(visual());
  await square.ready;
  assert.equal(visuals.load(visual("v2")), square);
  context.model.output_brief.products[0].framing = {
    "en-US": { "9:16": { zoom: 1.3 } },
  };
  assert.equal(
    visuals.load(visual("v3")),
    square,
    "editing Story preserves Square",
  );
  context.model.output_brief.products[0].framing["en-US"]["1:1"] = {
    zoom: 1.2,
  };
  const changed = visuals.load(visual("v4"));
  await changed.ready;
  assert.notEqual(changed, square);
  context.model.visual_sources.p = "changed-same-path";
  const sourceChanged = visuals.load(visual("v4"));
  await sourceChanged.ready;
  assert.notEqual(sourceChanged, changed);
  context.model.visual_logo = "replaced-same-path";
  const logoChanged = visuals.load(visual("v4"));
  await logoChanged.ready;
  assert.notEqual(logoChanged, sourceChanged);
  context.model.visual_fonts = { noto_sans: "replacement-font-sha" };
  const fontChanged = visuals.load(visual("v4"));
  await fontChanged.ready;
  assert.notEqual(fontChanged, logoChanged);
  context.model.visual_products = { p: "replacement-prepared-cutout-sha" };
  const cutoutChanged = visuals.load(visual("v4"));
  await cutoutChanged.ready;
  assert.notEqual(cutoutChanged, fontChanged);
  const other = visuals.load(visual("v4", "1x1", "other"));
  await assert.rejects(other.ready, /superseded/);
  assert.notEqual(other, cutoutChanged);
  assert.equal(requests.length, 6);
});
test("adding a font on another format leaves this format's visual identity unchanged", async () => {
  const { visuals, context } = cache();
  context.model.visual_fonts = { noto_sans: "noto-sha" };
  const square = visuals.load(visual());
  await square.ready;
  context.model.visual_fonts.barlow_condensed = "barlow-sha";
  context.model.output_brief.products[0].framing = {
    "en-US": {
      "9:16": { text_placements: { message: { font_id: "barlow_condensed" } } },
    },
  };
  assert.equal(visuals.load(visual("v2")), square);
});
test("queued unchanged previews adopt the new version while superseded content is never fetched", async () => {
  const { visuals, context } = cache();
  const held = [],
    fetched = [];
  const response = {
    ok: true,
    blob: async () => ({ type: "image/png", size: 64 }),
  };
  context.model.version = "v1";
  context.fetch = (url) => {
    fetched.push(url);
    return url.includes("/hold/")
      ? new Promise((resolve) => held.push(() => resolve(response)))
      : Promise.resolve(response);
  };
  const occupying = [0, 1, 2].map((i) => visuals.load(`/hold/${i}`).ready);
  const queued = visuals.load(visual());
  const obsolete = visuals.load(visual("v1", "9x16"));
  context.model.version = "v2";
  context.model.output_brief.products[0].framing = {
    "en-US": { "9:16": { zoom: 1.2 } },
  };
  held.forEach((release) => release());
  await Promise.all(occupying);
  await queued.ready;
  await assert.rejects(obsolete.ready, /superseded/);
  assert.equal(fetched.at(-1), visual("v2"));
  assert.equal(fetched.length, 4);
});
test("compressed images are evicted according to decoded pixel cost", async () => {
  const { visuals, revoked } = cache();
  const first = visuals.load("/fixture/0.png");
  await first.ready;
  for (let i = 1; i < 36; i++) await visuals.load(`/fixture/${i}.png`).ready;
  assert.ok(
    revoked.includes(first.blobURL),
    "64-byte PNG still costs 640×640 RGBA bytes",
  );
  const reloaded = visuals.load("/fixture/0.png");
  await reloaded.ready;
  assert.notEqual(reloaded, first);
});
test("failed preview requests do not poison subsequent requests", async () => {
  const { visuals, requests } = cache();
  const first = visuals.load("/failure.png");
  await assert.rejects(first.ready);
  const retry = visuals.load("/failure.png");
  await assert.rejects(retry.ready);
  assert.notEqual(first, retry);
  assert.equal(requests.length, 2);
});

test("a mounted image is retained for an unchanged partial refresh and replaced for same-URL changed bytes", async () => {
  const { visuals, context, requests } = cache();
  const root = { current: null };
  class Node {
    constructor() {
      this.naturalWidth = 640; this.naturalHeight = 640;
      this.dataset = {}; this.attrs = new Map(); this.isConnected = false;
    }
    get attributes() { return [...this.attrs].map(([name, value]) => ({ name, value })); }
    setAttribute(name, value) {
      this.attrs.set(name, value);
      if (name === "data-visual-url") this.dataset.visualUrl = value;
      if (name === "data-visual-mounted") this.dataset.visualMounted = value;
    }
    removeAttribute(name) {
      this.attrs.delete(name);
      if (name === "data-visual-url") delete this.dataset.visualUrl;
      if (name === "data-visual-mounted") delete this.dataset.visualMounted;
    }
    replaceWith(next) { this.isConnected = false; next.isConnected = true; root.current = next; }
    cloneNode() { const next = new Node(); next.src = this.src; return next; }
    async decode() {}
  }
  context.Image = Node;
  context.$$ = () => [root.current];
  context.page = "editor";
  root.current = new Node();
  root.current.isConnected = true;
  root.current.setAttribute("data-visual-url", visual());
  visuals.mount(root);
  await visuals.load(visual()).ready;
  await new Promise((resolve) => setImmediate(resolve));
  const first = root.current;
  assert.equal(first.dataset.visualMounted, "true");
  assert.equal(requests.length, 1);
  visuals.mount(root);
  assert.equal(root.current, first);
  assert.equal(requests.length, 1);
  context.model.visual_sources.p = "replacement-at-same-path";
  visuals.mount(root);
  await visuals.load(visual()).ready;
  await new Promise((resolve) => setImmediate(resolve));
  assert.notEqual(root.current, first);
  assert.equal(first.isConnected, false);
  assert.equal(root.current.dataset.visualUrl, visual());
  assert.equal(root.current.dataset.visualMounted, "true");
  assert.equal(requests.length, 2);
});
