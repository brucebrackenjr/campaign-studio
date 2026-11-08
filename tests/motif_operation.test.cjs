// Run the shipped controller against delayed responses; no browser or provider.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
const flush = () => new Promise((resolve) => setImmediate(resolve));
const campaign = (run, id = "292e9e84-eb00-5fdb-87e8-423f5e51fac4") => ({
  value: { campaign_id: id },
  run: run ? { id: run } : null,
  manifest: null,
});

test("Brief concept thumbnails require a source in the current plan", () => {
  const context = vm.createContext({
    document: { body: { dataset: { page: "campaign", cap: "5" } } },
    structuredClone,
  });
  const run = code => vm.runInContext(code, context);
  run(readFileSync("src/campaign_studio/static/motif/api.js", "utf8"));
  run(`
    draft={campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',name:'Saved',audience:'Audience',creative_direction:'Scene',
      brand:{name:'Brand',palette:[],logo_provenance:{},logo_path:null},copy_provenance:{},
      markets:{'en-US':{copy:{message:'Exact words'}}},
      products:[{id:'concept',source:{role:'concept',provenance:{}}},
                {id:'photo',source:{role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/photo.png',provenance:{}}}],
      replay:{sources:{concept:{path:'campaigns/earlier/inputs/master.png'}}}};
    model={run:{id:'new-plan'},concept_previews:{}};viewModel();
  `);
  assert.equal(run("C.products[0].image"), null);
  assert.equal(run("C.products[1].image"), "/uploads/photo.png");
  run("model.concept_previews.concept='/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4/sources/concept/1x1';viewModel()");
  assert.equal(run("C.products[0].image"), "/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4/sources/concept/1x1");
  run("page='review';model.output_run_id='earlier';model.manifest={revision_id:'saved'};viewModel()");
  assert.equal(run("C.products[0].image"), "/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4/runs/earlier/revisions/saved/sources/concept");
});

function harness() {
  const pending = [],
    timers = [],
    notices = [],
    handlers = {},
    elements = new Map();
  const get = (selector) => {
    if (!elements.has(selector))
      elements.set(selector, {
        classList: {
          remove() {
            this.removed = true;
          },
          add() {},
        },
        textContent: "",
        childNodes: [],
        querySelector: get,
      });
    return elements.get(selector);
  };
  const context = vm.createContext({
    crypto: require('node:crypto').webcrypto,
    withBriefPreviewPaused: (action, current = () => true) => current() ? action() : undefined,
    AbortController,
    document: {
      body: { dataset: { cap: "5", page: "production" } },
      querySelector: get,
      addEventListener(name, callback) {
        // These cases exercise the primary action dispatcher; format controls have
        // separate connected coverage. Keep the first listener instead of replacing it.
        if (!handlers[name]) handlers[name] = callback;
      },
    },
    window: { addEventListener() {} },
    structuredClone,
    modalView() {},
    localDialog: null, studioWorkspace: null, flushCurrentWork:async()=>true,
    destroyBriefFlow() {},
    mountBriefFlow() {},
    invalidatePreview() {},
    setTimeout: (fn) => {
      timers.push(fn);
      return timers.length;
    },
    clearTimeout() {},
    queueMicrotask,
    WORDING_RECOVERY: {},
    MotifTextLimits: { issue: () => null, guardFields: () => true }, // Length boundary has its own real-contract cases.
    captureProject() {},
    captureProductInputs() {},
    projectChanged() {},
    getSaveState: () => ({}),
    refreshProductionStatus() {},
    destroyProjectViews() {},
    mountProjectViews() {},
    clearProjectTarget() {},
    reopenProjectEditor() {},
    returnFromWording() {},
  });
  const run = (code) => vm.runInContext(code, context);
  run(readFileSync("src/campaign_studio/static/motif/api.js", "utf8"));
  run("viewModel=()=>{};refreshCatalog=()=>new Promise(()=>{});");
  run(
    readFileSync("src/campaign_studio/static/motif/production.js", "utf8"),
  );
  run(readFileSync("src/campaign_studio/static/motif/storage.js", "utf8"));
  run(readFileSync("src/campaign_studio/static/motif/actions.js", "utf8"));
  run("projectQueue={isDirty:()=>dirty};ensureProductionPlan=()=>{};");
  run("realCloseModal = closeModal");
  context.pending = pending;
  context.notices = notices;
  run(
    "api=(url,body)=>new Promise((resolve,reject)=>pending.push({url,body,resolve,reject}));render=()=>{};closeModal=()=>{};updateURL=()=>{};toast=message=>notices.push(message);",
  );
  const use = (data) => run(`useData(${JSON.stringify(data)})`);
  use(campaign("old"));
  return { run, use, pending, timers, notices, get, handlers, context };
}

test("Compare editor actions bind their own pane without changing the language or other pane", async () => {
  const h = harness();
  h.context.opened = [];
  h.run(`
    C={id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',products:[{id:'first',name:'First'},{id:'second',name:'Second'}]};
    selected=[1,0];formats=[2,1];lang='es';
    openModal=name=>opened.push({name,product:currentProduct,format:currentFormat,language:lang});
  `);
  for (const [action, name, product, format] of [
    ['text:0:1', 'text', 0, 1], ['text:1:2', 'text', 1, 2],
    ['frame:0:1', 'frame', 0, 1], ['inspect:1:2:es', 'inspect', 1, 2],
  ]) {
    await h.handlers.click({target:{closest:()=>({dataset:{action}})},preventDefault(){}});
    const value = h.context.opened.at(-1);
    assert.equal(value.name, name); assert.equal(value.product, product);
    assert.equal(value.format, format); assert.equal(value.language, 'es');
    assert.equal(h.run('JSON.stringify(selected)'), '[1,0]');
    assert.equal(h.run('JSON.stringify(formats)'), '[2,1]');
  }
  assert.equal(h.pending.length, 0);
});

test("new product identity is durable-shaped at creation, without remapping existing products", () => {
  const h = harness();
  const first = h.run("newProduct()"), second = h.run("newProduct()");
  assert.match(first.id, /^product-[0-9a-f]{32}$/);
  assert.notEqual(first.id, second.id);
  h.run(`draft.products=[{id:'product-existing',name:'Existing',source:{role:'concept',provenance:{}}}];C.products=[];projectBuffers={form_drafts:{}};openModal=()=>{}`);
  h.run("productDraft(\"product-existing\")");
  assert.equal(h.run("modalProduct.id"), "product-existing");
});

test("Production product authoring targets current stable IDs while the reviewed plan stays pinned", async () => {
  const h = harness();
  h.run(`
    draft.products=['a','b'].map(id=>({id,name:'Authored '+id,description:'Current description',
      source:{role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/'+id+'.png',provenance:{label:'Origin',rights:'Rights'}},
      framing:{'en-US':{'1:1':{zoom:1.2}}},copy_overrides:{'es-MX':{cta:'Ver'}}}));
    C.products=[{id:'b',name:'Reviewed b'},{id:'a',name:'Reviewed a'}];
    projectBuffers={form_drafts:{}};openModal=name=>modal=name;markDirty=()=>dirty=true;
    productDraft('a');
  `);
  assert.equal(h.run('modalProduct.name'), 'Authored a');
  h.run("modalProduct.source.path='Campaigns/Test-12345678/Assets/Products/replacement.png'");
  assert.equal(h.run('draft.products[0].source.path'), 'Campaigns/Test-12345678/Assets/Products/a.png', 'upload stays in the dialog until Update');
  for (const [selector, value] of Object.entries({
    '#product-name':'Updated a','#product-description':'Updated description',
    '#product-origin':'Own work','#product-rights':'Owned','input[name="treatment"]:checked':'reuse',
  })) h.get(selector).value = value;
  h.run('draft.products.reverse();saveProductDraft()');
  assert.equal(h.run("draft.products.find(p=>p.id==='a').name"), 'Updated a');
  assert.equal(h.run("draft.products.find(p=>p.id==='b').name"), 'Authored b');
  assert.equal(h.run("draft.products.find(p=>p.id==='a').framing['en-US']['1:1'].zoom"), 1.2);
  assert.equal(h.run("draft.products.find(p=>p.id==='a').copy_overrides['es-MX'].cta"), 'Ver');
  assert.equal(h.run("C.products[1].name"), 'Reviewed a');
  h.run("productDraft('a')");
  await h.run("dispatchAction({action:'confirmRemove'})");
  assert.equal(h.run('draft.products.length'), 1);
  assert.equal(h.run('draft.products[0].id'), 'b');
  assert.equal(h.pending.length, 0, 'authoring never prepares or starts a plan');
  assert.throws(()=>h.run("productDraft('a')"), /no longer/);
});

test("Add product uses authored count and preserves new identity independently of an older five-product plan", async () => {
  const h = harness();
  h.run("draft.products=[];C.products=Array(5).fill({id:'old'});projectBuffers={form_drafts:{}};openModal=name=>modal=name");
  await h.run("dispatchAction({action:'addProduct'})");
  assert.equal(h.run('modal'), 'product');
  assert.match(h.run('modalProduct.id'), /^product-[0-9a-f]{32}$/);
  h.run("draft.products=Array(5).fill({id:'current'});modal=''");
  await h.run("dispatchAction({action:'addProduct'})");
  assert.equal(h.run('modal'), 'product-cap');
  h.run("draft.products.pop();modal=''");
  await h.run("dispatchAction({action:'addProduct'})");
  assert.equal(h.run('modal'), 'product');
  assert.equal(h.pending.length, 0);
});

test("one generated-product description commits to both the card and generation input", () => {
  const h = harness();
  h.run(`draft.products=[{id:'generated',name:'Bells',description:'Old card',
    source:{role:'concept',description:'Old generation input',provenance:{}},copy_overrides:{'es-MX':{cta:'Ver'}}}];
    projectBuffers={form_drafts:{}};openModal=name=>modal=name;markDirty=()=>dirty=true;productDraft('generated');`);
  for (const [selector,value] of Object.entries({'#product-name':'Bells','#product-description':'Show every brass bell.',
    '#product-origin':'Own work','#product-rights':'Owned'})) h.get(selector).value=value;
  h.run('saveProductDraft()');
  assert.equal(h.run('draft.products[0].description'),'Show every brass bell.');
  assert.equal(h.run('draft.products[0].source.description'),'Show every brass bell.');
  assert.equal(h.run("draft.products[0].copy_overrides['es-MX'].cta"),'Ver');
  assert.equal(h.run('draft.products[0].id'),'generated');
  assert.equal(h.pending.length,0);
});

test("Campaign and Production source inspection uses the current stable ID without changing accepted source inspection", async () => {
  const h=harness();
  h.run(`draft.products=[{id:'new',name:'Current attachment',description:'Current',
    source:{role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/new.png',provenance:{}}}];
    C.products=[{id:'old',name:'Accepted attachment'}];openModal=name=>modal=name;`);
  await h.run("dispatchAction({action:'productSource:new'})");
  assert.equal(h.run('modalProduct.name'),'Current attachment');
  assert.equal(h.run('modalProduct.image'),'/uploads/new.png');
  assert.equal(h.run('C.products[0].name'),'Accepted attachment');
  await h.run("dispatchAction({action:'source:0'})");
  assert.equal(h.run('modalProduct'),null);
  assert.equal(h.run('currentProduct'),0);
  await h.run("dispatchAction({action:'productSource:removed'})");
  assert.match(h.notices.at(-1),/no longer/);
});

test("explicit rule add/edit assigns and preserves a stable internal ID", async () => {
  const h = harness();
  h.run("draft.markets={'en-US':{phrase_rules:[]}};lang='en';ruleIndex=-1;markDirty=()=>{};model.run=null");
  h.get('#mw-rule-phrase').value = 'guaranteed';
  const click = () => h.handlers.click({target:{closest:()=>({dataset:{action:'saveRule'}})},preventDefault(){}});
  await click();
  const id = h.run("draft.markets['en-US'].phrase_rules[0].id");
  assert.match(id, /^rule-[0-9a-f]{32}$/);
  h.run('ruleIndex=0');
  await click();
  assert.equal(h.run("draft.markets['en-US'].phrase_rules[0].id"), id);
  assert.equal(h.run("Object.keys(draft.markets['en-US'].phrase_rules[0]).sort().join(',')"), 'case_sensitive,id,match,phrase');
});

function navigationHarness() {
  const h = harness();
  h.run(`
    C={id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',markets:['en','es'],products:[{id:'a'},{id:'b'}]};
    draft={campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',audience:'Typed audience'};
    baseline=JSON.stringify({campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',audience:'Saved audience'});
    projectBuffers={form_drafts:{fields:{'brief/copy/en':{'copy-headline':'A'}}}};
    page='review';dirty=true;modal='';saved=[{id:'62220994-1d32-5713-80d2-ac0b6ff0baf3'},{id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'}];wordingRecovery=null;
    openModal=name=>{modal=name};closeModal=()=>{modal=''};
    refreshCatalog=async()=>{};scrollTo=()=>{};
  `);
  h.context.steps = [];
  h.run(`
    model.project={authored:{brief:JSON.parse(baseline),form_drafts:{fields:{'brief/copy/en':{'copy-headline':'Saved pending A'}}}}};
    restoreProject=value=>{draft=structuredClone(value.brief);projectBuffers.form_drafts=structuredClone(value.form_drafts)};
    saveProject=async()=>{steps.push('project');dirty=false;return true};
    flushCurrentWork=async()=>{try{return await saveProject()}catch{return false}};
    updateWordingPreview=async()=>{throw new Error('Project Save must not preview images')};
    saveDraft=async()=>{throw new Error('Project Save must not use a separate brief writer')};
    saveWording=async()=>{throw new Error('Project Save must not apply images')};
  `);
  const click = (action, campaignId) =>
    h.handlers.click({
      target: { closest: () => ({ dataset: { action, campaignId } }) },
      preventDefault() {},
    });
  return { ...h, click };
}

test("campaign metric display keeps complete zero, lower bounds, missing usage, and campaign identity distinct", () => {
  const h = harness();
  const display = (coverage, count, estimate, subtotal = 0) =>
    h.run(`campaignMetricDisplay(${JSON.stringify({
      campaign_id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4", coverage, attempted_requests: count,
      usage_cost_estimate: { estimated_usd: estimate, known_usage_subtotal_usd: subtotal },
    })}, '292e9e84-eb00-5fdb-87e8-423f5e51fac4', false)`);
  const zero = display("complete", 0, 0);
  assert.equal(zero.attempts, "0");
  assert.equal(zero.spend, "$0.00");
  assert.equal(zero.spend_helper, "No generation attempts in this campaign.");
  assert.equal(display("complete", 2, 0.106).spend, "$0.11");
  assert.equal(display("complete", 1, 0.00108).spend, "< $0.01");
  const partial = display("complete", 2, null, 0.0995);
  assert.equal(partial.attempts, "2");
  assert.equal(partial.spend, "Incomplete");
  assert.equal(partial.spend_helper, "Known portion: $0.10 USD.");
  assert.equal(display("complete", 1, null).spend_helper, "Usage or pricing could not be verified.");
  const incomplete = display("incomplete", 1, null, 0.0065);
  assert.equal(incomplete.attempts, "1+");
  assert.equal(incomplete.attempts_helper, "Recorded attempts · history incomplete.");
  assert.equal(incomplete.detail, "Earlier request records are incomplete. The recorded count is a minimum.");
  assert.equal(display("unknown", 0, null).attempts_helper, "Earlier records are incomplete.");
  assert.equal(display("unknown", 0, null).detail, "Earlier records are incomplete.");
  assert.equal(display("unknown", null, null).detail, "Earlier records are incomplete.");
  assert.doesNotMatch(partial.detail, /recorded count is a minimum/);
  for (const amount of [0.0042, 0.125, 1234.565])
    assert.equal(h.run(`compactUSD(${amount})`), amount < 0.01 ? "< $0.01" : amount === 0.125 ? "$0.13" : "$1,234.57");
  assert.equal(h.run("campaignMetricDisplay(null,'292e9e84-eb00-5fdb-87e8-423f5e51fac4',true).spend"), "Loading…");
  assert.equal(h.run("campaignMetricDisplay(null,'292e9e84-eb00-5fdb-87e8-423f5e51fac4',false).spend"), "Unavailable");
  assert.equal(h.run("campaignMetricDisplay({campaign_id:'62220994-1d32-5713-80d2-ac0b6ff0baf3',coverage:'complete',attempted_requests:99},'292e9e84-eb00-5fdb-87e8-423f5e51fac4',false).attempts"), "Unavailable");
});

test("campaign totals from a polled response never cross campaign boundaries", async () => {
  for (const switchesCampaign of [false, true]) {
    const h = harness();
    h.run("model.campaign_metrics={campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',attempted_requests:1}");
    const polling = h.run("poll()");
    if (switchesCampaign) {
      h.use(campaign("new", "62220994-1d32-5713-80d2-ac0b6ff0baf3"));
      h.run("model.campaign_metrics={campaign_id:'62220994-1d32-5713-80d2-ac0b6ff0baf3',attempted_requests:7}");
    }
    h.pending.shift().resolve({run_id:"old", active:true,
      campaign_metrics:{campaign_id:"292e9e84-eb00-5fdb-87e8-423f5e51fac4",attempted_requests:2}});
    await polling;
    assert.equal(h.run("model.campaign_metrics.attempted_requests"), switchesCampaign ? 7 : 2);
  }
});

test("non-campaign modal transitions skip capture while existing campaign work is retained", () => {
  for (const activeCampaign of [false, true]) {
    const h = harness();
    let captured = 0;
    h.context.captureProject = () => {
      assert.ok(h.run("draft?.campaign_id"));
      captured++;
    };
    h.context.restoreProjectControls = () => {};
    h.run(`draft=${activeCampaign ? "{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'}" : "null"};modal='import';modalView=()=>''`);
    h.run("openModal('invalid-import')");
    assert.equal(h.run("modal"), "invalid-import");
    assert.equal(captured, activeCampaign ? 1 : 0);
  }
});

test("delayed initial dialog focus preserves a newer choice and cannot enter a closed dialog", () => {
  for (const state of ["initial", "focused", "closed"]) {
    const h = harness(), save = {}, dialog = h.get('#overlay [role="dialog"]');
    let focused = 0;
    dialog.isConnected = true;
    dialog.contains = element => element === save;
    h.get("#overlay button.close").focus = () => focused++;
    h.context.restoreProjectControls = () => {};
    h.run("modal='';openModal('product')");
    if (state === "focused") h.context.document.activeElement = save;
    if (state === "closed") dialog.isConnected = false;
    h.timers.shift()();
    assert.equal(focused, state === "initial" ? 1 : 0);
  }
});

test("draft-only Save and leave preserves campaign forms despite pending image cleanup", async () => {
  for (const pending of [[], [{ kind: "retirement", id: "existing" }]]) {
      const h = navigationHarness();
      h.context.policy = { pending };
      h.run(`
        model.image_cleanup=policy;
        projectBuffers.form_drafts.fields['brief/copy/es']={'copy-headline':'Otro borrador'};
        showStorage=state=>{modal=state};
      `);
      await h.run("navigate('library')");
      assert.equal(h.run("modal"), "");
      assert.equal(h.run("page"), "library");
      assert.deepEqual(h.context.steps, ["project"]);
      assert.equal(h.run("dirty"), false);
      assert.deepEqual(JSON.parse(h.run("JSON.stringify(model.image_cleanup.pending)")), pending);
      assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/es']['copy-headline']"), "Otro borrador");
      assert.equal(h.pending.length, 0);
  }
});

test("Save and leave preserves unfinished Brief forms; editor-owned Apply checks pending cleanup", async () => {
  for (const pending of [[], [{kind:"retirement",id:"existing"}]]) {
    const h=navigationHarness();
    h.context.policy={pending};
    h.run("model.image_cleanup=policy;showStorage=state=>{modal=state}");
    await h.run("navigate('library')");
    assert.equal(h.run("page"),"library");
    assert.deepEqual(h.context.steps,["project"]);
    assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']"),"A");
    h.run("page='review';modal='edit'");
    // Transient Apply owns its pending-cleanup flow so format Apply-and-switch uses
    // the same guard; generic action dispatch must not open a duplicate notice.
    assert.equal(h.run("guardImageCleanup('saveCorrection')"),false);
    assert.equal(h.run("modal"),"edit");
    assert.equal(h.pending.length,0);
  }
});

test("Cancel, close, and Escape abort only pending inventory and never reopen its late result", async () => {
  for (const exit of ["storage:cancel", "close", "Escape"]) {
    const h = navigationHarness();
    h.get("#overlay").childNodes = [];
    h.run(`
      api=(url,body,options)=>new Promise((resolve,reject)=>{
        pending.push({url,body,options,resolve,reject});
        options.signal.addEventListener('abort',()=>reject(new Error('Canceled inventory')));
      });
    `);
    const checking = h.click("storage:preview-delete");
    assert.equal(h.run("busy"), true);
    assert.equal(h.run("storageView.state"), "delete-loading");
    const old = h.pending[0];
    if (exit === "Escape")
      h.handlers.keydown({ key: "Escape", preventDefault() {} });
    else await h.click(exit);
    assert.equal(old.options.signal.aborted, true);
    await checking;
    assert.equal(h.run("busy"), false);
    assert.equal(h.run("modal"), "");
    assert.equal(h.run("draft.audience"), "Typed audience");
    assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']"), "A");
    const next = h.click("storage:preview-delete");
    assert.equal(h.run("busy"), true);
    old.resolve({ token: "obsolete" });
    await flush();
    assert.equal(h.run("busy"), true);
    assert.equal(h.run("storageView.state"), "delete-loading");
    await h.click("storage:cancel");
    await next;
    await h.run("navigate('library')");
    assert.equal(h.run("modal"), "");
    assert.equal(h.pending.length, 2); // No confirmation or cleanup request.
  }
});

test("Workbench deletion previews and confirms the clicked card even when another campaign is open", async () => {
  const h = navigationHarness();
  const target = "62220994-1d32-5713-80d2-ac0b6ff0baf3";
  const active = h.run("draft.campaign_id");
  h.run(`page='library'; saved[0].name='Second campaign'; finishDeletion=async()=>{};`);
  h.run(readFileSync("src/campaign_studio/static/motif/storage-components.js", "utf8"));
  h.context.MotifStorage = h.context.window.MotifStorage;
  const preview = h.click(`storage:card-delete:${target}`);
  assert.equal(h.pending[0].url, `/api/campaigns/${target}/cleanup/preview`);
  h.pending[0].resolve({ token: "second-only", name: "Second campaign" });
  await preview;
  assert.equal(h.run("storageView.state"), "delete-confirm");
  assert.equal(h.run("draft.campaign_id"), active);
  const confirmation = h.click("storage:confirm-delete");
  assert.equal(h.pending[1].url, `/api/campaigns/${target}/cleanup/confirm`);
  assert.equal(h.pending[1].body.token, "second-only");
  h.pending[1].resolve({ state: "complete" });
  await confirmation;
});

test("product JPEG attachment preserves unrelated Brief logo and raw work", async () => {
  const h = navigationHarness();
  h.run("draft.brand={logo_path:'Campaigns/Test-12345678/Assets/Products/original.png'};modalProduct={id:'jpeg-product',source:{path:'old-product.png'}};draft.products=[structuredClone(modalProduct)]");
  const product = h.run("upload({type:'image/jpeg',name:'photo.jpg'})");
  h.pending[0].resolve({reference:'Campaigns/Test-12345678/Assets/Products/photo.jpg',url:'/uploads/photo.jpg'});
  await product;
  assert.equal(h.run("modalProduct.source.path"),"Campaigns/Test-12345678/Assets/Products/photo.jpg");
  assert.equal(h.run("draft.brand.logo_path"),"Campaigns/Test-12345678/Assets/Products/original.png");
  assert.equal(h.run("draft.audience"),"Typed audience");
});

test("deliverable totals follow each supported selected-language count", () => {
  const h = harness();
  for (const products of [1, 5])
    for (const markets of [["en"], ["es"], ["en", "es"]]) {
      h.run(`C={products:Array(${products}),markets:${JSON.stringify(markets)}}`);
      assert.equal(h.run("total()"), products * markets.length * 3);
    }
});

test("page exits automatically flush Campaign fields without an Apply or discard dialog", async()=>{
  for(const target of ["library","campaign","production","handoff"]) {
    const h=navigationHarness(); await h.run(`navigate('${target}')`);
    assert.equal(h.run("page"),target); assert.equal(h.run("modal"),"");
    assert.deepEqual(h.context.steps,["project"]);
    assert.equal(h.run("draft.audience"),"Typed audience");
    assert.equal(h.pending.length,0);
  }
});
test("replacement navigation saves fields and keeps the selected Production tab",async()=>{
  const h=navigationHarness(); h.run("page='production';tab='copy'"); await h.click("nav:products");
  assert.equal(h.run("page"),"production"); assert.equal(h.run("tab"),"copy");
  assert.equal(h.run("modal"),""); assert.deepEqual(h.context.steps,["project"]);
});
test("unfinished Campaign values remain saveable before leaving without Production admission",async()=>{
  const h=navigationHarness(); h.run("draft.audience='';projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']=''");
  await h.run("navigate('library')"); assert.equal(h.run("page"),"library");
  assert.equal(h.run("draft.audience"),""); assert.equal(h.pending.length,0);
});
test("a save that reorders Workbench keeps the chosen destination campaign identity",async()=>{
  const h=navigationHarness(); h.run(`openCampaign=async id=>steps.push('open:'+id);
    saveProject=async()=>{saved.reverse();dirty=false;return true}`);
  await h.click("openCampaign","62220994-1d32-5713-80d2-ac0b6ff0baf3");
  assert.equal(h.context.steps.at(-1),"open:62220994-1d32-5713-80d2-ac0b6ff0baf3");
});
test("failed or held autosave retains all editing fields and blocks navigation until retry",async()=>{
  const h=navigationHarness(); h.run("saveProject=async()=>false"); await h.run("navigate('library')");
  assert.equal(h.run("page"),"review"); assert.equal(h.run("dirty"),true);
  assert.match(h.notices.at(-1),/changes are still here/);
  h.run("draft.audience='Newer audience';saveProject=async()=>{dirty=false;return true}");
  await h.run("navigate('library')"); assert.equal(h.run("page"),"library");
  assert.equal(h.run("draft.audience"),"Newer audience");
});
test("navigation waits for the current metadata acknowledgement and respects a failed acknowledgement",async()=>{
  const h=navigationHarness(); h.run("saveProject=()=>new Promise(resolve=>{finishProject=resolve})");
  const leaving=h.run("navigate('library')"); await flush();
  assert.equal(h.run("page"),"review");
  h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']='Newer';finishProject(false)");
  await leaving; assert.equal(h.run("page"),"review");
  assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']"),"Newer");
});

test("Review saved work closes recovery before revealing the saved images", async () => {
  const h = harness();
  h.run(
    "modal='network-unavailable';closeModal=realCloseModal;lastFocus=null;" +
      "document.querySelectorAll=()=>[];operation={run_id:'old',notice:'Outcome unknown'}",
  );
  h.get("#app").inert = true;
  h.get("#overlay").innerHTML = "Recovery dialog";
  await h.handlers.click({
    target: { closest: () => ({ dataset: { action: "partialReview" } }) },
    preventDefault() {},
  });
  assert.equal(h.run("modal"), "");
  assert.equal(h.run("page"), "review");
  assert.equal(h.run("reviewTab"), "overview");
  assert.equal(h.get("#overlay").innerHTML, "");
  assert.equal(h.get("#app").inert, false);
  assert.equal(h.run("operation.notice"), "Outcome unknown");
  assert.equal(h.pending.length, 0);
  assert.deepEqual(h.notices, []);
});

test("fresh run clears prior operation, while same-run data retains its progress and error", () => {
  const h = harness();
  h.run(
    "operation={run_id:'old',active:false,notice_code:'source_unavailable',notice:'Replace source'}",
  );
  h.use(campaign("old"));
  assert.equal(h.run("operation.notice_code"), "source_unavailable");
  h.use(campaign("new"));
  assert.equal(h.run("operation"), null);
  h.run("operation={run_id:'new',active:true,completed:3,notice:'New issue'}");
  h.use(campaign("new"));
  assert.equal(h.run("operation.completed"), 3);
  assert.equal(h.run("operation.notice"), "New issue");
  h.use(campaign("other", "fc3b38b3-91c9-55c5-bfdf-55d14de08106"));
  assert.equal(h.run("operation"), null);
});

test("Stop confirmation targets the displayed run and rejects a later run", async () => {
  const h = harness();
  h.run("stopTarget={campaign:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',run:'old'}");
  const stopping = h.run("confirmStop()");
  assert.equal(h.pending[0].url, "/api/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4/stop");
  assert.equal(h.pending[0].body.run_id, "old");
  h.pending.shift().resolve({});
  await stopping;
  assert.match(h.notices[0], /in-flight request/);
  h.use(campaign("new"));
  await assert.rejects(h.run("confirmStop()"), /active run changed/);
  assert.equal(h.pending.length, 0);
});

test("late prior-run status or failure cannot overwrite a new-run issue", async () => {
  for (const fails of [false, true]) {
    const h = harness();
    const polling = h.run("poll()");
    h.use(campaign("new"));
    h.run("operation={run_id:'new',notice:'Current run issue'}");
    const request = h.pending.shift();
    if (fails) request.reject(new Error("Old transport failure"));
    else
      request.resolve({
        run_id: "old",
        active: false,
        notice: "Old source unavailable",
      });
    await polling;
    assert.equal(h.run("operation.notice"), "Current run issue");
    assert.equal(h.pending.length, 0);
    assert.equal(h.notices.length, 0);
  }
});

test("late campaign refresh from completed polling cannot roll back a newer plan", async () => {
  const h = harness();
  const polling = h.run("poll()");
  h.pending.shift().resolve({ run_id: "old", active: false });
  await flush();
  assert.match(h.pending[0].url, /motif\/campaigns\/292e9e84-eb00-5fdb-87e8-423f5e51fac4$/);
  h.use(campaign("new"));
  h.run("operation={run_id:'new',notice:'New issue'}");
  h.pending.shift().resolve(campaign("old"));
  await polling;
  assert.equal(h.run("model.run.id"), "new");
  assert.equal(h.run("operation.notice"), "New issue");
});

test("scheduled polls retain their run and stop without a request after a new plan", async () => {
  const h = harness();
  const polling = h.run("poll()");
  h.pending.shift().resolve({ run_id: "old", active: true, completed: 4 });
  await polling;
  assert.equal(h.run("operation.completed"), 4);
  assert.equal(h.timers.length, 1);
  h.use(campaign("new"));
  await h.timers.shift()();
  assert.equal(h.pending.length, 0);
  assert.equal(h.run("operation"), null);
});

test("a terminal source read cannot replace a newer same-run save projection", async () => {
  const h = harness();
  h.run("model.version='before';model.composition_version='before'");
  const polling = h.run("poll()");
  h.pending.shift().resolve({run_id: "old", active: false});
  await flush();
  const read = h.pending.shift();
  h.run("model.version='new-save';model.composition_version='new-save';model.manifest={complete:true}");
  read.resolve({...campaign("old"), version: "older-read", composition_version: "older-read"});
  await polling;
  assert.equal(h.run("model.version"), "new-save");
  assert.equal(h.run("model.composition_version"), "new-save");
  assert.equal(h.run("model.manifest.complete"), true);
  assert.equal(h.timers.length, 1, "read-only polling is retried without another save or generation request");
  const retry = h.timers.shift()();
  await flush();
  assert.match(h.pending[0].url, /\/status$/);
  assert.equal(h.pending[0].body, undefined);
  h.pending.shift().resolve({run_id: "different", active: false});
  await retry;
});

test("same-run completion preserves and shows its own error, not a fabricated success", async () => {
  const h = harness();
  const polling = h.run("poll()");
  h.pending.shift().resolve({
    run_id: "old",
    active: false,
    notice: "Actual current failure",
  });
  await flush();
  h.pending.shift().resolve(campaign("old"));
  await polling;
  assert.equal(h.run("operation.notice"), "Actual current failure");
  assert.deepEqual(h.notices, ["Actual current failure"]);
});

test("terminal polling keeps the running presentation until its matching result is available", async () => {
  for (const outcome of ['complete', 'failed-read', 'switched-campaign']) {
    const h = harness(), shown = [];
    h.context.refreshProductionStatus = () => shown.push(h.run("({active:operation?.active,state:model.run.state})"));
    h.run("model.run.state='planned';operation={run_id:'old',active:true,completed:29}");
    const polling = h.run('poll()');
    h.pending.shift().resolve({run_id:'old',active:false,completed:30});
    await flush();
    assert.equal(h.run('operation.active'), true, outcome);
    assert.equal(shown.length, 0, 'no inactive status beside the old planned model');
    if (outcome === 'switched-campaign') {
      h.use(campaign('foreign', '62220994-1d32-5713-80d2-ac0b6ff0baf3'));
      h.run("operation={run_id:'foreign',active:true}");
    }
    if (outcome === 'failed-read') h.pending.shift().reject(new Error('Current result could not be loaded.'));
    else { const result=campaign('old'); result.run.state='complete'; h.pending.shift().resolve(result); }
    await polling;
    assert.equal(h.pending.length, 0, 'neither Plan nor Start is dispatched');
    if (outcome === 'complete') {
      assert.equal(shown.length, 1);
      assert.equal(shown[0].active, false);
      assert.equal(shown[0].state, 'complete');
    } else if (outcome === 'failed-read') {
      assert.equal(h.run('operation.active'), true);
      assert.deepEqual(h.notices, ['Current result could not be loaded.']);
      assert.equal(h.timers.length, 1, 'existing status poll remains attached to the run');
      const retrying=h.timers.shift()();
      assert.match(h.pending[0].url, /\/status$/);
      h.pending.shift().resolve({run_id:'old',active:false,completed:30});await flush();
      const result=campaign('old');result.run.state='complete';h.pending.shift().resolve(result);await retrying;
      assert.equal(h.run('model.run.state'), 'complete');
      assert.equal(shown.at(-1).active, false);
    } else {
      assert.equal(h.run('operation.run_id'), 'foreign');
      assert.equal(shown.length, 0);
      assert.equal(h.timers.length, 0);
    }
  }
});

test("async capacity refusal and reopening use the measured storage dialog", async () => {
  for (const kind of ["workspace_budget", "disk_space"]) {
    const h = harness();
    h.run("openModal=name=>{modal=name};recoveryTarget='old'");
    const capacity = {
      kind,
      used_bytes: 123456,
      budget_bytes: 200000,
      projected_bytes: 100000,
      free_bytes: 400000,
      reserve_bytes: 500000,
    };
    const status = {
      run_id: "old",
      active: false,
      notice_code: "storage_capacity",
      notice: "Saved work is preserved.",
      capacity,
    };
    const polling = h.run("poll()");
    h.pending.shift().resolve(status);
    await flush();
    const saved = campaign("old");
    saved.run_attempts = [{ state: "unknown" }];
    saved.image_cleanup = { pending: [{ kind: "retirement" }] };
    h.pending.shift().resolve(saved);
    await polling;
    assert.equal(h.run("modal"), "storage");
    assert.equal(h.run("recoveryTarget"), null);
    assert.equal(
      h.run("storageView.state"),
      kind === "workspace_budget" ? "storage-budget" : "storage-disk",
    );
    assert.deepEqual(JSON.parse(h.run("JSON.stringify(storageView.data)")), {
      operationLabel: "Completing this image operation",
      usedBytes: capacity.used_bytes,
      budgetBytes: capacity.budget_bytes,
      projectedBytes: capacity.projected_bytes,
      freeBytes: capacity.free_bytes,
      reserveBytes: capacity.reserve_bytes,
      requiredAdditionalFreeBytes: 200000,
    });
    h.run("modal='';storageView=null");
    const opening = h.run("openCampaign('292e9e84-eb00-5fdb-87e8-423f5e51fac4','production')");
    h.pending.shift().resolve(campaign("old"));
    await flush();
    h.pending.shift().resolve(status);
    await opening;
    assert.equal(h.run("modal"), "storage");
    assert.equal(h.run("storageView.data.usedBytes"), capacity.used_bytes);
    assert.equal(h.pending.length, 0);
  }
});

test("failed, stopped, and completed runs retain Production without forced navigation", async () => {
  for (const state of ["partial", "stopped", "complete"]) {
    const h = harness();
    const polling = h.run("poll()");
    h.pending.shift().resolve({
      run_id: "old",
      active: false,
      ...(state === "partial" ? { notice: "A saved failure" } : {}),
    });
    await flush();
    const value = campaign("old");
    value.run.state = state;
    value.manifest = { complete: state === "complete" };
    h.pending.shift().resolve(value);
    await polling;
    assert.equal(h.run("page"), "production");
  }
});

test("late opening status cannot move a different campaign or overwrite its operation", async () => {
  const h = harness();
  const opening = h.run("openCampaign('292e9e84-eb00-5fdb-87e8-423f5e51fac4','production')");
  h.pending.shift().resolve(campaign("old"));
  await flush();
  h.use(campaign("other-run", "fc3b38b3-91c9-55c5-bfdf-55d14de08106"));
  h.run(
    "page='campaign';operation={run_id:'other-run',notice:'Other campaign issue'}",
  );
  h.pending
    .shift()
    .resolve({ run_id: "old", active: false, notice: "Old failure" });
  await opening;
  assert.equal(h.run("draft.campaign_id"), "fc3b38b3-91c9-55c5-bfdf-55d14de08106");
  assert.equal(h.run("page"), "campaign");
  assert.equal(h.run("operation.notice"), "Other campaign issue");
});

test("successful fresh campaign navigation dismisses obsolete toast without clearing same-run errors", async () => {
  const h = harness();
  const opening = h.run("openCampaign('292e9e84-eb00-5fdb-87e8-423f5e51fac4','campaign')");
  h.pending.shift().resolve(campaign("old"));
  await flush();
  h.pending
    .shift()
    .resolve({ run_id: "old", active: false, notice: "Saved source issue" });
  await opening;
  assert.equal(h.get("#toast").classList.removed, true);
  assert.equal(h.run("operation.notice"), "Saved source issue");
});

test("expanded attempt estimates include only positive known-portion helpers", () => {
  const h = harness();
  h.context.dialog = (title, body) => body;
  h.context.dataRow = () => "";
  h.context.btn = () => "";
  h.run("C={id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',name:'Campaign',products:[]};model.run_attempts=[]");
  for (const [coverage,count,subtotal,missing,reason] of [
    ['complete',2,0,2,'request'],['complete',1,0,1,'missing_usage'],
    ['complete',2,.1,1,'missing_usage'],['complete',2,.001,1,'missing_usage'],
    ['incomplete',2,0,1,'request'],['incomplete',2,.11,1,'request'],['unknown',null,0,0,'request'],
  ]) {
    h.run(`model.campaign_metrics=${JSON.stringify({campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',coverage,attempted_requests:count,
      usage_cost_estimate:{estimated_usd:null,known_usage_subtotal_usd:subtotal,unreported_attempts:missing,incomplete_reasons:[{code:reason,count:missing}]}})}`);
    const shown=h.run('campaignMetricDisplay()');
    assert.equal(shown.has_known_portion,subtotal>0);
    const paragraph=h.run('attemptsModal()').match(/<strong>Campaign estimate:<\/strong> (.*?)<\/p>/)[1];
    const expected=(subtotal>0?shown.spend_helper+' ':'')+shown.detail;
    assert.equal(paragraph,h.run(`esc(${JSON.stringify(expected)})`));
    if(subtotal===0)assert.ok(paragraph.split(shown.spend_helper).length<=2);
  }
});

test("format-switch choice follows live preview readiness, rechecks clicks, and releases its owner", async () => {
  for (const ending of ["apply", "discard", "keep", "escape", "stale", "removed"]) {
    const h = harness(), observations = [], focused = [];
    let observer, resolveChoice;
    const previous = { inert: false, isConnected: true };
    const buttons = Object.fromEntries(["keep", "discard", "apply"].map(choice => [choice, {
      dataset: { editorChoice: choice }, disabled: choice === "apply",
      focus() { h.context.document.activeElement = this; focused.push(choice); },
    }]));
    const host = { isConnected: false, listeners: {},
      querySelector: selector => buttons[selector.includes('"apply"') ? "apply" : "keep"],
      addEventListener(name, fn) { this.listeners[name] = fn; },
      remove() { this.isConnected = false; },
    };
    const root = h.get("#overlay");
    root.children = [previous]; root.append = node => { node.isConnected = true; };
    h.context.document.createElement = () => host;
    h.context.document.activeElement = { isConnected: true, focus() { focused.push("original"); } };
    h.context.MutationObserver = class {
      constructor(fn) { observer = this; this.notify = fn; }
      observe(target, options) { observations.push({target, options}); }
      disconnect() { this.disconnected = true; }
    };
    h.context.MotifEditorControls = { switchPrompt: () => ({ title: "Change format", body: "Keep work", cancel: "Keep editing", discard: "Discard and switch", primary: "Apply and switch" }) };
    h.context.dialog = (_title, _body, footer) => footer;
    h.run("ready=false;owned=true");
    const choice = h.run("chooseEditorFormat({product:'Bag',format:'1:1',canApply:()=>ready,isCurrent:()=>owned})");
    choice.then(value => { resolveChoice = value; });
    assert.equal(previous.inert, true); assert.equal(buttons.apply.disabled, true);
    assert.equal(observations.length, 1); assert.equal(focused.at(-1), "keep");
    h.run("ready=true"); observer.notify();
    assert.equal(buttons.apply.disabled, false); assert.equal(focused.at(-1), "keep", "readiness must not steal focus");
    h.run("ready=false");
    host.listeners.click({stopPropagation() {}, target: { closest: () => buttons.apply }});
    assert.equal(buttons.apply.disabled, true); assert.equal(resolveChoice, undefined, "a newly blocked/failed preview must not apply");
    h.run("ready=true"); observer.notify();
    if (ending === "stale") { h.run("owned=false"); observer.notify(); }
    else if (ending === "removed") { host.isConnected = false; observer.notify(); }
    else if (ending === "escape") host.listeners.keydown({key:"Escape", stopPropagation() {}, preventDefault() {}});
    else host.listeners.click({stopPropagation() {}, target: { closest: () => buttons[ending] }});
    assert.equal(await choice, ["apply", "discard"].includes(ending) ? ending : "keep");
    assert.equal(previous.inert, false); assert.equal(observer.disconnected, true);
    assert.equal(host.isConnected, false);
    if (ending === "stale") assert.notEqual(focused.at(-1), "original");
  }
});

test('a product upload response cannot attach or toast into a different campaign or a newer product dialog', async () => {
  for(const replacement of ['campaign','product']) {
    const h=harness();
    h.run("modalProduct={source:{path:'original.png'}}");
    const target=h.run('modalProduct'), owner=h.run('draft');
    const uploading=h.run("upload({type:'image/png',name:'new.png'})");
    if(replacement==='campaign')h.run("draft={campaign_id:'62220994-1d32-5713-80d2-ac0b6ff0baf3'}");
    else h.run("modalProduct={source:{path:'other.png'}}");
    h.pending[0].resolve({reference:'Campaigns/Test-12345678/Assets/Products/new.png',url:'/new.png',width:20,height:20});
    await uploading;
    assert.equal(target.source.path,'original.png');
    if(replacement==='campaign')assert.equal(h.notices.length,0);
    else assert.equal(h.run('modalProduct.source.path'),'other.png');
    assert.equal(owner.campaign_id,'292e9e84-eb00-5fdb-87e8-423f5e51fac4');
  }
});

test("a JSON import opens the new campaign without prompting for its named images", async () => {
  const h = harness(), calls = [], source = {size: 123, text: async () => '{"name":"Imported"}'};
  h.get("#json-file").files = [source];
  h.context.calls = calls;
  h.run(`
    page = "library"; modal = "import";
    draft = {products: [{id:"p", source:{role:"finished_scene",path:""}}]};
    model = {attachment_hints:{p:"photo.png"}};
    api = async (path,value,options) => {calls.push([path,value.name,options.body]); return {campaign_id:"fresh-id"}};
    refreshCatalog = async () => calls.push(["refresh"]);
    openCampaign = async (id,target) => calls.push(["open",id,target]);
    openModal = name => calls.push(["modal",name]);
  `);
  await h.run("dispatchAction({action:'doImport'})");
  assert.deepEqual(calls.map(row => Array.from(row)), [
    ["/api/motif/import","Imported",source], ["open","fresh-id","campaign"], ["refresh"],
  ]);
});

function productUploadHarness() {
  const h=harness();
  h.run("modal='product';modalProduct={id:'a',name:'Lantern',description:'Brass lantern',treatment:'reuse',source:{role:'finished_scene',path:'old.png',provenance:{}}};draft.products=[structuredClone(modalProduct)];projectBuffers={form_drafts:{}};openModal=()=>{};markDirty=()=>{}");
  h.context.api=(url,body,options)=>new Promise((resolve,reject)=>h.pending.push({url,body,options,resolve,reject}));
  return h;
}
test('resume confirmation cannot dispatch a changed request estimate',async()=>{
  for (const changed of [false,true]) {
    const h=harness();
    h.context.changeEstimate=changed;
    h.run(`
      model.retry_plan={digest:'same-plan',generation_products:['a'],cost_estimate:{estimated_usd:0.1}};
      globalThis.started=0;
      reviewRetryPlan=async()=>{if(changeEstimate)model.retry_plan.cost_estimate.estimated_usd=0.2;};
      startDisplayedPlan=async()=>{started++;};
      openModal=name=>{modal=name;};
    `);
    await h.run("dispatchAction({action:'confirmResume'})");
    assert.equal(h.run('started'),changed?0:1);
    if(changed) {
      assert.equal(h.run('modal'),'retry-plan');
      assert.match(h.notices.at(-1),/estimate changed/);
    }
  }
});
test('a transparent scene upload preserves the old photo and reports a typed warning for the dialog',async()=>{
  const h=productUploadHarness(),task=h.run("upload({name:'transparent.png',type:'image/png'})");
  h.pending[0].resolve({reference:'transparent.png',url:'/uploads/transparent.png',opaque:false});
  await assert.rejects(task,error=>error.input_image?.kind==='transparent_scene' && /transparent areas/.test(error.message));
  assert.equal(h.run('modalProduct.source.path'),'old.png');
  const replacement=h.run("upload({name:'photo.jpg',type:'image/jpeg'})");
  h.pending[1].resolve({reference:'photo.jpg',url:'/uploads/photo.jpg',opaque:true});
  await replacement;
  assert.equal(h.run('modalProduct.error'),'');
  assert.equal(h.run('modalProduct.source.path'),'photo.jpg');
});
test('product Update cannot commit the old photo while a chosen replacement is pending',async()=>{
  const h=productUploadHarness(),task=h.run("upload({name:'new.png',type:'image/png'})");
  assert.equal(h.run('modalProduct.uploading'),true);
  h.run('saveProductDraft()');
  assert.equal(h.run('draft.products[0].source.path'),'old.png');
  h.pending[0].resolve({reference:'new.png',url:'/uploads/new.png'});
  await task;
  assert.equal(h.run('modalProduct.uploading'),false);
  assert.equal(h.run('modalProduct.source.path'),'new.png');
  h.get('#product-name').value='Lantern';
  h.get('#product-description').value='Brass lantern';
  h.get('#product-origin').value='Uploaded';
  h.get('#product-rights').value='Owned';
  h.run('saveProductDraft()');
  assert.equal(h.run('draft.products[0].source.path'),'new.png');
});
test('canceling a pending product upload preserves the earlier photo after a late response',async()=>{
  const h=productUploadHarness(),task=h.run("upload({name:'new.png',type:'image/png'})");
  h.run('cancelProductUpload()');
  assert.equal(h.pending[0].options.signal.aborted,true);
  h.pending[0].resolve({reference:'new.png',url:'/uploads/new.png'});
  await task;
  assert.equal(h.run('modalProduct.uploading'),false);
  assert.equal(h.run('modalProduct.source.path'),'old.png');
  assert.equal(h.run('draft.products[0].source.path'),'old.png');
});
test('failed and timed out photo uploads settle so the editor can retry or cancel',async()=>{
  for (const timeout of [false,true]) {
    const h=productUploadHarness(),task=h.run("upload({name:'new.png',type:'image/png'})");
    if (timeout) h.timers[h.run('pendingProductUpload.timer')-1]();
    h.pending[0].reject(new Error('Image refused'));
    if (timeout) await task;
    else await assert.rejects(task,/Image refused/);
    assert.equal(h.run('modalProduct.uploading'),false);
    assert.equal(h.run('pendingProductUpload'),null);
    assert.equal(h.run('modalProduct.source.path'),'old.png');
    if(timeout) assert.match(h.run('modalProduct.error'),/took too long/);
  }
});
test('pending Brand uploads guard the top-level navigation and the campaign chooser',async()=>{
  const h=harness();h.context.briefLogoUploadPending=()=>true;h.context.scrollTo=()=>{};
  h.run("page='campaign';dirty=false");
  await h.run("navigate('production')");
  assert.equal(h.run('page'),'campaign');
  assert.match(h.notices[0],/logo upload/);
  h.run("showCampaignChooser=()=>{throw Error('Must retain the active logo upload')}");
  await h.run("dispatchAction({action:'campaigns'})");
  assert.equal(h.run('page'),'campaign');
  h.context.briefLogoUploadPending=()=>false;
  await h.run("navigate('production')");
  assert.equal(h.run('page'),'production');
});
test('a newer product photo choice wins when the earlier upload responds last',async()=>{
  const h=productUploadHarness();
  const first=h.run("upload({name:'first.png',type:'image/png'})");
  const second=h.run("upload({name:'second.png',type:'image/png'})");
  assert.equal(h.pending[0].options.signal.aborted,true);
  h.pending[1].resolve({reference:'second.png',url:'/uploads/second.png'});
  await second;
  h.pending[0].resolve({reference:'first.png',url:'/uploads/first.png'});
  await first;
  assert.equal(h.run('modalProduct.source.path'),'second.png');
  assert.equal(h.run('modalProduct.uploading'),false);
});

test("a committed import survives failed catalog refresh without invalid-brief feedback or duplication", async () => {
  const h = harness(), source = {size: 123, text: async () => '{"name":"Imported"}'};
  h.get("#json-file").files = [source];
  h.context.calls = [];
  h.run(`
    page="library";modal="import";draft=null;
    api=async path=>{calls.push(["post",path]);return {campaign_id:"saved-import"}};
    openCampaign=async id=>calls.push(["open",id]);
    refreshCatalog=async()=>{throw Error("Unreadable neighboring manifest")};
    openModal=name=>calls.push(["modal",name]);
  `);
  await h.run("dispatchAction({action:'doImport'})");
  assert.deepEqual(h.context.calls.map(row => Array.from(row)), [["post","/api/motif/import"],["open","saved-import"]]);
  assert.match(h.notices[0], /brief was imported/);
  await h.run("dispatchAction({action:'doImport'})");
  assert.equal(h.context.calls.filter(c=>c[0]==="post").length,1);
  assert.equal(h.run("importFile"),null);
});

test("a busy import preserves its chosen file and offers retry without blaming its contents", async () => {
  const h = harness(), source = {size: 123, text: async () => '{"name":"Imported"}'};
  h.get("#json-file").files = [source];
  h.run(`
    page="library";modal="import";draft=null;
    api=async()=>{throw Object.assign(Error("Another campaign operation is already active."), {code:"busy",status:409})};
    openModal=name=>modal=name;
  `);
  await h.run("dispatchAction({action:'doImport'})");
  assert.equal(h.run("modal"), "import-busy");
  assert.equal(h.run("importFile"), source);
  assert.equal(h.run("importedCampaignId"), null);
  Object.assign(h.context, {
    dialog: (title, body, buttons) => title + body + buttons,
    notice: message => message,
    btn: (label, action) => label + action,
  });
  const markup = h.run("modalView()");
  assert.match(markup, /Another operation is finishing/);
  assert.doesNotMatch(markup, /repair|Correct the named fields/);
  await h.run("dispatchAction({action:'returnImport'})");
  assert.equal(h.run("modal"), "import");
  assert.equal(h.run("importFile"), source);
});

test("failed opening after committed import retries the saved identity, never the JSON POST", async () => {
  const h=harness(), source={size:123,text:async()=>'{"name":"Imported"}'};
  h.get("#json-file").files=[source];h.context.calls=[];
  h.run(`
    page="library";modal="import";draft=null;
    api=async path=>{calls.push(["post",path]);return {campaign_id:"saved-import"}};
    openCampaign=async id=>{calls.push(["open",id]);throw Error("Temporary page error")};
    refreshCatalog=async()=>calls.push(["refresh"]);
    openModal=name=>calls.push(["modal",name]);
  `);
  await h.run("dispatchAction({action:'doImport'})");
  assert.equal(h.context.calls.at(-1)[1],"import-open-failed");
  h.run("openCampaign=async id=>calls.push(['open',id])");
  await h.run("dispatchAction({action:'openImported'})");
  assert.equal(h.context.calls.filter(c=>c[0]==="post").length,1);
  assert.equal(h.context.calls.filter(c=>c[0]==="open").length,2);
  assert.equal(h.context.calls.at(-1)[0],"refresh");
});

test('Individual PNGs focuses the rendered download destination without queued trigger restoration', async () => {
  for (const available of [true, false]) {
    const h = harness(), microtasks = [], events = [];
    let scrollY = 0, rendered = false;
    const trigger = {isConnected: true, dataset: {action: 'exportZip'}, focus() {
      events.push('trigger focus'); h.context.document.activeElement = this; scrollY = 0;
    }};
    const destination = {focus(options) {
      assert.equal(rendered, true);
      assert.equal(h.get('#app').inert, false);
      assert.equal(h.run('modal'), '');
      assert.equal(options.preventScroll, true);
      events.push('download focus'); h.context.document.activeElement = this;
    }};
    const overview = h.get('.overview-grid');
    overview.querySelector = () => available ? destination : null;
    overview.focus = options => {
      assert.equal(overview.tabIndex, -1);
      assert.equal(rendered, true);
      assert.equal(options.preventScroll, true);
      events.push('grid focus'); h.context.document.activeElement = overview;
    };
    overview.scrollIntoView = options => {
      assert.equal(options.block, 'start'); events.push('grid scroll'); scrollY = 400;
    };
    h.context.document.querySelectorAll = () => [trigger];
    h.context.queueMicrotask = fn => microtasks.push(fn);
    h.context.trigger = trigger;
    h.context.render = () => {rendered = true; trigger.isConnected = false;};
    h.run("draft=null;page='handoff';modal='export-failure';lastFocus=trigger;closeModal=realCloseModal");
    h.get('#app').inert = true;
    h.get('#overlay').innerHTML = 'Export failure';
    await h.run("dispatchAction({action:'exportOverview'})");
    for (const task of microtasks.splice(0)) task();
    assert.equal(h.context.document.activeElement, available ? destination : overview);
    assert.equal(scrollY, 400);
    assert.deepEqual(events, [available ? 'download focus' : 'grid focus', 'grid scroll']);
    assert.equal(h.get('#overlay').innerHTML, '');
    assert.equal(h.run('page'), 'handoff');
    assert.deepEqual(h.notices, []);
    assert.equal(h.pending.length, 0);
  }
});

test('ordinary modal close and cancel still restore focus before and after the caller rerenders', () => {
  for (const cancel of [false, true]) {
    const h = harness(), microtasks = [], focused = [];
    const trigger = {isConnected: true, dataset: {action: 'exportZip'}, focus() {
      focused.push('original'); h.context.document.activeElement = this;
    }};
    const replacement = {dataset: {action: 'exportZip'}, focus() {
      focused.push('replacement'); h.context.document.activeElement = this;
    }};
    h.context.document.querySelectorAll = () => [replacement];
    h.context.queueMicrotask = fn => microtasks.push(fn);
    h.context.trigger = trigger;
    h.run("draft=null;modal='export-failure';lastFocus=trigger;closeModal=realCloseModal");
    h.run(`closeModal({cancel:${cancel}})`);
    assert.deepEqual(focused, ['original']);
    assert.equal(microtasks.length, 1);
    trigger.isConnected = false;
    microtasks.shift()();
    assert.deepEqual(focused, ['original', 'replacement']);
    assert.equal(h.context.document.activeElement, replacement);
  }
});

test('Handoff layout repair flushes pending work and opens the exact product, market, format, and tool', async () => {
  const h=harness();h.context.repairs=[];
  h.run("draft=null;page='handoff';openStudioWorkspace=async mode=>repairs.push({mode,product:currentProduct,format:currentFormat,language:lang,page});updateURL=()=>{}");
  await h.run("dispatchAction({action:'repairLayout:1:2:es:text'})");
  assert.deepEqual(JSON.parse(JSON.stringify(h.context.repairs)),[{mode:'text',product:1,format:2,language:'es',page:'review'}]);
  h.run("page='handoff';flushCurrentWork=async()=>false");
  await h.run("dispatchAction({action:'repairLayout:0:0:en:framing'})");
  assert.equal(h.context.repairs.length,1);assert.equal(h.run('page'),'handoff');
});


test("opening an incomplete Workbench campaign selects Production instead of a saved Studio tab", async () => {
  const h = harness();
  const value = campaign("old");
  value.value.products = [{id: "one", source: {role: "concept"}}];
  value.editable_products = {};
  value.project = {authored: {ui_context: {page: "review", tab: "products"}}};
  const opening = h.run("openCampaign('292e9e84-eb00-5fdb-87e8-423f5e51fac4')");
  h.pending.shift().resolve(value);
  await flush();
  h.pending.shift().resolve({run_id:"old", active:false});
  await opening;
  assert.equal(h.run("page"), "production");
});

function newProductPhotoHarness() {
  const h = productUploadHarness(), revoked = [];
  h.run("draft.products=[];modalProduct.source.path='';modalProduct.origin='Own photo';modalProduct.rights='Owned'");
  h.context.document.querySelectorAll = () => [];
  h.context.URL = {createObjectURL: file => 'blob:' + file.name, revokeObjectURL: url => revoked.push(url)};
  h.context.FileReader = class {readAsDataURL(file) {this.result = 'data:image/png;base64,cGhvdG8='; this.onload();}};
  for (const [field, value] of Object.entries({name:'Lantern',description:'Brass lantern',origin:'Own photo',rights:'Owned'}))
    h.get('#product-'+field).value=value;
  h.revoked = revoked;
  return h;
}

test('new product selections validate without saving, and Cancel discards the local file', async()=>{
  for (const cancelBeforeResponse of [false,true]) {
    const h = newProductPhotoHarness(), target = h.run('modalProduct');
    const task = h.run("upload({name:'new.png',type:'image/png'})");
    assert.equal(h.pending[0].url,'/api/uploads/validate');
    if (cancelBeforeResponse) h.run('realCloseModal({restoreFocus:false})');
    h.pending[0].resolve({width:20,height:20,opaque:true});
    await task;
    if (!cancelBeforeResponse) {
      assert.equal(target.localFile.name,'new.png');
      assert.equal(target.localImage,'blob:new.png');
      assert.equal(target.source.path,'');
      h.run('realCloseModal({restoreFocus:false})');
      assert.deepEqual(h.revoked,['blob:new.png']);
    }
    assert.equal(target.localFile,undefined);
    assert.equal(h.run('draft.products.length'),0);
    assert.equal(h.pending.length,1,'Cancel does not need a persistence or cleanup request');
  }
});

test('choosing another new product photo releases the previous browser preview',async()=>{
  const h=newProductPhotoHarness();
  for (const name of ['first.png','second.png']) {
    const task=h.run(`upload({name:'${name}',type:'image/png'})`);
    h.pending.at(-1).resolve({opaque:true}); await task;
  }
  assert.equal(h.run('modalProduct.localFile.name'),'second.png');
  assert.deepEqual(h.revoked,['blob:first.png']);
  assert.equal(h.run('draft.products.length'),0);
  assert.ok(h.pending.every(request=>request.url==='/api/uploads/validate'));
});

test('Add sends file and product once; failure retains both and success adopts the saved reference',async()=>{
  for(const fail of [false,true]) {
    const h=newProductPhotoHarness(), target=h.run('modalProduct');
    const selecting=h.run("upload({name:'new.png',type:'image/png'})");
    h.pending[0].resolve({opaque:true});await selecting;
    h.context.withProjectApply = action => action();
    h.context.restoreProject = value => h.run(`draft=${JSON.stringify(value.brief)}`);
    h.context.acknowledgements=[];
    h.run("model.project={base:{run_id:null,revision_id:null,metadata_snapshot_id:null}};projectQueue={version:()=> 'current',applied: value=>acknowledgements.push(value)};closeModal=realCloseModal");
    const adding=h.run('saveProductDraft()');await flush();
    assert.equal(h.pending.length,2);
    const request=h.pending[1];
    assert.match(request.url,/\/products\/photo$/);
    assert.equal(request.body.version,'current');
    assert.equal(request.body.image_base64,'cGhvdG8=');
    assert.equal(request.body.product.source.path,'');
    assert.equal(h.run('draft.products.length'),0);
    assert.equal(target.adding,true);
    if(fail) request.reject(new Error('The campaign changed elsewhere. Refresh the page.'));
    else {
      const authored={brief:JSON.parse(h.run('JSON.stringify(draft)'))};
      authored.brief.products.push({...request.body.product,source:{...request.body.product.source,path:'Campaigns/Test-12345678/Assets/Products/Lantern-abc.png'}});
      request.resolve({project:{version:'saved',authored,base:request.body.base},campaign:{project:{authored}}});
    }
    await adding;
    assert.equal(target.adding,false);
    if(fail) {
      assert.equal(target.localFile.name,'new.png');assert.equal(target.name,'Lantern');
      assert.match(target.error,/changed elsewhere/);assert.equal(h.run('draft.products.length'),0);
    } else {
      assert.equal(target.error,'');
      assert.equal(h.run('draft.products.length'),1);
      assert.match(h.run('draft.products[0].source.path'),/Lantern-abc\.png$/);
      assert.equal(h.context.acknowledgements.length,1);
      assert.deepEqual(h.revoked,['blob:new.png']);
    }
  }
});
