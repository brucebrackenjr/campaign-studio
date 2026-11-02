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
        setAttribute() {},
        querySelectorAll: () => [],
        querySelector: (selector) => get(selector),
      });
    return elements.get(selector);
  };
  const pending = [], timers = [];
  const context = vm.createContext({
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
    model: { output_brief: {brand: {}}, output_run_id: "run", manifest: { revision_id: "revision" }, project: { base: {} } },
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
    
    page: "review",
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
  context.window = context;
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

test("unaccepted wording keeps the editor and exact draft with explicit preview retry", async () => {
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
  assert.equal(h.run("textEditor.recovery"), "retry");
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
    const h = harness();
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
    for (const action of ["report", "exportZip", "exportFolder"])
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

test("folder export binds the displayed review snapshot and returns a selectable escaped location", async () => {
  const h = harness();
  h.context.model.manifest.complete = true;
  h.context.model.manifest.metrics = { completed_images: 12 };
  h.context.model.metadata = { snapshot_id: "a".repeat(64) };
  h.context.damagedFiles = () => 0;
  h.context.openModal = () => {};
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
  const element = h.get("#overlay .modal-body");
  element.insertAdjacentHTML = (position, html) => {
    element.innerHTML += html;
  };
  const operation = h.run("createFolder(false)");
  assert.equal(h.pending[0].body.snapshot_id, "a".repeat(64));
  h.pending.shift().resolve({
    location: '/tmp/Creative "Review" <folder>',
    url: "/reports/21e50cf4-4634-413f-a6d4-f928f28f8106/index.html",
  });
  await operation;
  assert.match(element.innerHTML, /readonly/);
  assert.ok(element.innerHTML.includes("&quot;Review&quot; &lt;folder&gt;"));
  assert.ok(element.innerHTML.includes("/reports/21e50cf4-4634-413f-a6d4-f928f28f8106/index.html"));
});

test("Production recovery projects real saved counts and attempt outcomes", () => {
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
  assert.match(html, /class="pr-operation-count">6 <small>\/ 12 images/);
  assert.equal((html.match(/pr-product-card/g)||[]).length, 2);
  assert.match(
    html,
    /Result not confirmed/,
  );
  assert.match(html, /data-action="retryPlan"/);
  assert.match(html, /data-action="partialReview"/);
  assert.ok(!html.includes("Your paid image is already saved"));
  assert.equal(h.pending.length, 0);
});

// Brief palette and representative pixels are covered by brief_host.test.cjs
// and test_brief_feedback.py; the old decorative Brand snapshot was retired.

test("product image choices preserve current modes and expose the two approved actions", () => {
  for (const treatment of ['reuse','concept','prepared']) {
    const context=vm.createContext({document:{addEventListener(){}},window:{},operation:null,page:'production',
      modal:'product',draft:{products:[{id:'product'}]},projectBuffers:{form_drafts:{}},
      modalProduct:{id:'product',name:'Product',description:'Details',source:{role:treatment==='concept'?'concept':treatment==='prepared'?'prepared_product':'finished_scene',description:'Generation details'},treatment},
      retainedProductPhoto:()=>({source:{path:'retained.png'},url:'/retained'}),
    });
    context.window=context;
    for(const file of ['producer-repair.js','producer-host.js','presentation.js'])
      vm.runInContext(readFileSync(`src/campaign_studio/static/motif/${file}`,'utf8'),context);
    const html=context.modalView();
    assert.match(html,/data-pr-action="mode-photo" aria-pressed="(?:true|false)">Choose existing image/);
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

test("failed export retains exact target and partial intent for explicit retry", async () => {
  const h = harness();
  h.context.model.manifest.metrics = { completed_images: 6 };
  h.context.model.manifest.complete = false;
  h.context.model.metadata = { snapshot_id: "saved-review-snapshot" };
  h.context.damagedFiles = () => false;
  const first = h.run("downloadZIP(true)");
  h.pending.shift().reject(
    Object.assign(new Error("Local package writing failed."), {
      status: 503,
    }),
  );
  await first;
  assert.equal(h.context.modal, "export-failure");
  assert.equal(h.run("exportRecovery.allowPartial"), true);
  assert.equal(
    h.run("exportRecovery.message"),
    "Local package writing failed.",
  );
  const retry = h.run("retryExport()");
  assert.equal(h.pending[0].body.allow_partial, true);
  assert.equal(h.pending[0].body.snapshot_id, "saved-review-snapshot");
  h.pending.shift().reject(
    Object.assign(new Error("Local package still unavailable."), {
      status: 503,
    }),
  );
  await retry;
  h.context.model.manifest.revision_id = "newer";
  await h.run("retryExport()");
  assert.equal(h.pending.length, 0);
  assert.equal(h.context.modal, "");
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

test("conflicting wording needs explicit same-target adoption and fresh preview before Apply", async () => {
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
  const adoption = h.run("recoverText()");
  h.pending.shift().resolve({ output_brief: {brand: {}}, output_run_id: "run", manifest: { revision_id: "new", outputs: [{ product_id: "bag", market: "es-MX", ratio: "1:1", url: "/latest" }] } });
  await flush();
  h.timers.shift()();
  assert.equal(h.pending[0].body.revision_id, "new");
  assert.equal(h.pending[0].body.values.message, "First");
  h.pending.shift().resolve(result);
  await adoption;
  assert.equal(h.context.modal, "text");
  assert.equal(h.pending.length, 0);
  assert.equal(h.run("textEditor.values.message"), "First");
});

test("known refused Apply offers preview retry without automatically applying again", async () => {
  const h = harness();
  const preview = h.run("previewText()");
  h.pending.shift().resolve(result);
  await preview;
  const failed = h.run("applyText()");
  await flush();
  h.pending.shift().reject(Object.assign(new Error("Review the proposed text."), { status: 422 }));
  await failed;
  assert.equal(h.run("textEditor.recovery"), "retry");
  const retry = h.run("recoverText()");
  assert.match(h.pending[0].url, /preview$/);
  h.pending.shift().resolve(result);
  await retry;
  assert.equal(h.pending.length, 0);
  assert.equal(h.get('[data-action="applyText"]').disabled, false);
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
    h.pending.shift().resolve({output_brief:{brand:{}},output_run_id:"run",rule_withheld_outputs:["bag/es-MX/1x1"],
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
  h.pending.shift().resolve({output_brief:{brand:{}},output_run_id:"run",rule_withheld_outputs:["bag/es-MX/1x1"],
    manifest:{revision_id:"revision",outputs:[{id:"bag/es-MX/1x1",product_id:"bag",market:"es-MX",ratio:"1:1",url:null}]}});
  await check;
  assert.equal(h.pending.length, 0);
  h.timers.shift()();
  assert.match(h.pending[0].url, /\/text\/preview$/);
  h.pending.shift().resolve(result); await flush();
  const apply = h.run("applyText()"); await flush();
  assert.equal(h.run("studioBusy()"), true);
  h.pending.shift().resolve({revision_id:"accepted"}); await flush();
  h.pending.shift().resolve({output_brief:{brand:{}},output_run_id:"run",manifest:{revision_id:"accepted",
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
    h.pending.shift().resolve({output_brief:{brand:{}},output_run_id:"run",rule_withheld_outputs:retired?["bag/es-MX/1x1"]:[],
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
    h.pending.shift().resolve({output_run_id:'run',output_brief:{brand:{}},manifest:{revision_id:'accepted',
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


test("partial package confirmation keeps the requested folder or report action without creating it", async () => {
  const h = harness();
  h.run('requireIntactExport=()=>{};model.manifest.complete=false');
  await h.run('createFolder(false)');
  assert.equal(h.run('pendingExport'), 'folder');
  assert.equal(h.context.modal, 'partial-export');
  await h.run('createFolder(true)');
  assert.equal(h.run('pendingExport'), 'report');
  assert.equal(h.pending.length, 0);
});


test("new-campaign dialog never inherits the previously opened campaign identity", () => {
  for (const name of ["Autumn Winds", "Bridle & Bough", ""]) {
    const context = vm.createContext({
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
    C: {products: [{id: "bell", name: "Bell strap"}], markets: ["en"]},
    FMT: [{name: "Square", ratio: "1:1"}], LANG: {en: "English (United States)"},
    filter: "all", pageNum: 0, model: {manifest: null},
    outputFor: () => null, currentRuleWithholding: () => [],
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
