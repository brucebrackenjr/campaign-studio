const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const campaign = '5fcc7583-3966-4ce3-98b3-e5ef39567c69';
function finding(product, market='en-US', field='message') {
  return {rule:'rule',context:{product_id:product,product_name:product,market,
    phrase:{configured_phrase:'Make',matches:[{field,value:'Make a move',origin:'product_override',path:['markets',market,'copy',field]}]}}};
}
function harness() {
  const pending=[],nodes=new Map(),focused=[],renders=[];
  const get = selector => {
    if (!nodes.has(selector)) nodes.set(selector,{value:'Make a move',dataset:{},focus(){focused.push(selector)},
      setSelectionRange(start,end){this.selection=[start,end]},setAttribute(name,value){this[name]=value},insertAdjacentHTML(){}});
    return nodes.get(selector);
  };
  const context=vm.createContext({
    MotifTextLimits: {issue: () => null, guardFields: () => true},document:{addEventListener(){}},window:{scrollY:123,scrollTo(){}},queueMicrotask,
    $:get,$$:()=>[],model:{version:'v1',output_run_id:'run',manifest:{revision_id:'r1'},wording_issue:null},
    C:{id:campaign,products:[{id:'p1'},{id:'p2'}]},draft:{markets:{'en-US':{phrase_rules:[]},'es-MX':{phrase_rules:[]}}},
    MARKET:{en:'en-US',es:'es-MX'},page:'handoff',tab:'brand',reviewTab:'compare',selected:[1,0],formats:[2,1],
    currentProduct:1,currentFormat:2,lang:'es',filter:'missing',pageNum:1,modal:null,studioWorkspace:null,
    activeBlockedProposal:()=>null,updateURL(){},render(){renders.push(context.page)},
    openModal(kind){context.modal=kind;context.wordingEditor={}},productDraft(){context.modal='product'},
    async openStudioWorkspace(){context.modal='text';context.textEditor={}},paintText(){},
    api:()=>new Promise((resolve,reject)=>pending.push({resolve,reject})),toast(){},busy:false,
    dispatchAction:async()=>{},useData(data){context.model=data;}});
  context.window.MotifWording=undefined;
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/wording.js','utf8'),context);
  context.MotifWording=context.window.MotifWording;
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/brief-host.js','utf8'),context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/wording-host.js','utf8'),context);
  const run=code=>vm.runInContext(code,context);
  return {context,run,pending,focused,get,renders};
}
test('format repeats collapse and a removed selected case clamps to a remaining correction',()=>{
  const h=harness();
  h.context.model.wording_issue={code:'prohibited_phrase',matches:[finding('p1'),finding('p1'),finding('p1'),finding('p2')]};
  assert.equal(h.run('currentWordingView().cases.length'),2);
  h.run('wordingCaseId=currentWordingView().cases[1].id');
  h.context.model.wording_issue.matches=[finding('p1')];
  assert.equal(h.run('currentWordingView().index'),0);
  assert.match(h.run('wordingNotice()'),/Edit headline in Studio/);
  h.context.model.wording_issue=null;
  assert.equal(h.run('wordingExportBlocked()'),false);
});
test('correction binds field and language, selects the match, then returns to the exact origin',async()=>{
  const h=harness();h.context.model.wording_issue={code:'prohibited_phrase',matches:[finding('p1')]};
  await h.run('openWordingCase(currentWordingView().cases[0], "edit:p1:en-US:message")');
  assert.equal(h.context.currentProduct,0);assert.equal(h.context.lang,'en');assert.equal(h.context.currentFormat,2);
  assert.equal(h.context.modal,'text');assert.equal(h.context.textEditor.selected,'message');
  assert.deepEqual(h.get('#mf-word-message').selection,[0,4]);
  h.context.modal=null;h.run('returnFromWording("text")');await Promise.resolve();
  assert.equal(h.context.page,'handoff');assert.equal(h.context.lang,'es');assert.equal(h.context.currentProduct,1);
  assert.equal(JSON.stringify(h.context.selected),'[1,0]');assert.equal(h.context.filter,'missing');
  assert.equal(h.focused.at(-1),'[data-wording-slot] [data-mw]');
});
test('pending invalid Spanish form opens its exact index and unknown issues remain blocked',()=>{
  const h=harness();h.context.model.wording_issue={code:'pending_rules',pending_rule_targets:[{market:'es-MX',rule_index:1}]};
  h.run('ruleIndex=-1;openProhibitedRule(null,true)');
  assert.equal(h.context.lang,'es');assert.equal(h.run('ruleIndex'),1);assert.equal(h.context.modal,'rule');
  assert.equal(h.context.page,'campaign');
  h.context.model.wording_issue={code:'unexpected'};
  assert.equal(h.run('currentWordingView().phase'),'failed');assert.equal(h.run('wordingExportBlocked()'),true);
});
test('late check cannot replace a newer campaign model or leave its export permanently pending',async()=>{
  const h=harness(),request=h.run('refreshWordingStatus()');
  assert.equal(h.run('currentWordingView().phase'),'pending');
  h.context.model={...h.context.model,version:'v2',wording_issue:null};
  h.pending[0].resolve({version:'v1',output_run_id:'run',manifest:{revision_id:'r1'},wording_issue:{code:'unexpected'}});
  await request;assert.equal(h.context.model.version,'v2');assert.equal(h.run('currentWordingView().phase'),'clear');
});
test('changed current revision requires refresh and a failed check never enables export',async()=>{
  const h=harness(),request=h.run('refreshWordingStatus()');
  h.pending[0].resolve({version:'v1',output_run_id:'run',manifest:{revision_id:'r2'},wording_issue:null});
  await request;assert.equal(h.run('currentWordingView().phase'),'stale');
  const retry=h.run('refreshWordingStatus()');h.pending[1].reject(new Error('transport'));await retry;
  assert.equal(h.run('currentWordingView().phase'),'failed');assert.equal(h.run('wordingExportBlocked()'),true);
});


test('resolved notice restores keyboard focus to the page heading only if its old control owned focus',()=>{
  const h=harness(),slot={innerHTML:'old',contains:node=>node===h.context.document.activeElement};
  const heading=h.get('#main h1');heading.tagName='H1';
  h.context.$=selector=>selector==='#main h1'?heading:null;
  h.context.$$=selector=>selector==='[data-wording-slot]'?[slot]:[];
  h.context.document.activeElement={};
  h.run('paintWordingAvailability()');
  assert.equal(slot.innerHTML,'');assert.equal(heading.tabindex,'-1');
  assert.equal(h.focused.at(-1),'#main h1');
});

test('draft correction writes only its actual override or inherited field through the project queue',async()=>{
  for (const [origin,market,field] of [['product_override','en-US','message'],['product_override','es-MX','cta']]) {
    const h=harness(),saves=[];
    h.context.blockedProposal=null;h.context.model.manifest=null;h.context.page='production';
    h.context.draft={campaign_id:campaign,markets:{'en-US':{copy:{message:'Make a move',cta:'Make a move'}},'es-MX':{copy:{message:'Make a move',cta:'Make a move'}}},products:[{id:'p1',copy_overrides:{}},{id:'p2',copy_overrides:{'en-US':{message:'Other headline',cta:'Other CTA'},'es-MX':{message:'Otro',cta:'Otra'}}}]};
    if(origin==='product_override') h.context.draft.products[0].copy_overrides[market]={[field]:'Make a move'};
    h.context.projectBuffers={form_drafts:{fields:{'brief/brand/en':{'text-color':'  unfinished  '},'brief/copy/en':{'copy-headline':'Distinct pending headline'}}}};
    h.context.markDirty=()=>{};
    h.context.saveProject=async()=>{saves.push(structuredClone(h.context.draft));return true;};
    h.context.closeModal=()=>{h.context.modal=null;h.run('draftWording=null;returnFromWording("draft-wording")');};
    const row=finding('p1',market,field);row.context.phrase.matches[0].origin=origin;
    h.context.item=h.context.MotifWording.fromIssue({matches:[row]}).cases[0];
    const before=structuredClone(h.context.draft),buffers=JSON.stringify(h.context.projectBuffers);
    h.run('openWordingCase(item,"edit:case")');
    assert.equal(h.context.modal,'draft-wording');assert.equal(h.context.page,'campaign');
    assert.deepEqual(h.get('#draft-wording-value').selection,[0,4]);
    assert.deepEqual(h.context.draft,before);assert.equal(saves.length,0);
    h.run('draftWording.value="Corrected exact words"');await h.run('applyDraftWording()');
    const expected=structuredClone(before);
    const target=origin==='product_override'?expected.products[0].copy_overrides[market]:expected.markets[market].copy;
    target[field]='Corrected exact words';
    if(origin==='market_shared') for(const product of expected.products) delete product.copy_overrides?.[market]?.[field];
    assert.deepEqual(h.context.draft,expected);assert.deepEqual(saves,[expected]);
    if(origin==='market_shared' && market==='en-US' && field==='message') assert.equal(h.context.projectBuffers.form_drafts.fields['brief/copy/en']['copy-headline'],'Corrected exact words');
    else assert.equal(JSON.stringify(h.context.projectBuffers),buffers);
    assert.equal(h.context.page,'production');assert.equal(h.context.lang,'es');assert.equal(h.context.modal,null);
  }
});
test('clean explicit refresh adopts the saved project and queue while preserving the viewing context',async()=>{
  const h=harness();let version='v1',dirty=false,phase='saved',adopted;
  h.context.projectQueue={version:()=>version,isDirty:()=>dirty,getSaveState:()=>({phase})};
  h.context.useData=(data,options)=>{adopted={data,options};version=data.project.version;h.context.model=data;h.context.tab='copy';h.context.lang='en';};
  const request=h.run('refreshSavedWording()');
  const server={version:'v2',project:{version:'v2',authored:{form_drafts:{fields:{'rule/en/-1':{'mw-rule-phrase':'care'}}}}}};
  h.pending[0].resolve(server);await request;
  assert.equal(adopted.options.reset,true);assert.equal(version,'v2');
  assert.equal(adopted.data.project.authored.form_drafts.fields['rule/en/-1']['mw-rule-phrase'],'care');
  assert.equal(h.context.page,'handoff');assert.equal(h.context.tab,'brand');assert.equal(h.context.lang,'es');
});
test('refresh cannot overwrite typing, completed local autosave, or a replaced campaign owner',async()=>{
  for(const change of ['typing','autosave','campaign']) {
    const h=harness();let version='v1',dirty=false,adopted=false;
    h.context.projectQueue={version:()=>version,isDirty:()=>dirty,getSaveState:()=>({phase:'saved'})};
    h.context.useData=()=>{adopted=true;};
    const request=h.run('refreshSavedWording()');
    if(change==='typing')dirty=true;
    else if(change==='autosave')version='v3';
    else h.context.model={...h.context.model,version:'different-owner'};
    h.pending[0].resolve({version:'v2'});
    if(change==='campaign')await request;
    else await assert.rejects(request,/changed while refreshing/);
    assert.equal(adopted,false);
  }
});


test('a required missing logo blocks export with clear wording even when phrase checks pass',()=>{
  const h=harness();h.context.model.logo_issue={code:'logo_required'};
  assert.equal(h.run('wordingExportBlocked()'),false);
  assert.equal(h.run('exportBlocked()'),true);
  h.context.model.logo_issue=null;
  assert.equal(h.run('exportBlocked()'),false);
});

test('deduplicated phrase evidence keeps exact editable copy and every matching rule',()=>{
  const h=harness(), first=finding('p1'), repeated=finding('p1');
  repeated.rule='second-rule';
  repeated.context.phrase.configured_phrase='move';
  repeated.context.phrase.matches[0].value=null;
  h.context.model.wording_issue={code:'prohibited_phrase',matches:[first,repeated]};
  const row=h.run('currentWordingView().cases[0]');
  assert.equal(row.value,'Make a move');
  assert.deepEqual(Array.from(row.terms),['Make','move']);
  assert.deepEqual(Array.from(row.ruleIds),['rule','second-rule']);
  // A subset can arrive in a different order; a later full value fills its reference.
  h.context.model.wording_issue.matches=[repeated,first];
  assert.equal(h.run('currentWordingView().cases[0].value'),'Make a move');
});

test('one correction click follows the next unresolved product after flushing the current edit',async()=>{
 const h=harness();h.context.model.wording_issue={matches:[finding('p1'),finding('p2')]};
 h.context.studioWorkspace={};
 h.context.resolveStudioChanges=async()=>{h.context.model.wording_issue={matches:[finding('p2')]};return true;};
 h.context.leaveStudioWorkspace=async()=>true;
 await h.run('openWordingCase(currentWordingView().cases[0], "edit:p1")');
 assert.equal(h.context.currentProduct,1);
 assert.equal(h.context.textEditor.selected,'message');
});


test('correction on the visible product focuses its field without rebuilding the canvas',async()=>{
 const h=harness();h.context.model.wording_issue={matches:[finding('p1')]};
 h.context.studioWorkspace={product:'p1',mode:'text'};h.context.currentProduct=0;h.context.lang='en';h.context.textEditor={};
 h.context.resolveStudioChanges=async()=>true;
 h.context.leaveStudioWorkspace=async()=>{throw Error('must keep the current canvas')};
 await h.run('openWordingCase(currentWordingView().cases[0], "edit:p1")');
 assert.equal(h.context.textEditor.selected,'message');assert.equal(h.renders.length,0);
 assert.deepEqual(h.get('#mf-word-message').selection,[0,4]);
});

 test('inherited prohibited copy opens its Campaign source instead of creating product overrides',async()=>{
 for(const field of ['message','cta']) for(const market of ['en-US','es-MX']) {
  const h=harness(),row=finding('p1',market,field);row.context.phrase.matches[0].origin='market_shared';
  h.context.model.wording_issue={matches:[row]};
  await h.run('openWordingCase(currentWordingView().cases[0],"edit:shared")');
  assert.equal(h.context.page,'campaign');assert.equal(h.context.tab,'copy');
  assert.equal(h.context.lang,market==='en-US'?'en':'es');assert.equal(h.context.modal,null);
  assert.equal(h.focused.at(-1),`[data-bf-field="${field}"]`);
  assert.deepEqual(h.get(`[data-bf-field="${field}"]`).selection,[0,4]);
 }
});
