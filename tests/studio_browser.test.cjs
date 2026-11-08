// Exercise the shipped framing controller with separately deferred HTTP and PNG
// decodes. The canvas and layout are covered by the actual browser proof.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("Home distinguishes an unconfirmed request result from ordinary run readiness", () => {
  const context = vm.createContext({scheduleStudioSave(){},
    window: { addEventListener() {} },
    document: { addEventListener() {} },
  });
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/studio.js", "utf8"),
    context,
  );
  for (const status of ["partial", "draft"]) {
    assert.equal(
      context.workbenchStatus({ status, outcome_unknown: true }),
      "Result not confirmed",
    );
  }
  assert.equal(context.workbenchStatus({ status: "running" }), "In progress");
  assert.equal(context.workbenchStatus({ status: "complete" }), "Ready to review");
  assert.equal(context.workbenchStatus({ status: "draft" }), "Draft");
  assert.equal(
    context.workbenchStatus({ status: "complete", damaged_images: 1 }),
    "Needs recovery",
  );
});

function framingHarness() {
  const elements = new Map(),
    requests = [],
    decodes = [],
    displayDecodes = [];
  function get(selector) {
    if (selector === "#frame-primary") selector = "[data-action=saveFrame]";
    if (!elements.has(selector)) {
      const element = {
        disabled: false,
        hidden: false,
        textContent: "",
        value: "",
        style: {},
        dataset: {},
        setAttribute(name, value) { this[name] = value; },
        getAttribute(name) { return this[name] ?? null; },
        classList: { add() {}, remove() {}, toggle() {} },
        closest() {
          return this;
        },
        focus() {},
      };
      elements.set(selector, element);
    }
    return elements.get(selector);
  }
  const controls = [
    "#frame-zoom",
    "#frame-focal",
    "#frame-vertical",
    "#frame-layout",
    "[data-action=saveFrame]",
  ].map(get);
  const context = vm.createContext({scheduleStudioSave(){},
    $: get, studioWorkspace: null, imageEditorRoot: () => get("#overlay"),
    $$: () => controls,
    AbortController, structuredClone, clearTimeout,
    setTimeout,
    cancelAnimationFrame() {},
    requestAnimationFrame: () => 1,
    C: { id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4", products: [{ id: "bag", image: "/source" }] },
    model: {
      output_run_id: "run",
      manifest: { revision_id: "revision" },
      source_sizes: { bag: [1000, 1000] },
      output_brief: {
        products: [{ id: "bag", source: { subject_box: null } }],
      },
    },
    MARKET: { es: "es-MX" },
    lang: "es",
    modal: "frame",
    currentProduct: 0,
    currentFormat: 0,
    FMT: [{ ratio: "1:1", w: 1080, h: 1080 }],
    api: (url, body) =>
      new Promise((resolve, reject) =>
        requests.push({ url, body, resolve, reject }),
      ),
    copy: structuredClone,
    withProjectApply: operation => operation(),
    projectChanged() {},
    imageCleanupReady: async (_campaign, current) => current(),
    imageCapacity: async () => false,
    showPendingImageCleanup() {},
    useData() {},
    closeModal() {
      context.modal = "";
    },
    render() {},
    toast() {},
  });
  context.window = context;
  context.refreshes = 0;
  context.location = {reload(){context.refreshes++;}};
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/save-state.js", "utf8"), context);
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/direct-manipulation.js", "utf8"), context);
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/editor-controls.js", "utf8"), context);
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/framing.js", "utf8"),
    context,
  );
  context.drawFrameSource = () => {};
  context.drawFrameSafety = () => {};
  context.decodedImage = (url) =>
    new Promise((resolve, reject) => decodes.push({ url, resolve, reject }));
  get("#frame-image").decode = () =>
    new Promise((resolve, reject) => displayDecodes.push({ resolve, reject }));
  const run = (code) => vm.runInContext(code, context);
  run("beginFraming(); framingSource = {width:1000,height:1000,src:'/source'}");
  async function finishPreview(url = "/final") {
    requests.shift().resolve({
      digest: url,
      blocked: false,
      images: [{ id: "bag/es-MX/1x1", url, overlay_url: "/overlay" }],
    });
    await flush();
    for (const decode of decodes.splice(0)) decode.resolve({ src: decode.url });
    await flush();
    for (const decode of displayDecodes.splice(0)) decode.resolve();
    await flush();
  }
  return {
    run,
    context,
    get,
    controls,
    requests,
    decodes,
    displayDecodes,
    finishPreview,
  };
}

test("framing Apply waits for the exact final PNG decode and restores a roundtrip", async () => {
  const h = framingHarness();
  h.run("framingDraft.zoom=1.4; framingDraft.focal=70");
  const preview = h.run("refreshFramePreview()");
  h.requests.shift().resolve({
    digest: "changed",
    blocked: false,
    images: [{ id: "bag/es-MX/1x1", url: "/changed", overlay_url: "/overlay" }],
  });
  await flush();
  assert.equal(h.get("[data-action=saveFrame]").disabled, true);
  for (const decode of h.decodes.splice(0)) decode.resolve({ src: decode.url });
  await flush();
  assert.equal(h.get("[data-action=saveFrame]").disabled, true);
  h.displayDecodes.shift().resolve();
  await preview;
  assert.equal(h.get("[data-action=saveFrame]").disabled, false);
  h.run("framingDraft.zoom=1; framingDraft.focal=50");
  const restored = h.run("refreshFramePreview()");
  await h.finishPreview("/saved-composition");
  await restored;
  assert.equal(h.get("#frame-image").src, "/saved-composition");
  assert.equal(h.get("[data-action=saveFrame]").disabled, true);
  assert.equal(h.run("framePreview.body.framing.focal_x"), 0.5);
});

test("first framing image stays hidden until decoded and failure preserves the prior preview", async () => {
  const h = framingHarness();
  const first = h.run("refreshFramePreview()");
  assert.equal(h.get("#frame-image").hidden, true);
  assert.equal(h.get("#frame-status").textContent, "Preparing preview…");
  assert.equal(h.get("[data-action=saveFrame]").disabled, true);
  h.requests.shift().reject(new Error("Controlled first preview failure"));
  await first;
  assert.equal(h.get("#frame-image").hidden, true);
  assert.match(h.get("#frame-status").textContent, /Preview unavailable/);
  const retry = h.run("refreshFramePreview()");
  h.requests.shift().resolve({
    digest: "valid", blocked: false,
    images: [{ id: "bag/es-MX/1x1", url: "/valid", overlay_url: "/overlay" }],
  });
  await flush();
  assert.equal(h.get("#frame-image").hidden, true);
  for (const decode of h.decodes.splice(0)) decode.resolve({ src: decode.url });
  await flush();
  assert.equal(h.get("#frame-image").hidden, true);
  assert.equal(h.get("#frame-status").textContent, "Preparing preview…");
  h.displayDecodes.shift().resolve();
  await retry;
  assert.equal(h.get("#frame-image").hidden, false);
  assert.equal(h.get("#frame-status").textContent, "");
  h.run("framingDraft.zoom=1.2");
  const changed = h.run("refreshFramePreview()");
  assert.equal(h.get("#frame-image").hidden, false);
  assert.equal(h.get("#frame-image").src, "/valid");
  h.requests.shift().reject(new Error("Controlled subsequent preview failure"));
  await changed;
  assert.equal(h.get("#frame-image").src, "/valid");
  assert.equal(h.get("#frame-image").hidden, false);
  assert.match(h.get("#frame-status").textContent, /Preview unavailable/);
  assert.equal(h.get("[data-action=saveFrame]").dataset.action, "recoverFrame");
  assert.equal(h.get("[data-action=saveFrame]").disabled, false);
});

test("framing preview 409 while autosave is dirty keeps edits and updates the DOM status", async () => {
  const h = framingHarness();
  h.context.status = ""; // Browsers expose window.status as a string.
  h.context.studioSave = {dirty:()=>true,conflict:()=>assert.fail("own pending save must not be an external conflict")};
  h.get("#frame-image").src="/last-visible-preview";
  h.run("framingDraft.zoom=1.4;framingDraft.focal=70");
  const before=h.run("JSON.stringify(framingDraft)");
  const preview=h.run("refreshFramePreview()");
  h.requests.shift().reject(Object.assign(new Error("Newer saved work exists"), {status:409,code:"composition_conflict"}));
  assert.equal(await preview,false);
  assert.equal(h.get("#frame-status").textContent,"");
  assert.equal(h.context.status,"");
  assert.equal(h.run("JSON.stringify(framingDraft)"),before);
  assert.equal(h.get("#frame-image").src,"/last-visible-preview");
  assert.equal(h.run("framingRecovery"),null);
  assert.equal(h.run("framePreview"),null);
  assert.equal(h.requests.length,0,"a pending autosave does not trigger a stale-read or save retry");
  // This window's acknowledged save advances its preview token normally.
  h.context.studioSave.dirty=()=>false;
  h.run("framingContext.revision_id='own-acknowledged-save'");
  const current=h.run("refreshFramePreview()");
  assert.equal(h.requests[0].body.revision_id,"own-acknowledged-save");
  await h.finishPreview("/own-saved-preview"); await current;
  assert.equal(h.run("framingRecovery"),null);
  assert.equal(h.get("#frame-image").src,"/own-saved-preview");
  assert.equal(h.context.refreshes,0);
});

test("framing preview does not start after its status element has been removed", async () => {
  const h = framingHarness();
  h.context.$=selector=>selector==="#frame-status"?null:h.get(selector);
  h.run("framingDraft.zoom=1.4");
  assert.equal(await h.run("refreshFramePreview()"),false);
  assert.equal(h.requests.length,0);
  assert.equal(h.run("framingDraft.zoom"),1.4);
});

for (const boundary of ["closed","reopened","newer preview"]) {
  test(`a late dirty framing 409 cannot affect a ${boundary} editor`, async () => {
    const h = framingHarness();
    h.context.status="";
    let dirtyChecks=0;
    h.context.studioSave={dirty:()=>{dirtyChecks++;return true;}};
    h.run("framingDraft.zoom=1.4");
    const old=h.run("refreshFramePreview()");
    const request=h.requests.shift();
    let latest=null;
    if(boundary==="newer preview") {
      h.run("framingDraft.zoom=1.8");
      latest=h.run("refreshFramePreview()");
    } else {
      h.run("endFraming();modal=''");
      if(boundary==="reopened") h.run("modal='frame';beginFraming();framingDraft.zoom=1.8");
    }
    const status=h.get("#frame-status");
    status.textContent="Current editor owns this message";
    request.reject(Object.assign(new Error("Late conflict"),{status:409}));
    assert.equal(await old,false);
    assert.equal(status.textContent,"Current editor owns this message");
    assert.equal(dirtyChecks,0,"ownership is checked before accessing the current save controller");
    assert.equal(h.context.status,"");
    assert.equal(h.run("framingDraft?.zoom ?? null"),boundary==="closed"?null:1.8);
    if(latest) {
      h.requests.shift().reject(Object.assign(new Error("Current conflict"),{status:409}));
      assert.equal(await latest,false);
      assert.equal(dirtyChecks,1);
      assert.equal(status.textContent,"");
    }
  });
}

test("older responses, decode failures, and closing cannot approve stale framing", async () => {
  const h = framingHarness();
  h.run("framingDraft.zoom=1.4");
  const old = h.run("refreshFramePreview()");
  const oldRequest = h.requests.shift();
  h.run("framingDraft.focal=70");
  const latest = h.run("refreshFramePreview()");
  oldRequest.resolve({ images: [] });
  await old;
  assert.equal(h.decodes.length, 0);
  h.requests.shift().resolve({
    digest: "latest",
    blocked: false,
    images: [{ id: "bag/es-MX/1x1", url: "/bad-png", overlay_url: "/overlay" }],
  });
  await flush();
  h.decodes.shift().reject(new Error("PNG decode failed"));
  h.decodes.shift().resolve({ src: "/overlay" });
  await latest;
  assert.equal(h.get("[data-action=saveFrame]").dataset.action, "recoverFrame");
  assert.equal(h.run("framePreview"), null);
  assert.match(h.get("#frame-status").textContent, /Preview unavailable/);
  const closing = h.run("refreshFramePreview()");
  const pending = h.requests.shift();
  h.run("endFraming(); modal=''");
  pending.resolve({ images: [] });
  await closing;
  assert.equal(h.decodes.length, 0);
  assert.equal(h.run("framePreview"), null);
});

test("a later draft invalidates pending decode and preserves the latest signature", async () => {
  const h = framingHarness();
  h.run("framingDraft.zoom=1.4");
  const old = h.run("refreshFramePreview()");
  h.requests.shift().resolve({
    digest: "old",
    blocked: false,
    images: [{ id: "bag/es-MX/1x1", url: "/old", overlay_url: "/old-overlay" }],
  });
  await flush();
  const oldDecodes = h.decodes.splice(0);
  h.run("framingDraft.focal=65.3");
  const latest = h.run("refreshFramePreview()");
  await h.finishPreview("/latest");
  for (const decode of oldDecodes) decode.resolve({ src: decode.url });
  await Promise.all([old, latest]);
  assert.equal(h.get("#frame-image").src, "/latest");
  assert.equal(h.run("framePreview.body.framing.focal_x"), 0.653);
});

test("Apply locks framing controls and requires explicit reconciliation after an unknown failure", async () => {
  const h = framingHarness();
  h.run("framingDraft.zoom=1.4; framingDraft.focal=60");
  const preview = h.run("refreshFramePreview()");
  await h.finishPreview();
  await preview;
  const saving = h.run("saveFraming()");
  assert.ok(h.controls.every((control) => control.disabled));
  await flush();
  assert.equal(h.requests[0].body.framing.focal_x, 0.6);
  h.requests.shift().reject(new Error("Local save failed"));
  assert.equal(await saving, false);
  assert.equal(h.run("framingDraft.focal"), 60);
  assert.equal(h.run("frameSaving"), false);
  assert.equal(h.run("framingRecovery"), "uncertain");
  assert.equal(h.get("#frame-primary").textContent, "Check current image");
  assert.equal(h.get("#frame-primary").getAttribute("aria-label"), "Check current image");
  assert.equal(h.requests.length, 0);
  const checking = h.run("recoverFraming()");
  assert.equal(h.get("#frame-primary").getAttribute("aria-label"), "Checking…");
  h.requests.shift().resolve({ output_run_id: "run", manifest: { revision_id: "revision" } });
  await checking;
  assert.equal(h.get("#frame-primary").getAttribute("aria-label"), "Retry preview");
  const retry = h.run("recoverFraming()");
  await h.finishPreview();
  await retry;
  assert.equal(h.get("#frame-primary").dataset.action, "saveFrame");
  assert.equal(h.get("#frame-primary").textContent, "Apply");
  assert.equal(h.get("#frame-primary").getAttribute("aria-label"), "Apply framing");
  assert.equal(h.get("#frame-primary").disabled, false);
  assert.equal(h.requests.length, 0);
});

test("known precommit framing refusals restore ready status and retain the exact draft preview", async () => {
  for (const boundary of ["capacity", "cleanup"]) {
    const h = framingHarness();
    h.run("framingDraft.zoom=1.4;framingDraft.focal=60");
    const preview = h.run("refreshFramePreview()");
    await h.finishPreview();
    await preview;
    const readyStatus = h.get("#frame-status").textContent;
    const signature = h.run("framePreview.signature");
    let closeNotice;
    if (boundary === "cleanup") h.context.imageCleanupReady = async () => false;
    else h.context.imageCapacity = () => new Promise(resolve => { closeNotice = resolve; });
    const applying = h.run("saveFraming()");
    await flush();
    if (boundary === "capacity") {
      h.requests.shift().reject({ status: 409, code: "storage_capacity" });
      await flush();
      closeNotice(true);
    }
    assert.equal(await applying, false);
    assert.equal(h.get("#frame-status").textContent, boundary === "cleanup" ? readyStatus : h.context.MotifEditorControls.copy.applyFailed);
    assert.equal(h.get("#frame-primary").disabled, false);
    assert.equal(h.run("framePreview.signature"), signature);
    assert.equal(h.run("framingDraft.focal"), 60);
    assert.equal(h.run("framingRecovery"), boundary === "cleanup" ? null : "apply");
    assert.equal(h.run("frameSaving"), false);
    assert.equal(h.context.modal, "frame");
    assert.equal(h.requests.length, 0);
  }
});

test("an unaccepted framing candidate keeps its draft and offers Try again", async () => {
  const h = framingHarness();
  h.run("framingDraft.zoom=1.4; framingDraft.focal=60");
  const preview = h.run("refreshFramePreview()");
  await h.finishPreview();
  await preview;
  const applying = h.run("saveFraming()");
  await flush();
  h.requests.shift().reject({status:409,code:"apply_not_accepted",message:"Could not apply. Your edits are still here."});
  assert.equal(await applying, false);
  assert.equal(h.run("framingDraft.focal"), 60);
  assert.equal(h.run("framingDraft.zoom"), 1.4);
  assert.equal(h.run("framingRecovery"), "apply");
  assert.equal(h.context.modal, "frame");
  assert.equal(h.get("#frame-primary").textContent, "Try again");
  assert.match(h.get("#frame-status").textContent, /previous image is unchanged/);
  assert.equal(h.requests.length, 0);
  const retry = h.run("recoverFraming()");await flush();
  assert.match(h.requests[0].url, /framing\/apply$/);
  assert.equal(h.requests[0].body.framing.zoom,1.4);
  h.requests.shift().reject({status:409,code:"apply_not_accepted"});await retry;
  assert.equal(h.run("framingRecovery"),"apply");
});

test("Clear persists an explicit opt-out from inherited protection; visibility is not a crop edit", async () => {
  const h = framingHarness();
  h.context.model.output_brief.products[0].source.subject_box = {
    left: 0.3,
    top: 0.3,
    right: 0.7,
    bottom: 0.7,
  };
  h.run("beginFraming(); framingSource={width:1000,height:1000,src:'/source'}");
  assert.equal(h.run("framingDraft.subject.x"), 0.3);
  const original = h.run("JSON.stringify(frameBody())");
  h.run("framingShowSafety=true; syncFrameSafetyControl()");
  assert.equal(h.run("JSON.stringify(frameBody())"), original);
  h.run("clearFrameSubject()");
  assert.equal(h.run("frameBody().framing.subject_box"), null);
  assert.equal(h.run("frameBody().framing.inherit_subject"), false);
  assert.equal(h.run("framingShowSafety"), false);
  assert.equal(h.run("framingMarking"), false);
  await h.finishPreview();
});

test("crop geometry uses fractional pan and the half-source-pixel axis threshold", () => {
  const h = framingHarness();
  const small = h.run(
    "frameCrop(500,500,{w:1080,h:1080},{zoom:1.001,focal:0,vertical:100})",
  );
  const larger = h.run(
    "frameCrop(501,501,{w:1080,h:1080},{zoom:1.001,focal:0,vertical:100})",
  );
  assert.equal(small.focal, 50);
  assert.equal(small.vertical, 50);
  assert.equal(larger.focal, 0);
  assert.equal(larger.vertical, 100);
  const crop = h.run(
    "frameCrop(1000,1000,{w:1080,h:1080},{zoom:2,focal:33.3,vertical:66.7})",
  );
  assert.ok(Math.abs(crop.sx - 166.5) < 1e-9);
  assert.ok(Math.abs(crop.sy - 333.5) < 1e-9);
  assert.equal(
    h.run(
      "frameSubjectFits({sx:200,sy:200,sw:600,sh:600},{x:.1995,y:.2,w:.6005,h:.6},1000,1000)",
    ),
    true,
  );
  assert.equal(
    h.run(
      "frameSubjectFits({sx:200,sy:200,sw:600,sh:600},{x:.199499,y:.2,w:.600501,h:.6},1000,1000)",
    ),
    false,
  );
});

test("source-inherited safety is identified until edited or explicitly cleared", async () => {
  const h = framingHarness();
  h.context.model.output_brief.products[0].source.subject_box = {
    left: 0.2,
    top: 0.2,
    right: 0.8,
    bottom: 0.8,
  };
  h.run("beginFraming(); framingSource={width:1000,height:1000,src:'/source'}");
  const preview = h.run("refreshFramePreview()");
  await h.finishPreview();
  await preview;
  assert.equal(
    h.get("#frame-subject-state").textContent,
    "Source area retained",
  );
  assert.equal(h.run("inheritedFrameSubject()"), true);
  assert.equal(h.run("frameBody().framing.subject_box.left"), 0.2);
  h.run("framingDraft.subject.x=.21; syncFrameSafetyControl()");
  assert.equal(h.run("inheritedFrameSubject()"), false);
  assert.doesNotMatch(
    h.get("#frame-subject-help").textContent,
    /came with the source/,
  );
  h.run("framingDraft.subject=null; syncFrameSafetyControl()");
  assert.equal(h.run("inheritedFrameSubject()"), false);
  assert.equal(h.run("frameBody().framing.subject_box"), null);
});

test("carousel focus reveals a clipped card without moving the page or unrelated cards", () => {
  for (const reduced of [false, true]) {
    const handlers = {},
      moves = [];
    let bounds = { left: 1210, right: 1580 };
    const card = { getBoundingClientRect: () => bounds };
    const track = {
      scrollLeft: 0,
      scrollWidth: 2500,
      clientWidth: 1356,
      getBoundingClientRect: () => ({ left: 42, right: 1398 }),
      contains: (node) => node === card,
      addEventListener: (name, fn) => {
        handlers[name] = fn;
      },
      scrollBy: (move) => moves.push(move),
    };
    const context = vm.createContext({scheduleStudioSave(){},
      $: (selector) =>
        selector === "#workbench-track" ? track : { disabled: false },
      window: { addEventListener() {} },
      document: { addEventListener() {} },
      matchMedia: () => ({ matches: reduced }),
    });
    vm.runInContext(
      readFileSync("src/campaign_studio/static/motif/studio.js", "utf8"),
      context,
    );
    vm.runInContext("bindWorkbench()", context);
    handlers.focusin({ target: { closest: () => card } });
    assert.equal(moves[0].left, 190);
    assert.equal(moves[0].behavior, reduced ? "instant" : "smooth");
    bounds = { left: 400, right: 770 };
    handlers.focusin({ target: { closest: () => card } });
    assert.equal(moves.length, 1);
    bounds = { left: 10, right: 380 };
    handlers.focusin({ target: { closest: () => card } });
    assert.equal(moves[1].left, -40);
    handlers.focusin({ target: { closest: () => ({}) } });
    assert.equal(moves.length, 2);
  }
});

test("failed workbench thumbnail preserves its campaign target", () => {
  let onError;
  const card = { campaignId: "8f57d6d7-9318-564d-b7a8-1b78b4e978b7", title: "Field Notes" };
  const artwork = {
    children: [],
    parentElement: card,
    // Model the different DOM replacement boundaries, not the handler's choice.
    set innerHTML(value) {
      this.children = [{ html: value }];
    },
  };
  const thumbnail = {
    parentElement: artwork,
    matches: (selector) => selector === "img[data-workbench-thumbnail]",
    set outerHTML(value) {
      const index = artwork.children.indexOf(this);
      if (index >= 0) artwork.children.splice(index, 1, { html: value });
    },
  };
  artwork.children = [thumbnail];
  const context = vm.createContext({scheduleStudioSave(){},
    window: { addEventListener() {} },
    document: {
      addEventListener(name, handler, capture) {
        if (name === "error") {
          assert.equal(capture, true);
          onError = handler;
        }
      },
    },
  });
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/studio.js", "utf8"),
    context,
  );
  onError({ target: thumbnail });
  assert.equal(artwork.children.length, 1);
  assert.match(artwork.children[0].html, /Image unavailable/);
  assert.deepEqual(artwork.parentElement, {
    campaignId: "8f57d6d7-9318-564d-b7a8-1b78b4e978b7",
    title: "Field Notes",
  });
  const preserved = [...artwork.children];
  onError({ target: { matches: () => false } });
  assert.deepEqual(artwork.children, preserved);
});

function staleFraming(h) {
  h.run(`beginFraming();framingContext.revision_id='old';framingDraft.zoom=1.4;framingDraft.focal=60;framingShowSafety=true;framingRecovery='latest';showFrameRecovery()`);
}
function latestFrame(h, revision = 'new') {
  const current = structuredClone(h.context.model);
  current.manifest = { revision_id: revision, outputs: [{ product_id: 'bag', market: 'es-MX', ratio: '1:1', url: '/saved' }] };
  current.editable_products = { bag: true };
  return current;
}

test('stale framing offers Refresh without reading or adopting newer settings', async () => {
  const h = framingHarness(); staleFraming(h);
  await h.run('requestFramePreview()');
  h.run('resetFrame();framingDraft.zoom=1.6;requestFramePreview()');
  assert.equal(h.requests.length, 0);
  assert.equal(h.get('#frame-primary').textContent, 'Refresh');
  assert.equal(h.get('#frame-status').textContent, h.context.MotifSave.conflictMessage);
  await h.run('recoverFraming()');
  assert.equal(h.context.refreshes, 1);
  assert.equal(h.requests.length, 0);
  assert.equal(h.decodes.length, 0);
  assert.equal(h.run('framingContext.revision_id'), 'old');
  assert.equal(h.run('framingDraft.zoom'), 1.6);
});

test('a removed framing editor cannot refresh or begin a stale read', async () => {
  const h = framingHarness(); staleFraming(h);
  h.run('endFraming(); modal=""');
  await h.run('recoverFraming()');
  assert.equal(h.context.refreshes, 0);
  assert.equal(h.requests.length, 0);
  assert.equal(h.decodes.length, 0);
});

test('framing retries transient preview errors and confirms identity before classifying a 409', async () => {
  const h = framingHarness(); h.run('framingDraft.zoom=1.4');
  const first = h.run('requestFramePreview()'); h.requests.shift().reject(new Error('offline')); await first;
  assert.equal(h.get('#frame-primary').textContent, 'Retry preview');
  const retry = h.run('recoverFraming()');
  assert.equal(h.requests[0].body.revision_id, 'revision');
  await h.finishPreview('/retry'); await retry;
  assert.equal(h.get('#frame-primary').textContent, 'Apply');
  for (const [revision, expected] of [['revision', 'preview'], ['next', 'latest']]) {
    const preview = h.run('requestFramePreview()');
    h.requests.shift().reject({status:409,code:'state'}); await flush();
    h.requests.shift().resolve(latestFrame(h, revision)); await preview;
    assert.equal(h.run('framingRecovery'), expected);
  }
  h.run('framingDraft.zoom=1.8;requestFramePreview()');
  assert.equal(h.requests.length, 0);
});

test("range input and change events coalesce paced nudges to the final exact preview and cancel cleanly", async () => {
  const h = framingHarness(), timers = new Map();
  let now = 0, next = 0;
  h.context.setTimeout = (fn, delay) => { const id = ++next; timers.set(id, {fn, at: now + delay}); return id; };
  h.context.clearTimeout = id => timers.delete(id);
  const advance = async ms => {
    now += ms;
    for (const [id, timer] of [...timers]) if (timer.at <= now) { timers.delete(id); timer.fn(); }
    await flush();
  };
  const get = selector => {
    const e = h.get(selector);
    e.id = selector.replace(/^#/, "");
    e.listeners ||= {};
    e.addEventListener = (name, fn) => { e.listeners[name] = fn; };
    return e;
  };
  h.context.$ = get;
  const sliders = ["#frame-zoom", "#frame-focal", "#frame-vertical"].map(get);
  h.context.$$ = selector => selector === ".framing-control input" ? sliders : [...sliders, get("#frame-layout")];
  h.context.ResizeObserver = class { observe() {} disconnect() {} };
  h.run("framingDraft.zoom=1.4;bindFraming()");
  await h.finishPreview("/initial");
  const slider = get("#frame-focal");
  for (let i = 1; i <= 20; i++) {
    slider.value = 50 - i / 10;
    slider.listeners.input({target: slider});
    slider.listeners.change({target: slider});
    assert.equal(h.get("[data-action=saveFrame]").disabled, true);
    if (i < 20) await advance(100);
  }
  assert.equal(h.requests.length, 0, "change must not bypass the input debounce");
  await advance(159); assert.equal(h.requests.length, 0);
  await advance(1); assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].body.framing.focal_x, 0.48);
  assert.equal(h.requests[0].body.product_id, "bag");
  assert.equal(h.requests[0].body.market, "es-MX");
  assert.equal(h.requests[0].body.ratio, "1:1");
  await h.finishPreview("/final-nudges");
  assert.equal(h.get("[data-action=saveFrame]").disabled, false);
  assert.equal(h.run("framePreview.result.digest"), "/final-nudges");
  slider.value = 47; slider.listeners.input({target: slider}); slider.listeners.change({target: slider});
  h.run("endFraming();modal=''");
  await advance(200);
  assert.equal(h.requests.length, 0, "closing cancels the queued request");
  assert.equal(h.run("framePreview"), null);
});

test("dragging the framed photo tracks source pixels, clamps travel, and cancels independently of safety marking", async () => {
  const h = framingHarness();
  const handlers = {}, pan = h.get("#frame-pan");
  Object.assign(pan, {
    addEventListener: (type, listener) => { handlers[type] = listener; },
    getBoundingClientRect: () => ({ width: 500, height: 500 }),
    closest: () => null, setPointerCapture(id) { this.capture = id; },
    hasPointerCapture(id) { return this.capture === id; }, releasePointerCapture() { this.capture = null; },
  });
  let paint;
  h.context.requestAnimationFrame = next => { paint = next; return 1; };
  h.context.requestFramePreview = () => {};
  h.run("framingSource.width=1500;framingDraft.zoom=2;framingPan=bindFramePan()");
  function fire(type, x, y, extra={}) {
    handlers[type]({ target:pan, button:0, pointerId:1, clientX:x, clientY:y,
      preventDefault() {}, stopPropagation() {}, ...extra });
  }
  const position = () => Array.from(h.run("[framingDraft.focal,framingDraft.vertical]"), value => Number(value.toFixed(8)));
  fire("pointerdown", 0, 0); fire("pointermove", 100, 100); paint();
  assert.equal(await h.run("saveFraming()"), false, "an active drag cannot publish mid-gesture");
  assert.deepEqual(position(), [40,30]); // 100 displayed pixels = 100 source pixels on both axes.
  fire("pointermove", -5000, 5000); paint(); assert.deepEqual(position(), [100,0]);
  fire("pointercancel", -5000, 5000); assert.deepEqual(position(), [50,50]);
  fire("pointerdown", 0, 0); fire("pointermove", 100, 100); paint();
  fire("lostpointercapture", 100, 100); assert.deepEqual(position(), [50,50]);
  h.run("framingDraft.zoom=1");
  fire("pointerdown", 0, 0); fire("pointermove", 50, 100); paint(); fire("pointerup", 50, 100);
  assert.deepEqual(position(), [30,50]); // At fit, only horizontal travel exists.
  fire("pointerdown", 0, 0); fire("pointermove", 50, 0); paint();
  fire("keydown",0,0,{key:"Escape"}); assert.deepEqual(position(),[30,50]);
  for (const state of ["framingMarking=true", "framingMarking=false;frameSaving=true",
    "frameSaving=false;framingRecovery='uncertain'"]) {
    h.run(state);fire("pointerdown",0,0);assert.equal(pan.capture,null);
  }
  h.run("framingRecovery=null");pan.hidden=true;fire("pointerdown",0,0);
  assert.equal(pan.capture,null,"hidden pan surface cannot capture");
});

test('marked safety area applies directly after exact preview decode, without a separate editing commit', async () => {
  const h = framingHarness();
  h.run('markFrameSubject()');
  assert.equal(h.run('framingMarking'), true);
  assert.equal(h.get('#frame-primary').disabled, true);
  assert.equal(h.requests.length, 1);
  assert.ok(Math.abs(h.requests[0].body.framing.subject_box.left - .25) < 1e-12);
  await h.finishPreview('/marked');
  assert.equal(h.get('#frame-primary').disabled, false);
  assert.equal(h.get('#frame-source').hidden, false);
  assert.equal(h.get('#frame-image').hidden, true);
  assert.equal(h.get('#frame-status').textContent, '');
  h.context.finishFraming = async () => true;
  const apply = h.run('saveFraming()');
  await flush();
  assert.match(h.requests[0].url, /framing\/apply$/);
  assert.equal(h.requests[0].body.preview_digest, '/marked');
  assert.ok(Math.abs(h.requests[0].body.framing.subject_box.right - .75) < 1e-12);
  h.requests.shift().resolve({revision_id:'applied'});
  await flush();
  h.requests.shift().resolve({manifest:{revision_id:'applied'}});
  assert.equal(await apply, true);
});

test('closing safety edits discards them; a crop cutting the current area never enables Apply', async () => {
  const h = framingHarness();
  h.run('markFrameSubject();endFraming();modal=""');
  h.requests.shift().resolve({digest:'late',blocked:false,images:[]});
  await flush();
  assert.equal(h.decodes.length, 0);
  h.run('modal="frame";beginFraming();framingSource={width:1000,height:1000,src:"/source"}');
  assert.equal(h.run('framingDraft.subject'), null);
  h.run('framingMarking=true;framingDraft.subject={x:.1,y:.1,w:.8,h:.8};framingDraft.zoom=2;requestFramePreview()');
  await h.finishPreview('/unsafe');
  assert.equal(h.get('#frame-primary').disabled, true);
  assert.match(h.get('#frame-status').textContent, /Crop cuts the marked product area/);
  await assert.rejects(h.run('saveFraming()'), /safe framing preview/);
  assert.equal(h.requests.length, 0);
});

test('drawing cancellation restores the previous safety box and keyboard edits preview the updated box', async () => {
  const h = framingHarness(), events = new Map();
  h.context.ResizeObserver = class { observe() {} disconnect() {} };
  for (const selector of ['#frame-source','#frame-show-safety','#frame-subject-box']) {
    const element = h.get(selector);
    element.addEventListener = (name, handler) => events.set(selector+':'+name, handler);
  }
  const stage=h.get('#frame-source');
  stage.getBoundingClientRect=()=>({left:0,top:0,width:100,height:100});
  stage.setPointerCapture=()=>{}; stage.hasPointerCapture=()=>false;
  h.context.$$=()=>[];
  h.context.bindFramePan=()=>null;
  h.run('bindFraming()'); await h.finishPreview('/initial');
  h.run('markFrameSubject()'); await h.finishPreview('/marked');
  const original=h.run('JSON.stringify(framingDraft.subject)');
  const fire=(name, extra={})=>events.get('#frame-source:'+name)({pointerId:1,button:0,clientX:10,clientY:10,preventDefault(){},stopPropagation(){},...extra});
  fire('pointerdown');
  assert.equal(h.get('#frame-primary').disabled, true);
  assert.equal(await h.run('saveFraming()'), false);
  fire('pointermove',{clientX:80,clientY:80});
  assert.notEqual(h.run('JSON.stringify(framingDraft.subject)'), original);
  fire('pointercancel');
  assert.equal(h.run('JSON.stringify(framingDraft.subject)'), original);
  await h.finishPreview('/restored');
  events.get('#frame-subject-box:keydown')({key:'ArrowRight',preventDefault(){}});
  assert.ok(Math.abs(h.requests[0].body.framing.subject_box.left - .26) < 1e-12);
  await h.finishPreview('/moved');
  events.get('#frame-subject-box:keydown')({key:'ArrowDown',shiftKey:true,preventDefault(){}});
  assert.ok(Math.abs(h.requests[0].body.framing.subject_box.bottom - .76) < 1e-12);
  await h.finishPreview('/resized');
  assert.equal(h.get('#frame-primary').disabled, false);
});


test("a failed read after accepted Framing Apply never claims that the prior image stayed unchanged", async()=>{
 const h=framingHarness();h.run("framingDraft.zoom=1.4");
 const preview=h.run("refreshFramePreview()");await h.finishPreview();await preview;
 const saving=h.run("saveFraming()");await flush();h.requests.shift().resolve({revision_id:"new"});await flush();
 h.requests.shift().reject({status:409,code:"apply_not_accepted"});await saving;
 assert.equal(h.run("framingRecovery"),"uncertain");
 assert.doesNotMatch(h.get("#frame-status").textContent,/previous image is unchanged/);
});


test('Something New precedes the existing campaigns without changing their recent order', () => {
  const context=vm.createContext({
    workbenchCampaigns:()=>[{id:'newest'},{id:'older'},{id:'oldest'}],
    pendingDeletionControl:()=>'',btn:()=>'',head:()=>'',homeHero:()=>'',
  });
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'),context);
  vm.runInContext("head=()=>'';homeHero=()=>'';workbenchCard=c=>'<article data-card=\"'+c.id+'\"></article>'",context);
  const html=vm.runInContext('library()',context);
  assert.ok(html.indexOf('Something new?')<html.indexOf('data-card="newest"'));
  assert.ok(html.indexOf('data-card="newest"')<html.indexOf('data-card="older"'));
  assert.ok(html.indexOf('data-card="older"')<html.indexOf('data-card="oldest"'));
});
