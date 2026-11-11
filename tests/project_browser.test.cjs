const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness(validate, canAutosave) {
  const context = vm.createContext({ structuredClone, setTimeout, clearTimeout, modalProduct: { treatment: "concept" } });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/project.js", "utf8"), context);
  let current = { brief: { name: "Original", color: "#12" }, form_drafts: {} };
  const calls = [], statuses = [], acknowledgements = [];
  const saved = { version: "v1", base: { revision_id: "r1" }, authored: structuredClone(current), saved_at: "before" };
  const queue = context.projectSaveQueue({
    capture: () => structuredClone(current),
    validate,
    canAutosave,
    restore: (value) => { current = value; },
    send: async (campaign, value) => {
      calls.push({ campaign, value: structuredClone(value) });
      return api.reply(campaign, value);
    },
    changed: (value) => statuses.push(value),
    acknowledged: (receipt) => acknowledgements.push(structuredClone(receipt)),
    delay: 5,
  });
  const api = {
    queue, calls, statuses, acknowledgements, saved,
    reply: async () => ({ version: "v2", saved_at: "now", authored_hash: "ack" }),
    get current() { return current; },
    set current(value) { current = value; },
  };
  queue.open("08639a3c-6df4-53cd-9cf4-e1ce27b66273", saved);
  return api;
}

test("confirmed deletion retires only its queue owner and ignores a late save acknowledgement", async () => {
  const h = harness();
  h.current.brief.name = "Pending timer";
  h.queue.markChanged();
  assert.equal(h.queue.forget("014c1210-29a9-50d2-823a-7a936aecd62b"), false);
  assert.equal(h.queue.isDirty(), true);
  assert.equal(h.queue.forget("08639a3c-6df4-53cd-9cf4-e1ce27b66273"), true);
  h.current = null;
  assert.equal(h.queue.isDirty(), false);
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 0);

  h.queue.open("08639a3c-6df4-53cd-9cf4-e1ce27b66273", h.saved);
  h.current.brief.name = "Already in flight";
  let release;
  h.reply = () => new Promise(resolve => { release = resolve; });
  const oldSave = h.queue.save();
  h.queue.forget("08639a3c-6df4-53cd-9cf4-e1ce27b66273");
  h.current = null;
  assert.equal(await h.queue.saveCurrent(), false);
  assert.equal(h.queue.isDirty(), false);
  h.queue.open("8f57d6d7-9318-564d-b7a8-1b78b4e978b7", {version: "b1", base: {}, authored: {brief: {name: "Surviving campaign"}}});
  release({version: "deleted-a2", saved_at: "late"});
  assert.equal(await oldSave, false);
  assert.equal(h.queue.isDirty(), false);
  assert.equal(h.current.brief.name, "Surviving campaign");
  h.current.brief.name = "New work";
  h.reply = async () => ({version: "b2"});
  await h.queue.saveCurrent();
  assert.equal(h.calls[1].campaign, "8f57d6d7-9318-564d-b7a8-1b78b4e978b7");
  assert.equal(h.calls[1].value.version, "b1");
  h.queue.stop();
});

test("Save acknowledgement compares producer edits without an absent automatic replay receipt", () => {
  const context = vm.createContext({copy: structuredClone});
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/project.js", "utf8"), context);
  const authored = {brand:{logo_path:"new-logo"}, markets:{en:{copy:"Exact words"}}, products:[{id:"a",description:"Kept"}], replay:null};
  const planned = {...structuredClone(authored), replay:{history:"Verified reuse",sources:{a:{path:"owned-source"}}}};
  assert.equal(context.sameProjectInputs(authored, planned), true);
  for (const change of [a=>a.brand.logo_path="different",a=>a.markets.en.copy="",a=>a.products[0].description="Changed",a=>a.replay={history:"Explicit change"},...[false,0,"",[],{}].map(value=>a=>a.replay=value)]) {
    const altered=structuredClone(authored);change(altered);
    assert.equal(context.sameProjectInputs(altered, planned), false);
  }
  assert.equal(authored.replay, null);
  assert.equal(planned.replay.sources.a.path, "owned-source");
});

test("a reviewed uploaded image stays reviewed after an acknowledgement without optional subject bounds", () => {
  const brief={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',products:[{id:'a',source:{role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/photo.png'}}]};
  const planned=structuredClone(brief); planned.products[0].source.subject_box=null;
  const context=vm.createContext({copy:structuredClone,draft:brief,model:{run:{state:'planned'},project:{},plan_brief:planned,unplanned_draft:false},logoSession:null,dirty:false,refreshCampaignIdentity(){}});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  let options; context.projectSaveQueue=o=>{options=o;return {open(){}};};
  context.initializeProject({});
  options.acknowledged({version:'ack'},brief.campaign_id,{brief});
  assert.equal(context.model.unplanned_draft,false);
  assert.equal(Object.hasOwn(brief.products[0].source,'subject_box'),false,'raw upload is preserved');
  for (const source of [{...brief.products[0].source,subject_box:[0,0,1,1]},{...brief.products[0].source,path:'different'},{...brief.products[0].source,subject_box:''}]) {
    const changed=structuredClone(brief); changed.products[0].source=source;
    assert.equal(context.sameProjectInputs(changed,planned),false);
  }
});

test("a newly added product keeps its first reviewed plan through a context-save acknowledgement", () => {
  const brief={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',products:[{id:'new',source:{role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/new.png'},copy_overrides:{}}]};
  const planned=structuredClone(brief);
  planned.products[0].source.subject_box=null;
  planned.products[0].framing={};
  const context=vm.createContext({copy:structuredClone,draft:brief,model:{run:{state:'planned'},project:{},plan_brief:planned,unplanned_draft:false},logoSession:null,dirty:false,refreshCampaignIdentity(){}});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  let options; context.projectSaveQueue=o=>{options=o;return {open(){}};};
  context.initializeProject({});
  options.acknowledged({version:'context-ack'},brief.campaign_id,{brief,ui_context:{page:'production'}});
  assert.equal(context.model.unplanned_draft,false,'saving the current page must not invalidate the reviewed plan');
  assert.equal(Object.hasOwn(brief.products[0],'framing'),false,'raw product remains unchanged');
  assert.equal(context.sameProjectInputs(planned,brief),true,'absent and empty framing are equivalent in either direction');
  for (const framing of [null,[],false,'',{'en-US':{'1:1':{zoom:1.1}}}]) {
    const changed=structuredClone(brief); changed.products[0].framing=framing;
    assert.equal(context.sameProjectInputs(changed,planned),false,'invalid or actual framing edits still need review');
  }
  const changed=structuredClone(brief);changed.products[0].description='A deliberate product edit';
  assert.equal(context.sameProjectInputs(changed,planned),false);
});

test("Save receipt clears completed presentation drift but cannot re-admit a partial request", () => {
  for (const state of ['complete','partial','planned']) {
    const brief={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',replay:null},planned={...brief,replay:{history:'Automatic reuse'}};
    const context=vm.createContext({copy:structuredClone,draft:brief,model:{run:{state},project:{},plan_brief:planned,output_brief:planned,unplanned_draft:true},logoSession:null,dirty:false,refreshCampaignIdentity(){}});
    vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
    let options;context.projectSaveQueue=o=>{options=o;return {open(){}};};
    context.initializeProject({});
    options.acknowledged({version:'ack'},'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',{brief});
    assert.equal(context.model.unplanned_draft,state!=='complete');
  }
});

test("current server readiness replaces an obsolete deleted-product request after autosave", () => {
  const brief={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',products:[{id:'retained',source:{role:'finished_scene',path:'owned.png'}}]};
  const context=vm.createContext({copy:structuredClone,draft:brief,model:{run:{state:'planned'},project:{},plan_brief:{...brief,products:[]},unplanned_draft:true},logoSession:null,dirty:false,refreshCampaignIdentity(){}});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  let options;context.projectSaveQueue=o=>{options=o;return {open(){}};};context.initializeProject({});
  options.acknowledged({version:'current',unplanned_draft:false,plan:{generation_products:[]},plan_brief:brief},brief.campaign_id,{brief});
  assert.equal(context.model.unplanned_draft,false);
  assert.deepEqual(context.model.plan.generation_products,[]);
});

test("committing a product restores its data without moving away from the current subtab", () => {
  const context=vm.createContext({copy:structuredClone,draft:null,tab:'products',lang:'es',currentFormat:2});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  context.restoreProject({brief:{products:[{id:'new'}]},ui_context:{tab:'copy',language:'en',currentFormat:0}}, {restoreContext:false});
  assert.equal(context.tab,'products');assert.equal(context.lang,'es');assert.equal(context.currentFormat,2);
  assert.equal(context.draft.products[0].id,'new');
});

test("saved complete color shorthand is display-equivalent without hiding invalid or changed drafts", () => {
  const context = vm.createContext({copy: structuredClone, window: {}});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/brief-flow.js','utf8'),context);
  context.MotifBriefFlow = context.window.MotifBriefFlow;
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  const planned = {brand:{palette:['#FFF4DC','#B5542F'],text_color:'#FFF4DC'},products:[{framing:{'en-US':{'1:1':{text_placements:{cta:{color:'#B5542F'}}}}}}]};
  const authored = structuredClone(planned);
  authored.brand.palette[1] = 'B5542F';
  authored.brand.text_color = ' fff4dc ';
  authored.products[0].framing['en-US']['1:1'].text_placements.cta.color = 'b5542f';
  const before = structuredClone(authored);
  assert.equal(context.sameProjectInputs(authored,planned),true);
  assert.deepEqual(authored,before,'comparison must not rewrite saved raw work');
  for (const value of ['#B55420','B5542','BLAH',123456,null]) {
    const changed = structuredClone(authored);
    changed.brand.palette[1] = value;
    assert.equal(context.sameProjectInputs(changed,planned),false);
  }
  const changed = structuredClone(authored);
  changed.products[0].framing['en-US']['1:1'].text_placements.cta.color = '#FFF4DC';
  assert.equal(context.sameProjectInputs(changed,planned),false);
});

test("own preparation advances only unchanged explicit replay paths before the next autosave", () => {
  const context = vm.createContext({copy:structuredClone});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  const before={brand:{},markets:{},products:[],replay:{history:'Original provenance',sources:{a:{path:'old/master.png',sha256:'verified'}}}};
  const after=structuredClone(before);after.replay.sources.a.path='new/master.png';
  const local=structuredClone(before);local.replay.history='Newer authored metadata';
  context.reconcileAppliedBrief(local,before,after);
  assert.equal(local.replay.sources.a.path,'new/master.png');
  assert.equal(local.replay.sources.a.sha256,'verified');
  assert.equal(local.replay.history,'Newer authored metadata');
  assert.equal(context.sameProjectInputs(local,after),false,'newer provenance remains a real change');
  local.replay.sources.a.path='deliberate/replacement.png';
  context.reconcileAppliedBrief(local,before,after);
  assert.equal(local.replay.sources.a.path,'deliberate/replacement.png');
  for (const replay of [null,false,0,'',[],{}]) {
    const raw={...structuredClone(before),replay};
    context.reconcileAppliedBrief(raw,before,after);
    assert.deepEqual(raw.replay,replay,'automatic or invalid raw replay is not replaced');
  }
});

test("manual and autosave use one serialized request and preserve newer invalid typing", async () => {
  const h = harness();
  h.current.brief.name = "";
  let release;
  h.reply = () => new Promise((resolve) => { release = resolve; });
  h.queue.markChanged();
  const manual = h.queue.save();
  const same = h.queue.save();
  assert.equal(h.calls.length, 1);
  h.current.brief.name = "Newer unfinished name";
  release({ version: "v2", saved_at: "now" });
  assert.equal(await manual, false);
  assert.equal(await same, false);
  assert.equal(h.current.brief.name, "Newer unfinished name");
  assert.equal(h.queue.getSaveState().phase, "dirty");
  h.reply = async () => ({ version: "v3", saved_at: "later" });
  h.queue.markChanged();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].value.version, "v2");
  assert.equal(h.calls[1].value.authored.brief.name, "Newer unfinished name");
  assert.equal(h.queue.getSaveState().phase, "saved");
  h.queue.stop();
});

test("lost response retries only the exact submitted state before newer edits", async () => {
  const h = harness();
  h.current.brief.name = "Committed response lost";
  h.reply = async () => { throw new TypeError("Failed to fetch"); };
  await assert.rejects(h.queue.save());
  assert.equal(h.queue.getSaveState().message, "");
  const original = structuredClone(h.calls[0].value);
  h.current.brief.name = "Newer words";
  h.reply = async () => ({ version: "v2", saved_at: "committed" });
  assert.equal(await h.queue.save(), false);
  assert.deepEqual(h.calls[1].value, original);
  assert.equal(h.current.brief.name, "Newer words");
  assert.equal(await h.queue.save(), true);
  assert.equal(h.calls[2].value.version, "v2");
  assert.equal(h.calls[2].value.authored.brief.name, "Newer words");
});

test("unresolved save notice survives retry and an older acknowledgement until latest work is saved", async () => {
  const h = harness();
  h.current.brief.name = 'First unsaved edit';
  h.reply = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(h.queue.save());
  assert.equal(h.queue.getSaveState().problem.phase, 'error');
  let release;
  h.reply = () => new Promise(resolve => { release = resolve; });
  const retry = h.queue.save();
  assert.equal(h.queue.getSaveState().phase, 'saving');
  assert.equal(h.queue.getSaveState().problem.phase, 'error');
  h.current.brief.name = 'Newer edit during retry';
  release({version:'v2',saved_at:'first confirmed'});
  assert.equal(await retry, false);
  assert.equal(h.queue.getSaveState().phase, 'dirty');
  assert.equal(h.queue.getSaveState().problem.phase, 'error');
  h.reply = async () => ({version:'v3',saved_at:'latest confirmed'});
  assert.equal(await h.queue.saveCurrent(), true);
  assert.equal(h.calls[2].value.authored.brief.name, 'Newer edit during retry');
  assert.equal(h.queue.getSaveState().problem, null);
  h.current.brief.name = 'Conflicting edit';
  h.reply = async () => { throw Object.assign(new Error('Review newer saved work.'),{code:'authored_conflict',status:409}); };
  await assert.rejects(h.queue.save());
  assert.equal(h.queue.getSaveState().problem.phase,'conflict');
  assert.equal(await h.queue.saveCurrent(),false);
  h.queue.open('new-campaign',{...h.saved,authored:{brief:{name:'Other campaign'}}});
  assert.equal(h.queue.getSaveState().problem,null);
  h.queue.stop();
});

test("dismissal persists a closed saved editor while ordinary selection stays read-only", async () => {
  const h = harness();
  h.saved.authored.ui_context = { modal: 'rule', ruleIndex: 0 };
  h.queue.open('08639a3c-6df4-53cd-9cf4-e1ce27b66273', h.saved);
  h.current.ui_context.modal = null;
  h.queue.retainContext();
  await h.queue.saveCurrent();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].value.authored.ui_context.modal, null);
  h.current.ui_context.productId = 'b';
  h.queue.markChanged();
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 1);
  assert.equal(h.queue.isDirty(), false);
  h.queue.stop();
});

test("old open-editor acknowledgement cannot override a later dismissal or reopened edit", async () => {
  for (const reopen of [false, true]) {
    const h = harness();
    let release;
    h.reply = () => new Promise(resolve => { release = resolve; });
    h.current.ui_context = {modal:'product',productId:'a'};
    h.current.form_drafts.a = {'copy-headline':'First'};
    const first = h.queue.save();
    h.current.ui_context.modal = null;
    h.queue.retainContext();
    if (reopen) {
      h.current.ui_context = {modal:'product',productId:'b'};
      h.current.form_drafts.b = {'copy-headline':'Newer'};
      h.queue.markChanged();
    }
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal(h.calls.length, 1);
    h.reply = async () => ({version:'v3',saved_at:'latest'});
    release({version:'v2',saved_at:'first'});
    assert.equal(await first, false);
    await h.queue.saveCurrent();
    assert.equal(h.calls.length, 2);
    assert.equal(h.calls[1].value.authored.ui_context.modal, reopen?'product':null);
    assert.equal(h.calls[1].value.authored.form_drafts.a['copy-headline'], 'First');
    if (reopen) assert.equal(h.calls[1].value.authored.form_drafts.b['copy-headline'], 'Newer');
    assert.equal(h.queue.isDirty(), false);
    h.queue.stop();
  }
});

test("explicit scoped cancellation during Save drains the clear without resurrecting another target", async () => {
  const h = harness(); let release;
  h.current.form_drafts = {a:{'copy-headline':'Canceled'},b:{'copy-headline':'Kept'}};
  h.current.ui_context={modal:'product'};
  h.reply=()=>new Promise(resolve=>{release=resolve});
  const first=h.queue.save();
  delete h.current.form_drafts.a;
  h.current.ui_context.modal=null;
  h.queue.markChanged();h.queue.retainContext();
  release({version:'v2',saved_at:'old'});
  assert.equal(await first,false);
  h.reply=async()=>({version:'v3',saved_at:'cancel'});
  await h.queue.saveCurrent();
  assert.deepEqual(h.calls[1].value.authored.form_drafts,{b:{'copy-headline':'Kept'}});
  assert.equal(h.calls[1].value.authored.ui_context.modal,null);
  h.queue.stop();
});

test("dismissal after a failed Save retains failure and context without retrying it", async () => {
  const h=harness();
  h.current.ui_context={modal:'product'};h.current.form_drafts.a={'copy-headline':'Kept'};
  h.reply=async()=>{throw Object.assign(Error('Disk unavailable'),{status:503})};
  await assert.rejects(h.queue.save());
  h.current.ui_context.modal=null;h.queue.retainContext();h.queue.markChanged();
  await new Promise(resolve=>setTimeout(resolve,15));
  assert.equal(h.calls.length,1);
  assert.equal(h.queue.getSaveState().phase,'error');
  h.reply=async()=>({version:'v2',saved_at:'retry'});
  await h.queue.saveCurrent();
  assert.equal(h.calls.at(-1).value.authored.ui_context.modal,null);
  h.queue.stop();
});

test("publication observed during a delayed Save advances only the exact acknowledged version", async () => {
  for (const competing of [false, true]) {
    const h = harness();
    let release;
    h.current.brief.name = "Saved during production";
    h.reply = () => new Promise(resolve => { release = resolve; });
    const pending = h.queue.save();
    h.queue.observe({ version: competing ? "another-tab-v3" : "v2", base: { revision_id: "published-r2" }, authored: structuredClone(h.current) });
    release({ version: "v2", saved_at: "acknowledged" });
    assert.equal(await pending, true);
    h.current.brief.name = "Newer typing after publication";
    h.reply = async () => ({ version: "v4", saved_at: "later" });
    await h.queue.save();
    assert.equal(h.calls[1].value.version, "v2");
    assert.equal(h.calls[1].value.base.revision_id, competing ? "r1" : "published-r2");
    h.queue.stop();
  }
});

test("opening sparse campaign buffers never saves a phantom edit during source completion", async () => {
  const h = harness();
  const sparse = {...structuredClone(h.saved), authored: {brief: structuredClone(h.current.brief)}};
  h.queue.open("campaign", sparse);
  Object.assign(h.current, {form_drafts: {}, review_drafts: {}, feedback: {}, ui_context: {page: "production"}});
  h.queue.markChanged();
  assert.equal(h.queue.isDirty(), false);
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 0, "empty controls must not race a completed generation with an authored POST");
  const completed = {...structuredClone(sparse), version: "failed-or-completed-source", base: {run_id: "source-run"}};
  assert.equal(h.queue.observe(completed, {sourceCompletion: true}), true);
  assert.equal(h.queue.version(), "failed-or-completed-source");
  assert.equal(h.queue.isDirty(), false);
  h.current.brief.name = "Next real edit";
  await h.queue.saveCurrent();
  assert.equal(h.calls[0].value.version, "failed-or-completed-source");
  assert.equal(h.calls[0].value.authored.brief.name, "Next real edit");
  h.queue.stop();
});

test("sparse-buffer equivalence preserves actual and invalid pending values", () => {
  for (const key of ["form_drafts", "review_drafts", "feedback"]) {
    for (const value of [{field: "Unfinished work"}, null, [], false, ""]) {
      const h = harness();
      h.queue.open("campaign", {...structuredClone(h.saved), authored: {brief: structuredClone(h.current.brief)}});
      h.current[key] = value;
      assert.equal(h.queue.isDirty(), true, `${key}: ${JSON.stringify(value)}`);
      h.queue.stop();
    }
  }
});

test("source completion advances its unchanged authored guard and preserves newer local typing", async () => {
  for (const typing of [false, true]) {
    const h = harness();
    if (typing) h.current.brief.name = "New typing during generation";
    const result = {...structuredClone(h.saved), version: "source-result", base: {run_id: "generated"}};
    assert.equal(h.queue.observe(result, {sourceCompletion: true}), true);
    assert.equal(h.queue.version(), "source-result");
    assert.equal(h.queue.isDirty(), typing);
    assert.equal(h.calls.length, 0, "reading a completed source does not write a draft");
    assert.equal(h.current.brief.name, typing ? "New typing during generation" : "Original");
    h.current.brief.name = "Next ordinary edit";
    await h.queue.saveCurrent();
    assert.equal(h.calls[0].value.version, "source-result");
    assert.deepEqual(h.calls[0].value.base, {run_id: "generated"});
    h.queue.stop();
  }
});

test("source polling cannot adopt a competing authored edit or an unclassified guard change", async () => {
  for (const competing of [false, true]) {
    const h = harness();
    const result = {...structuredClone(h.saved), version: "another-version"};
    if (competing) result.authored.brief.color = "#ffffff";
    assert.equal(h.queue.observe(result, {sourceCompletion: competing}), false);
    assert.equal(h.queue.version(), "v1");
    h.current.brief.name = "My edit";
    h.reply = async () => { throw Object.assign(Error("Updated elsewhere"), {code: "authored_conflict", status: 409}); };
    await assert.rejects(h.queue.saveCurrent());
    assert.equal(h.calls[0].value.version, "v1");
    assert.equal(h.queue.getSaveState().phase, "conflict");
    assert.equal(h.current.brief.name, "My edit");
    h.queue.stop();
  }
});

test("source completion during a delayed save acknowledges only its exact committed content", async () => {
  for (const competing of [false, true]) {
    const h = harness();
    h.current.brief.name = "Saved during generation";
    let release;
    h.reply = () => new Promise(resolve => { release = resolve; });
    const pending = h.queue.save();
    const result = {version: "source-result", base: {run_id: "generated"}, authored: structuredClone(h.current), saved_at: "source-time"};
    if (competing) result.authored.brief.color = "#ffffff";
    assert.equal(h.queue.observe(result, {sourceCompletion: true}), false);
    h.current.brief.name = "Newer typing";
    release({version: "saved-before-source", saved_at: "ack-time", composition_version: "saved-before-source",
      source_urls: {}, manifest: {complete: false, outputs: []}});
    assert.equal(await pending, false);
    assert.equal(h.queue.version(), competing ? "saved-before-source" : "source-result");
    assert.equal(h.acknowledgements[0].version, competing ? "saved-before-source" : "source-result");
    assert.equal(Object.hasOwn(h.acknowledgements[0], "composition_version"), competing,
      "a late save response must not replace a newer source projection");
    assert.equal(Object.hasOwn(h.acknowledgements[0], "source_urls"), competing);
    assert.equal(Object.hasOwn(h.acknowledgements[0], "manifest"), competing);
    assert.equal(h.current.brief.name, "Newer typing");
    h.reply = async () => ({version: "next-save"});
    await h.queue.saveCurrent();
    assert.equal(h.calls[1].value.version, competing ? "saved-before-source" : "source-result");
    assert.equal(h.calls[1].value.authored.brief.name, "Newer typing");
    h.queue.stop();
  }
});

test("an unchanged source observation cannot downgrade a newer context-save acknowledgement", async () => {
  const h = harness();
  h.saved.authored.ui_context = {modal: "rule"};
  h.queue.open("campaign", h.saved);
  h.current.ui_context = {modal: null};
  h.queue.retainContext();
  let release;
  h.reply = () => new Promise(resolve => {release = resolve;});
  const pending = h.queue.save();
  h.queue.observe(structuredClone(h.saved), {sourceCompletion: true});
  release({version: "new-context-save"});
  assert.equal(await pending, true);
  assert.equal(h.queue.version(), "new-context-save");
  assert.equal(h.acknowledgements[0].version, "new-context-save");
  assert.equal(h.queue.isDirty(), false);
  h.queue.stop();
});

test("a debounce consumed during a slow acknowledgement drains newer work automatically", async () => {
  const h = harness();
  let release;
  h.current.brief.name = "First edit";
  h.reply = () => new Promise((resolve) => { release = resolve; });
  const first = h.queue.save();
  h.current.brief.name = "Newest edit";
  h.queue.markChanged();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 1);
  h.reply = async () => ({ version: "v3", saved_at: "newest" });
  release({ version: "v2", saved_at: "earlier" });
  assert.equal(await first, false);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].value.authored.brief.name, "Newest edit");
  assert.equal(h.calls[1].value.version, "v2");
  assert.equal(h.queue.getSaveState().phase, "saved");
  h.queue.stop();
});

test("a new campaign's dirty state drains after an older campaign request releases the queue", async () => {
  const h = harness();
  let release;
  h.current.brief.name = "Old campaign edit";
  h.reply = () => new Promise((resolve) => { release = resolve; });
  const first = h.queue.save();
  h.queue.open("8f57d6d7-9318-564d-b7a8-1b78b4e978b7", { version: "b1", base: {}, authored: { brief: { name: "B" } } });
  h.current.brief.name = "Newer B";
  h.queue.markChanged();
  await new Promise((resolve) => setTimeout(resolve, 20));
  h.reply = async () => ({ version: "b2", saved_at: "B saved" });
  release({ version: "a2", saved_at: "A saved" });
  await first;
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].campaign, "8f57d6d7-9318-564d-b7a8-1b78b4e978b7");
  assert.equal(h.calls[1].value.version, "b1");
  assert.equal(h.calls[1].value.authored.brief.name, "Newer B");
  assert.equal(h.queue.getSaveState().phase, "saved");
  h.queue.stop();
});

test("Campaign conflict pauses every later write until the page is refreshed", async () => {
  const h = harness();
  h.current.form_drafts = { "p/en": { "copy-headline": "Local words" } };
  h.reply = async () => { throw Object.assign(new Error("Changed elsewhere"), { status: 409, code: "authored_conflict" }); };
  await assert.rejects(h.queue.save());
  h.current.brief.name = "Keep editing";
  h.queue.markChanged();
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 1);
  assert.equal(h.queue.getSaveState().phase, "conflict");
  assert.equal(await h.queue.saveCurrent(), false);
  assert.equal(await h.queue.save(), false);
  assert.equal(h.queue.applied({version:"newer",base:{}}), false);
  h.queue.resumeAutosave();
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 1);
  assert.equal(h.queue.version(), "v1");
  assert.equal(h.current.form_drafts["p/en"]["copy-headline"], "Local words");
  assert.equal(h.queue.replaceSaved, undefined);
  h.queue.stop();
});

test("recovery clicks do not retry failed saves and navigation choices suspend automatic writes", async () => {
  const h = harness();
  h.current.brief.name = "Rejected work";
  h.reply = async () => { throw Object.assign(new Error("Controlled refusal"), { status: 503 }); };
  await assert.rejects(h.queue.save());
  assert.equal(h.queue.getSaveState().message, "Controlled refusal");
  h.queue.markChanged();
  h.queue.suspendAutosave();
  h.queue.markChanged();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 1);
  assert.equal(h.queue.getSaveState().phase, "error");
  h.queue.resumeAutosave();
  h.queue.markChanged();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 1);
  h.reply = async () => ({ version: "v2", saved_at: "retried" });
  await h.queue.saveCurrent();
  h.current.brief.name = "Ordinary unsaved work";
  h.queue.markChanged();
  h.queue.suspendAutosave();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 2);
  h.queue.resumeAutosave();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 3);
  assert.equal(h.calls[2].value.authored.brief.name, "Ordinary unsaved work");
  h.queue.stop();
});

test("opening another campaign restores its saved buffers and ignores a late old acknowledgement", async () => {
  const h = harness();
  h.current.brief.name = "Tab A";
  let release;
  h.reply = () => new Promise((resolve) => { release = resolve; });
  const pending = h.queue.save();
  const b = { version: "b1", base: {}, authored: { brief: { name: "Campaign B" }, form_drafts: { fields: {"brief/brand/en": {"text-color":"#12"}} } } };
  h.queue.open("8f57d6d7-9318-564d-b7a8-1b78b4e978b7", b);
  release({ version: "a2", saved_at: "a-time" });
  assert.equal(await pending, false);
  assert.equal(h.current.brief.name, "Campaign B");
  h.saved = { ...b, version: "b2", authored: { brief: { name: "Fetched B" }, form_drafts: { fields: {"brief/brand/en": {"text-color":"#123"}} } } };
  h.queue.open("8f57d6d7-9318-564d-b7a8-1b78b4e978b7", h.saved);
  assert.equal(h.current.form_drafts.fields["brief/brand/en"]["text-color"], "#123");
  assert.equal(h.current.brief.name, "Fetched B");
});

test("known refusal retains edits, and a confirmed apply advances only the shared queue base", async () => {
  const h = harness();
  h.current.brief.name = "Pending metadata";
  h.reply = async () => { throw Object.assign(new Error("Operation active"), { status: 409, code: "busy" }); };
  await assert.rejects(h.queue.save());
  h.current.brief.name = "Still here";
  h.queue.applied({ version: "v5", base: { revision_id: "r5" } });
  h.reply = async () => ({ version: "v6", saved_at: "after apply" });
  assert.equal(await h.queue.save(), true);
  assert.equal(h.calls[1].value.version, "v5");
  assert.equal(h.calls[1].value.base.revision_id, "r5");
  assert.equal(h.current.brief.name, "Still here");
  h.queue.stop();
  await flush();
});

test("raw form capture restores unfinished strings and source choice without persisting file inputs", () => {
  const context = vm.createContext({ structuredClone, setTimeout, clearTimeout, modalProduct: { treatment: "concept" } });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/project.js", "utf8"), context);
  const controls = [
    { id: "brand-palette", type: "text", value: "  #12,  unfinished\n" },
    { id: "product-name", type: "text", value: "" },
    { id: "json-file", type: "file", value: "private file chooser state" },
    { id: "preview-token", type: "hidden", value: "must not persist" },
  ];
  const root = {
    querySelectorAll: () => controls,
    querySelector: selector => controls.find(control => `#${control.id}` === selector),
  };
  const saved = context.projectFormValues(root);
  assert.equal(saved["brand-palette"], "  #12,  unfinished\n");
  assert.equal(saved["product-name"], undefined, "uncommitted product fields are transient");
  assert.equal(saved.treatment, undefined);
  assert.equal(saved["json-file"], undefined);
  assert.equal(saved["preview-token"], undefined);
  controls[0].value = "Different";
  context.modalProduct.treatment = "reuse";
  context.restoreProjectFormValues(root, saved);
  assert.equal(controls[0].value, "  #12,  unfinished\n");
  assert.equal(context.modalProduct.treatment, "reuse", "saved campaign controls cannot change the product mode");
});

test("ordinary selection changes alone do not trigger an authored save or reorder saved activity", async () => {
  const h = harness();
  h.current.ui_context = { page: "review", productId: "another", language: "es" };
  h.queue.markChanged();
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 0);
  assert.equal(h.queue.isDirty(), false);
  assert.equal(await h.queue.save(), true);
});

test("project Save retains raw forms and excludes transient image-editor scratch", () => {
  const context = vm.createContext({
    structuredClone, setTimeout, clearTimeout,
    copy: structuredClone,
    draft: { campaign_id: "8fca7c2c-091a-5bfe-ace8-b6af9c0e801c", name: "", products: [{ id: "p1" }, { id: "p2" }] },
    C: { products: [{ id: "p1" }, { id: "p2" }] },
    model: { manifest: { revision_id: "r1" }, metadata: { snapshot_id: "m1" } },
    page: "review", tab: "brand", lang: "en", modal: "frame",
    currentFormat: 0, currentProduct: 0, ruleIndex: -1,
    reviewTab: "product", selected: [0, 1], formats: [0, 0],
    modalProduct: null,
    framingContext: { campaign: "8fca7c2c-091a-5bfe-ace8-b6af9c0e801c", run_id: "run", revision_id: "r1", product_id: "p1", market: "en-US", ratio: "1:1" },
    framingDraft: { zoom: 1.37, focal: 12.5, vertical: 50, subject: null },
    framingShowSafety: false,
    logoEditor: null,
    MARKET: { en: "en-US", es: "es-MX" },
    FMT: [{ ratio: "1:1" }],
    $: () => ({ querySelectorAll: () => [], querySelector: () => null }),
  });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/project.js", "utf8"), context);
  vm.runInContext('projectBuffers.form_drafts={fields:{"brief/brand/en":{"text-color":"#12"}}}', context);
  const value = context.captureProject();
  assert.deepEqual(Object.keys(value).sort(), ["brief", "feedback", "form_drafts", "review_drafts", "ui_context"]);
  assert.equal(value.form_drafts.fields["brief/brand/en"]["text-color"], "#12");
  assert.equal(value.ui_context.modal, null);
  context.modal = "product";
  context.modalProduct = {id:"p1",name:"Uncommitted",treatment:"concept",source:{role:"concept"}};
  const productValue = context.captureProject();
  assert.deepEqual(productValue.brief.products, value.brief.products);
  assert.deepEqual(productValue.form_drafts, value.form_drafts);
  assert.equal(productValue.ui_context.modal, null);
  assert.equal(productValue.ui_context.modalProductId, undefined);
  context.modal = "frame";
  context.restoreProject(value);
  assert.equal(context.draft.name, "");
  value.ui_context.productId = "p2";
  value.ui_context.currentFormat = 2;
  context.restoreProject(value);
  assert.equal(context.currentProduct, 1);
  assert.equal(context.selected[0], 1);
  assert.equal(context.formats[0], 2);
  assert.equal(context.selected[1], 1);
  value.ui_context.reviewTab = "compare";
  value.ui_context.productId = "p1";
  value.ui_context.currentFormat = 0;
  context.restoreProject(value);
  assert.equal(context.selected[0], 1, "restoring another surface leaves Compare slots alone");
  assert.equal(context.formats[0], 2);
});

test("Save and leave waits for the latest captured state after an older flight", async () => {
  const h = harness();
  let firstRelease, secondRelease;
  h.current.brief.name = "First captured text";
  h.reply = () => new Promise((resolve) => { firstRelease = resolve; });
  const first = h.queue.save();
  h.current.brief.name = "Last text before leaving";
  h.queue.markChanged();
  let left = false;
  const leaving = h.queue.saveCurrent().then((value) => { left = value; return value; });
  h.reply = () => new Promise((resolve) => { secondRelease = resolve; });
  firstRelease({ version: "v2", saved_at: "first" });
  await first;
  await flush();
  assert.equal(left, false);
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].value.authored.brief.name, "Last text before leaving");
  secondRelease({ version: "v3", saved_at: "latest" });
  assert.equal(await leaving, true);
  assert.equal(h.queue.getSaveState().phase, "saved");
  h.queue.stop();
});

test("an explicit Apply holds the same queue and preserves edits until its new base is reconciled", async () => {
  const h = harness();
  h.current.brief.name = "Saved before Apply";
  assert.equal(await h.queue.saveCurrent(), true);
  assert.ok(h.queue.hold());
  h.current.brief.name = "New typing during Apply";
  h.queue.markChanged();
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 1);
  assert.equal(await h.queue.saveCurrent(), false);
  assert.equal(h.queue.applied({ version: "canonical-v3", base: { revision_id: "r2" } }), true);
  h.reply = async () => ({ version: "v4", saved_at: "after apply" });
  h.queue.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].value.version, "canonical-v3");
  assert.equal(h.calls[1].value.base.revision_id, "r2");
  assert.equal(h.calls[1].value.authored.brief.name, "New typing during Apply");
  h.queue.stop();
});

test("Save presentation remains focusable and suppresses repeated unavailable activation", async () => {
  const elements = new Map(), handlers = {};
  const element = (selector) => {
    if (!elements.has(selector)) elements.set(selector, {
      disabled: false, dataset: {}, hidden: false,
      setAttribute(name, value) { this[name] = value; },
    });
    return elements.get(selector);
  };
  const root = {
    dataset: {}, classList: { add() {} },
    querySelector: element, contains: () => true,
    addEventListener(name, callback) { handlers[name] = callback; },
    removeEventListener() {},
  };
  let state = { phase: "dirty", busy: false }, notify, calls = 0, release;
  const context = vm.createContext({ window: {}, Intl, Date, Promise,
    document: { addEventListener() {}, removeEventListener() {} } });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/save-state.js", "utf8"), context);
  const view = context.window.MotifSave.mount(root, {
    getSaveState: () => state,
    subscribeSaveState(callback) { notify = callback; return () => {}; },
    onSave() {
      calls += 1;
      notify(state = { phase: "saving", busy: false });
      return new Promise(resolve => { release = resolve; });
    },
  });
  const save = element('[data-hs="save"]');
  save.dataset.hs = "save";
  const click = () => handlers.click({ target: { closest: () => save } });
  click();
  await flush();
  assert.equal(calls, 1);
  assert.equal(save.disabled, false);
  assert.equal(save["aria-disabled"], "true");
  click();
  await flush();
  assert.equal(calls, 1);
  release();
  for (const unavailable of [{ phase: "conflict" }, { phase: "dirty", busy: true }]) {
    notify(state = unavailable);
    click();
    await flush();
    assert.equal(save.disabled, false);
    assert.equal(save["aria-disabled"], "true");
    assert.equal(calls, 1);
  }
  notify(state = { phase: "error", message: "" });
  assert.equal(save["aria-disabled"], "false");
  assert.equal(element('[data-hs-body]').textContent,
    "Your work is still here. Use Save to try again. Keep this tab open until saving succeeds.");
  notify(state = { phase: "error", message: "Free disk space before trying again." });
  assert.equal(element('[data-hs-body]').textContent, "Free disk space before trying again.");
  view.destroy();
});

test("image adoption compares with this tab's acknowledged copy and preserves a competing Save token", async () => {
  const before = {campaign_id:'08639a3c-6df4-53cd-9cf4-e1ce27b66273', brand:{}, markets:{'en-US':{copy:{message:'Shared',cta:'Explore'}}}, products:[{id:'rain',copy_overrides:{}},{id:'rest',copy_overrides:{}}]};
  const authored = {brief:structuredClone(before),form_drafts:{fields:{'brief/copy/en':{'copy-headline':'Shared','copy-cta':'Explore'}}}};
  const context = vm.createContext({structuredClone,copy:structuredClone,setTimeout,clearTimeout,draft:structuredClone(before)});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  const initial={version:'tab0-v1',base:{revision_id:'initial'},authored};
  const calls=[];
  const queue=context.projectSaveQueue({capture:()=>({brief:structuredClone(context.draft),form_drafts:structuredClone(authored.form_drafts)}),restore(){},send:async(_,request)=>{calls.push(request);throw Object.assign(new Error('Other tab saved'),{status:409,code:'authored_conflict'});},delay:10000});
  queue.open('08639a3c-6df4-53cd-9cf4-e1ce27b66273',initial);
  context.queueUnderTest=queue;
  vm.runInContext("projectOwner='08639a3c-6df4-53cd-9cf4-e1ce27b66273';projectQueue=queueUnderTest;projectBuffers.form_drafts={fields:{'brief/copy/en':{'copy-headline':'Shared','copy-cta':'Explore'}}};",context);
  const tab1=structuredClone(initial);tab1.version='tab1-v2';tab1.base.revision_id='rest-applied';tab1.authored.brief.products[1].copy_overrides={'en-US':{cta:'Give your knees a garden break.'}};
  const latest={value:{campaign_id:'08639a3c-6df4-53cd-9cf4-e1ce27b66273'},project:tab1};
  context.receiveProject(latest,{project:initial});
  assert.deepEqual(context.draft.products[1].copy_overrides,{});
  // The latest fetched model already contains Rest's new copy, but it is not
  // this tab's accepted Save baseline. A distinct raw edit must remain local.
  context.draft.markets['en-US'].copy.cta='  unfinished CTA  ';
  const applied=structuredClone(latest);applied.project.base.revision_id='rain-applied';
  applied.project.authored.brief.products[0].copy_overrides={'en-US':{message:'Water a little wonder.'}};
  context.receiveProject(applied,latest,{applied:true});
  assert.equal(context.draft.products[1].copy_overrides['en-US'].cta,'Give your knees a garden break.');
  assert.equal(context.draft.products[0].copy_overrides['en-US'].message,'Water a little wonder.');
  assert.equal(context.draft.markets['en-US'].copy.cta,'  unfinished CTA  ');
  await assert.rejects(queue.saveCurrent(),/Other tab saved/);
  assert.equal(calls[0].version,'tab0-v1','image adoption cannot authorize replacing other-tab authored work');
  assert.equal(queue.getSaveState().phase,'conflict');
  queue.stop();
});

test("applied copy reconciles raw controls and product fields individually without erasing newer work", () => {
  const before={brand:{},markets:{'en-US':{copy:{message:'Old',cta:'Explore'}},'es-MX':{copy:{message:'Antes',cta:'Ver'}}},products:[{id:'a',copy_overrides:{'en-US':{message:'Before',cta:'Old CTA'}}}]};
  const after=structuredClone(before);after.markets['en-US'].copy={message:'New',cta:'See details'};after.products[0].copy_overrides['en-US']={message:'Applied',cta:'Applied CTA'};
  const fields={'brief/copy/en':{'copy-headline':'Old','copy-cta':'Explore'},'brief/copy/es':{'copy-headline':'Antes','copy-cta':'Ver'}};
  const saved={version:'v1',base:{},authored:{brief:before,form_drafts:{fields}}};
  const context=vm.createContext({structuredClone,copy:structuredClone,setTimeout,clearTimeout,draft:structuredClone(before)});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  context.localFields=structuredClone(fields);context.localFields['brief/copy/en']['copy-cta']='  newer unfinished  ';
  context.draft.products[0].copy_overrides['en-US'].cta='Independent CTA';
  context.saved=saved;
  vm.runInContext("projectOwner='8fca7c2c-091a-5bfe-ace8-b6af9c0e801c';projectQueue=projectSaveQueue({capture:()=>({brief:draft}),restore(){},send(){},delay:10000});projectQueue.open('8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',saved);projectBuffers.form_drafts={fields:localFields};",context);
  const current=structuredClone(saved);current.authored.brief=after;current.authored.form_drafts.fields['brief/copy/en']={'copy-headline':'New','copy-cta':'See details'};
  context.receiveProject({value:{campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c'},project:current},{project:saved});
  assert.equal(context.localFields['brief/copy/en']['copy-headline'],'New');
  assert.equal(context.localFields['brief/copy/en']['copy-cta'],'  newer unfinished  ');
  assert.equal(context.localFields['brief/copy/es']['copy-headline'],'Antes');
  assert.equal(context.draft.products[0].copy_overrides['en-US'].message,'Applied');
  assert.equal(context.draft.products[0].copy_overrides['en-US'].cta,'Independent CTA');
  for(const invalid of [null,false,'',[]]) {
    const local=structuredClone(before);local.products[0].copy_overrides=invalid;
    context.reconcileAppliedBrief(local,before,after);
    assert.deepEqual(local.products[0].copy_overrides,invalid);
  }
});

test("own late Save acknowledgement reconciles observed applied Copy without overwriting later typing", () => {
  const brief={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',brand:{},products:[],markets:{'en-US':{copy:{message:'Before',cta:'Before CTA'}}}};
  const retired={product_id:'p1',market:'en-US',ratio:'1:1'},later={...retired,ratio:'16:9'};
  const submitted={brief,form_drafts:{brand_color_defaults:[retired],fields:{'brief/copy/en':{'copy-headline':'Before','copy-cta':'Before CTA'}}}};
  const current={base:{revision_id:'applied'},authored:structuredClone(submitted)};
  current.authored.brief.markets['en-US'].copy={message:'Applied',cta:'Applied CTA'};
  current.authored.form_drafts.brand_color_defaults=[];
  current.authored.form_drafts.fields['brief/copy/en']={'copy-headline':'Applied','copy-cta':'Applied CTA'};
  const context=vm.createContext({copy:structuredClone,refreshCampaignIdentity(){},draft:structuredClone(brief),model:{project:{}},MotifTextLimits:{purposeIssue:()=>null},logoSession:null,dirty:false});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  let options;context.projectSaveQueue=o=>{options=o;return {open(){}};};context.initializeProject({});
  vm.runInContext("projectBuffers.form_drafts={fields:{'brief/copy/en':{'copy-headline':'Before','copy-cta':'  still typing  '}}};",context);
  context.localTargets=[retired,later];vm.runInContext('projectBuffers.form_drafts.brand_color_defaults=copy(localTargets)',context);
  options.acknowledged({version:'ack'},'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',submitted,current);
  assert.equal(context.draft.markets['en-US'].copy.message,'Applied');
  assert.equal(vm.runInContext("projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline']",context),'Applied');
  assert.equal(vm.runInContext("projectBuffers.form_drafts.fields['brief/copy/en']['copy-cta']",context),'  still typing  ');
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(projectBuffers.form_drafts.brand_color_defaults)',context)),[later]);
});

test('Studio color Apply retires its acknowledged Brand reset before the next normal Save', async()=>{
  const cid='8fca7c2c-091a-5bfe-ace8-b6af9c0e801c';
  const square={product_id:'p1',market:'en-US',ratio:'1:1'}, landscape={...square,ratio:'16:9'}, other={...square,product_id:'p2'};
  const brief={campaign_id:cid,brand:{},markets:{},audience:'Unfinished audience',products:[{id:'p1',framing:{'en-US':{'1:1':{text_placements:{message:{color:null,left:.2},cta:{color:null}}}}}}]};
  const initial={version:'saved-reset',base:{revision_id:'r1'},authored:{brief,form_drafts:{brand_color_defaults:[square,landscape],fields:{'brief/campaign/en':{audience:'Unfinished audience'}}}}};
  const context=vm.createContext({structuredClone,copy:structuredClone,setTimeout,clearTimeout,draft:structuredClone(brief),dirty:false});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  context.initial=initial;
  const writes=[];context.sendAuthored=async(id,body)=>{writes.push(structuredClone(body));return {version:'saved-after-apply'};};
  vm.runInContext(`projectOwner='${cid}';projectBuffers.form_drafts=copy(initial.authored.form_drafts);
    projectQueue=projectSaveQueue({capture:()=>({brief:copy(draft),form_drafts:copy(projectBuffers.form_drafts)}),restore(){},send:sendAuthored,delay:10000});
    projectQueue.open(projectOwner,initial,{restoreState:false});projectQueue.hold();`,context);
  const accepted=structuredClone(initial);
  accepted.version='accepted-studio';accepted.base.revision_id='r2';
  accepted.authored.brief.products[0].framing['en-US']['1:1'].text_placements.message.color='#733B31';
  accepted.authored.form_drafts.brand_color_defaults=[landscape];
  // Distinct newer work is outside the acknowledgement and must survive it.
  context.other=other;
  vm.runInContext('projectBuffers.form_drafts.brand_color_defaults.push(other);draft.audience="Later audience";',context);
  context.receiveProject({value:{campaign_id:cid},project:accepted},{project:initial},{applied:true});
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(projectBuffers.form_drafts.brand_color_defaults)',context)),[landscape,other]);
  vm.runInContext('projectQueue.release()',context);
  assert.equal(await context.saveProject(),true);
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].authored.form_drafts.brand_color_defaults,[landscape,other]);
  assert.equal(writes[0].authored.brief.products[0].framing['en-US']['1:1'].text_placements.message.color,'#733B31');
  assert.equal(writes[0].authored.brief.audience,'Later audience');
  assert.equal(writes[0].authored.form_drafts.fields['brief/campaign/en'].audience,'Unfinished audience');

  // A fresh reset after the Apply is new intent, not the retired prior choice.
  const saved=JSON.parse(vm.runInContext('JSON.stringify({version:projectQueue.version(),base:{revision_id:"r2"},authored:projectQueue.authored()})',context));
  context.square=square;
  vm.runInContext('projectBuffers.form_drafts.brand_color_defaults.push(square);draft.products[0].framing["en-US"]["1:1"].text_placements.message.color=null;',context);
  context.receiveProject({value:{campaign_id:cid},project:saved},{project:accepted});
  assert.equal(await context.saveProject(),true);
  assert.deepEqual(writes[1].authored.form_drafts.brand_color_defaults,[landscape,other,square]);
  assert.equal(writes[1].authored.brief.products[0].framing['en-US']['1:1'].text_placements.message.color,null);
  vm.runInContext('projectQueue.stop()',context);
});

test('a competing fetched draft cannot retire this tab’s pending Brand reset',()=>{
  const cid='8fca7c2c-091a-5bfe-ace8-b6af9c0e801c', target={product_id:'p1',market:'en-US',ratio:'1:1'};
  const initial={version:'mine',base:{},authored:{brief:{campaign_id:cid,brand:{},products:[],markets:{}},form_drafts:{brand_color_defaults:[target]}}};
  const context=vm.createContext({structuredClone,copy:structuredClone,setTimeout,clearTimeout,draft:structuredClone(initial.authored.brief)});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);context.initial=initial;
  vm.runInContext(`projectOwner='${cid}';projectBuffers.form_drafts=copy(initial.authored.form_drafts);
    projectQueue=projectSaveQueue({capture:()=>({brief:copy(draft),form_drafts:copy(projectBuffers.form_drafts)}),restore(){},send(){}});projectQueue.open(projectOwner,initial);`,context);
  const theirs=structuredClone(initial);theirs.version='another-tab';theirs.authored.form_drafts.brand_color_defaults=[];
  context.receiveProject({value:{campaign_id:cid},project:theirs},{project:initial});
  assert.deepEqual(JSON.parse(vm.runInContext('JSON.stringify(projectBuffers.form_drafts.brand_color_defaults)',context)),[target]);
  assert.equal(vm.runInContext('projectQueue.version()',context),'mine');
  vm.runInContext('projectQueue.stop()',context);
});

test('acknowledged campaign label updates without replacing the focused field or newer typing',()=>{
  const label={textContent:'Old name'},brand={textContent:'Old brand'},selector={title:'Old name',querySelector:q=>q==='.campaign-heading-name'?label:brand},field={value:'Newer unsaved name'},document={activeElement:field,title:'Old name',addEventListener(){}};
  const brief={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',name:'Newer unsaved name'};
  const context=vm.createContext({document,$:()=>selector,page:'campaign',copy:structuredClone,draft:brief,model:{project:{}},MotifIdentityLayout:{fit(){}},MotifTextLimits:{purposeIssue:()=>null},logoSession:null,dirty:false});
  const api=readFileSync('src/campaign_studio/static/motif/api.js','utf8');
  vm.runInContext(api.slice(api.indexOf('function campaignIdentity('),api.indexOf('async function saveDraft(')),context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
  let options;context.projectSaveQueue=o=>{options=o;return {open(){}};};
  context.initializeProject({});
  options.acknowledged({version:'ack'},brief.campaign_id,{brief:{...brief,name:'Saved campaign name'}});
  assert.equal(selector.title,'Saved campaign name');assert.equal(label.textContent,'Saved campaign name');
  assert.equal(document.title,'Motif – Campaign Studio · Saved campaign name');
  assert.equal(document.activeElement,field);assert.equal(field.value,'Newer unsaved name');assert.equal(context.draft.name,'Newer unsaved name');
});

test('opening and closing an unchanged Production inspector remains read-only across page, language, and format selection', async () => {
  const h=harness();
  h.saved.authored.ui_context={page:'review',language:'en',currentFormat:0,productId:'a',modal:null};
  h.queue.open('08639a3c-6df4-53cd-9cf4-e1ce27b66273',h.saved);
  h.current.ui_context={page:'production',language:'es',currentFormat:1,productId:'b',modal:'inspect'};
  h.queue.markChanged();
  h.current.ui_context.modal=null;
  h.queue.retainContext();
  assert.equal(h.queue.isDirty(),false);
  await new Promise(resolve=>setTimeout(resolve,15));
  assert.equal(h.calls.length,0);
  assert.equal(h.queue.getSaveState().phase,'saved');
  h.queue.stop();
});

test('lost acknowledgement stays unresolved when local work reverts, then reconciles the exact request before saving the revert', async () => {
  const h = harness(), original = structuredClone(h.current);
  let server = structuredClone(original), writes = 0;
  h.reply = async (_, value) => {
    if (h.calls.length === 1) {
      server = structuredClone(value.authored); writes++;
      throw new Error('Response lost after commit');
    }
    if (h.calls.length === 2) {
      assert.deepEqual(structuredClone(value), h.calls[0].value, 'retry must acknowledge the exact committed request');
      return {version:'v2', saved_at:'first commit'};
    }
    assert.equal(value.version,'v2');
    server = structuredClone(value.authored); writes++;
    return {version:'v3', saved_at:'revert commit'};
  };
  h.current.brief.name = 'B committed without acknowledgement';
  await assert.rejects(h.queue.save(),/Response lost/);
  h.current = structuredClone(original);
  h.queue.markChanged(); h.queue.stop();
  assert.equal(h.queue.isDirty(),true,'unknown pending commit is still unsaved work');
  assert.equal(h.queue.getSaveState().problem.phase,'error');
  assert.equal(h.queue.getSaveState().phase,'dirty');
  assert.notDeepEqual(server,original);
  assert.equal(await h.queue.saveCurrent(),true);
  assert.equal(h.calls.length,3);
  assert.equal(writes,2,'bounded receipt retry does not create another commit');
  assert.deepEqual(server,original);
  assert.deepEqual(h.current,original);
  assert.equal(h.queue.isDirty(),false);
  assert.equal(h.queue.getSaveState().problem,null);
  h.queue.open(h.calls[0].campaign,{version:'v3',base:h.saved.base,authored:server});
  assert.deepEqual(h.current,original,'reopening uses the confirmed revert');
  h.queue.stop();
});

test('an old operation cannot release a new campaign queue hold', async()=>{
  const h=harness(),old=h.queue.hold();
  h.queue.open('second',{version:'b1',base:{},authored:{brief:{name:'Other campaign'},form_drafts:{}}});
  assert.equal(h.queue.getSaveState().busy,false);
  const current=h.queue.hold();h.current.brief.name='New current work';h.queue.markChanged();
  h.queue.release(old);assert.equal(h.queue.getSaveState().busy,true);
  await new Promise(resolve=>setTimeout(resolve,15));assert.equal(h.calls.length,0);
  h.queue.release(current);await new Promise(resolve=>setTimeout(resolve,15));
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].campaign,'second');
  assert.equal(h.calls[0].value.authored.brief.name,'New current work');h.queue.stop();
});


test('campaign identity uses complete independent brand and campaign fields',()=>{
  const name={textContent:''},brand={textContent:''},selector={querySelector:q=>q==='.campaign-heading-name'?name:brand},document={title:''};
  const draft={campaign_id:'current',name:'Autumn – A new day',brand:{name:'Autumn Winds'},markets:{'en-US':{copy:{message:'Stay a little longer.'}}}};
  const context=vm.createContext({draft,model:{},MotifIdentityLayout:{fit(){}},MotifTextLimits:{purposeIssue:(purpose,value)=>Array.from(value||'').length>(purpose==='brand_name'?20:25)?'Too long':null},document,$:()=>selector,page:'campaign'});
  const api=readFileSync('src/campaign_studio/static/motif/api.js','utf8');
  vm.runInContext(api.slice(api.indexOf('function campaignIdentity('),api.indexOf('async function saveDraft(')),context);
  context.refreshCampaignIdentity();
  assert.equal(name.textContent,draft.name);
  assert.equal(brand.textContent,draft.brand.name);
  assert.equal(selector.title,`${draft.brand.name} · ${draft.name}`);
  assert.equal(document.title,`Motif – Campaign Studio · ${selector.title}`);
  context.refreshCampaignIdentity('W'.repeat(25),'M'.repeat(20));
  assert.equal(name.textContent,'W'.repeat(25));assert.equal(brand.textContent,'M'.repeat(20));
  context.refreshCampaignIdentity('Other campaign','');
  assert.equal(brand.hidden,true);assert.equal(name.textContent,'Other campaign');
  assert.equal(selector.title,'Other campaign');
  assert.equal(draft.name,'Autumn – A new day');
});

test('over-limit header drafts use only their own acknowledged identity without changing the fields',()=>{
 const draft={campaign_id:'a',name:'W'.repeat(26),brand:{name:'W'.repeat(21)}};
 const context=vm.createContext({draft,model:{project:{authored:{brief:{campaign_id:'a',name:'Accepted campaign',brand:{name:'Accepted brand'}}}}},MotifTextLimits:{purposeIssue:(purpose,value)=>Array.from(value||'').length>(purpose==='brand_name'?20:25)?'Too long':null}});
 const api=readFileSync('src/campaign_studio/static/motif/api.js','utf8');
 vm.runInContext(api.slice(api.indexOf('function campaignIdentity('),api.indexOf('function refreshCampaignIdentity(')),context);
 assert.equal(context.campaignIdentity().name,'Accepted campaign');assert.equal(context.campaignIdentity().brand,'Accepted brand');
 assert.equal(draft.name,'W'.repeat(26));assert.equal(draft.brand.name,'W'.repeat(21));
 draft.campaign_id='b';
 assert.equal(context.campaignIdentity().name,'Untitled campaign');assert.equal(context.campaignIdentity().brand,'');
 draft.name='New campaign';draft.brand.name='New brand';
 assert.equal(context.campaignIdentity().name,'New campaign');assert.equal(context.campaignIdentity().brand,'New brand');
 context.model.project.authored.brief={campaign_id:'b',name:'B acknowledged',brand:{name:'B brand'}};
 draft.name='W'.repeat(26);draft.brand.name='W'.repeat(21);
 assert.equal(context.campaignIdentity().name,'B acknowledged');assert.equal(context.campaignIdentity().brand,'B brand');
});

for (const correctionTiming of ['before refusal', 'after refusal']) test(`length refusal saves corrected work ${correctionTiming}`, async()=>{
  const h=harness();let reject;
  h.reply=()=>new Promise((resolve,no)=>{reject=no;});
  h.current.brief.name='W'.repeat(26);
  const refused=h.queue.save();
  if(correctionTiming==='before refusal'){
    h.current.brief.name='Corrected campaign';h.queue.markChanged();
    await new Promise(resolve=>setTimeout(resolve,15));
  }
  reject(Object.assign(new Error('Campaign name: Use no more than 25 characters (26 entered).'),{status:422,code:'text_too_long'}));
  await assert.rejects(refused,/Campaign name.*25/);
  assert.match(h.queue.getSaveState().problem.message,/Campaign name.*25/);
  let persisted;
  h.reply=async(id,request)=>{persisted=structuredClone(request.authored);return{version:'v2'};};
  if(correctionTiming==='after refusal'){h.current.brief.name='Corrected campaign';h.queue.markChanged();}
  await new Promise(resolve=>setTimeout(resolve,25));
  assert.equal(h.calls.length,2);assert.equal(persisted.brief.name,'Corrected campaign');
  assert.equal(h.queue.getSaveState().phase,'saved');assert.equal(h.queue.getSaveState().problem,null);
  assert.equal(h.current.brief.name,'Corrected campaign');h.queue.stop();
});

test('unchanged over-limit work does not retry automatically or discard the typed value',async()=>{
  const h=harness();h.current.brief.name='W'.repeat(26);
  h.reply=async()=>{throw Object.assign(new Error('Campaign name: Use no more than 25 characters.'),{status:422,code:'text_too_long'});};
  await assert.rejects(h.queue.save(),/25/);
  h.queue.markChanged();await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(h.calls.length,1);assert.equal(h.current.brief.name,'W'.repeat(26));assert.equal(h.queue.isDirty(),true);h.queue.stop();
});

for(const buffer of [false,true])test(`local length refusal retains ${buffer?'off-tab buffer':'brief'} text without a request or failure banner, then correction autosaves`,async()=>{
 const invalid=value=>Array.from(buffer?value.form_drafts?.fields?.other?.['campaign-name']||'':value.brief.name).length>25?'Campaign name: Use no more than 25 characters.':null;
 const h=harness(invalid);
 const set=value=>{if(buffer)h.current.form_drafts={fields:{other:{'campaign-name':value}}};else h.current.brief.name=value;};
 set('🦊'.repeat(26));h.queue.markChanged();await new Promise(resolve=>setTimeout(resolve,20));
 assert.equal(await h.queue.saveCurrent(),false);assert.equal(h.calls.length,0);assert.equal(h.queue.isDirty(),true);
 assert.equal(h.queue.getSaveState().phase,'dirty');assert.equal(h.queue.getSaveState().problem,null);assert.match(h.queue.getSaveState().validationMessage,/Campaign name.*25/);
 set('Corrected');h.queue.markChanged();await new Promise(resolve=>setTimeout(resolve,20));
 assert.equal(h.calls.length,1);assert.equal(invalid(h.calls[0].value.authored),null);assert.equal(h.queue.isDirty(),false);assert.equal(h.queue.getSaveState().validationMessage,'');h.queue.stop();
});

test('over-limit newer typing during an accepted save stays in place and waits for correction',async()=>{
 const h=harness(value=>Array.from(value.brief.name).length>25?'Campaign name: Use no more than 25 characters.':null);let resolve;
 h.reply=()=>new Promise(yes=>resolve=yes);h.current.brief.name='First allowed';const saving=h.queue.save();
 h.current.brief.name='W'.repeat(26);h.queue.markChanged();resolve({version:'v2'});assert.equal(await saving,false);
 await new Promise(resolve=>setTimeout(resolve,20));assert.equal(h.calls.length,1);assert.equal(h.current.brief.name,'W'.repeat(26));assert.equal(h.queue.getSaveState().problem,null);
 h.reply=async()=>({version:'v3'});h.current.brief.name='Corrected newer';h.queue.markChanged();await new Promise(resolve=>setTimeout(resolve,20));
 assert.equal(h.calls.length,2);assert.equal(h.calls[1].value.authored.brief.name,'Corrected newer');assert.equal(h.queue.isDirty(),false);h.queue.stop();
});

test('opening the project and reading its save status never captures controls before the campaign view exists',()=>{
 const context=vm.createContext({structuredClone,setTimeout,clearTimeout});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
 const queue=context.projectSaveQueue({capture(){throw Error('Campaign view not mounted');},restore(){},send(){},validate(){throw Error('No authored capture is ready');}});
 queue.open('new-owner',{version:'v1',base:{},authored:{brief:{name:'Loaded'}}},{restoreState:false});
 assert.equal(queue.getSaveState().phase,'saved');assert.equal(queue.getSaveState().validationMessage,'');queue.stop();
});


test("Campaign autosave waits two seconds and never fires from Studio or a leaving Campaign timer", async () => {
  const timers = new Map(); let next = 0, eligible = true, writes = 0;
  const context = vm.createContext({ structuredClone, setTimeout(fn, delay) { const id = ++next; timers.set(id,{fn,delay}); return id; }, clearTimeout(id) {timers.delete(id);} });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/project.js", "utf8"), context);
  let authored={brief:{name:"Original"}};
  const queue=context.projectSaveQueue({capture:()=>structuredClone(authored),restore(){},canAutosave:()=>eligible,
    send:async()=>{writes++;return {version:"next"};}});
  queue.open("campaign",{version:"first",base:{},authored:structuredClone(authored)});
  authored.brief.name="One";queue.markChanged();assert.equal([...timers.values()][0].delay,2000);
  authored.brief.name="Two";queue.markChanged();assert.equal(timers.size,1,"new typing restarts the wait");
  eligible=false;[...timers.values()][0].fn();await flush();assert.equal(writes,0,"a timer cannot save after entering Studio");
  timers.clear();authored.brief.name="Studio local state";queue.markChanged();assert.equal(timers.size,0);
  eligible=true;queue.markChanged(true);[...timers.values()][0].fn();await flush();assert.equal(writes,1);
  queue.stop();
});

test("an accepted Studio Apply acknowledges server metadata with no follow-up authored save", async () => {
  const h=harness(undefined,()=>false);
  assert.ok(h.queue.hold());
  h.current.brief.framing={zoom:1.5};
  h.queue.applied({version:"applied",base:{revision_id:"new"},authored:structuredClone(h.current)});
  h.queue.release();await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(h.queue.isDirty(),false);assert.equal(h.calls.length,0);
  assert.equal(await h.queue.saveCurrent(),true);assert.equal(h.calls.length,0);
  h.queue.stop();
});

test("Campaign autosave status appears in the active panel, confirms once, and then hides", () => {
  const timers=new Map();let id=0,listen,root={hidden:false,textContent:""};
  const context=vm.createContext({window:{},setTimeout(fn){timers.set(++id,fn);return id;},clearTimeout(id){timers.delete(id);}});
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/save-state.js","utf8"),context);
  const view=context.window.MotifSave.mountAutosave(()=>root,{getSaveState:()=>({phase:"saved"}),subscribeSaveState(fn){listen=fn;return()=>{listen=null;};}});
  assert.equal(root.hidden,true);listen({phase:"dirty"});assert.equal(root.hidden,true);
  listen({phase:"saving"});assert.equal(root.textContent,"Auto-saving…");assert.equal(root.hidden,false);
  root={hidden:false,textContent:""};view.refresh();assert.equal(root.textContent,"Auto-saving…","form rerender retains current status");
  listen({phase:"saved"});assert.equal(root.textContent,"Saved");listen({phase:"saved"});assert.equal(timers.size,1);
  [...timers.values()][0]();assert.equal(root.hidden,true);assert.equal(root.textContent,"");
  listen({phase:"dirty"});listen({phase:"error"});assert.equal(root.hidden,true);
  view.destroy();assert.equal(listen,null);
});

test("explicit latest-version adoption preserves another tab's Brand, product, and audience edits",()=>{
 const context=vm.createContext({copy:structuredClone});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
 const before={name:'Campaign',audience:'Original',creative_direction:'Scene',brand:{name:'Brand',text_color:'#FFFFFF',palette:['#FFFFFF','#733B31']},markets:{'en-US':{copy:{message:'Old',cta:'Explore'}}},products:[{id:'one',name:'Product',description:'Old description',source:{path:'source.png'}}]};
 const after=structuredClone(before);after.brand.text_color='#733B31';after.audience='Updated audience';after.products[0].name='Updated product';
 const local=structuredClone(before);context.reconcileAppliedBrief(local,before,after);
 assert.deepEqual(local,after);
 const typing=structuredClone(before);typing.audience='Still typing';context.reconcileAppliedBrief(typing,before,after);
 assert.equal(typing.audience,'Still typing');assert.equal(typing.brand.text_color,'#733B31');
});

test('all-products Studio wording preserves canonical empty override keys without a phantom Campaign save', () => {
  for (const newerTyping of [false, true]) {
    const before={campaign_id:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',brand:{},markets:{'en-US':{copy:{message:'Shared before',cta:'Explore'}}},products:[{id:'a',copy_overrides:{'en-US':{message:'First override'}}},{id:'b',copy_overrides:{'en-US':{message:'Second override'}}}]};
    const after=structuredClone(before);after.markets['en-US'].copy.message='Warm nights start here';
    for (const product of after.products) product.copy_overrides={'en-US':{}};
    const saved={version:'v1',base:{},authored:{brief:before}};
    const context=vm.createContext({structuredClone,copy:structuredClone,setTimeout,clearTimeout,draft:structuredClone(before),saved});
    vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),context);
    vm.runInContext("projectOwner=saved.authored.brief.campaign_id;projectQueue=projectSaveQueue({capture:()=>({brief:draft}),restore(){},send(){throw Error('No extra Campaign save')},delay:10000});projectQueue.open(projectOwner,saved)",context);
    if (newerTyping) context.draft.products[0].copy_overrides['en-US'].cta='Still typing my CTA';
    const current={...saved,version:'v2',authored:{brief:after}};
    context.receiveProject({value:{campaign_id:before.campaign_id},project:current},{project:saved},{applied:true});
    assert.deepEqual(context.draft.products[1].copy_overrides,{'en-US':{}});
    assert.deepEqual(context.draft.products[0].copy_overrides,newerTyping?{'en-US':{cta:'Still typing my CTA'}}:{'en-US':{}});
    assert.equal(vm.runInContext('projectQueue.isDirty()',context),newerTyping);
    vm.runInContext('projectQueue.stop()',context);
  }
});


test('rename and rule capture save recoverable proposals without applying them',()=>{
 const field={id:'rename-name',type:'text',value:'Proposed name'};
 const root={querySelectorAll:()=>[field]};
 const c=vm.createContext({structuredClone,setTimeout,clearTimeout,copy:structuredClone,
  draft:{campaign_id:'x',name:'Original name',markets:{'en-US':{phrase_rules:[{phrase:'Original rule'}]}},products:[]},
  C:{products:[]},page:'review',tab:'copy',lang:'en',modal:'rename',currentFormat:0,currentProduct:0,ruleIndex:0,
  MARKET:{en:'en-US'},imageEditorRoot:()=>root,$:()=>root});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/project.js','utf8'),c);
 let captured=c.captureProject();assert.equal(captured.brief.name,'Original name');
 assert.equal(captured.form_drafts.fields.rename['rename-name'],'Proposed name');
 c.clearProjectTarget();assert.equal(c.captureProject().brief.name,'Original name');
 c.modal='rule';field.id='mw-rule-phrase';field.value='Proposed rule';
 captured=c.captureProject();assert.equal(captured.brief.markets['en-US'].phrase_rules[0].phrase,'Original rule');
 assert.equal(captured.form_drafts.fields['rule/en/0']['mw-rule-phrase'],'Proposed rule');
 c.clearProjectTarget();assert.equal(c.draft.markets['en-US'].phrase_rules[0].phrase,'Original rule');
});
