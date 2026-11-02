// Controlled host boundaries for the shipped Brief adapter, with no provider.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const pending = [], timers = new Map(); let next = 1;
  const c = vm.createContext({
    structuredClone, TextEncoder, crypto:require('node:crypto').webcrypto,
    setTimeout(fn) { const id=next++; timers.set(id,fn); return id; }, clearTimeout(id) { timers.delete(id); },
    operation:null, copy:structuredClone, page:'campaign', tab:'brand', lang:'en', productLimit:5,
    currentProduct:0,currentFormat:0, LANG:{en:'English (United States)',es:'Spanish (Mexico)'}, MARKET:{en:'en-US',es:'es-MX'},FMT:[{ratio:'1:1'}],
    C:{products:[{id:'a'}]}, projectBuffers:{form_drafts:{},feedback:{}},
    model:{version:'v1',manifest:{revision_id:'r1'},concept_previews:{},attachment_hints:{}},
    draft:{campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4', name:'Campaign', audience:'Audience',creative_direction:'Scene',
      brand:{name:'Brand',text_color:'#FFFFFF',palette:['#FFFFFF'],logo_path:'private/uploads/logo.png',logo_provenance:{}},copy_provenance:{label:'Source',rights:'Rights'},
      markets:{'en-US':{copy:{message:'English',cta:null}},'es-MX':{copy:{message:'Español',cta:null}}},
      products:[{id:'a',name:'Alpha',description:'A',source:{role:'concept'},framing:{},copy_overrides:{}}]},
    assetURL:ref=>ref ? '/asset':null,markDirty(){},toast(){},$:()=>({focus(){},content:'csrf'}),
    api:(url,body,options)=>new Promise((resolve,reject)=>pending.push({url,body,options,resolve,reject})),
    wordingCheckPhase:null, paintWordingAvailability(){},
    showCapacity(){},dispatchAction:async()=>{},updateURL(){},render(){},
  });
  const run=code=>vm.runInContext(code,c);
  c.window=c;
  run(readFileSync('src/campaign_studio/static/motif/wording.js','utf8'));
  run(readFileSync('src/campaign_studio/static/motif/brief-flow.js','utf8'));
  run(readFileSync('src/campaign_studio/static/motif/brief-host.js','utf8'));
  run(`briefFacts.campaign='292e9e84-eb00-5fdb-87e8-423f5e51fac4';briefFacts.logos.set(draft.brand.logo_path,{width:200,height:80,type:'PNG'});
    briefHost={owner:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',productId:'a',ratio:'1:1',generation:0,upload:0,dead:false,view:{render(){},preview(request){return requestBriefPreview(briefHost,request)}}};
    refreshBriefFlow();`);
  return {c,run,pending,timers,plain:code=>JSON.parse(run('JSON.stringify('+code+')'))};
}
test('raw default is preserved without silently expanding palette; explicit use normalizes and deduplicates',async()=>{
  const h=harness();
  h.run("changeBriefField(briefHost,'defaultColor',' 61177c ',{commit:false})");
  assert.equal(h.run('draft.brand.text_color'),' 61177c ');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF']);
  assert.equal(h.run('briefHost.vm.preview.phase'),'color');
  await h.run("briefAction(briefHost,'add-default')");
  assert.equal(h.run('draft.brand.text_color'),'#61177C');
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF','#61177C']);
  h.run("changeBriefField(briefHost,'palette-1','ffffff',{commit:true})");
  assert.deepEqual(h.plain('draft.brand.palette'),['#FFFFFF']);
  assert.equal(h.run('briefHost.vm.preview.phase'),'invalid');
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
  h.run('briefHost.view.render=rerender;refreshBriefFlow()');
  assert.equal(renders,1);assert.equal(h.run('draft.brand.text_color'),'#FFFFFF');
});
test('acknowledged Brief refresh replaces only authoritative concept facts and updates current cards', async () => {
  const h = harness(); let views = 0;
  h.c.viewModel = () => views++;
  h.run("model.concept_previews={a:'/old-master'};model.run={id:'run'}");
  const before = h.plain('draft');
  const refreshing = h.run("refreshBriefSources('v1', copy(draft))");
  assert.equal(h.pending[0].url, '/api/motif/campaigns/292e9e84-eb00-5fdb-87e8-423f5e51fac4');
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
  for(let i=0;i<5;i++) await flush();
  assert.equal(h.pending.length,1);
  for(const word of ['First','Second','Latest']) h.run(`changeBriefField(briefHost,'message',${JSON.stringify(word)},{commit:false});flushBriefPreview(briefHost)`);
  assert.equal(h.pending.length,1);
  h.pending[0].resolve({}); // Superseded before target/digest consumption.
  await assert.rejects(h.run('briefHost.flight'));
  for(let i=0;i<5;i++) await flush();
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
  for (let i=0;i<5;i++) await flush();
  assert.equal(h.pending.length,1);
  h.run("changeBriefField(briefHost,'message','Latest',{commit:false});flushBriefPreview(briefHost)");
  const action = h.run("withBriefPreviewPaused(()=>api('/plan',{}))");
  h.run("changeBriefField(briefHost,'message','Still typing',{commit:false});flushBriefPreview(briefHost)");
  assert.equal(h.pending.length,1,'no producer request until the owned render settles');
  h.pending[0].resolve({});
  for (let i=0;i<5;i++) await flush();
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
  for(let i=0;i<5;i++)await flush();
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
  for (let i=0;i<5;i++) await flush();
  const action = h.run("withBriefPreviewPaused(()=>api('/plan',{}))");
  h.pending[0].reject(new Error('Controlled preview failure'));
  for (let i=0;i<5;i++) await flush();
  h.pending[1].reject(Object.assign(new Error('Other operation is active'),{status:409,code:'busy'}));
  await assert.rejects(action,/Other operation is active/);
  assert.equal(h.pending.length,2);
  assert.equal(h.run('briefPreviewPaused()'),false);
});

test('a logo selected before navigation never uploads after its old preview drains', async () => {
  const h = harness();
  h.run('flushBriefPreview(briefHost)');
  for (let i=0;i<5;i++) await flush();
  const action = h.run("attachBriefLogo(briefHost,{name:'logo.jpg',type:'image/jpeg'})");
  h.run("briefHost.dead=true;draft.campaign_id='62220994-1d32-5713-80d2-ac0b6ff0baf3'");
  h.pending[0].resolve({});
  await action;
  assert.equal(h.pending.length,1);
  assert.equal(h.run('draft.brand.logo_path'),'private/uploads/logo.png');
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
  h.pending[0].resolve({reference:'private/uploads/new.jpg',provenance:{label:'New'}});
  await first;assert.equal(h.run('draft.brand.logo_path'),'private/uploads/logo.png');
  h.run('briefHost.dead=false');
  const second=h.run("attachBriefLogo(briefHost,{name:'logo.jpg',type:'image/jpeg'})");
  h.pending[1].resolve({reference:'private/uploads/accepted.jpg',type:'JPEG',width:10,height:20,opaque:true,provenance:{label:'User-supplied logo',rights:'Not provided',synthetic:false}});
  await second;
  assert.equal(h.run('draft.brand.logo_path'),'private/uploads/accepted.jpg');
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
  h.run("draft.products[0].source.path='private/uploads/attached.jpg';refreshBriefFlow()");
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
  h.run("draft.products[0].source={role:'prepared_product',path:'private/uploads/cutout.png'};refreshBriefFlow()");
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
  events.pointerdown({target:destination,button:0,isPrimary:true});
  events.focusout({target:source,relatedTarget:null,stopPropagation(){}});
  assert.deepEqual(kept,[destination,destination],'the explicit pointer target also covers missing relatedTarget');
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
