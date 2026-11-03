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
    localDialog: null, studioWorkspace: null,
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
      assert.equal(h.run("modal"), "unsaved");
      await h.click("saveLeave");
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
    await h.run("navigate('library')"); await h.click("saveLeave");
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
    assert.equal(h.run("modal"), "unsaved");
    assert.equal(h.pending.length, 2); // No confirmation or cleanup request.
  }
});

test("Workbench deletion previews and confirms the clicked card even when another campaign is open", async () => {
  const h = navigationHarness();
  const target = "62220994-1d32-5713-80d2-ac0b6ff0baf3";
  const active = h.run("draft.campaign_id");
  h.run(`page='library'; saved[0].name='Second campaign'; campaignDistinction=()=>''; finishDeletion=async()=>{};`);
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
  h.run("draft.brand={logo_path:'Campaigns/Test-12345678/Assets/Products/original.png'};modalProduct={source:{path:'old-product.png'}}");
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

test("page exits guard unacknowledged work while saved pending wording stays navigable", async () => {
  const h=navigationHarness();
  for(const target of ["library","campaign","production","handoff"]) {
    h.run("dirty=true;page='review'"); await h.run(`navigate('${target}')`);
    assert.equal(h.run("modal"),"unsaved"); assert.equal(h.run("page"),"review");
    await h.click("close");
    assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']"),"A");
  }
  for(const action of ["openCampaign","new","import"]) {
    await h.click(action,"62220994-1d32-5713-80d2-ac0b6ff0baf3"); assert.equal(h.run("modal"),"unsaved"); assert.equal(h.pending.length,0);
  }
  await h.click("close"); await h.click("format:1:2");
  assert.equal(h.run("formats[1]"),2);
  h.run("dirty=false"); await h.run("navigate('library')");
  assert.equal(h.run("page"),"library"); assert.equal(h.run("modal"),"");
  assert.equal(h.run("Object.keys(projectBuffers.form_drafts.fields).length"),1);
});

test("replacement navigation selects Products only after the common unsaved guard resolves", async () => {
  const h = navigationHarness();
  h.run("page='production';tab='copy'");
  await h.click("nav:products");
  assert.equal(h.run("modal"), "unsaved");
  assert.equal(h.run("page"), "production");
  assert.equal(h.run("tab"), "copy");
  await h.click("close");
  assert.equal(h.run("draft.audience"), "Typed audience");
  await h.click("nav:products");
  await h.click("saveLeave");
  assert.equal(h.run("page"), "production");
  assert.equal(h.run("tab"), "copy");
  assert.equal(h.run("draft.audience"), "Typed audience");
  assert.deepEqual(h.context.steps, ["project"]);
  assert.equal(h.pending.length, 0);
  h.run("page='production';tab='copy'");
  await h.click("nav:products");
  assert.equal(h.run("page"), "production");
  assert.equal(h.run("tab"), "copy");
  assert.equal(h.run("modal"), "");
  assert.equal(h.pending.length, 0);
});

test("Discard and leave restores acknowledged Brief and pending targets without clearing another campaign",async()=>{
  const h=navigationHarness(); await h.run("navigate('library')"); await h.click("discardLeave");
  assert.equal(h.run("page"),"library"); assert.equal(h.run("draft.audience"),"Saved audience");
  assert.deepEqual(JSON.parse(h.run("JSON.stringify(projectBuffers.form_drafts)")), {
    fields: {"brief/copy/en": {"copy-headline": "Saved pending A"}}
  });
  assert.deepEqual(h.context.steps,["project"]); assert.equal(h.pending.length,0);
});

test("Save and leave preserves all pending targets through the common save without applying any",async()=>{
  const h=navigationHarness(); await h.run("navigate('library')"); await h.click("saveLeave");
  assert.deepEqual(h.context.steps,["project"]); assert.equal(h.run("page"),"library");
  assert.equal(h.run("draft.audience"),"Typed audience");
  assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']"),"A");
});

test("invalid unfinished target values remain saveable without a production-readiness check",async()=>{
  const h=navigationHarness();h.run("draft.audience='';projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']='';projectBuffers.form_drafts.fields['brief/copy/es']={'copy-headline':'  '}");
  await h.run("navigate('library')");await h.click("saveLeave");
  assert.deepEqual(h.context.steps,["project"]); assert.equal(h.run("page"),"library");
  assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']"),"");
  assert.equal(h.run("projectBuffers.form_drafts.fields['brief/copy/es']['copy-headline']"),"  ");
});

test("a save that reorders Workbench retains the intended destination campaign identity", async () => {
  const h = navigationHarness();
  h.run(`
    openCampaign=async id=>steps.push('open:'+id);
    refreshCatalog=async()=>{saved=[{id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'},{id:'62220994-1d32-5713-80d2-ac0b6ff0baf3'}]};
    saveProject=async()=>{dirty=false;await refreshCatalog();return true};
  `);
  await h.click("openCampaign", "62220994-1d32-5713-80d2-ac0b6ff0baf3");
  await h.click("saveLeave");
  assert.equal(h.context.steps.at(-1), "open:62220994-1d32-5713-80d2-ac0b6ff0baf3");
});

test("a failed common save retains every pending target and does not navigate",async()=>{
  const h=navigationHarness();h.run("saveProject=async()=>{throw new Error('Controlled save failure')}");
  await h.run("navigate('library')");await h.click("saveLeave");
  assert.equal(h.run("page"),"review");assert.equal(h.run("modal"),"unsaved");
  assert.equal(h.run("navigationError"),"Controlled save failure");assert.equal(h.run("dirty"),true);
  assert.equal(h.run("Object.keys(projectBuffers.form_drafts.fields).length"),1);assert.equal(h.run("pendingNav"),"library");
});

test("Save and leave distinguishes a temporary hold from conflict and preserves newer input until retry",async()=>{
  const h=navigationHarness();let state={busy:true,problem:null};h.context.getSaveState=()=>state;
  h.run("saveProject=async()=>false");await h.run("navigate('library')");await h.click('saveLeave');
  assert.equal(h.run('navigationError'),'Saving will finish in a moment. Try again.');
  assert.equal(h.run('page'),'review');
  state={busy:true,problem:{phase:'conflict'}};await h.click('saveLeave');
  assert.equal(h.run('navigationError'),'Saving is paused. Keep editing, then review the update.');
  h.run("draft.audience='Newer audience';projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']='Newest headline'");
  state={busy:false,problem:null};h.run("saveProject=async()=>{steps.push(structuredClone({draft,projectBuffers}));dirty=false;return true}");
  await h.click('saveLeave');assert.equal(h.run('page'),'library');
  assert.equal(h.context.steps[0].draft.audience,'Newer audience');
  assert.equal(h.context.steps[0].projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline'],'Newest headline');
});

test("Save and leave waits for the common queue's current acknowledgement",async()=>{
  const h=navigationHarness();h.run("saveProject=()=>new Promise(resolve=>{finishProject=resolve})");
  await h.run("navigate('library')");const leaving=h.click("saveLeave");
  assert.equal(h.run("page"),"review");h.run("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']='Newer';finishProject(false)");
  await leaving;assert.equal(h.run("page"),"review");assert.equal(h.run("modal"),"unsaved");
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
    ["/api/motif/import","Imported",source], ["refresh"], ["open","fresh-id","campaign"],
  ]);
});
