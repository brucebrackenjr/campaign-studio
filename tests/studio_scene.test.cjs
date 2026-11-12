const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const geometry = require("../src/campaign_studio/static/motif/scene-geometry.js");

const scripts = path.join(__dirname, "../src/campaign_studio/static/motif");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));

function harness(t) {
  const requests = [], phases = [], messages = [], timers = new Map();
  let timerId = 0;
  const globals = {
    structuredClone,
    copy: structuredClone,
    console,
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    studioWorkspace: null,
    model: { version: "v1", output_brief: { brand: {} }, metadata: { creatives: {} } },
    saveProject: async () => true,
    getSaveState: () => null,
    refreshStudioTargets: () => {},
    paintWordingAvailability: () => {},
    MotifTextLimits: { issue: () => null },
    MotifSceneGeometry: geometry,
    MotifManipulation: require("../src/campaign_studio/static/motif/direct-manipulation.js"),
    api: (requestPath, request) => new Promise((resolve, reject) => {
      requests.push({ path: requestPath, request: structuredClone(request), resolve, reject });
    }),
    useData(data) { globals.model = data; },
    recordPhase(state) { phases.push(state); },
    $() { throw new Error("Unexpected DOM access in state-only test"); },
  };
  const context = vm.createContext(globals);
  for (const file of ["framing.js", "studio-autosave.js", "studio-scene.js"])
    vm.runInContext(fs.readFileSync(path.join(scripts, file), "utf8"), context, { filename: file });
  vm.runInContext("this.Scene = StudioScene; paintStudioSave = recordPhase;", context);
  const scene = Object.create(context.Scene.prototype);
  scene.owner = { campaign: "campaign", mode: "text", root: {} };
  scene.product = "p"; scene.market = "en-US"; scene.format = 0;
  scene.size = { ratio: "1:1", w: 1080, h: 1080 };
  scene.target = "p/en-US/1x1"; scene.selected = "message";
  scene.current = () => true;
  scene.sequence = 0; scene.blocks = {}; scene.brand = {};
  scene.state = {
    framing: { zoom: 1, focal_x: 0.5, focal_y: 0.5, text_placements: {}, logo_placement: null },
    values: { message: "Old", cta: "Go" }, scopes: {}, pendingStyles: {},
    asset: null, description: "",
  };
  scene.saved = structuredClone(scene.state);
  for (const method of ["paint", "paintControlsState", "textControls", "publishThumbnail"])
    scene[method] = () => {};
  scene.status = (message, retry) => messages.push({ message, retry });
  scene.createSaveQueue();
  t.after(() => scene.queue.stop());
  const acknowledge = (index, version, metadata = {}) => requests[index].resolve({
    version, output_brief: { brand: {} }, metadata: { creatives: metadata },
  });
  return { scene, requests, phases, messages, globals, context, acknowledge, timers };
}

function keyEvent(key, { shift = false, target = "canvas", corner } = {}) {
  return {
    key, shiftKey: shift, prevented: false, stopped: false,
    target: { closest(selector) {
      if (selector === ".ws-scene-safety" && target === "safety") return {};
      if (selector === "[data-mf-corner]" && corner) return { dataset: { mfCorner: corner } };
      return null;
    } },
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
}

test("a loaded logo updates Reset in the existing controls, including when no logo is returned", async (t) => {
  for (const hasLogo of [true, false]) {
    const { scene, context, globals, requests } = harness(t);
    const reset = { disabled: hasLogo };
    scene.owner.mode = "logo";
    scene.paintControlsState = context.Scene.prototype.paintControlsState;
    globals.model.output_brief.markets = { "en-US": {} };
    globals.$ = selector => selector === '[data-scene-action="reset-logo"]' ? reset : null;
    scene.resource = async () => ({});
    const loading = scene.refresh(); await tick();
    assert.equal(reset.disabled, hasLogo, "control remains unchanged while the packet is pending");
    requests[0].resolve({
      source: { url: "/source.png", key: "source" },
      logo: hasLogo ? { url: "/logo.png", key: "logo" } : null,
      text_blocks: {}, findings: [],
    });
    await loading;
    assert.equal(scene.ready, true);
    assert.equal(reset.disabled, !hasLogo, "the already-mounted button follows loaded logo availability");
    assert.equal(requests.length, 1, "updating Reset does not save or request another preview");
  }
});

test("an in-flight logo save preserves later text and framing changes across tool selection", async (t) => {
  const { scene, requests, acknowledge } = harness(t);
  scene.owner.mode = "logo";
  scene.state.framing.logo_placement = { left: 0.1, top: 0.2, width: 0.3 };
  const saving = scene.queue.flush(); await tick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].request.revision_id, "v1");
  scene.owner.mode = "text";
  scene.state.values.message = "New headline";
  scene.state.scopes.message = { kind: "product", sequence: 1 };
  scene.state.framing.zoom = 1.2;
  acknowledge(0, "v2"); await tick();
  assert.equal(scene.state.values.message, "New headline");
  assert.equal(requests.length, 2);
  assert.equal(requests[1].request.revision_id, "v2");
  assert.equal(requests[1].request.values.message, "New headline");
  assert.equal(requests[1].request.framing.zoom, 1.2);
  assert.deepEqual(plain(requests[1].request.framing.logo_placement), { left: 0.1, top: 0.2, width: 0.3 });
  acknowledge(1, "v3"); await saving;
  assert.equal(scene.queue.dirty(), false);
});

test("uncertain retry keeps the exact description request and sends newer CTA only after acknowledgement", async (t) => {
  const { scene, requests, acknowledge } = harness(t);
  scene.state.description = "A useful description";
  const failing = scene.queue.flush(); await tick();
  requests[0].reject(new Error("Response lost"));
  await assert.rejects(failing, /Response lost/);
  const original = structuredClone(requests[0].request);
  scene.state.values.cta = "Later CTA";
  scene.state.scopes.cta = { kind: "product", sequence: 1 };
  const retry = scene.queue.flush(); await tick();
  assert.deepEqual(requests[1].request, original);
  acknowledge(1, "v2", { [scene.target]: { version: 1 } }); await tick();
  assert.equal(requests[2].request.revision_id, "v2");
  assert.equal(requests[2].request.values.cta, "Later CTA");
  assert.equal(requests[2].request.description_edit, undefined);
  acknowledge(2, "v3", { [scene.target]: { version: 1 } }); await retry;
  assert.equal(scene.queue.dirty(), false);
});

test("a stale-version refusal pauses saves without dropping cross-tool input", async (t) => {
  const { scene, requests, phases } = harness(t);
  scene.state.framing.zoom = 1.3;
  const failing = scene.queue.flush(); await tick();
  requests[0].reject(Object.assign(new Error("Changed in another tab"), { status: 409 }));
  await assert.rejects(failing, /another tab/);
  scene.owner.mode = "text"; scene.state.values.message = "Keep my typing";
  assert.equal(await scene.queue.flush(), false);
  assert.equal(requests.length, 1);
  assert.equal(scene.queue.dirty(), true);
  assert.equal(scene.state.values.message, "Keep my typing");
  assert.equal(phases.at(-1).phase, "conflict");
});

test("style chosen before geometry is dirty, survives preview failure, and saves after recovery", async (t) => {
  const { scene, requests, messages, acknowledge } = harness(t);
  scene.style({ font_id: "barlow_condensed", color: "#112233" });
  assert.equal(scene.queue.dirty(), true);
  assert.deepEqual(plain(scene.state.pendingStyles.message), { font_id: "barlow_condensed", color: "#112233" });
  assert.equal(scene.rasterDirty, true);
  scene.refresh = async () => { scene.previewError = new Error("Preview offline"); };
  await assert.rejects(scene.queue.flush(), /Retry the preview/);
  assert.equal(requests.length, 0);
  assert.equal(scene.queue.dirty(), true);
  assert.equal(messages.at(-1).retry, true);
  scene.refresh = async () => {
    scene.previewError = null;
    scene.state.framing.text_placements.message = {
      left: 0.1, top: 0.2, width: 0.6, font_size: 40, ...scene.pendingStyles.message,
    };
    delete scene.pendingStyles.message;
  };
  const saving = scene.queue.flush(); await tick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].request.framing.text_placements.message.font_id, "barlow_condensed");
  assert.equal(requests[0].request.framing.text_placements.message.color, "#112233");
  acknowledge(0, "v2"); await saving;
  assert.equal(scene.queue.dirty(), false);
});

test("save preparation waits for a current geometry packet after the first is superseded", async (t) => {
  const { scene, requests, acknowledge } = harness(t);
  scene.style({ font_id: "barlow_condensed" });
  let reads = 0;
  scene.refresh = async () => {
    if (++reads === 1) return;
    scene.state.framing.text_placements.message = {
      left: 0.1, top: 0.2, width: 0.6, font_size: 40, ...scene.pendingStyles.message,
    };
    delete scene.pendingStyles.message;
  };
  const saving = scene.queue.flush(); await tick();
  assert.equal(reads, 2);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].request.framing.text_placements.message.font_id, "barlow_condensed");
  acknowledge(0, "v2"); await saving;
});

test("a pending text raster cannot publish an old thumbnail as the new composition", (t) => {
  const { scene, context, globals } = harness(t);
  scene.ready = true; scene.rasterDirty = true;
  scene.owner.localThumbnail = { signature: "previous", pixels: "previous words" };
  let domAccess = false;
  globals.$ = () => { domAccess = true; throw new Error("Must not publish old pixels"); };
  context.Scene.prototype.publishThumbnail.call(scene);
  assert.equal(domAccess, false);
  assert.deepEqual(scene.owner.localThumbnail, { signature: "previous", pixels: "previous words" });
});

test("a completed resize may publish provisional pixels but only the exact raster receives a current signature", (t) => {
  const { scene, context, globals } = harness(t);
  scene.ready = true; scene.rasterDirty = true;
  let drawings = 0;
  const canvas = { getContext: () => ({ drawImage: () => { drawings++; } }) };
  const slot = { querySelector: () => canvas }, button = {};
  const pending = { sequence: 2, pending: "/old-thumbnail", signature: "old" };
  scene.owner.thumbnails = new Map([[button, pending]]);
  globals.$ = (selector) => selector === ".ws-thumb" ? slot : button;
  globals.studioVisualTarget = () => "current-target";
  globals.studioThumbnailSignature = () => "exact-current-signature";
  globals.outputFor = () => ({});
  globals.currentProduct = 0; globals.lang = "en";
  context.Scene.prototype.publishThumbnail.call(scene);
  assert.equal(drawings, 0, "ordinary wording/font changes cannot publish a stale raster");
  context.Scene.prototype.publishThumbnail.call(scene, { provisional: true });
  assert.equal(drawings, 1);
  assert.equal(scene.owner.localThumbnail.signature, null);
  assert.equal(pending.signature, null);
  assert.equal(pending.pending, null);
  assert.equal(pending.sequence, 3, "late image requests cannot replace the provisional canvas");
  scene.rasterDirty = false;
  context.Scene.prototype.publishThumbnail.call(scene);
  assert.equal(drawings, 2);
  assert.equal(scene.owner.localThumbnail.signature, "exact-current-signature");
  assert.equal(pending.signature, "exact-current-signature");
});

test("apply-all remains field-specific and later ordinary typing returns to product scope", (t) => {
  const { scene } = harness(t);
  scene.state.scopes.message = { kind: "all_products", sequence: 1 };
  let request = scene.body();
  assert.deepEqual(plain(request.values), { message: "Old" });
  assert.deepEqual(plain(request.field_scopes), { message: "all_products" });
  scene.saved = structuredClone(scene.state);
  scene.state.values.message = "One product";
  scene.state.scopes.message = { kind: "product", sequence: 2 };
  request = scene.body();
  assert.deepEqual(plain(request.values), { message: "One product" });
  assert.deepEqual(plain(request.field_scopes), { message: "product" });
  assert.equal(request.framing, undefined);
});

test("an unchanged native change event preserves apply-all scope and the pending save sequence", (t) => {
  const { scene } = harness(t);
  scene.scopeSequence = 7;
  scene.sequence = 12;
  scene.state.scopes.message = { kind: "all_products", sequence: 7 };
  const before = structuredClone(scene.state);
  let stopped = false;
  scene.input({
    type: "change",
    target: { dataset: { mfValue: "message" }, value: scene.state.values.message, matches: () => false },
    stopPropagation() { stopped = true; },
  });
  assert.equal(stopped, true);
  assert.deepEqual(plain(scene.state), before);
  assert.equal(scene.scopeSequence, 7);
  assert.equal(scene.sequence, 12);
  assert.equal(scene.body().field_scopes.message, "all_products");
  scene.input({
    type: "input",
    target: { dataset: { mfValue: "message" }, value: "New typing", matches: () => false },
    stopPropagation() {},
  });
  assert.deepEqual(plain(scene.state.scopes.message), { kind: "product", sequence: 8 });
  assert.equal(scene.sequence, 13);
});

test("scene hit testing uses the visible padded CTA frame above overlapping headline and logo", (t) => {
  const { scene } = harness(t);
  scene.output = { getBoundingClientRect: () => ({ width: 540, height: 540 }) };
  scene.textBounds = (id) => id === "cta" ? [100, 100, 200, 110] : [0, 0, 400, 300];
  scene.logoBounds = () => [0, 0, 1080, 1080];
  const frame = scene.selectionFrame("cta");
  assert.equal(frame.height, 64);
  assert.equal(scene.hit({ x: 150, y: 70 }), "cta", "transparent padding belongs to the visible CTA selection");
  assert.equal(scene.hit({ x: 20, y: 20 }), "message");
  assert.equal(scene.hit({ x: 1000, y: 1000 }), "logo");
  scene.logoBounds = () => null;
  assert.equal(scene.hit({ x: 1000, y: 1000 }), "framing");
});

test("retained scene signatures preserve unaffected formats and reject changed owned image or font contents", () => {
  const context = vm.createContext({
    window: {},
    C: { id: "campaign", products: [{ id: "p" }] },
    currentProduct: 0, lang: "en", MARKET: { en: "en-US" },
    FMT: [{ ratio: "1:1" }, { ratio: "9:16" }],
    model: {
      output_brief: { products: [{ id: "p", source: { path: "same.png" }, framing: {} }], brand: {} },
      visual_sources: { p: "source-sha" }, visual_products: { p: "cutout-sha" },
      visual_logo: "logo-sha", visual_fonts: { noto_sans: "font-sha" },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(scripts, "visuals.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(scripts, "studio-workspace.js"), "utf8"), context);
  const output = { url: "/composition/v1/image", copy: { message: "Hello", cta: "Go" } };
  const signature = () => context.studioThumbnailSignature(output, 0);
  let previous = signature();
  output.url = "/composition/v2/image";
  context.model.output_brief.products[0].framing = { "en-US": { "9:16": { zoom: 1.3 } } };
  assert.equal(signature(), previous, "a Story-only edit and new save version preserve Square");
  context.model.visual_fonts.barlow_condensed = "first-use-barlow-sha";
  context.model.output_brief.products[0].framing["en-US"]["9:16"].text_placements = {
    message: { font_id: "barlow_condensed" },
  };
  assert.equal(signature(), previous, "a new font used only in Story preserves Square");
  for (const change of [
    () => { context.model.visual_sources.p = "new-source"; },
    () => { context.model.visual_products.p = "new-cutout"; },
    () => { context.model.visual_logo = "new-logo"; },
    () => { context.model.visual_fonts.noto_sans = "new-font"; },
  ]) {
    change();
    const current = signature();
    assert.notEqual(current, previous);
    previous = current;
  }
});

test("logo corner resizing publishes locally without scheduling a text raster", (t) => {
  const { scene } = harness(t);
  scene.ready = true;
  scene.owner.mode = "logo";
  scene.logo = { size: [400, 200], default_bounds: [100, 100, 300, 200] };
  scene.output = { hasPointerCapture: () => false };
  scene.rasterDirty = false;
  let previews = 0, thumbnails = 0;
  scene.schedulePreview = () => { previews++; };
  scene.publishThumbnail = () => { thumbnails++; };
  scene.keyDown(keyEvent("ArrowRight", { corner: "se", shift: true }));
  assert.ok(scene.state.framing.logo_placement.width > 200 / 1080);
  assert.equal(scene.rasterDirty, false);
  assert.equal(previews, 0);
  assert.equal(thumbnails, 1);
  scene.gesture = { id: "logo", corner: "se", pointer: 7, start: scene.readGesture("logo") };
  scene.state.framing.logo_placement.width += 0.02;
  scene.pointerEnd({ pointerId: 7 });
  assert.equal(scene.rasterDirty, false);
  assert.equal(previews, 0);
  assert.equal(thumbnails, 2);
  assert.equal(scene.queue.dirty(), true);
});

test("Framing arrow keys pan the photograph and Shift uses ten output pixels", (t) => {
  const { scene } = harness(t);
  scene.ready = true; scene.owner.mode = "framing";
  scene.layers = { source: { naturalWidth: 1920, naturalHeight: 1920 } };
  scene.state.framing.zoom = 2;
  const right = keyEvent("ArrowRight"); scene.keyDown(right);
  assert.ok(Math.abs(scene.state.framing.focal_x - (0.5 - 1 / 1080)) < 1e-12);
  const down = keyEvent("ArrowDown", { shift: true }); scene.keyDown(down);
  assert.ok(Math.abs(scene.state.framing.focal_y - (0.5 - 10 / 1080)) < 1e-12);
  assert.equal(right.prevented && down.prevented, true);
  assert.equal(scene.queue.dirty(), true);
});

test("focused safety area moves, resizes, and clamps without panning the photograph", (t) => {
  const { scene } = harness(t);
  scene.ready = true; scene.owner.mode = "framing";
  scene.sourceSubject = { left: 0.2, top: 0.3, right: 0.5, bottom: 0.6 };
  scene.state.framing.inherit_subject = true;
  scene.keyDown(keyEvent("ArrowRight", { target: "safety" }));
  assert.ok(Math.abs(scene.state.framing.subject_box.left - 0.21) < 1e-12);
  assert.deepEqual(plain({ ...scene.state.framing.subject_box, left: 0.21 }), { left: 0.21, top: 0.3, right: 0.51, bottom: 0.6 });
  scene.keyDown(keyEvent("ArrowDown", { target: "safety", shift: true }));
  assert.equal(scene.state.framing.subject_box.bottom, 0.61);
  assert.equal(scene.state.framing.focal_x, 0.5);
  assert.equal(scene.state.framing.focal_y, 0.5);
  assert.equal(scene.state.framing.inherit_subject, false);
  scene.state.framing.subject_box = { left: 0.5, top: 0.5, right: 1, bottom: 1 };
  scene.keyDown(keyEvent("ArrowRight", { target: "safety" }));
  scene.keyDown(keyEvent("ArrowDown", { target: "safety", shift: true }));
  assert.deepEqual(plain(scene.state.framing.subject_box), { left: 0.5, top: 0.5, right: 1, bottom: 1 });
});

test("Escape cancels a held layer gesture and restores its prior framing", (t) => {
  const { scene } = harness(t);
  const original = structuredClone(scene.state.framing);
  scene.gesture = { id: "logo", pointer: 7, start: { framing: original } };
  scene.state.framing.logo_placement = { left: 0.5, top: 0.5, width: 0.2 };
  scene.output = { hasPointerCapture: () => false };
  const escape = keyEvent("Escape"); scene.keyDown(escape);
  assert.equal(scene.gesture, null);
  assert.deepEqual(plain(scene.state.framing), original);
  assert.equal(escape.prevented, true);
  assert.equal(scene.queue.dirty(), false);
});

for (const arrival of ["before request", "during response"])
  test(`pending text pixels resume after a held pointer ${arrival}`, async (t) => {
    const { scene, requests, globals, timers } = harness(t);
    scene.ready = true;
    scene.rasterDirty = true;
    scene.layers = { marker: "old pixels" };
    scene.state.values.message = "New wording";
    scene.output = { hasPointerCapture: () => false };
    scene.resource = async () => ({ naturalWidth: 1080, naturalHeight: 1080 });
    globals.decodedImage = async url => ({ url });
    const packet = {
      source: { url: "/source", key: "source-sha" }, logo: null, findings: [],
      text_blocks: { message: { url: "/new-words", placement: { font_size: 40 } } },
    };
    let first;
    if (arrival === "during response") first = scene.refresh();
    scene.gesture = { id: "logo", pointer: 7, start: { framing: structuredClone(scene.state.framing) } };
    if (first) requests[0].resolve(packet);
    else first = scene.refresh();
    await first;
    assert.equal(scene.layers.marker, "old pixels", "do not replace pixels during the held gesture");
    scene.pointerEnd({ pointerId: 7 });
    assert.ok(timers.has(scene.previewTimer), "release must resume the skipped text raster");
    const refresh = timers.get(scene.previewTimer)();
    requests.at(-1).resolve(packet);
    await refresh;
    assert.equal(scene.rasterDirty, false);
    assert.equal(scene.layers.blocks.message.image.url, "/new-words");
    assert.equal(scene.state.values.message, "New wording");
  });

test("persistent resize handles announce the selected logo, headline, or CTA", (t) => {
  const { scene, globals } = harness(t);
  scene.ready = true;
  const label = {};
  const handles = ["nw", "ne", "sw", "se"].map(corner => ({
    dataset: { mfCorner: corner }, setAttribute(name, value) { this[name] = value; },
  }));
  scene.selection = { style: {}, classList: { toggle() {} }, setAttribute(name, value) { this[name] = value; } };
  scene.selectionFrame = () => ({ left: 20, top: 30, width: 100, height: 80 });
  globals.$ = () => label;
  globals.$$ = () => handles;
  for (const [mode, selected, name] of [["logo", "message", "Logo"], ["text", "cta", "Call to action"], ["text", "message", "Headline"]]) {
    scene.owner.mode = mode; scene.selected = selected;
    scene.paintSelection();
    assert.equal(label.textContent, name);
    assert.equal(handles[0]["aria-label"], `Resize ${name} from top left. Arrow keys resize. Shift uses ten pixels.`);
    assert.ok(handles.every(handle => handle["aria-label"].startsWith(`Resize ${name} from `)));
  }
});
