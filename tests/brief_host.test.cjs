// Controlled host boundaries for the shipped Brief adapter, with no provider.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {EventEmitter, once} = require('node:events');
function harness() {
  const pending = [], timers = new Map(), saveListeners = new Set(); let next = 1, saveState = null;
  const requests = new EventEmitter();
  async function waitForRequests(count) {
    while (pending.length < count) await once(requests, 'submitted');
  }
  const c = vm.createContext({
    AbortController, structuredClone, TextEncoder, crypto:require('node:crypto').webcrypto,
    setTimeout(fn) { const id=next++; timers.set(id,fn); return id; }, clearTimeout(id) { timers.delete(id); },
    getSaveState:()=>saveState,subscribeSaveState(fn){saveListeners.add(fn);fn(saveState);return()=>saveListeners.delete(fn);},
    operation:null, copy:structuredClone, page:'campaign', tab:'brand', lang:'en', productLimit:5,
    currentProduct:0,currentFormat:0, LANG:{en:'English (United States)',es:'Spanish (Mexico)'}, MARKET:{en:'en-US',es:'es-MX'},FMT:[{ratio:'1:1'}],
    C:{products:[{id:'a'}]}, projectBuffers:{form_drafts:{},feedback:{}},
    model:{version:'v1',manifest:{revision_id:'r1'},concept_previews:{},attachment_hints:{}},
    draft:{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4', name:'Campaign', audience:'Audience',creative_direction:'Scene',
      brand:{name:'Brand',text_color:'#FFFFFF',palette:['#FFFFFF'],logo_path:'Campaigns/Test-12345678/Assets/Products/logo.png',logo_provenance:{}},copy_provenance:{label:'Source',rights:'Rights'},
      markets:{'en-US':{copy:{message:'English',cta:null}},'es-MX':{copy:{message:'Español',cta:null}}},
      products:[{id:'a',name:'Alpha',description:'A',source:{role:'concept'},framing:{},copy_overrides:{}}]},
    assetURL:ref=>ref ? '/asset':null,markDirty(){},toast(){},$:()=>({focus(){},content:'csrf'}),
    api:(url,body,options)=>new Promise((resolve,reject)=>{
      pending.push({url,body,options,resolve,reject});
      requests.emit('submitted');
    }),
    wordingCheckPhase:null, paintWordingAvailability(){},
    showCapacity(){},dispatchAction:async()=>{},updateURL(){},render(){},flushCurrentWork:async()=>true,
  });
  const run=code=>vm.runInContext(code,c);
  c.window=c;
  run(readFileSync('src/campaign_studio/static/motif/wording.js','utf8'));
  run(readFileSync('src/campaign_studio/static/motif/brief-flow.js','utf8'));
  run(readFileSync('src/campaign_studio/static/motif/brief-host.js','utf8'));
  run(`briefFacts.campaign='292e9e84-eb00-5fdb-87e8-423f5e51fac4';briefFacts.logos.set(draft.brand.logo_path,{width:200,height:80,type:'PNG'});
    briefHost={owner:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',productId:'a',ratio:'1:1',generation:0,upload:0,dead:false,view:{render(){},preview(request){return requestBriefPreview(briefHost,request)}}};
    refreshBriefFlow();`);
  return {c,run,pending,timers,waitForRequests,publishSaveState(state){saveState=state;for(const listener of saveListeners)listener(state);},plain:code=>JSON.parse(run('JSON.stringify('+code+')'))};
}
function previewView(h, decode = () => Promise.resolve()) {
  let renders = 0, stage = {}, caption = {};
  h.c.document = {activeElement:null};
  h.c.Image = class { decode() { return decode(this.src); } };
  h.c.root = {
    addEventListener(){}, contains(){return false;}, querySelectorAll(){return [];},
    querySelector(selector){return selector==='[data-bf-preview-stage]'?stage:selector==='[data-bf-preview-caption]'?caption:null;},
    set innerHTML(_value){renders++;stage={innerHTML:''};caption={innerHTML:''};},
  };
  h.run('briefHost.view=MotifBriefFlow.mount(root,{onPreview:request=>requestBriefPreview(briefHost,request)});briefHost.previewSignature=null;refreshBriefFlow()');
  return {renders:()=>renders, stage:()=>stage.innerHTML};
}
async function respondPreview(h, request, extra = {}) {
  const digest = await h.run('briefRequestDigest('+JSON.stringify(request.body)+')');
  request.resolve({request_digest:digest,target:{campaign_id:request.body.brief.campaign_id,
    product_id:request.body.product_id,market:request.body.market,ratio:request.body.ratio},
    status:'ready',kind:'typography',image:{url:'/current-preview'},fitted_outputs:[],...extra});
}

test('Brand preview renders current fields without waiting for or forcing Campaign autosave', async () => {
  const h=harness(), view=previewView(h);
  h.publishSaveState({phase:'dirty'});
  h.c.saveProject=()=>assert.fail('a preview cannot save early');
  h.run("changeBriefField(briefHost,'brandName','Updated Brand');flushBriefPreview(briefHost)");
  await h.waitForRequests(1);
  assert.equal(h.pending[0].body.version,'v1');
  assert.equal(h.pending[0].body.brief.brand.name,'Updated Brand');
  await respondPreview(h,h.pending[0]);await h.run('briefHost.flight');
  assert.equal(h.run('briefHost.vm.preview.phase'),'ready');
  const renders=view.renders();
  h.run("model.version='v2';model.manifest.revision_id='v2';refreshBriefFlow()");
  h.publishSaveState({phase:'saved'});
  assert.equal(view.renders(),renders,'an acknowledgement does not clear decoded pixels');
  assert.equal(h.pending.length,1);assert.equal(h.timers.size,0);
});

test('typing during an active preview coalesces the newest fields and ignores the old response', async () => {
  const h=harness();previewView(h);h.publishSaveState({phase:'saving'});
  h.c.saveProject=()=>assert.fail('a preview cannot start another save');
  h.run("changeBriefField(briefHost,'message','First');flushBriefPreview(briefHost)");
  await h.waitForRequests(1);
  h.run("changeBriefField(briefHost,'message','Latest');flushBriefPreview(briefHost);model.version='v3';refreshBriefFlow()");
  await respondPreview(h,h.pending[0]);await h.waitForRequests(2);
  assert.equal(h.pending[1].body.version,'v3');
  assert.equal(h.pending[1].body.brief.markets['en-US'].copy.message,'Latest');
  await respondPreview(h,h.pending[1]);await h.run('briefHost.flight');
  assert.equal(h.run('briefHost.vm.preview.phase'),'ready');assert.equal(h.timers.size,0);
});

test('other product source facts during decode update cards without restarting the selected image request', async () => {
  const h=harness();let finishDecode,started;
  h.run("draft.products.push({id:'b',name:'Beta',source:{role:'concept'},framing:{},copy_overrides:{}})");
  const decoding=new Promise(resolve=>started=resolve);
  const view=previewView(h,()=>{started();return new Promise(resolve=>finishDecode=resolve);});
  h.c.viewModel=()=>{};h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
  const flight=h.run('briefHost.flight');await respondPreview(h,h.pending[0]);await decoding;
  const generation=h.run('briefHost.generation'),renders=view.renders();
  const facts=h.run("refreshBriefSources('v1',copy(draft))");
  h.pending[1].resolve({version:'v1',value:{campaign_id:h.c.draft.campaign_id},concept_previews:{b:'/restored-source'}});
  await facts;
  assert.equal(h.run('briefHost.generation'),generation);assert.equal(view.renders(),renders);
  finishDecode();await flight;
  assert.equal(h.run('briefHost.vm.preview.phase'),'ready');
  assert.equal(h.run('briefHost.vm.products[1].image'),'/restored-source');
  assert.equal(h.pending.filter(p=>p.url.endsWith('/brief-preview')).length,1);
  assert.equal(h.timers.size,0);
});

test('changed selected imagery or production run invalidates a ready preview', async () => {
  for(const change of ['source','run']) {
    const h=harness();previewView(h);h.c.viewModel=()=>{};
    h.run("model.run={id:'run-1'};refreshBriefFlow();flushBriefPreview(briefHost)");
    await h.waitForRequests(1);await respondPreview(h,h.pending[0]);await h.run('briefHost.flight');
    const generation=h.run('briefHost.generation');
    if(change==='source') {
      const facts=h.run("refreshBriefSources('v1',copy(draft))");
      h.pending[1].resolve({version:'v1',value:{campaign_id:h.c.draft.campaign_id},run:{id:'run-1'},concept_previews:{a:'/new-source'}});
      await facts;
    } else h.run(change==='revision'?"model.manifest.revision_id='r2';refreshBriefFlow()":"model.run.id='run-2';refreshBriefFlow()");
    assert.ok(h.run('briefHost.generation')>generation,change);
    assert.equal(h.run('briefHost.vm.preview.phase'),'pending');
    h.run('flushBriefPreview(briefHost)');
    await h.waitForRequests(change==='source'?3:2);
    await respondPreview(h,h.pending.at(-1),{kind:'composition'});await h.run('briefHost.flight');
    assert.equal(h.pending.filter(p=>p.url.endsWith('/brief-preview')).length,2);
    assert.equal(h.run('briefHost.vm.preview.kind'),'composition');
  }
});

test('selection-only preview changes do not write an already saved campaign', async () => {
  const h=harness();previewView(h);
  h.c.getSaveState=()=>({phase:'saved'});h.c.saveProject=()=>assert.fail('selection must not save');
  await h.run("briefAction(briefHost,'format:9:16')");h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(1);assert.equal(h.pending[0].body.ratio,'9:16');
  await respondPreview(h,h.pending[0]);await h.run('briefHost.flight');
  assert.equal(h.run('briefHost.vm.preview.phase'),'ready');
});

test('preview never retries a failed autosave and an abandoned response is ignored', async () => {
  for(const phase of ['error','conflict']) {
    const h=harness();previewView(h);h.publishSaveState({phase,problem:{phase}});
    h.c.saveProject=()=>assert.fail('no hidden save retry');
    h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
    await respondPreview(h,h.pending[0]);await h.run('briefHost.flight');
    assert.equal(h.pending.length,1);
  }
  const h=harness();previewView(h);h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(1);const flight=h.run('briefHost.flight');
  h.run('briefHost.dead=true;briefHost.view.destroy()');await respondPreview(h,h.pending[0]);await flight;
  assert.equal(h.run('briefHost.vm.preview.phase'),'pending');
});

test('an own save racing a read permits one new-version preview, never adopts an external version',async()=>{
  const h=harness();previewView(h);h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
  h.run("model.version='v2';refreshBriefFlow()");
  h.pending[0].reject(Object.assign(Error('stale'),{status:409}));await h.waitForRequests(2);
  assert.equal(h.pending[1].body.version,'v2');await respondPreview(h,h.pending[1]);await h.run('briefHost.flight');
  assert.equal(h.run('briefHost.vm.preview.phase'),'ready');
  h.run("changeBriefField(briefHost,'message','Changed');flushBriefPreview(briefHost)");await h.waitForRequests(3);
  h.pending[2].reject(Object.assign(Error('stale elsewhere'),{status:409}));await h.run('briefHost.flight');
  assert.equal(h.pending.length,3);assert.equal(h.run('briefHost.vm.preview.phase'),'failed');
});

test('a preview PNG or layer expired by our autosave refreshes once without clearing current pixels',async()=>{
  for(const expired of ['image','layer']) {
    const h=harness();let failDecode,started;
    const decoding=new Promise(resolve=>started=resolve);
    const view=previewView(h,url=>url===(expired==='image'?'/old-image':'/old-base')
      ?(started(),new Promise((_resolve,reject)=>failDecode=reject)):Promise.resolve());
    h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
    const flight=h.run('briefHost.flight');
    await respondPreview(h,h.pending[0],{image:{url:'/old-image'},layers:{base_url:'/old-base',text_blocks:{}}});
    await decoding;
    const stage=h.c.root.querySelector('[data-bf-preview-stage]');
    stage.innerHTML='current recolored pixels';stage.firstElementChild={tagName:'CANVAS'};
    const renders=view.renders();
    h.run("model.version='v2';refreshBriefFlow()");
    assert.equal(h.pending.length,1,'a normal acknowledgement does not duplicate the request');
    failDecode(Error('404 after own autosave'));await h.waitForRequests(2);
    assert.equal(h.pending[1].body.version,'v2');
    assert.equal(view.stage(),'current recolored pixels');assert.equal(view.renders(),renders);
    await respondPreview(h,h.pending[1],{image:{url:'/new-image'}});await flight;
    assert.equal(h.run('briefHost.vm.preview.phase'),'ready');assert.equal(h.pending.length,2);
    assert.match(view.stage(),/new-image/);assert.equal(h.timers.size,0);
  }
});

test('a failed image decode without our own newer save remains actionable and does not retry',async()=>{
  const h=harness(),view=previewView(h,()=>Promise.reject(Error('image unavailable')));
  h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
  await respondPreview(h,h.pending[0]);await h.run('briefHost.flight');
  assert.equal(h.pending.length,1);assert.equal(h.run('briefHost.vm.preview.phase'),'failed');
  assert.match(view.stage(),/Retry preview/);
});

test('a late expired image cannot retry or replace a newer edit, target, or abandoned component',async()=>{
  for(const change of ['edit','target','leave']) {
    const h=harness();let failDecode,started;
    h.run("draft.products.push({id:'b',name:'Beta',source:{role:'concept'},framing:{},copy_overrides:{}})");
    const decoding=new Promise(resolve=>started=resolve);
    previewView(h,url=>url==='/old-image'?(started(),new Promise((_resolve,reject)=>failDecode=reject)):Promise.resolve());
    h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
    const flight=h.run('briefHost.flight');await respondPreview(h,h.pending[0],{image:{url:'/old-image'}});await decoding;
    h.run("model.version='v2';refreshBriefFlow()");
    if(change==='edit') h.run("changeBriefField(briefHost,'message','Newest wording');flushBriefPreview(briefHost)");
    if(change==='target') h.run("changeBriefField(briefHost,'previewProduct','b');flushBriefPreview(briefHost)");
    if(change==='leave') h.run('briefHost.dead=true;briefHost.view.destroy()');
    failDecode(Error('obsolete image'));await flight;
    if(change==='leave'){assert.equal(h.pending.length,1);continue;}
    await h.waitForRequests(2);
    assert.equal(h.pending.length,2,'the second request belongs to the new edit, not the expired one');
    if(change==='edit') assert.equal(h.pending[1].body.brief.markets['en-US'].copy.message,'Newest wording');
    if(change==='target') assert.equal(h.pending[1].body.product_id,'b');
    await respondPreview(h,h.pending[1]);await h.run('briefHost.flight');
    assert.equal(h.run('briefHost.vm.preview.phase'),'ready');assert.equal(h.pending.length,2);
  }
});

test('invalid raw input remains untouched and retry still requests a fresh checked preview', async () => {
  const h=harness();previewView(h);h.c.getSaveState=()=>({phase:'dirty',validationMessage:'Shorten the audience.'});
  h.c.saveProject=()=>assert.fail('invalid authored values must not be submitted');
  h.run("changeBriefField(briefHost,'defaultColor','BROKEN');flushBriefPreview(briefHost)");
  assert.equal(h.pending.length,0);assert.equal(h.run('draft.brand.text_color'),'BROKEN');
  h.run("changeBriefField(briefHost,'defaultColor','#FFFFFF');flushBriefPreview(briefHost)");
  await h.waitForRequests(1);h.pending[0].reject(new Error('Preview failed'));
  await h.run('briefHost.flight');assert.equal(h.run('briefHost.vm.preview.phase'),'failed');
  await h.run("briefAction(briefHost,'retry-preview')");h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(2);await respondPreview(h,h.pending[1]);await h.run('briefHost.flight');
  assert.equal(h.run('briefHost.vm.preview.phase'),'ready');
});

test('coalescing preserves exact response digest and target checks', async () => {
  for(const field of ['digest','campaign_id','product_id','market','ratio']) {
    const h=harness();previewView(h);h.run('flushBriefPreview(briefHost)');await h.waitForRequests(1);
    const request=h.pending[0],target={campaign_id:h.c.draft.campaign_id,product_id:'a',market:'en-US',ratio:'1:1'};
    if(field!=='digest')target[field]='different';
    await respondPreview(h,request,field==='digest'?{request_digest:'wrong'}:{target});
    await h.run('briefHost.flight');assert.equal(h.run('briefHost.vm.preview.phase'),'failed',field);
  }
});

test('custom color notice follows the selected image even when its explicit color matches the default', async () => {
  const h=harness();h.run("draft.products[0].framing={'en-US':{'1:1':{text_placements:{message:{color:'#FFFFFF'}}}}};draft.products.push({id:'b',name:'Beta',source:{role:'concept'},framing:{},copy_overrides:{}});refreshBriefFlow()");
  assert.equal(h.run('briefHost.vm.customPreviewColors'),true);
  await h.run("briefAction(briefHost,'format:9:16')");assert.equal(h.run('briefHost.vm.customPreviewColors'),false);
  await h.run("briefAction(briefHost,'format:1:1');briefAction(briefHost,'language:es-MX')");assert.equal(h.run('briefHost.vm.customPreviewColors'),false);
  await h.run("briefAction(briefHost,'language:en-US');changeBriefField(briefHost,'previewProduct','b')");assert.equal(h.run('briefHost.vm.customPreviewColors'),false);
  h.run("changeBriefField(briefHost,'previewProduct','a')");assert.equal(h.run('briefHost.vm.customPreviewColors'),true);
});

test('using the campaign default clears only selected image colors and retains its other text settings', async () => {
  const h=harness();let dirty=0;h.c.markDirty=()=>dirty++;
  h.run(`draft.products[0].framing={'en-US':{'1:1':{text_placements:{message:{color:'#FFFFFF',left:.2,font:'barlow'},cta:{color:'#FFFFFF',top:.7,size:30}}},'9:16':{text_placements:{message:{color:'#FFFFFF',left:.3}}}},'es-MX':{'1:1':{text_placements:{cta:{color:'#FFFFFF',top:.4}}}}};
    draft.products.push({id:'b',name:'Beta',source:{role:'concept'},framing:{'en-US':{'1:1':{text_placements:{message:{color:'#FFFFFF'}}}}},copy_overrides:{}});refreshBriefFlow()`);
  const expected=h.plain('draft');
  expected.products[0].framing['en-US']['1:1'].text_placements.message.color=null;
  expected.products[0].framing['en-US']['1:1'].text_placements.cta.color=null;
  await h.run("briefAction(briefHost,'use-preview-default')");
  assert.deepEqual(h.plain('draft'),expected);assert.equal(dirty,1);
  assert.equal(h.run('briefHost.vm.customPreviewColors'),false);
});
test('Brand controls cannot add edits or color-reset intent while images are updating', async () => {
  const h=harness();
  h.run("draft.products[0].framing={'en-US':{'1:1':{text_placements:{message:{color:'#112233'}}}}}; brandUpdating=true; refreshBriefFlow()");
  const before=h.plain('draft');
  h.c.markDirty=()=>assert.fail('must not add work during the accepted image update');
  h.run("changeBriefField(briefHost,'defaultColor','#112233')");
  await h.run("briefAction(briefHost,'use-preview-default')");
  await h.run("briefAction(briefHost,'remove-logo')");
  assert.deepEqual(h.plain('draft'),before);
  assert.equal(h.run('projectBuffers.form_drafts.brand_color_defaults'),undefined);
  h.run('brandUpdating=false');
  h.c.markDirty=()=>{};
  await h.run("briefAction(briefHost,'use-preview-default')");
  assert.equal(h.run("draft.products[0].framing['en-US']['1:1'].text_placements.message.color"),null);
});
test('raw default is preserved without silently expanding palette; explicit use normalizes and deduplicates',async()=>{
  const h=harness();
  h.run("changeBriefField(briefHost,'defaultColor',' 61177c ',{commit:false})");
  assert.equal(h.run('draft.brand.text_color'),' 61177c ');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF']);
  assert.equal(h.run('briefHost.vm.preview.phase'),'invalid');
  await h.run("briefAction(briefHost,'add-default')");
  assert.equal(h.run('draft.brand.text_color'),'#61177C');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF','#61177C']);
  h.run("changeBriefField(briefHost,'palette-1','ffffff',{commit:true})");
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF']);
  assert.equal(h.run('briefHost.vm.preview.phase'),'invalid');
});
test('Brand default color saves directly and palette errors retain a clear remedy', async()=>{
  const h=harness();
  h.run("changeBriefField(briefHost,'defaultColor','611')");
  assert.ok(h.run('briefBrandColorProblem()'));
  h.run("changeBriefField(briefHost,'defaultColor','61177c')");
  assert.match(h.run('briefBrandColorProblem()'),/palette/);
  const html=h.run('MotifBriefFlow.renderBrief(briefViewModel(briefHost))');
  assert.doesNotMatch(html,/data-bf-action="(?:update-images|use-default)"/);
  assert.match(html,/Add default back/);
  await h.run("briefAction(briefHost,'restore-default')");
  assert.equal(h.run('briefBrandColorProblem()'),'');
  h.run("changeBriefField(briefHost,'defaultColor','#FFFFFF')");
  assert.equal(h.run('briefBrandColorProblem()'),'');
  assert.equal(h.run('draft.brand.text_color'),'#FFFFFF');
});

test('Campaign can continue to Production with zero products and unfinished values without preparing a plan',async()=>{
  const h=harness(), actions=[];
  h.c.dispatchAction=async action=>actions.push(action.action);
  h.run("draft.products=[];draft.brand.text_color='BROKEN';refreshBriefFlow()");
  await h.run("briefAction(briefHost,'production')");
  assert.deepEqual(actions,['nav:production']);
  assert.equal(h.pending.length,0);
  assert.equal(h.run('draft.brand.text_color'),'BROKEN');
  const html=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'copy'})");
  assert.match(html,/bf-tab-products/);
  assert.match(html,/Next: Production/);
});
test('independent language raw values survive mount and only explicit same-language edits resolve duplicates',()=>{
  const h=harness();
  h.run(`projectBuffers.form_drafts.fields={'brief/brand/en':{'brand-name':'BLAH','text-color':'#61177c','brand-palette':'#FFFFFF,#CBCBCB,#ffffff'},'brief/brand/es':{'brand-name':'HOLD','text-color':'#61177c','brand-palette':'ffffff,cbcbcb'}};refreshBriefFlow()`);
  const before=h.plain('projectBuffers.form_drafts');
  assert.equal(h.run('briefHost.vm.brandName'),'BLAH');
  assert.equal(h.run('briefHost.vm.defaultDraft'),'#61177c');
  assert.deepEqual(h.plain('projectBuffers.form_drafts'),before);
  h.run("changeBriefField(briefHost,'brandName','Current English',{commit:false})");
  assert.deepEqual(h.plain("projectBuffers.form_drafts.fields['brief/brand/es']"),before.fields['brief/brand/es']);
  h.run("lang='es';refreshBriefFlow()"); assert.equal(h.run('briefHost.vm.brandName'),'HOLD');
});
test('palette changes preserve custom text colors across other products and languages and give a named remedy',async()=>{
  const h=harness();
  h.run(`draft.brand.palette.push('#61177C');draft.products.push({id:'b',name:'Beta',source:{role:'concept'},framing:{'es-MX':{'9:16':{text_placements:{cta:{color:'#61177C'}}}}}});refreshBriefFlow()`);
  await h.run("briefAction(briefHost,'remove-color:1')");
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF','#61177C']);
  assert.match(h.run('briefHost.vm.paletteMessage'),/Beta.*Spanish \(Mexico\).*9:16/);
  assert.equal(h.run('briefHost.vm.paletteAction.action'),'open-studio');
  assert.equal(h.run('briefHost.paletteTarget.product'),'b');
  h.run("changeBriefField(briefHost,'palette-1','112233',{commit:true})");
  assert.equal(h.run("draft.products[1].framing['es-MX']['9:16'].text_placements.cta.color"),'#61177C');
  assert.equal(h.run('briefHost.vm.preview.phase'),'invalid');
  assert.equal(h.run('briefHost.vm.paletteAction.action'),'restore-used-color');
});
test('component rerender blur does not become a second edit or recursive render',()=>{
  const h=harness(); let renders=0;
  h.c.rerender=()=>{renders++;h.run("changeBriefField(briefHost,'defaultColor','BROKEN',{commit:true})")};
  h.run("briefHost.view.render=rerender;draft.audience='Changed audience';refreshBriefFlow()");
  assert.equal(renders,1);assert.equal(h.run('draft.brand.text_color'),'#FFFFFF');
});
test('acknowledged Brief refresh replaces only authoritative concept facts and updates current cards', async () => {
  const h = harness(); let views = 0;
  h.c.viewModel = () => views++;
  h.run("model.concept_previews={a:'/old-master'};model.run={id:'run'}");
  const before = h.plain('draft');
  const refreshing = h.run("refreshBriefSources('v1', copy(draft))");
  assert.equal(h.pending[0].url, '/api/motif/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4?after_edit=true');
  h.pending[0].resolve({version:'v1',value:{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'},run:{id:'run'},concept_previews:{}});
  await refreshing;
  assert.deepEqual(h.plain('model.concept_previews'), {});
  assert.equal(h.run('briefHost.vm.products[0].image'), null);
  assert.deepEqual(h.plain('draft'), before);
  const restored = h.run("refreshBriefSources('v1', copy(draft))");
  h.pending[1].resolve({version:'v1',value:{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'},run:{id:'run'},concept_previews:{a:'/restored-master'}});
  await restored;
  assert.equal(h.run('briefHost.vm.products[0].image'), '/restored-master');
  assert.equal(views, 2);
});
test('concept refresh refuses newer typing, save versions, owners, run identities, and failed reads', async () => {
  const early = harness();
  early.run("globalThis.acknowledgedBrief=copy(draft);draft.audience='Typed before acknowledgement'");
  await early.run("refreshBriefSources('v1', acknowledgedBrief)");
  assert.equal(early.pending.length, 0, 'an old Save receipt cannot verify newer raw inputs');
  for (const kind of ['typing','version','owner','response-version','run','failure']) {
    const h = harness(); h.c.viewModel = () => assert.fail('stale result changed the view');
    h.run("model.concept_previews={a:'/kept-master'};model.run={id:'run'}");
    const refreshing = h.run("refreshBriefSources('v1', copy(draft))");
    if (kind === 'typing') h.run("draft.audience='Newer unsaved audience'");
    if (kind === 'version') h.run("model.version='v2'");
    if (kind === 'owner') h.run("draft={...draft,campaign_id:'3c073361-c0a3-55d6-b62d-a2ca9ce2ba87'};model={...model}");
    if (kind === 'failure') h.pending[0].reject(new Error('Read failed'));
    else h.pending[0].resolve({version:kind === 'response-version'?'other-tab':'v1',
      value:{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'},run:{id:kind === 'run'?'new-run':'run'},concept_previews:{}});
    await refreshing;
    assert.equal(h.run('model.concept_previews.a'), '/kept-master', kind);
    assert.equal(h.pending.length, 1, 'no automatic write or read retry');
    if (kind === 'typing') assert.equal(h.run('draft.audience'), 'Newer unsaved audience');
  }
});
test('acknowledged source refresh updates Production in place without resetting its coverage or scroll', async () => {
  const h = harness(); let refreshed = 0;
  h.c.viewModel = () => {};
  h.c.render = () => assert.fail('source refresh rebuilt the workspace');
  h.c.refreshProductionStatus = () => refreshed++;
  h.run("page='production';model.run={id:'run'}");
  const refreshing = h.run("refreshBriefSources('v1', copy(draft))");
  h.pending[0].resolve({version:'v1',value:{campaign_id:h.c.draft.campaign_id},run:{id:'run'},concept_previews:{a:'/current'}});
  await refreshing;
  assert.equal(refreshed, 1);
  assert.equal(h.run('model.concept_previews.a'), '/current');
});
test('preview queue coalesces rapid typing and rejects stale request identity',async()=>{
  const h=harness();
  h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(1);
  assert.equal(h.pending.length,1);
  for(const word of ['First','Second','Latest']) h.run(`changeBriefField(briefHost,'message',${JSON.stringify(word)},{commit:false});flushBriefPreview(briefHost)`);
  assert.equal(h.pending.length,1);
  h.pending[0].resolve({}); // Superseded before target/digest consumption.
  await assert.rejects(h.run('briefHost.flight'));
  await h.waitForRequests(2);
  assert.equal(h.pending.length,2);
  assert.equal(h.pending[1].body.brief.markets['en-US'].copy.message,'Latest');
  const request=h.pending[1];
  const digest=await h.run('briefRequestDigest('+JSON.stringify(request.body)+')');
  request.resolve({request_digest:digest,target:{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',product_id:'a',market:'en-US',ratio:'1:1'},status:'ready',kind:'typography',image:{url:'/preview'},fitted_outputs:[]});
  await h.run('briefHost.flight');
});

test('producer action drains the owned preview and overtakes queued background work', async () => {
  const h = harness();
  h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(1);
  assert.equal(h.pending.length,1);
  h.run("changeBriefField(briefHost,'message','Latest',{commit:false});flushBriefPreview(briefHost)");
  const action = h.run("withBriefPreviewPaused(()=>api('/plan',{}))");
  h.run("changeBriefField(briefHost,'message','Still typing',{commit:false});flushBriefPreview(briefHost)");
  assert.equal(h.pending.length,1,'no producer request until the owned render settles');
  h.pending[0].resolve({});
  await h.waitForRequests(2);
  assert.deepEqual(h.pending.map(p=>p.url),['/api/motif/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4/brief-preview','/plan']);
  assert.equal(h.run('briefPreviewPaused()'),true);
  h.run('refreshBriefFlow();flushBriefPreview(briefHost)');
  assert.equal(h.pending.length,2,'background preview stays paused for the producer action');
  h.pending[1].resolve({ok:true});
  assert.equal((await action).ok,true);
  assert.equal(h.run('briefPreviewPaused()'),false);
  assert.equal(h.run("briefHost.vm.copy['en-US'].message"),'Still typing');
});

test('a held campaign action does not suppress another campaign preview or release its active pause',async()=>{
  const h=harness();
  const a=h.run("withBriefPreviewPaused(()=>api('/plan-A',{}))");
  assert.equal(h.run('briefPreviewPaused()'),true);
  h.run("draft.campaign_id='62220994-1d32-5713-80d2-ac0b6ff0baf3';briefHost.owner=draft.campaign_id;refreshBriefFlow();flushBriefPreview(briefHost)");
  await h.waitForRequests(2);
  assert.equal(h.run('briefPreviewPaused()'),false);
  const preview=h.pending[1];assert.match(preview.url,/62220994-1d32-5713-80d2-ac0b6ff0baf3\/brief-preview$/);
  const digest=await h.run('briefRequestDigest('+JSON.stringify(preview.body)+')');
  preview.resolve({request_digest:digest,target:{campaign_id:h.c.draft.campaign_id,product_id:'a',market:'en-US',ratio:'1:1'},status:'ready',kind:'typography',image:{url:'/preview-B'},fitted_outputs:[]});
  await h.run('briefHost.flight');
  const b=h.run("withBriefPreviewPaused(()=>api('/plan-B',{}))");
  const bUpload=h.run("withBriefPreviewPaused(()=>api('/upload-B',{}))");
  h.pending[0].resolve({ok:true});await a;
  assert.equal(h.run('briefPreviewPaused()'),true,'A cannot release B');
  h.run('refreshBriefFlow();flushBriefPreview(briefHost)');assert.equal(h.pending.length,4);
  h.pending[2].resolve({ok:true});await b;
  assert.equal(h.run('briefPreviewPaused()'),true,'same-campaign upload still owns its pause');
  h.pending[3].resolve({ok:true});await bUpload;
  assert.equal(h.run('briefPreviewPaused()'),false);
  assert.equal(h.run('briefPreviewActions.size'),0);
});

test('preview failure does not retry the action or hide genuine concurrent-operation refusal', async () => {
  const h = harness();
  h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(1);
  const action = h.run("withBriefPreviewPaused(()=>api('/plan',{}))");
  h.pending[0].reject(new Error('Controlled preview failure'));
  await h.waitForRequests(2);
  h.pending[1].reject(Object.assign(new Error('Other operation is active'),{status:409,code:'busy'}));
  await assert.rejects(action,/Other operation is active/);
  assert.equal(h.pending.length,2);
  assert.equal(h.run('briefPreviewPaused()'),false);
});

test('a logo selected before navigation never uploads after its old preview drains', async () => {
  const h = harness();
  h.run('flushBriefPreview(briefHost)');
  await h.waitForRequests(1);
  const action = h.run("attachBriefLogo(briefHost,{name:'logo.jpg',type:'image/jpeg'})");
  h.run("briefHost.dead=true;draft.campaign_id='62220994-1d32-5713-80d2-ac0b6ff0baf3'");
  h.pending[0].resolve({});
  await action;
  assert.equal(h.pending.length,1);
  assert.equal(h.run('draft.brand.logo_path'),'Campaigns/Test-12345678/Assets/Products/logo.png');
});

test('Brand logo rejection uses the existing image dialog and preserves prior artwork and raw fields', async () => {
  const h = harness(), failures = [];
  h.c.imageInputFailure = (error,file,logo,owner)=>failures.push({error,file,logo,owner});
  h.run("changeBriefField(briefHost,'defaultColor','.ddd',{commit:false})");
  const before = h.plain('draft');
  const action = h.run("attachBriefLogo(briefHost,{name:'large.png',type:'image/png',size:4096})");
  const error = {message:'Choose an image up to 40 megapixels.',input_image:{kind:'pixel_count',width:7000,height:6000}};
  h.pending[0].reject(error); await action;
  assert.equal(failures.length,1);
  assert.equal(failures[0].error,error);
  assert.equal(failures[0].logo,true);
  assert.equal(failures[0].owner,h.c.draft);
  assert.equal(h.run('briefHost.logoMessage'),'');
  assert.deepEqual(h.plain('draft'),before);
});
test('late logo upload cannot assign to a closed or newer owner; JPEG bytes are sent to authoritative role validation',async()=>{
  const h=harness();
  const first=h.run("attachBriefLogo(briefHost,{name:'logo.jpg',type:'image/jpeg'})");
  assert.equal(h.pending[0].options.headers['X-Image-Role'],'logo');
  h.run('briefHost.dead=true');
  h.pending[0].resolve({reference:'Campaigns/Test-12345678/Assets/Products/new.jpg',provenance:{label:'New'}});
  await first;assert.equal(h.run('draft.brand.logo_path'),'Campaigns/Test-12345678/Assets/Products/logo.png');
  h.run('briefHost.dead=false');
  const second=h.run("attachBriefLogo(briefHost,{name:'logo.jpg',type:'image/jpeg'})");
  h.pending[1].resolve({reference:'Campaigns/Test-12345678/Assets/Products/accepted.jpg',type:'JPEG',width:10,height:20,opaque:true,provenance:{label:'User-supplied logo',rights:'Not provided',synthetic:false}});
  await second;
  assert.equal(h.run('draft.brand.logo_path'),'Campaigns/Test-12345678/Assets/Products/accepted.jpg');
  assert.equal(h.run('draft.brand.logo_provenance.rights'),'Not provided');
});

test('format actions preserve the full colon-delimited ratio', async () => {
  const h=harness();
  for(const ratio of ['1:1','9:16','16:9']) {
    await h.run(`briefAction(briefHost,${JSON.stringify('format:'+ratio)})`);
    assert.equal(h.run('briefHost.ratio'),ratio);
    assert.equal(h.run('briefHost.vm.ratio'),ratio);
  }
});

test('missing Campaign images open the existing product editor and attachment hints clear after upload', async () => {
  const h = harness();
  h.run(`model.attachment_hints.a='requested-photo.jpg';
    draft.products[0].source={role:'finished_scene',path:''};refreshBriefFlow()`);
  assert.equal(h.run('briefHost.vm.products[0].attachmentHint'),'requested-photo.jpg');
  const missing=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'products'})");
  assert.match(missing,/Attach requested-photo.jpg/);
  assert.match(missing,/Edit product/);assert.doesNotMatch(missing,/Inspect source/);
  assert.doesNotMatch(missing,/bf-chip|Supplied scene/);
  assert.equal(h.run("'sourceLabel' in briefHost.vm.products[0]"),false);
  assert.equal(h.run('draft.products[0].source.role'),'finished_scene');
  assert.match(missing, /class="bf-add-image" data-bf-action="edit-product:a" aria-label="Add image for Alpha"/);
  const actions=[];h.c.dispatchAction=async action=>actions.push(action.action);
  await h.run("briefAction(briefHost,'edit-product:a')");assert.deepEqual(actions,['product:a']);
  h.run('operation={active:true};refreshBriefFlow()');
  const busy=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'products'})");
  assert.match(busy,/aria-label="Add image for Alpha" disabled/);
  h.run('operation=null');
  h.run("draft.products[0].source.path='Campaigns/Test-12345678/Assets/Products/attached.jpg';refreshBriefFlow()");
  const attached=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'products'})");
  assert.match(attached,/class="bf-product-photo-button" data-bf-action="edit-product:a"/);
  assert.match(attached,/Inspect source/);
  await h.run("briefAction(briefHost,'source:a')");
  assert.equal(actions.at(-1),'productSource:a');
  assert.equal(h.run('briefHost.vm.products[0].attachmentHint'),null);
  assert.equal(h.run('model.attachment_hints.a'),'requested-photo.jpg');
  h.run("draft.products[0].source={role:'concept'};refreshBriefFlow()");
  assert.equal(h.run('briefHost.vm.products[0].attachmentHint'),null);
  assert.equal(h.run('draft.products[0].source.role'),'concept');
  const generated=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'products'})");
  assert.match(generated,/Image will be generated\./);
  assert.doesNotMatch(generated,/Add image|Inspect source/);
  h.run("draft.products[0].source={role:'prepared_product',path:'Campaigns/Test-12345678/Assets/Products/cutout.png'};refreshBriefFlow()");
  const prepared=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'products'})");
  assert.doesNotMatch(prepared,/Product stays intact|bf-chip|bf-add-image/);
  assert.equal(h.run("'sourceDetail' in briefHost.vm.products[0]"),false);
  assert.match(prepared,/<img src="\/asset" alt="Alpha source">/);
  assert.equal(h.run('draft.products[0].source.role'),'prepared_product');
});

test('used-color remedy opens the captured text target without changing raw work', async () => {
  const h = harness(), opened = [];
  Object.assign(h.c, { selected:[0], formats:[0], reviewTab:'overview', modal:null,
    textEditor:null, paintText(){}, openModal(kind){
      h.c.modal=kind; h.c.textEditor={selected:'message'};
      opened.push({kind,product:h.c.currentProduct,format:h.c.currentFormat,language:h.c.lang});
    }});
  h.run(`draft.brand.palette.push('#61177C');draft.products.push({id:'b',name:'Beta',source:{role:'concept'},
    framing:{'es-MX':{'1:1':{text_placements:{cta:{color:'#61177C'}}}}}});
    changeBriefField(briefHost,'audience','  Unfinished audience  ',{commit:false});refreshBriefFlow()`);
  await h.run("briefAction(briefHost,'remove-color:1')");
  const before = h.plain('({draft, buffers:projectBuffers})');
  await h.run("briefAction(briefHost,'open-studio')");
  assert.deepEqual(opened,[{kind:'text',product:1,format:0,language:'es'}]);
  assert.equal(h.run('textEditor.selected'),'cta');
  assert.deepEqual(h.plain('({draft, buffers:projectBuffers})'),before);
});

test('color blur preserves an explicit action until click, while ordinary blur still commits', async () => {
  const h = harness(), events = {}, commits = [];
  h.c.AbortController = AbortController;
  h.c.root = {
    addEventListener(name, handler) { events[name] = handler; },
    contains(element) { return element?.inside === true; },
  };
  h.c.onField = (field,value,options) => {
    commits.push({field,value,options});
    h.run(`changeBriefField(briefHost,${JSON.stringify(field)},${JSON.stringify(value)},${JSON.stringify(options)})`);
  };
  h.c.onAction = action => h.run(`briefAction(briefHost,${JSON.stringify(action)})`);
  h.run('MotifBriefFlow.mount(root,{onField,onAction})');
  const input = {value:'61177c',dataset:{bfField:'defaultColor'},matches:()=>true};
  const action = {inside:true,disabled:false,dataset:{bfAction:'add-default'}};
  action.closest = () => action;
  const event = {target:input,relatedTarget:action,stopPropagation(){},preventDefault(){}};
  h.run("changeBriefField(briefHost,'defaultColor','61177c',{commit:false})");
  events.focusout(event);
  assert.equal(commits.length,0, 'focus transfer must not replace the pending action node');
  assert.equal(h.run('draft.brand.text_color'),'61177c', 'focusing an action is not accepting it');
  events.click({...event,target:action});
  assert.equal(h.run('draft.brand.text_color'),'#61177C');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF','#61177C']);
  events.focusout({...event,relatedTarget:null,target:{...input,value:'ffffff',dataset:{bfField:'palette-1'}}});
  assert.equal(commits.length,1);
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF'], 'ordinary blur normalizes and deduplicates');
  events.focusout({...event,relatedTarget:null,target:{...input,value:'.ddd'}});
  assert.equal(h.run('draft.brand.text_color'),'.ddd', 'invalid raw input remains exact');
});

test('nonfocusable pointer actions survive blur without accepting on pointer-down or cancellation', () => {
  const h=harness(), events={}, pointerEvents={}, commits=[];
  h.c.AbortController=AbortController;
  h.c.root={
    ownerDocument:{addEventListener(name,handler){pointerEvents[name]=handler;}},
    addEventListener(name,handler){events[name]=handler;},
    contains(element){return element?.inside===true;},
  };
  h.c.onField=(field,value,options)=>{
    commits.push({field,value,options});
    h.run(`changeBriefField(briefHost,${JSON.stringify(field)},${JSON.stringify(value)},${JSON.stringify(options)})`);
  };
  h.c.onAction=action=>h.run(`briefAction(briefHost,${JSON.stringify(action)})`);
  h.run('MotifBriefFlow.mount(root,{onField,onAction})');
  const input={inside:true,value:'334455',dataset:{bfField:'defaultColor'},matches:()=>true,closest:()=>null};
  const action={inside:true,disabled:false,dataset:{bfAction:'add-default'}};
  action.closest=()=>action;
  const pointer={target:action,button:0,isPrimary:true};
  const blur={target:input,relatedTarget:null,stopPropagation(){}};
  h.run("changeBriefField(briefHost,'defaultColor','334455',{commit:false})");
  pointerEvents.pointerdown?.(pointer);
  events.focusout(blur);
  assert.equal(commits.length,0,'null relatedTarget must not replace the intended pointer action');
  assert.equal(h.run('draft.brand.text_color'),'334455');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF'],'pointer-down is not acceptance');
  pointerEvents.pointerup?.(pointer);
  events.click({...pointer,preventDefault(){},stopPropagation(){}});
  assert.equal(h.run('draft.brand.text_color'),'#334455');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF','#334455']);

  h.run("changeBriefField(briefHost,'defaultColor','778899',{commit:false})");
  input.value='778899';
  pointerEvents.pointerdown?.(pointer);events.focusout(blur);pointerEvents.pointercancel?.(pointer);
  assert.equal(h.run('draft.brand.text_color'),'778899','canceled pointer retains raw draft');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF','#334455'],'canceled pointer accepts nothing');
  events.focusout(blur);
  assert.equal(commits.length,1,'canceled pointer cannot suppress a later ordinary blur');

  pointerEvents.pointerdown?.(pointer);
  pointerEvents.pointerup?.({target:{closest:()=>null}});
  events.focusout(blur);
  assert.equal(commits.length,2,'release outside clears pointer intent');
  pointerEvents.pointerdown?.(pointer);
  pointerEvents.pointerdown?.({target:{closest:()=>null},button:0,isPrimary:true});
  events.focusout(blur);
  assert.equal(commits.length,3,'non-action pointer clears stale intent before ordinary blur');
  pointerEvents.pointerdown?.(pointer);
  events.keydown({target:input,key:'Tab'});
  events.focusout(blur);
  assert.equal(commits.length,4,'keyboard activity cannot inherit pointer intent');
});

test('color normalization keeps the unchanged incoming control through its synchronous rerender', () => {
  const h=harness(), events={}, kept=[];
  h.c.AbortController=AbortController;
  h.c.CSS={escape:String};h.c.document={activeElement:{inside:false}};
  const source={inside:true,dataset:{bfField:'defaultColor'},value:'abcdef',matches:()=>true};
  const destination={inside:true,dataset:{bfField:'brandName'},value:'Brand',tagName:'INPUT',type:'text',checked:false,matches:selector=>!selector.includes('checkbox')};
  destination.closest=selector=>selector.includes(',')?destination:null;
  const replacement={...destination,replaceWith(node){kept.push(node);}};
  h.c.root={
    addEventListener(name,handler){events[name]=handler;},
    contains(element){return element?.inside===true;},
    querySelectorAll(){return [];},querySelector(){return replacement;},
    set innerHTML(_html){
      // A real replacement emits another blur while the host render guard is held.
      events.focusout({target:source,relatedTarget:null,stopPropagation(){}});
    },
  };
  h.c.onField=(field,value,options)=>h.run(`changeBriefField(briefHost,${JSON.stringify(field)},${JSON.stringify(value)},${JSON.stringify(options)})`);
  h.run('briefHost.view=MotifBriefFlow.mount(root,{onField})');
  events.focusout({target:source,relatedTarget:destination,stopPropagation(){}});
  assert.equal(h.run('draft.brand.text_color'),'#ABCDEF');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF'],'blur normalizes without accepting a new palette color');
  assert.deepEqual(kept,[destination],'native focus and caret continue on the same input node');
  source.value='fedcba';
  events.pointerdown({target:destination,button:0,isPrimary:true});
  events.focusout({target:source,relatedTarget:null,stopPropagation(){}});
  assert.deepEqual(kept,[destination,destination],'the explicit pointer target also covers missing relatedTarget');
  source.value='abcdef';
  replacement.value='A different field after normalization';
  events.focusout({target:source,relatedTarget:destination,stopPropagation(){}});
  assert.equal(kept.length,2,'do not preserve a node that would overwrite changed field contents');
});

test('owned native file selection survives rerender, while cancellation and retired owners cannot upload', () => {
  for (const outcome of ['selected','cancelled','superseded','same-node-new-selection','destroyed']) {
    const h=harness(), events={}, files=[];
    const input=()=>({listeners:new Map(),value:'',files:[{name:'chosen.png'}],click(){},
      addEventListener(name,fn){this.listeners.set(name,fn);},
      removeEventListener(name,fn){if(this.listeners.get(name)===fn)this.listeners.delete(name);}});
    let current=input();
    h.c.AbortController=AbortController;
    h.c.document={activeElement:null};
    h.c.root={addEventListener(name,fn){events[name]=fn;},contains(){return false;},
      querySelector(){return current;},querySelectorAll(){return [];},
      set innerHTML(_value){current=input();}};
    h.c.onFile=file=>files.push(file);
    const view=h.run('MotifBriefFlow.mount(root,{onFile})');
    const click={target:{closest:()=>({dataset:{bfAction:'upload-logo'}})},preventDefault(){},stopPropagation(){}};
    events.click(click);
    const chosen=current, completion=chosen.listeners.get('change');
    if(outcome!=='same-node-new-selection') {
      view.render(h.run('briefHost.vm'));
      assert.notEqual(current,chosen,'the active chooser input was detached by a real component render');
    }
    if(outcome==='cancelled') chosen.listeners.get('cancel')({type:'cancel',stopPropagation(){}});
    if(['superseded','same-node-new-selection'].includes(outcome)) events.click(click);
    if(outcome==='destroyed') view.destroy();
    completion({type:'change',stopPropagation(){}});
    completion({type:'change',stopPropagation(){}});
    assert.equal(files.length,outcome==='selected'?1:0);
    if(outcome==='same-node-new-selection') assert.notEqual(chosen.listeners.get('change'),completion,'the new chooser owns its distinct callback');
    else assert.equal(chosen.listeners.size,0,'old callbacks are released after completion or ownership change');
  }
});

test('Campaign market selects keep current raw fields and switch through the existing market action', async () => {
  const h=harness();let captured=0;
  h.c.captureProject=()=>{captured++;assert.equal(h.run('lang'),'en');};
  h.run("changeBriefField(briefHost,'defaultColor','#12',{commit:false})");
  const before=h.plain('projectBuffers.form_drafts');
  await h.run("changeBriefField(briefHost,'market','es-MX',{commit:true})");
  assert.equal(captured,1);assert.equal(h.run('lang'),'es');
  assert.deepEqual(h.plain('projectBuffers.form_drafts'),before);
  for(const tab of ['brand','copy']) {
    const html=h.run(`MotifBriefFlow.renderBrief({...briefHost.vm,tab:'${tab}'})`);
    assert.equal((html.match(/<select data-bf-field="market">/g)||[]).length,1);
    assert.match(html,/<option value="es-MX" selected>Spanish \(Mexico\)/);
    assert.match(html,/<option value="en-US" >English \(United States\)/);
    assert.doesNotMatch(html,/bf-languages|bf-preview-language|data-bf-action="language:/);
  }
  await h.run("changeBriefField(briefHost,'market','de-DE',{commit:true})");
  assert.equal(h.run('lang'),'es');assert.equal(captured,1);
  const products=h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'products'})");
  assert.match(products,/class="pr-cap">1 of 5 products/);
  assert.match(products,/data-bf-action="add-product"/);
});

test('Brand and Copy preview the selected product without requiring a logo', () => {
  const h = harness();
  for (const section of ['brand', 'copy']) {
    h.run(`tab='${section}';draft.brand.logo_path=null;draft.products[0].source={role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/lantern.png'};refreshBriefFlow()`);
    assert.equal(h.run('briefHost.vm.preview.phase'), 'pending');
    assert.equal(h.run('briefHost.vm.products[0].image'), '/asset');
    assert.equal(h.run('briefHost.vm.logo'), null);
  }
  h.run("changeBriefField(briefHost,'defaultColor','#zz',{commit:false})");
  assert.equal(h.run('briefHost.vm.preview.phase'), 'invalid');
  assert.match(h.run('MotifBriefFlow.previewStage(briefHost.vm.preview)'), /six-digit default color/);
});

test('pending Brand upload disables Production and cancellation rejects its late result', async () => {
  const h = harness(), original = h.run('draft.brand.logo_path');
  const task = h.run("attachBriefLogo(briefHost,{name:'replacement.png',type:'image/png'})");
  assert.equal(h.run('briefLogoUploadPending()'),true);
  const html = h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'brand'})");
  assert.match(html,/data-bf-action="production" disabled/);
  assert.match(html,/Cancel upload/);
  await h.run("briefAction(briefHost,'cancel-logo-upload')");
  assert.equal(h.run('briefLogoUploadPending()'),false);
  assert.equal(h.pending[0].options.signal.aborted,true);
  h.pending[0].resolve({reference:'late.png',provenance:{}});
  await task;
  assert.equal(h.run('draft.brand.logo_path'),original);
});

test('Brand upload failure and timeout release navigation while preserving the selected logo', async () => {
  for (const timeout of [false,true]) {
    const h = harness(), original = h.run('draft.brand.logo_path'), failures=[];
    h.c.imageInputFailure = error=>failures.push(error);
    const task = h.run("attachBriefLogo(briefHost,{name:'replacement.png',type:'image/png'})");
    if (timeout) h.timers.get(h.run('briefHost.logoUpload.timer'))();
    h.pending[0].reject(new Error('Upload refused'));
    await task;
    assert.equal(h.run('briefLogoUploadPending()'),false);
    assert.equal(h.run('draft.brand.logo_path'),original);
    assert.equal(h.run('briefHost.logoUpload'),null);
    if (timeout) assert.match(h.run('briefHost.logoMessage'),/took too long/);
    else assert.equal(failures.length,1);
  }
});

test('accepted Brand upload restores Production and marks the replacement as authored work', async () => {
  const h=harness();let dirty=0;h.c.markDirty=()=>dirty++;
  const task=h.run("attachBriefLogo(briefHost,{name:'replacement.png',type:'image/png'})");
  h.pending[0].resolve({reference:'Campaigns/Test-12345678/Assets/Brand/new.png',provenance:{label:'Uploaded'}});
  await task;
  assert.equal(h.run('briefLogoUploadPending()'),false);
  assert.equal(h.run('draft.brand.logo_path'),'Campaigns/Test-12345678/Assets/Brand/new.png');
  assert.equal(dirty,1);
  assert.doesNotMatch(h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'brand'})"),/data-bf-action="production" disabled/);
});

test('logo policy applies across markets and removing artwork preserves the policy and product', async () => {
  const h=harness();
  const original=h.run("JSON.stringify(draft.products[0])");
  h.run("changeBriefField(briefHost,'requireLogo',true,{commit:true});lang='es';refreshBriefFlow()");
  assert.equal(h.run('briefHost.vm.requireLogo'),true);
  assert.match(h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'brand'})"),/data-bf-field="requireLogo" checked/);
  await h.run("briefAction(briefHost,'remove-logo')");
  assert.equal(h.run("draft.brand.logo_path"),'');
  assert.equal(h.run("briefHost.vm.requireLogo"),true);
  assert.equal(h.run("JSON.stringify(draft.products[0])"),original);
  assert.equal(h.run("draft.brand.logo_provenance.label"),'Not provided');
  h.run("changeBriefField(briefHost,'requireLogo',false,{commit:true})");
  assert.equal(h.run("Object.hasOwn(draft.brand,'require_logo')"),false);
});

test('a new product photo is visible before the first headline is written',()=>{
  const h=harness();
  h.run("draft.brand.logo_path='';draft.products[0].source={role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/new.png'};draft.markets['en-US'].copy.message='';refreshBriefFlow()");
  assert.equal(h.run("briefHost.vm.preview.phase"),"source");
  assert.match(h.run("MotifBriefFlow.renderBrief({...briefHost.vm,tab:'brand'})"), /Product photo<\/strong>/);
  assert.match(h.run("MotifBriefFlow.previewStage(briefHost.vm.preview)"), /<img src="\/asset"/);
  h.run("changeBriefField(briefHost,'message','New headline',{commit:true})");
  assert.equal(h.run("briefHost.vm.preview.phase"),"pending");
});
