// Execute the shipped local-preview controller with deferred responses. No browser,
// provider, network request, or application build is used by these regression cases.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness() {
  const elements = new Map();
  const get = (selector) => {
    if (!elements.has(selector))
      elements.set(selector, {
        disabled: true, dataset: {}, style: {},
        textContent: "",
        innerHTML: "",
        decode: async () => {},
        classList: { toggle() {} },
        setAttribute() {}, removeAttribute() {}, getAttribute() { return null; }, hasAttribute() { return false; }, remove() {},
        querySelectorAll: () => [],
        querySelector: (selector) => get(selector),
      });
    return elements.get(selector);
  };
  const pending = [], timers = [];
  const context = vm.createContext({
    crypto:require("node:crypto").webcrypto, flushCurrentWork:async()=>true, flushStudioSave:async()=>true, studioSave:null, framingPan:null, framingDrawing:false, scheduleStudioSave(){},
    campaignIdentity: () => ({name: "Campaign", brand: "Brand"}),
    MotifTextLimits: {issue: () => null, purposeIssue: () => null, guardFields: () => true},
    document: { addEventListener() {} },
    $: get,
    $$: () => [],
    clearTimeout() {},
    setTimeout(fn) { timers.push(fn); return timers.length; },
    C: {
      id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4",
      products: [{ id: "bag", name: "Bag" }, { id: "chalk", name: "Chalk" }],
      copy: { es: { headline: "Saved", cta: "Explore" } },
      overrides: {},
      rules: { en: [], es: [] },
    },
    model: {version:"current-version", composition_version:"current-version", output_brief: {brand: {text_color:"#263B2E",palette:["#263B2E","#FFFFFF"]}}, output_run_id: "run", manifest: { revision_id: "revision" }, project: { base: {} } },
    projectBuffers: { form_drafts: {}, review_drafts: {}, feedback: {} },
    projectQueue: { applied() {} },
    withProjectApply: (operation) => operation(),
    projectChanged() {},
    copy: structuredClone,
    MARKET: { es: "es-MX" },
    LANG: { es: "Spanish (Mexico)" },
    FMT: [{ ratio: "1:1", w: 1080, h: 1080 }],
    operation: null,
    campaignMetricDisplay: () => ({ attempts: "Unknown", spend: "Unknown" }),
    
    page: "review", reviewTab: "products",
    draft: null,
    activeRetry: () => null,
    activeBlockedProposal: () => null,
    correctionSummary: null,
    currentProduct: 0,
    selected: [0],
    currentFormat: 0,
    lang: "es",
    modal: "text", studioWorkspace: {},
    valueFor: (product, language, field) => context.C.overrides[context.C.products[product].id]?.[language]?.[field] ?? context.C.copy[language][field],
    imageCleanupReady: async () => true,
    imageCapacity: async () => false,
    showPendingImageCleanup() {},
    api: (url, body) =>
      new Promise((resolve, reject) =>
        pending.push({ url, body, resolve, reject }),
      ),
    viewModel() {},
    decodedImage: async (url) => ({ src: url }),
    creative: () => "<saved>",
    esc: (value) => value,
    openModal(name) {
      context.modal = name;
    },
    closeModal() {
      context.modal = "";
    },
    render() {},
    toast() {},
    studioApplied(value) { context.useData(value); return true; },
    navigate(target) { context.destination = target; },
    useData(value) { if (value.output_run_id) context.model = value; },
  });
  context.apiResponse = context.api;
  const originalAPI = context.api;
  context.api = (url,body) => url.includes("/progress/") ? Promise.reject(new Error("No temporary export job")) : originalAPI(url,body);
  context.window = context;
  context.refreshes = 0;
  context.location = {reload(){context.refreshes++;}};
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/save-state.js", "utf8"), context);
  for (const file of ["wording.js", "wording-host.js"])
    vm.runInContext(readFileSync(`src/campaign_studio/static/motif/${file}`, "utf8"), context);
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/storage.js", "utf8"),
    context,
  );
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/editor-controls.js", "utf8"),
    context,
  );
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/review.js", "utf8"),
    context,
  );
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/text.js", "utf8"), context);
  vm.runInContext("beginText(); textEditor.values.message = 'First'", context);
  get("#text-image").src = "/saved";
  return {
    context,
    get,
    pending, timers,
    run: (code) => vm.runInContext(code, context),
  };
}
const result = {
  digest: "preview",
  blocked: false,
  images: [{ id: "bag/es-MX/1x1", url: "/draft", layers: {base_url: "/base", text_blocks: {}} }],
  geometry: {text_blocks: {}},
  impact: { render_outputs: ["bag/es-MX/1x1"], generation_products: [] },
  findings: [],
};

test('too-wide text names accurate remedies locally and retains context for another affected output',()=>{
  const h=harness();h.context.findings=[{severity:'blocking',item:'bag/es-MX/1x1',evidence:'An unbroken copy segment is too wide for the output.'}];
  const message='A word is too wide for its text area. Shorten the word or reset the text layout.';
  assert.equal(h.run("wordingBlockDetails(findings,'bag/es-MX/1x1')"),message);
  assert.equal(h.run("wordingBlockDetails(findings,'chalk/es-MX/1x1')"),'Bag · Spanish (Mexico) · 1:1 · '+message);
});

test("late response cannot enable Apply for newer transient wording", async () => {
  const h = harness();
  const promise = h.run("previewText()");
  h.run("textEditor.values.message = 'Latest'; scheduleTextPreview()");
  h.pending.shift().resolve(result);
  await promise;
  assert.equal(h.run("textEditor.preview"), null);
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  assert.equal(await h.run("applyText()"), false);
  assert.equal(h.pending.length, 0);
});

test("Apply locks the current session and preserves unrelated raw form work", async () => {
  const h = harness();
  h.context.projectBuffers.form_drafts = {fields: {"brief/copy/en": {"copy-headline": "Other work"}}};
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await preview;
  const saving = h.run("applyText()");
  await flush();
  assert.equal(h.pending[0].body.values.message, "First");
  assert.equal(h.pending[0].body.preview_digest, "preview");
  assert.equal(h.run("textEditor.saving"), true);
  assert.equal(h.run("endText()"), false);
  assert.equal(await h.run("applyText()"), false);
  h.pending.shift().resolve({ complete: true });
  await flush();
  h.pending.shift().resolve({});
  await saving;
  assert.equal(h.context.modal, "text");
  assert.equal(h.context.projectBuffers.form_drafts.fields["brief/copy/en"]["copy-headline"], "Other work");
  assert.equal(h.pending.length, 0);
});

test("failed Apply preserves exact transient draft and requires receipt recovery", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await preview;
  const saving = h.run("applyText()");
  await flush();
  h.pending.shift().reject(Object.assign(new Error("Storage unavailable"), { status: 503 }));
  assert.equal(await saving, false);
  assert.equal(h.context.modal, "text");
  assert.equal(h.run("textEditor.recovery"), "uncertain");
  assert.equal(h.run("textEditor.values.message"), "First");
  assert.equal(h.context.model.manifest.revision_id, "revision");
  assert.equal(h.pending.length, 0);
});

test("unaccepted wording keeps the exact draft and offers Try again without an automatic retry", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await preview;
  const saving = h.run("applyText()");
  await flush();
  h.pending.shift().reject(Object.assign(new Error("Could not apply. Your edits are still here."), {
    status: 409, code: "apply_not_accepted",
  }));
  assert.equal(await saving, false);
  assert.equal(h.context.modal, "text");
  assert.equal(h.run("textEditor.values.message"), "First");
  assert.equal(h.run("textEditor.recovery"), "apply");
  assert.equal(h.get('[data-action="recoverText"]').textContent, "Try again");
  assert.match(h.get("#text-status").textContent, /problem saving.*previous image is unchanged.*edits are still here/);
  assert.equal(h.context.correctionSummary, null);
  assert.equal(h.context.model.manifest.revision_id, "revision");
  assert.equal(h.pending.length, 0);
});

test("per-field intent includes checked unchanged text and excludes untouched unchecked fields", () => {
  const h = harness();
  let body = h.run("textBody()");
  assert.equal(body.product_id, "bag");
  assert.deepEqual(JSON.parse(JSON.stringify(body.values)), { message: "First" });
  assert.deepEqual(JSON.parse(JSON.stringify(body.field_scopes)), { message: "product" });
  h.run("textEditor.values.message='Saved'; textEditor.all.message=true");
  body = h.run("textBody()");
  assert.equal(body.values.message, "Saved");
  assert.equal(body.field_scopes.message, "all_products");
  assert.equal("cta" in body.values, false);
  h.run("textEditor.values.cta='Selected CTA'");
  body = h.run("textBody()");
  assert.equal(body.field_scopes.cta, "product");
  assert.equal(body.values.cta, "Selected CTA");
});

test("campaign names in page headings are escaped as text", () => {
  const h = harness();
  h.context.esc = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"),
    h.context,
  );
  const html = h.run('head("<img src=x>", "A & B <script>bad</script>")');
  assert.ok(html.includes("&lt;img src=x&gt;"));
  assert.ok(html.includes("A &amp; B &lt;script&gt;"));
  assert.ok(!html.includes("<script>"));
  // The unavailable-campaign recovery shell has no authored draft yet.
  h.context.page = "campaign";
  h.context.draft = null;
  const api = readFileSync("src/campaign_studio/static/motif/api.js", "utf8");
  vm.runInContext(api.slice(api.indexOf("function campaignIdentity("), api.indexOf("async function saveDraft(")), h.context);
  const recovery = h.run('shell("Campaign could not be opened")');
  assert.ok(recovery.includes("Untitled campaign"));
  assert.ok(recovery.includes("Campaign could not be opened"));
});


test("missing affected preview cannot permit Apply or reduce the intended scope", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve({ ...result, images: [] });
  await preview;
  assert.match(h.get("#text-status").textContent, /Preview unavailable/);
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  assert.equal(await h.run("applyText()"), false);
  assert.equal(h.pending.length, 0);
});




for (const [completed, damaged, expected] of [
  [0, 0, false],
  [6, 0, true],
  [11, 0, true],
  [12, 0, true],
  [12, 1, false],
])
  test(`Handoff projects ${completed} completed images and ${damaged} damaged files`, () => {
    const h = harness(); delete h.context.model.composition_version;
    vm.runInContext(
      readFileSync(
        "src/campaign_studio/static/motif/presentation.js",
        "utf8",
      ),
      h.context,
    );
    const buttons = [];
    h.context.btn = (text, action, kind, extra = "") => {
      buttons.push({ text, action, disabled: extra.includes("disabled") });
      return text;
    };
    h.context.head = () => "";
    h.context.notice = (title, body, kind, action) => {
      buttons.push({ action });
      return title + " " + body;
    };
    h.context.damagedFiles = () => damaged;
    h.context.overviewView = () => "";
    h.context.descriptionCount = () => 0;
    h.context.total = () => 12;
    h.context.badge = (value) => value;
    Object.assign(h.context.C, {
      name: "Example",
      brand: "Example",
      markets: ["en", "es"],
      revision: 1,
    });
    h.context.model.manifest = {
      complete: completed === 12,
      metrics: { completed_images: completed },
      outputs: Array.from({ length: 12 }, (_, index) => ({
        id: `output-${index}`,
        file: index < completed ? `${index}.png` : null,
        url: index < completed ? `/images/${index}.png` : null,
      })),
      findings: [],
    };
    const html = h.run("handoff()");
    if (completed > 0 && completed < 12) {
      const count = 12 - completed;
      assert.ok(html.includes(`${count} ${count === 1 ? "output is" : "outputs are"} missing.`));
    }
    assert.equal(html.includes("Nothing to export yet"), completed === 0);
    assert.ok(!buttons.some((button) => button.action === "exportFolder"));
    for (const action of ["report", "exportZip"])
      assert.equal(
        buttons.find((b) => b.action === action).disabled,
        !expected,
      );
    if (!expected) {
      assert.ok(buttons.some((b) => b.action === "nav:production"));
      assert.throws(
        () => h.run("requireIntactExport()"),
        /Nothing to export yet|Saved files are missing/,
      );
    } else assert.doesNotThrow(() => h.run("requireIntactExport()"));
  });

test("report preview opens the reviewed package in a separate tab or offers a link when popups are blocked", async () => {
  for (const popupBlocked of [false, true]) {
    const h = harness(), visited = [];
    h.context.model.manifest.complete = true;
    h.context.model.manifest.metrics = {completed_images:12};
    h.context.model.metadata = {snapshot_id:"a".repeat(64)};
    h.context.damagedFiles = () => false;
    const tab = {opener:{}, document:{body:{}}, location:{replace:url => visited.push(url)}};
    h.context.window.open = (url, where) => {
      assert.deepEqual([url, where], ["about:blank", "_blank"]);
      assert.equal(h.pending.length, 0);
      return popupBlocked ? null : tab;
    };
    const opening = h.run("previewReport()"); await flush();
    assert.match(h.pending[0].url, /\/delivery\/report-preview$/);
    assert.equal(h.pending[0].body.version, "current-version");
    assert.equal(h.pending[0].body.allow_partial, false);
    h.pending.shift().resolve({url:"/preview/test/index.html"});
    await opening;
    if (popupBlocked) {
      assert.equal(h.context.modal, "open-report");
      assert.equal(h.run("reportPreviewURL"), "/preview/test/index.html");
    } else {
      assert.equal(tab.opener, null);
      assert.deepEqual(visited, ["/preview/test/index.html"]);
    }
  }
});

test("a refused report preview closes its temporary tab and retains the partial retry", async () => {
  const h = harness();
  h.run('requireIntactExport=()=>{};model.manifest.complete=false;model.metadata={snapshot_id:"partial"}');
  let closed = false;
  h.context.window.open = () => ({document:{body:{}}, close(){closed = true;}});
  const opening = h.run("previewReport(true)"); await flush();
  assert.equal(h.pending[0].body.allow_partial, true);
  h.pending.shift().reject(Object.assign(new Error("Reload the current campaign."), {status:409}));
  await opening;
  assert.ok(closed);
  assert.equal(h.context.modal, "export-failure");
  assert.equal(h.run("exportRecovery.kind"), "report");
  assert.equal(h.run("exportRecovery.allowPartial"), true);
});

test("Production recovery projects source counts and attempt outcomes", () => {
  const h = harness();
  for (const file of ['production.js','producer-repair.js','producer-host.js'])
    vm.runInContext(readFileSync(`src/campaign_studio/static/motif/${file}`, 'utf8'), h.context);
  Object.assign(h.context,{productLimit:5,dirty:false,busy:false,keyAvailable:false,assetURL:()=>null,FMT:[{ratio:'1:1'},{ratio:'9:16'},{ratio:'16:9'}],LANG:{en:'English',es:'Español'}});
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"),
    h.context,
  );
  h.context.btn = (text, action, kind, extra = "") =>
    `<button data-action="${action}" ${extra}>${text}</button>`;
  h.context.head = (title) => title;
  h.context.notice = (title) => title;
  h.context.badge = (text) => text;
  h.context.draft = {campaign_id:h.context.C.id};
  h.context.C.name = "Saved campaign";
  h.context.C.products = [
    { id: "bag", name: "Brush roll" },
    { id: "chalk", name: "Chalk" },
  ];
  Object.assign(h.context.model, {
    run: { id: "run", state: "partial", allowance: 1 },
    output_run_id: "run",
    source_urls: { chalk: "/chalk-source.png" },
    plan_brief: {products:h.context.C.products.map(p=>({...p,source:{role:'concept'}})),markets:{'en-US':{},'es-MX':{}}},
    plan: { outputs: 12, generation_products: ["bag"],findings:[] },
    run_attempts: [
      { product_id: "bag", state: "unknown", error_kind: "overall_deadline" },
    ],
    manifest: {
      metrics: { elapsed_processing_seconds: 2 },
      outputs: Array.from({ length: 6 }, (_,i) => ({
        product_id: "chalk",market:i<3?"en-US":"es-MX",ratio:["1:1","9:16","16:9"][i%3],
        url: "/saved.png",
      })),
    },
    retry_plan: { allowance: 2 },
    notice: { message: "Remote result is unknown." },
  });
  h.context.currentBriefValue = () => h.context.model.plan_brief;
  const html = h.run("production()");
  assert.match(html, /Result not confirmed/);
  assert.match(html, /This run: 1 of 1 requests used · 0 remaining/);
  assert.match(html, /The request result was not confirmed. A charge may still apply./);
  assert.match(html, /class="pr-operation-count">1 <small>\/ 2 source images/);
  assert.equal((html.match(/pr-product-card/g)||[]).length, 2);
  assert.match(
    html,
    /Result not confirmed/,
  );
  assert.match(html, /data-action="resumeGeneration"/);
  assert.match(html, /data-pr-action="studio"/);
  assert.ok(!html.includes("Your paid image is already saved"));
  assert.equal(h.pending.length, 0);
});

// Brief palette and representative pixels are covered by brief_host.test.cjs
// and test_brief_feedback.py; the old decorative Brand snapshot was retired.

test("product image choices preserve current modes and expose the two approved actions", () => {
  for (const treatment of ['reuse','concept','prepared']) {
    const context=vm.createContext({
    MotifTextLimits: {issue: () => null, purposeIssue: () => null, guardFields: () => true},document:{addEventListener(){}},window:{},operation:null,page:'production',
      modal:'product',draft:{products:[{id:'product'}]},projectBuffers:{form_drafts:{}},
      modalProduct:{id:'product',name:'Product',description:'Details',source:{role:treatment==='concept'?'concept':treatment==='prepared'?'prepared_product':'finished_scene',description:'Generation details'},treatment},
      retainedProductPhoto:()=>({source:{path:'retained.png'},url:'/retained'}),
    });
    context.window=context;
    for(const file of ['producer-repair.js','producer-host.js','presentation.js'])
      vm.runInContext(readFileSync(`src/campaign_studio/static/motif/${file}`,'utf8'),context);
    const html=context.modalView();
    assert.match(html,/data-pr-action="mode-photo" aria-pressed="(?:true|false)">Select existing image/);
    assert.match(html,/data-pr-action="mode-concept" aria-pressed="(?:true|false)">Generate new image/);
    assert.match(html,treatment==='concept'?/data-pr-action="mode-concept" aria-pressed="true"/:/data-pr-action="mode-photo" aria-pressed="true"/);
    assert.equal(context.modalProduct.treatment,treatment);
    assert.doesNotMatch(html,/Adopt saved source|Place a product cutout|type="radio"/);
  }
});

test("source recovery guidance routes to Production instead of asserting source integrity", () => {
  const h = harness();
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"),
    h.context,
  );
  h.context.operation = {
    notice_code: "source_unavailable",
    notice: "Open Production, attach the intended source again.",
  };
  const actions = [];
  h.context.notice = (title, body, severity, action) => {
    actions.push(action);
    return title + " " + body;
  };
  assert.equal(h.run("unavailableSource()"), true);
  assert.match(
    h.run("sourceRecoveryNotice()"),
    /attach the intended source again/,
  );
  assert.deepEqual(actions, ["nav:production"]);
});

test("wording Apply waits for PNG decode and failure preserves the saved image and typing", async () => {
  const h = harness();
  let finish, fail;
  h.context.decodedImage = url => url === "/base" ? Promise.resolve({src:url}) :
    new Promise((resolve, reject) => {
      finish = resolve;
      fail = reject;
    });
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await flush();
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  assert.equal(h.run("textEditor.preview"), null);
  fail(new Error("Image load failed"));
  await preview;
  assert.equal(h.get("#text-image").src, "/saved");
  assert.match(h.get("#text-status").textContent, /Image load failed/);
  assert.equal(h.run("textEditor.values.message"), "First");
  h.context.modal = "text";
  const next = h.run("previewText()");
  h.pending.shift().resolve(result);
  await flush();
  let display;
  h.get("#text-image").decode = () =>
    new Promise((resolve) => (display = resolve));
  finish({ src: "/draft" });
  await flush();
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  display();
  await next;
  assert.equal(h.get('[data-action="applyText"]').disabled, false);
});

test("wording image decode cannot approve a later draft or closed dialog", async () => {
  for (const close of [false, true]) {
    const h = harness();
    let finish;
    h.context.decodedImage = url => url === "/base" ? Promise.resolve({src:url}) : new Promise((resolve) => (finish = resolve));
    const preview = h.run("previewText()");
    h.pending.shift().resolve(result);
    await flush();
    if (close) h.context.modal = "";
    else {
      h.run("textEditor.values.message = 'Latest'");
      h.run("scheduleTextPreview()");
    }
    finish({ src: "/old" });
    await preview;
    assert.equal(h.run("textEditor.preview"), null);
    assert.equal(h.get('[data-action="applyText"]').disabled, true);
    assert.notEqual(h.get("#text-image").src, "/old");
  }
});

test("late review metadata keeps an active dialog and cannot overwrite another revision or newer snapshot", async () => {
  for (const change of ["dialog", "campaign", "revision", "newer"]) {
    const h = harness();
    const renders = [];
    h.context.model.can_edit = true;
    h.context.model.can_describe = true;
    h.context.model.metadata = {
      creatives: { "bag/es-MX/1x1": { version: 0 } },
    };
    h.context.render = (options) => renders.push(options);
    const saving = h.run("saveMetadata(0,'es',0,{description:'A supplied description'})");
    h.context.modal = "frame";
    h.context.framingDraft = { zoom: 1.472 };
    if (change === "campaign") h.context.C.id = "431b08e0-fa8c-5067-8e65-35ad54d3a6cd";
    if (change === "revision") h.context.model.manifest.revision_id = "new";
    if (change === "newer")
      h.context.model.metadata.creatives["bag/es-MX/1x1"].version = 2;
    h.pending.shift().resolve({
      creatives: { "bag/es-MX/1x1": { version: 1, description: "A supplied description" } },
    });
    await saving;
    assert.equal(h.context.framingDraft.zoom, 1.472);
    if (change === "dialog") assert.equal(renders[0].preserveModal, true);
    else assert.equal(renders.length, 0);
  }
});

test("partial image descriptions remain editable while missing all-products inputs block Apply", async () => {
  const h = harness();
  h.context.model.can_edit = false;
  h.context.model.can_describe = true;
  h.context.model.metadata = { creatives: { "bag/es-MX/1x1": { version: 0 } } };
  const saving = h.run("saveMetadata(0,'es',0,{description:'A supplied description'})");
  assert.equal(h.pending[0].body.description, "A supplied description");
  h.pending.shift().resolve({ creatives: { "bag/es-MX/1x1": { version: 1, description: "A supplied description" } } });
  await saving;
  h.run("textEditor.all.message=true");
  const preview = h.run("previewText()");
  h.pending.shift().reject(Object.assign(new Error("Saved inputs missing for Chalk."), { status: 409, code: "edit_inputs" }));
  await flush();
  h.pending.shift().resolve(h.context.model);
  await preview;
  assert.equal(h.context.modal, "text");
  assert.match(h.get("#text-status").textContent, /Chalk/);
  assert.equal(h.run("textEditor.values.message"), "First");
  assert.equal(h.run("textEditor.all.message"), true);
  assert.equal(await h.run("applyText()"), false);
  assert.equal(h.pending.length, 0);
});

function zipHarness() {
  const h = harness();
  h.context.model.manifest.metrics = { completed_images: 6 };
  h.context.model.metadata = { snapshot_id: "review" };
  h.context.damagedFiles = () => false;
  h.get('meta[name="campaign-token"]').content = "local-token";
  const source = readFileSync("src/campaign_studio/static/motif/api.js", "utf8");
  h.run(source.slice(source.indexOf("async function apiResponse("), source.indexOf("async function api(")));
  return h;
}

test("Download ZIP posts the reviewed snapshot and gives the verified attachment to the browser", async () => {
  const h = zipHarness(), calls = [], sent = [], removed = [], revoked = [], timers = [];
  const button = h.get("#overlay [data-action='confirmPartial'], #overlay [data-action='retryExport']");
  button.textContent = "Download partial ZIP";
  button.setAttribute = (key, value) => button[key] = value;
  button.removeAttribute = (key) => delete button[key];
  let downloaded;
  h.context.fetch = async (url, options) => {
    calls.push({url, options});
    return new Response(Buffer.from([80, 75, 3, 4]), {
      headers: {"Content-Type":"application/zip", "Content-Disposition":"attachment; filename*=UTF-8''Caf%C3%A9%20autumn.zip"},
    });
  };
  h.context.URL = {
    createObjectURL(blob) { downloaded = blob; return "blob:campaign"; },
    revokeObjectURL(url) { revoked.push(url); },
  };
  const anchor = {click() { sent.push([this.href, this.download]); }, remove() { removed.push(this); }};
  h.context.document.createElement = (tag) => { assert.equal(tag, "a"); return anchor; };
  h.context.document.body = {append(el) { assert.equal(el, anchor); }};
  h.context.setTimeout = (fn, delay) => timers.push({fn, delay});
  const downloading = h.run("downloadZIP(true)"); await flush();
  await downloading;
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/delivery\/zip$/);
  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[0].options.headers["X-Campaign-Token"], "local-token");
  const body = JSON.parse(calls[0].options.body); assert.equal(body.version,"current-version"); assert.equal(body.allow_partial,true); assert.match(body.operation_id,/^[a-f0-9]{32}$/);
  assert.deepEqual([...new Uint8Array(await downloaded.arrayBuffer())], [80,75,3,4]);
  assert.deepEqual(sent, [["blob:campaign", "Café autumn.zip"]]);
  assert.deepEqual(removed, [anchor]);
  assert.deepEqual(revoked, []);
  const revoke = timers.find(t=>t.delay===60000); assert.ok(revoke); revoke.fn();
  assert.deepEqual(revoked, ["blob:campaign"]);
  assert.equal(h.context.modal, "export-success");
  assert.equal(h.run("exportReceipt.filename"), "Café autumn.zip");
  assert.equal(button.textContent, "Download partial ZIP");
  assert.equal(button["aria-busy"], undefined);
});

test("a stale ZIP response shows its server error and a changed target never starts a download", async () => {
  const h = zipHarness();
  h.context.fetch = async () => new Response(JSON.stringify({
    message:"The current images changed. Reload before exporting.", code:"stale_snapshot",
  }), {status:409, headers:{"Content-Type":"application/json"}});
  await h.run("downloadZIP(false)");
  assert.equal(h.context.modal, "export-failure");
  assert.match(h.run("exportRecovery.message"), /Reload before exporting/);
  let finish;
  h.context.fetch = async () => ({
    ok:true, headers:new Headers({"Content-Type":"application/zip"}),
    blob:() => new Promise((resolve) => { finish = resolve; }),
  });
  const delayed = h.run("downloadZIP(false)");
  await flush();
  h.context.C.id = "other-campaign";
  finish(new Blob(["old"]));
  await delayed;
  assert.equal(h.run("exportReceipt"), null);
  assert.equal(h.context.modal, "export-failure");
});

test("failed export retries the same captured composition and temporary job", async () => {
  const h=harness(); h.context.model.manifest.metrics={completed_images:6};
  h.context.model.manifest.complete=false; h.context.damagedFiles=()=>false;
  const first=h.run("downloadZIP(true)"); await flush();
  const initial=h.pending.shift();
  initial.reject(Object.assign(new Error("Local package writing failed."),{status:503})); await first;
  assert.equal(h.context.modal,"export-failure");
  const retry=h.run("retryExport()"); await flush();
  const next=h.pending.shift(); assert.deepEqual(next.body,initial.body);
  next.reject(Object.assign(new Error("Local package still unavailable."),{status:503})); await retry;
  assert.equal(h.run("exportRecovery.snapshot"),"current-version");
  assert.equal(h.run("exportRecovery.operationId"),initial.body.operation_id);
});

test("an export retry refused after another tab saves offers Refresh without resending the stale version", async () => {
  const h = harness();
  h.context.model.manifest.metrics = { completed_images: 6 };
  h.context.damagedFiles = () => false;
  h.context.dialog = (title, body, actions) => title + body + actions;
  h.context.notice = text => text;
  h.context.btn = (text, action) => `<button data-action="${action}">${text}</button>`;
  h.context.api = async () => ({status:"failed",completed:4,total:6,failed_outputs:[
    {product_name:"Bag",market:"es-MX",ratio:"1:1"},
    {product_name:"Chalk",market:"es-MX",ratio:"1:1"},
  ]});
  const first = h.run("downloadZIP(false)"); await flush();
  const original = h.pending.shift();
  original.reject(Object.assign(new Error("Two images could not be created."), {code:"delivery_retry",status:409}));
  await first;
  assert.match(h.run("exportFailureModal()"), /4 of 6 images/);
  assert.match(h.run("exportFailureModal()"), /Retry failed images/);

  // Another browser has saved, while this browser still holds its old version.
  const retry = h.run("retryExport()"); await flush();
  const obsolete = h.pending.shift();
  assert.deepEqual(obsolete.body, original.body);
  obsolete.reject(Object.assign(new Error("This campaign changed after the download started. Start a new download to use the current saved settings."), {code:"composition_conflict",status:409}));
  await retry;
  assert.equal(h.context.model.composition_version, "current-version");
  const html = h.run("exportFailureModal()");
  assert.match(html, /Refresh to load the latest changes/);
  assert.match(html, /Unsaved edits in this window will be discarded/);
  assert.match(html, /Download ZIP again/);
  assert.match(html, /<button data-action="retryExport">Refresh<\/button>/);
  assert.doesNotMatch(html, /Retry failed images|Retry ZIP download|4 of 6|We couldn’t create|Start a new download/);
  assert.equal(h.context.refreshes, 0, "the conflict must not refresh automatically");
  h.context.flushCurrentWork = () => assert.fail("Refresh must not flush or resend the stale campaign");
  h.context.api = h.context.apiResponse = () => assert.fail("Refresh must not retry an obsolete export");
  await h.run("retryExport()");
  assert.equal(h.context.refreshes, 1);
  assert.equal(h.context.MotifSave.refreshing(), true);
  assert.equal(h.pending.length, 0);
});

test("initial stale ZIP and report refusals offer the same explicit Refresh recovery", async () => {
  for (const [kind, code] of [["zip","composition_conflict"],["zip","stale_snapshot"],["report","stale_report"]]) {
    const h = harness();
    h.context.dialog = (title, body, actions) => title + body + actions;
    h.context.notice = text => text;
    h.context.btn = (text, action) => `<button data-action="${action}">${text}</button>`;
    h.context.refusal = Object.assign(new Error("Reload before trying again."), {code,status:409});
    await h.run(`exportFailure(refusal, deliveryPath(), deliveryVersion(), "${kind}", false)`);
    const html = h.run("exportFailureModal()");
    assert.match(html, /Refresh<\/button>/);
    assert.match(html, kind === "zip" ? /Download ZIP again/ : /open the report preview again/);
    assert.doesNotMatch(html, /Retry ZIP download|Retry report preview/);
    h.context.flushCurrentWork = () => assert.fail("A stale refusal must not flush before Refresh");
    await h.run("retryExport()");
    assert.equal(h.context.refreshes, 1);
    assert.equal(h.pending.length, 0);
  }
});

test("a font-unavailable ZIP response offers the reinstall remedy and Close without image retries", async () => {
  const h = zipHarness();
  const message = "The bundled composition font is missing or unreadable. Reinstall Motif – Campaign Studio to restore it, then try again. Your campaign settings and source images are unchanged.";
  h.context.fetch = async () => new Response(JSON.stringify({code:"font_unavailable",message}), {
    status:503,headers:{"Content-Type":"application/json"},
  });
  h.context.dialog = (title, body, actions) => title + body + actions;
  h.context.notice = text => text;
  h.context.btn = (text, action) => `<button data-action="${action}">${text}</button>`;
  const before = JSON.stringify(h.context.model);
  await h.run("downloadZIP(false)");
  assert.equal(h.context.modal,"export-failure");
  assert.equal(h.run("exportRecovery.code"),"font_unavailable");
  // The installation remedy takes precedence even over an earlier progress poll.
  h.run("exportRecovery.progress={status:'failed',completed:2,total:6,failed_outputs:[{product_name:'Bag',market:'es-MX',ratio:'1:1'}]}");
  const html = h.run("exportFailureModal()");
  assert.match(html,/A required font is unavailable/);
  assert.ok(html.includes(message));
  assert.match(html,/>Close<\/button>/);
  assert.doesNotMatch(html,/retryExport|Retry failed images|Retry ZIP download|2 of 6|We couldn’t create/);
  assert.equal(JSON.stringify(h.context.model),before);
  const closing = h.run("cancelExport()");
  assert.match(h.pending[0].url,/\/delivery\/cancel$/);
  h.pending.shift().resolve({status:"cancelled"});
  await closing;
  assert.equal(h.context.modal,"");
  assert.equal(h.run("exportRecovery"),null);
});

test("local fit guidance omits repeated target context without losing typing", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve({ ...result, blocked: true, images: [], findings: [{ item: "bag/es-MX/1x1", severity: "blocking", evidence: "Message does not fit the text area." }] });
  await preview;
  assert.equal(h.context.modal, "text");
  assert.match(h.get("#text-status").textContent, /^Message does not fit/);
  assert.equal(h.run("textEditor.values.message"), "First");
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  assert.equal(h.pending.length, 0);
});

test("text preview rejected during its own autosave resumes on the acknowledged token without Refresh", async()=>{
  const h=harness();
  h.context.studioSave={dirty:()=>true,conflict:()=>assert.fail("own autosave is not an external conflict")};
  const pending=h.run("previewText()");
  h.pending.shift().reject({status:409,code:"composition_conflict"}); await pending;
  assert.equal(h.run("textEditor.recovery"),null);
  assert.equal(h.get("#text-status").textContent,"", "own-save preview recovery is silent in Studio");
  assert.equal(h.pending.length,0);
  h.context.studioSave.dirty=()=>false;
  h.run("textEditor.revision='own-acknowledged-save'");
  const current=h.run("previewText()");
  assert.equal(h.pending[0].body.revision_id,"own-acknowledged-save");
  h.pending.shift().resolve(result); await current;
  assert.equal(h.run("textEditor.recovery"),null);
  assert.equal(h.run("textEditor.preview.displayed"),true);
  assert.equal(h.context.refreshes,0);
});

test("conflicting wording offers Refresh without adopting the newer state or saving again", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await preview;
  const saving = h.run("applyText()");
  await flush();
  h.pending.shift().reject(Object.assign(new Error("This version changed."), { status: 409 }));
  await saving;
  assert.equal(h.run("textEditor.recovery"), "stale");
  assert.equal(h.pending.length, 0);
  assert.equal(h.get('[data-action="recoverText"]').textContent, "Refresh");
  assert.equal(h.get('#text-status').textContent, h.context.MotifSave.conflictMessage);
  await h.run("recoverText()");
  assert.equal(h.context.refreshes, 1);
  assert.equal(h.pending.length, 0);
  assert.equal(h.run("textEditor.values.message"), "First");
  assert.equal(h.run("textEditor.revision"), "revision");
});

test("an explicit known failed Apply retries its retained proposal only when Try again is chosen", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await preview;
  const failed = h.run("applyText()");
  await flush();
  h.pending.shift().reject(Object.assign(new Error("The image could not be written."), { status: 409, code: "apply_not_accepted" }));
  await failed;
  assert.equal(h.run("textEditor.recovery"), "apply");
  assert.match(h.get("#text-status").textContent,/The image could not be written/);
  assert.equal(h.pending.length,0);
  const retry = h.run("recoverText()");await flush();
  assert.match(h.pending[0].url, /apply$/);
  assert.equal(h.pending[0].body.preview_digest,"preview");
  h.pending.shift().reject(Object.assign(new Error("Still unavailable"),{status:409,code:"apply_not_accepted"}));
  await retry;
  assert.equal(h.pending.length, 0);
  assert.equal(h.run("textEditor.values.message"), "First");
});

test("lost Apply response reconciles only its exact receipt and never resends automatically", async () => {
  for (const matches of [true, false]) {
    const h = harness();
    const preview = h.run("previewText()");
    h.pending.shift().resolve(result);
    await preview;
    const saving = h.run("applyText()");
    await flush();
    h.pending.shift().reject(new TypeError("Failed to fetch"));
    await saving;
    assert.equal(h.run("textEditor.recovery"), "uncertain");
    assert.equal(h.pending.length, 0);
    const check = h.run("recoverText()");
    h.pending.shift().resolve({ output_run_id: "run", manifest: { revision_id: "new", edit_receipt: { kind: "text", base_revision: "revision", preview_digest: matches ? "preview" : "different" } } });
    await check;
    assert.equal(h.pending.length, 0);
    if (matches) assert.equal(h.context.modal, "text");
    else {
      assert.equal(h.context.modal, "text");
      assert.equal(h.run("textEditor.recovery"), "stale");
      assert.equal(h.run("textEditor.values.message"), "First");
    }
  }
});

test("uncertain rule-withheld Text correction retries only a fresh preview and preserves the draft", async () => {
  for (const previewFails of [false, true]) {
    const h = harness(), preview = h.run("previewText()");
    h.pending.shift().resolve(result); await preview;
    const saving = h.run("applyText()"); await flush();
    h.pending.shift().reject(Object.assign(new Error("Service unavailable"), {status:503})); await saving;
    const check = h.run("recoverText()");
    h.pending.shift().resolve({output_brief:{brand:{text_color:"#263B2E",palette:["#263B2E","#FFFFFF"]}},output_run_id:"run",rule_withheld_outputs:["bag/es-MX/1x1"],
      manifest:{revision_id:"revision",outputs:[{id:"bag/es-MX/1x1",product_id:"bag",market:"es-MX",ratio:"1:1",url:null}]}});
    await check;
    assert.equal(h.run("textEditor.recovery"),null);
    assert.equal(h.run("textEditor.values.message"),"First");
    assert.equal(h.get('[data-action="applyText"]').disabled,true);
    assert.equal(h.pending.length,0);assert.equal(h.timers.length,1);
    h.timers.shift()();assert.match(h.pending[0].url,/\/text\/preview$/);
    assert.equal(h.pending[0].body.revision_id,"revision");assert.equal(h.pending[0].body.values.message,"First");
    if(previewFails)h.pending.shift().reject(Object.assign(new Error("Saved image source is unavailable."),{status:409}));
    else h.pending.shift().resolve(result);
    await flush();
    if(previewFails){
      assert.match(h.pending[0].url,/\/api\/motif\/campaigns\//);
      h.pending.shift().resolve({output_run_id:"run",manifest:{revision_id:"revision"}});await flush();
    }
    assert.equal(h.get('[data-action="applyText"]').disabled,previewFails);
    assert.equal(h.run("textEditor.values.message"),"First");assert.equal(h.pending.length,0);
  }
});

test("recovered Handoff correction releases Text before returning and permits the next Studio visit", async () => {
  const h = harness();
  h.context.document.body = {classList: {remove() {}}};
  h.context.frameSaving = false;
  h.context.logoEditor = null;
  h.context.formats = [0];
  h.context.captureProject = () => {};
  h.run(readFileSync("src/campaign_studio/static/motif/studio-workspace.js", "utf8"));
  h.run(`let destroyed=0,disconnected=0;
    studioWorkspace={campaign:C.id,product:'bag',mode:'text',sequence:0,root:{classList:{remove(){}}}};
    wordingReturn={kind:'text'};
    returnFromWording=()=>{page='handoff';wordingReturn=null};
    refreshStudioTargets=()=>{};
    textEditor.input={active(){return false},destroy(){destroyed++}};
    textEditor.observer={disconnect(){disconnected++}};`);
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result); await preview;
  const refused = h.run("applyText()"); await flush();
  h.pending.shift().reject(Object.assign(new Error("Service unavailable"), {status:503}));
  assert.equal(await refused, false);
  assert.equal(h.run("textEditor.recovery"), "uncertain");
  const check = h.run("recoverText()");
  h.pending.shift().resolve({output_brief:{brand:{text_color:"#263B2E",palette:["#263B2E","#FFFFFF"]}},output_run_id:"run",rule_withheld_outputs:["bag/es-MX/1x1"],
    manifest:{revision_id:"revision",outputs:[{id:"bag/es-MX/1x1",product_id:"bag",market:"es-MX",ratio:"1:1",url:null}]}});
  await check;
  assert.equal(h.pending.length, 0);
  h.timers.shift()();
  assert.match(h.pending[0].url, /\/text\/preview$/);
  h.pending.shift().resolve(result); await flush();
  const apply = h.run("applyText()"); await flush();
  assert.equal(h.run("studioBusy()"), true);
  h.pending.shift().resolve({revision_id:"accepted"}); await flush();
  h.pending.shift().resolve({output_brief:{brand:{text_color:"#263B2E",palette:["#263B2E","#FFFFFF"]}},output_run_id:"run",manifest:{revision_id:"accepted",
    outputs:[{id:"bag/es-MX/1x1",product_id:"bag",market:"es-MX",ratio:"1:1",url:"/accepted.png",copy:{message:"First",cta:"Explore"}}]}});
  assert.equal(await apply, true);
  assert.equal(h.context.page, "handoff");
  assert.equal(h.run("textEditor"), null);
  assert.equal(h.run("studioWorkspace"), null);
  assert.equal(h.run("destroyed"), 1);
  assert.equal(h.run("disconnected"), 1);
  assert.equal(h.run("studioBusy()"), false);
  assert.equal(h.pending.length, 0);
  h.run(`studioWorkspace={campaign:C.id,product:'bag',mode:'inspect',sequence:0,root:{classList:{remove(){}}}};
    modal='inspect';page='review';
    mountStudioTool=async mode=>{studioWorkspace.mode=mode;modal=mode};`);
  await h.run("switchStudioTarget({mode:'logo'})");
  assert.equal(h.run("studioWorkspace.mode"), "logo");
  assert.equal(await h.run("leaveStudioWorkspace()"), true);
  assert.equal(h.run("studioWorkspace"), null);
});

test("Text recovery refuses genuinely missing outputs and ignores a retired response owner", async () => {
  for (const retired of [false,true]) {
    const h=harness();h.run("textEditor.recovery='uncertain'");
    const check=h.run("recoverText()");
    if(retired)h.run("beginText();textEditor.values.message='New editor draft'");
    h.pending.shift().resolve({output_brief:{brand:{text_color:"#263B2E",palette:["#263B2E","#FFFFFF"]}},output_run_id:"run",rule_withheld_outputs:retired?["bag/es-MX/1x1"]:[],
      manifest:{revision_id:"revision",outputs:[{id:"bag/es-MX/1x1",product_id:"bag",market:"es-MX",ratio:"1:1",url:null}]}});
    await check;
    assert.equal(h.timers.length,0);assert.equal(h.pending.length,0);
    assert.equal(h.run("textEditor.values.message"),retired?"New editor draft":"First");
    if(!retired)assert.match(h.get('#text-status').textContent,/image is unavailable/);
  }
});

test("a lost Text acknowledgement preserves later placement, wording revert, and field scope without republishing", async () => {
  for (const change of ['placement','wording revert','scope']) {
    const h=harness();
    h.run("textEditor.placements={message:{left:0.1,top:0.2}};textEditor.all.message=true");
    const preview=h.run("previewText()");h.pending.shift().resolve(result);await preview;
    const saving=h.run("applyText()");await flush();
    h.pending.shift().reject(new TypeError('Lost acknowledgement'));await saving;
    if(change==='placement')h.run("textEditor.placements.message.left=0.15");
    if(change==='wording revert')h.run("textEditor.values.message='Saved'");
    if(change==='scope')h.run("textEditor.all.cta=true");
    h.run('scheduleTextPreview()');assert.equal(h.run('textEditor.preview'),null);
    const check=h.run('recoverText()');
    h.pending.shift().resolve({output_run_id:'run',output_brief:{brand:{text_color:"#263B2E",palette:["#263B2E","#FFFFFF"]}},manifest:{revision_id:'accepted',
      edit_receipt:{kind:'text',base_revision:'revision',preview_digest:'preview'},
      outputs:[{id:'bag/es-MX/1x1',product_id:'bag',market:'es-MX',ratio:'1:1',url:'/accepted',copy:{message:'First',cta:'Explore'}}]}});
    await check;
    assert.equal(h.context.modal,'text');assert.equal(h.run('textEditor.revision'),'accepted');
    assert.equal(h.run('textEditor.recovery'),null);assert.equal(h.run('textEditor.attempt'),null);
    assert.equal(h.pending.length,0);assert.equal(h.timers.length,1);
    h.timers.shift()();const next=h.pending.shift();assert.match(next.url,/\/text\/preview$/);
    assert.equal(next.body.revision_id,'accepted');
    if(change==='placement'){
      assert.equal(next.body.placements.message.left,0.15);assert.deepEqual(Object.keys(next.body.values),[]);
      assert.equal(h.run('textEditor.all.message'),false);
    } else if(change==='wording revert'){
      assert.equal(next.body.values.message,'Saved');assert.equal(next.body.field_scopes.message,'all_products');
      assert.deepEqual(Object.keys(next.body.placements),[]);
    } else {
      assert.equal(next.body.values.cta,'Explore');assert.equal(next.body.field_scopes.cta,'all_products');
      assert.equal(Object.hasOwn(next.body.values,'message'),false);assert.deepEqual(Object.keys(next.body.placements),[]);
    }
    next.resolve({...result,digest:'new-preview'});await flush();
    assert.equal(h.get('[data-action="applyText"]').disabled,false);assert.equal(h.pending.length,0);
    // Only an explicit second Apply sends the new intent, bound to its fresh preview.
    const applying=h.run('applyText()');await flush();const request=h.pending.shift();
    assert.match(request.url,/\/text\/apply$/);assert.equal(request.body.revision_id,'accepted');
    assert.equal(request.body.preview_digest,'new-preview');
    request.reject(Object.assign(new Error('Test refusal preserves the later draft'),{status:422}));await applying;
    assert.equal(h.context.modal,'text');assert.equal(h.pending.length,0);
  }
});

test('rule-only, recovery-only, and mixed omissions retain the correct independent remedies', () => {
  for (const [withheld, damaged] of [[true, 0], [false, 1], [true, 1]]) {
    const h = harness();
    vm.runInContext(readFileSync('src/campaign_studio/static/motif/presentation.js', 'utf8'), h.context);
    Object.assign(h.context, {
      head: () => '', btn: (label, action) => `[${action}]${label}`,
      notice: (title, body, kind, action) => `${title} ${body} [${action}]`,
      damagedFiles: () => damaged, overviewView: () => '', studioView: () => '', descriptionCount: () => 0,
      total: () => 2, badge: value => value, reviewTab: 'overview',
    });
    h.context.C.markets = ['es'];
    h.context.model.rule_withheld_outputs = withheld ? ['one'] : [];
    h.context.model.manifest = {complete:false,metrics:{completed_images:0},outputs:[{id:'one',url:null},...(damaged?[{id:'two',url:null}]:[])],findings:[]};
    h.context.model.wording_issue = withheld ? {code:'prohibited_phrase', matches:[{rule:'rule-1',context:{product_id:'bag',product_name:'Bag',market:'es-MX',phrase:{configured_phrase:'Siempre',market:'es-MX',matches:[{field:'message',value:'Siempre aquí',origin:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'}]}}}]} : null;
    for (const surface of ['review()', 'handoff()']) {
      const html = h.run(surface);
      assert.equal(html.includes('prohibited-word list'),withheld,surface);
      if (withheld) assert.match(html,/Edit headline in Studio/);
      if (withheld&&!damaged) assert.doesNotMatch(html,/Recover locally|Review recovery options|Recover missing work|Open Production to recover/);
      if (damaged) assert.match(html,/local recovery|recovery\./i);
    }
    assert.doesNotMatch(h.run('handoff()'), /Open Findings|finding groups|human look/);
  }
});


test("partial report asks for confirmation before opening a tab or sending a request", async () => {
  const h = harness();
  h.run('requireIntactExport=()=>{};model.manifest.complete=false');
  await h.run('previewReport()');
  assert.equal(h.run('pendingExport'), 'report');
  assert.equal(h.context.modal, 'partial-export');
  assert.equal(h.pending.length, 0);
});

test("new-campaign dialog never inherits the previously opened campaign identity", () => {
  for (const name of ["Autumn Winds", "Bridle & Bough", ""]) {
    const context = vm.createContext({
    MotifTextLimits: {issue: () => null, purposeIssue: () => null, guardFields: () => true},
      modal: "new", C: {name}, esc: String,
      input: (label, id, value) => `<input id="${id}" value="${value}">`,
      area: (label, id, value) => `<textarea id="${id}">${value}</textarea>`,
      btn: label => label,
    });
    vm.runInContext(readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"), context);
    const html = context.modalView();
    assert.match(html, /class="eyebrow">New campaign</);
    assert.match(html, /id="new-name" value=""/);
    assert.match(html, /id="new-brand" value=""/);
    assert.match(html, /id="new-audience"><\/textarea>/);
    assert.equal(context.C.name, name);
  }
});

test("Overview distinguishes first production from saved missing and withheld outputs", () => {
  const context = vm.createContext({
    MotifTextLimits: {issue: () => null, purposeIssue: () => null, guardFields: () => true},
    C: {products: [{id: "bell", name: "Bell strap"}], markets: ["en"]},
    FMT: [{name: "Square", ratio: "1:1"}], LANG: {en: "English (United States)"},
    filter: "all", pageNum: 0, model: {manifest: null},
    outputFor: () => null, currentRuleWithholding: () => [], layoutFindingsFor: () => [], MARKET: {en:"en-US"},
    unavailableSource: () => false, esc: String,
    btn: (label, action) => `<button data-action="${action}">${label}</button>`,
    badge: label => `<span>${label}</span>`,
  });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"), context);
  context.unavailableSource = () => false;
  let html = context.overviewView();
  assert.match(html, /Ready for first images/);
  assert.match(html, /data-action="plan">Review production plan/);
  assert.doesNotMatch(html, /Missing|saved output|recovery options/);
  context.model.manifest = {outputs: []};
  html = context.overviewView();
  assert.match(html, /Missing/);
  assert.match(html, /saved output is unavailable/);
  context.outputFor = () => ({id: "bell/en-US/1x1"});
  context.currentRuleWithholding = () => ["bell/en-US/1x1"];
  html = context.overviewView();
  assert.match(html, /Image withheld by current rules/);
  assert.match(html, /Correct the prohibited wording/);
  assert.doesNotMatch(html, /Ready for first images|recovery options/);
  context.currentRuleWithholding = () => [];
  context.unavailableSource = () => true;
  assert.match(context.overviewView(), /Replace the unavailable source in Production/);
});

test('combined Text validates its selected decoded image even when wording only changes other products', async () => {
  for (const blocked of [false, true]) {
    const h = harness();
    h.run("textEditor.values.message='Saved';textEditor.all.message=true");
    const preview = h.run('previewText()');
    assert.equal(h.pending[0].body.product_id, 'bag');
    assert.equal(h.pending[0].body.field_scopes.message, 'all_products');
    h.pending.shift().resolve({...result, blocked, impact:{render_outputs:['chalk/es-MX/1x1'],generation_products:[]}});
    await preview;
    assert.equal(h.get('[data-action="applyText"]').disabled, blocked);
    assert.equal(h.run('textEditor.product'), 'bag');
    assert.equal(h.run('textEditor.values.message'), 'Saved');
  }
});
test('returning wording to its opening value uses the current no-change preview and cannot Apply', async () => {
  const h = harness();
  h.run("textEditor.values.message='Saved'");
  const preview = h.run('previewText()');
  assert.deepEqual(JSON.parse(JSON.stringify(h.pending[0].body.values)), {});
  h.pending.shift().resolve({...result, impact:{render_outputs:[],generation_products:[]}});
  await preview;
  assert.equal(h.get('#text-image').src, '/draft');
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  assert.equal(await h.run('applyText()'), false);
  assert.equal(h.pending.length, 0);
});


test('combined wording and scope inputs lock only during Apply and recover after refusal', async () => {
  const h = harness(), fields = [{disabled:false}, {disabled:false}];
  h.context.$$ = selector => selector.includes('[data-mf-value]') ? fields : [];
  const preview = h.run('previewText()');
  assert.ok(fields.every(field => !field.disabled), 'preview leaves typing enabled');
  h.pending.shift().resolve(result); await preview;
  const saving = h.run('applyText()'); await flush();
  assert.ok(fields.every(field => field.disabled), 'submitted wording and scope cannot accept ignored input');
  h.pending.shift().reject(Object.assign(new Error('Retain this draft.'), {status:409,code:'apply_not_accepted'}));
  await saving;
  assert.ok(fields.every(field => !field.disabled));
  assert.equal(h.run('textEditor.values.message'), 'First');
});

test('absent CTA hint waits for current geometry after new wording', () => {
  const h = harness(), block = {dataset:{textBlock:'cta'}, setAttribute() {}};
  h.context.$$ = selector => selector === '[data-text-block]' ? [block] : [];
  h.run('textEditor.geometry={text_blocks:{}};textEditor.geometrySequence=0;paintText()');
  assert.equal(h.get('small').hidden, false);
  h.run("textEditor.values.cta='A new call to action';scheduleTextPreview()");
  assert.equal(h.get('small').hidden, true);
  h.run('textEditor.geometrySequence=textEditor.sequence;textEditor.pending=true;paintText()');
  assert.equal(h.get('small').hidden, true);
});


test("damaged saved images route Text to guarded Production recovery without retrying preview", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().reject(Object.assign(new Error("Recover saved images in Production."), {status: 409, code: "saved_images_unavailable"}));
  await flush();
  h.pending.shift().resolve({output_run_id: "run", manifest: {revision_id: "revision"}});
  await preview;
  assert.equal(h.run("textEditor.recovery"), "production");
  assert.equal(h.get('[data-action="recoverText"]').textContent, "Open recovery");
  assert.equal(h.get('[data-action="applyText"]').disabled, true);
  assert.equal(h.run("textEditor.values.message"), "First");
  const recovery = h.run("recoverText()");
  assert.equal(h.pending.length, 1);
  h.pending.shift().resolve({output_run_id: "run", manifest: {revision_id: "revision"}, file_integrity: {damaged_outputs: ["other/image"]}});
  await recovery;
  assert.equal(h.context.model.file_integrity.damaged_outputs[0], "other/image");
  assert.equal(h.context.destination, "production");
  assert.equal(h.pending.length, 0);
  assert.equal(h.run("textEditor.values.message"), "First");
});


test("a failed read after accepted Text Apply requires receipt recovery even when the read reports a refusal", async()=>{
 const h=harness();const preview=h.run("previewText()");h.pending.shift().resolve(result);await preview;
 const saving=h.run("applyText()");await flush();h.pending.shift().resolve({revision_id:"new"});await flush();
 h.pending.shift().reject({status:409,code:"apply_not_accepted"});await saving;
 assert.equal(h.run("textEditor.recovery"),"uncertain");
 assert.doesNotMatch(h.get("#text-status").textContent,/previous image is unchanged/);
});

test('export progress uses verified counts and reports continuing or retrying without invented percentages',async()=>{
 const h=harness(),button=h.get('progress'),details=[];
 button.parentElement={append:node=>details.push(node)};
 h.context.document.createElement=()=>({setAttribute(){},remove(){this.removed=true}});
 let progress={stage:'Creating finished images',completed:14,total:30,message:'There was a problem creating Lantern · Story · English. Continuing with the other images, then trying this one again.'};
 h.context.api=async()=>progress;h.context.button=button;
 h.run('stopProgress=watchDeliveryProgress(C.id,"operation",button)');
 await h.timers.shift()();
 assert.equal(button.textContent,'Creating images: 14 of 30.');
 assert.match(details[0].textContent,/Continuing with the other images/);
 progress={stage:'Retrying image',status:'retrying',completed:29,total:30,message:'Retrying Lantern · Story · English.'};
 await h.timers.shift()();assert.equal(button.textContent,'Retrying image…');
 assert.equal(details[0].textContent,'Retrying Lantern · Story · English.');
 h.run('stopProgress()');assert.equal(details[0].removed,true);
});
test('changed campaign cancels the retained export and starts a new composition snapshot',async()=>{
 const h=harness();h.context.calls=[];
 h.run(`exportRecovery={target:deliveryPath(),snapshot:'older',kind:'zip',allowPartial:false,operationId:'old-job'};
   api=async(path,body)=>calls.push({path,body});downloadZIP=async(partial,id)=>calls.push({partial,id});`);
 await h.run('retryExport()');
 assert.match(h.context.calls[0].path,/delivery\/cancel$/);
 assert.equal(h.context.calls[0].body.operation_id,'old-job');
 assert.equal(h.context.calls[1].id,null);
});


test("export failure lists every remaining image and shrinks after successful retries", () => {
  const h = harness();
  h.context.dialog = (title, body, actions) => title + body + actions;
  h.context.notice = text => text;
  h.context.btn = text => text;
  h.context.FMT = [{ratio:"9:16",name:"Story"},{ratio:"16:9",name:"Landscape"}];
  h.run(`exportRecovery={snapshot:deliveryVersion(),kind:"zip",progress:{status:"failed",completed:26,total:30,failed_outputs:[
    {id:"3",product_name:"Lantern",ratio:"9:16",market:"es-MX"},
    {id:"10",product_name:"Chair",ratio:"16:9",market:"es-MX"},
    {id:"12",product_name:"Sculpture",ratio:"9:16",market:"es-MX"},
    {id:"28",product_name:"Baker",ratio:"16:9",market:"es-MX"}]}}`);
  const first = h.run("exportFailureModal()");
  for (const name of ["Lantern", "Chair", "Sculpture", "Baker"]) assert.match(first,new RegExp(name));
  assert.equal((first.match(/<li>/g)||[]).length,4);
  assert.match(first,/26 of 30 images/);
  assert.match(first,/Retry failed images/);
  h.run("exportRecovery.progress.completed=29; exportRecovery.progress.failed_outputs=exportRecovery.progress.failed_outputs.slice(3)");
  const retried = h.run("exportFailureModal()");
  assert.match(retried,/Baker · Landscape · Spanish \(Mexico\)/);
  assert.doesNotMatch(retried,/Lantern|Chair|Sculpture/);
  assert.match(retried,/29 of 30 images/);
});

test('Handoff blocks invalid text layouts with an exact Studio repair path instead of offering a partial ZIP', () => {
 const h=harness();h.context.FMT=[{name:'Square',ratio:'1:1',w:1080,h:1080}];h.context.C.markets=['es'];h.context.model.editable_products={bag:true,chalk:true};
 h.context.model.manifest={complete:false,metrics:{completed_images:1},outputs:[
   {id:'bag/es-MX/1x1',product_id:'bag',market:'es-MX',ratio:'1:1',url:null},
   {id:'chalk/es-MX/1x1',product_id:'chalk',market:'es-MX',ratio:'1:1',url:'/valid'}],
   findings:[{item:'bag/es-MX/1x1',rule:'text_fit',severity:'blocking',evidence:'Headline extends beyond the canvas. Move or resize it to keep every character visible.',context:{product_id:'bag',market:'es-MX',ratio:'1:1'}}]};
 Object.assign(h.context,{head:()=>'',btn:(label,action,style,attributes)=>`<button data-action="${action}" ${attributes||''}>${label}</button>`,notice:(title,body)=>title+' '+body,damagedFiles:()=>false,descriptionCount:()=>0,total:()=>2,badge:value=>value,filter:'all',pageNum:0});
 h.context.outputFor=(product)=>h.context.model.manifest.outputs[product];
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'),h.context);
 const html=h.run('handoff()');
 assert.match(html,/Bag · Spanish \(Mexico\) · Square/);assert.match(html,/Adjust text in Studio/);
 assert.match(html,/data-action="repairLayout:0:0:es:text"/);
 assert.match(html,/data-action="exportZip"[^>]*disabled/);
 assert.doesNotMatch(html,/PARTIAL CAMPAIGN|output is missing|Partial handoff|Recover missing work/);
 assert.equal(h.run('otherUnavailableOutputs().length'),0);
 h.context.model.editable_products.bag=false;
 assert.match(h.run('handoff()'),/Restore source in Production/);
 assert.doesNotMatch(h.run('handoff()'),/The source images are available/);
 h.context.model.editable_products.bag=true;
 assert.throws(()=>h.run('requireIntactExport()'),/listed image layouts/);
 // A genuine missing source remains a partial export once the independent layout is fixed.
 h.context.model.manifest.findings=[];
 assert.equal(h.run('otherUnavailableOutputs().length'),1);
 assert.match(h.run('handoff()'),/1 output is missing/);
 assert.doesNotThrow(()=>h.run('requireIntactExport()'));
});

test('a blocked text-fit diagnostic displays renderer layers and geometry without claiming an exportable preview',async()=>{
 const h=harness();const promise=h.run('previewText()');
 h.pending.shift().resolve({...result,blocked:true,findings:[{rule:'text_fit',severity:'blocking',item:'bag/es-MX/1x1',evidence:'Headline extends beyond the canvas.'}]});
 await promise;
 assert.equal(h.get('#text-image').src,'/draft');assert.equal(h.get('#text-image').hidden,false);
 assert.ok(h.run('!!textEditor.layers'));assert.equal(h.run('textEditor.gestureBlock'),'message');
 assert.equal(h.run('textEditor.preview.displayed'),false);
 assert.equal(h.get('[data-action="applyText"]').disabled,true);
 assert.match(h.run('textEditor.message'),/extends beyond/);
});


test("Text font and palette appear before the first geometry response, and early choices retain measured placement",async()=>{
  const h=harness();
  h.get('#text-output').getBoundingClientRect=()=>({width:540,height:540});
  h.context.MotifManipulation={selectionFrame:rect=>rect};
  const first=h.run("previewText()");
  assert.match(h.get('#text-style').innerHTML,/Noto Sans/);
  assert.match(h.get('#text-style').innerHTML,/Brand palette/);
  h.run("changeTextStyle({font_id:'barlow_condensed'});changeTextStyle({color:'#FFFFFF'})");
  assert.match(h.get('#text-style').innerHTML,/barlow_condensed\" selected/);
  assert.equal(h.run('textEditor.pendingStyles.message.color'),'#FFFFFF');
  const placement={left:.12,top:.18,width:.6,font_size:59,font_id:'noto_sans',color:null};
  h.pending.shift().resolve({...result,geometry:{text_blocks:{message:{placement,bounds:[130,195,600,255]}}}});
  await first;await flush();
  assert.equal(await h.run('prepareTextStyleSave()'),true);
  assert.deepEqual(JSON.parse(h.run('JSON.stringify(textEditor.placements.message)')),{...placement,font_id:'barlow_condensed',color:'#FFFFFF'});
  assert.equal(h.run('Object.keys(textEditor.pendingStyles).length'),0);
});

test("Text saved style appears immediately without replacing its existing geometry",()=>{
  const h=harness();
  h.run("textEditor.savedPlacements={message:{left:.3,top:.4,width:.5,font_size:88,font_id:'barlow_condensed',color:'#FFFFFF'}};paintText()");
  assert.match(h.get('#text-style').innerHTML,/barlow_condensed\" selected/);
  h.run("changeTextStyle({color:null})");
  assert.equal(h.run('textEditor.placements.message.left'),.3);
  assert.equal(h.run('textEditor.placements.message.font_size'),88);
  assert.equal(h.run('textEditor.placements.message.color'),null);
});


test("a save flush cannot silently widen a captured whole-campaign selection", async () => {
  const h = zipHarness();
  h.context.model.manifest.outputs = [{id:"one", url:"/one"}];
  h.context.selectableExportImage = output => !!output?.url;
  h.context.fetch = () => assert.fail("Changed inventory must be reviewed, not downloaded");
  h.context.flushCurrentWork = async () => {
    h.context.model.manifest.outputs.push({id:"two", url:"/two"}); return true;
  };
  await assert.rejects(h.run('downloadZIP(false, null, null, ["one"])'), /available images changed/);
});
test("an empty explicit export selection cannot become a whole-campaign request", async () => {
  const h=zipHarness(); h.context.model.manifest.outputs=[{id:"one",url:"/one"}];
  h.context.selectableExportImage=output=>!!output?.url;
  h.context.fetch=()=>assert.fail("No request may be sent for an empty selection");
  await assert.rejects(h.run('downloadZIP(false, null, [], [])'), /Review your selected images/);
});
test("subset retry preserves copied IDs even when the caller changes its selection", async () => {
  const h=harness(); h.context.model.manifest.metrics={completed_images:2};
  h.context.model.manifest.outputs=[{id:"one",url:"/one"},{id:"two",url:"/two"}];
  h.context.selectableExportImage=output=>!!output?.url; h.context.damagedFiles=()=>false;
  h.context.ids=["one"];
  const first=h.run('downloadZIP(false,null,ids,ids)'); await flush();
  const original=h.pending.shift();
  h.context.ids.push("two"); original.reject(new Error("Controlled failure")); await first;
  assert.equal(JSON.stringify(h.run('exportRecovery.outputIds')), '["one"]');
  const retry=h.run('retryExport()'); await flush();
  const next=h.pending.shift(); assert.deepEqual(next.body,original.body);
  assert.equal(JSON.stringify(next.body.output_ids),'["one"]');
  next.reject(new Error("Controlled retry failure")); await retry;
});
test("a selected export receipt uses its captured count and singular grammar", () => {
  const h=harness();
  h.run('exportReceipt={count:1,selected:true,missing:0}');
  assert.match(h.run('exportReceiptSummary()'),/^1 selected image,/);
  h.context.model.manifest.metrics={completed_images:30};
  assert.match(h.run('exportReceiptSummary()'),/^1 selected image,/);
});
