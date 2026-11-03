const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {readFileSync}=require('node:fs');
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function harness(){
  const calls=[],updates=[],microtasks=[],saves=[];
  const context=vm.createContext({
    draft:{campaign_id:'one',products:[{id:'product',source:{path:'old.png'},copy_overrides:{}}],name:'Original'},
    copy:structuredClone,projectBuffers:{form_drafts:{}},modalProduct:null,
    model:{version:'v1',unplanned_draft:true,plan:{digest:'old'},run:{id:'old',state:'complete'}},
    page:'production',modal:'',busy:false,dirty:false,operation:null,
    currentBriefValue:()=>structuredClone(context.draft),getSaveState:()=>({}),
    projectQueue:{version:()=>context.model.version,hold:()=>({campaign:'one',base:{run_id:'accepted',revision_id:'r1',metadata_snapshot_id:'m1'}}),release(){},authored:()=>({brief:structuredClone(context.draft)}),applied(){}},
    sameProjectInputs:(a,b)=>JSON.stringify(a)===JSON.stringify(b),
    queueMicrotask:fn=>microtasks.push(fn),
    saveDraft:()=>new Promise((resolve,reject)=>saves.push({resolve,reject})),
    refreshProductionStatus(){},
    withBriefPreviewPaused:(action,current)=>current()?action():null,
    useData:data=>{updates.push(data);context.model=data;},
    api:(url,body)=>new Promise((resolve,reject)=>calls.push({url,body,resolve,reject})),
  });
  const run=code=>vm.runInContext(code,context);
  const project=readFileSync('src/campaign_studio/static/motif/project.js','utf8');
  run(project.slice(project.indexOf('function reconcileAppliedBrief('),project.indexOf('function receiveProject(')));
  run(readFileSync('src/campaign_studio/static/motif/production.js','utf8'));
  const actions=readFileSync('src/campaign_studio/static/motif/actions.js','utf8');
  run(actions.slice(actions.indexOf('async function prepareCurrentPlan('),actions.indexOf('async function poll(')));
  return {context,run,calls,updates,saves,microtasks};
}
test('automatic preparation saves and prepares once, never starts, and leaves a fresh complete run alone',async()=>{
  const h=harness();h.context.model.unplanned_draft=false;
  h.run('ensureProductionPlan()');assert.equal(h.saves.length,0);
  h.context.model.unplanned_draft=true;h.run('ensureProductionPlan();ensureProductionPlan()');
  assert.equal(h.saves.length,1);h.context.model.version='v2';h.saves.shift().resolve();await flush();
  assert.equal(h.calls.length,1);assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].body)),{version:'v2'});
  assert.match(h.calls[0].url,/\/plan$/);
  h.calls[0].resolve({version:'v2',plan:{digest:'current'},unplanned_draft:false,project:{authored:{brief:structuredClone(h.context.draft)}}});await flush();
  assert.equal(h.updates.length,1);assert.equal(h.context.page,'production');
  for(const fn of h.microtasks.splice(0))fn();assert.equal(h.saves.length,0);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/start')).length,0);
});
test('a held plan cannot adopt after a newer edit, completed Save, or campaign switch',async()=>{
  for(const mutation of ['edit','save','campaign']){
    const h=harness();const pending=h.run('prepareCurrentPlan()');h.saves.shift().resolve();await flush();
    if(mutation==='edit')h.context.draft.name='Newer name';
    if(mutation==='save')h.context.model.version='v2';
    if(mutation==='campaign')h.context.draft={campaign_id:'two',products:[{id:'other'}],name:'Other'};
    h.calls[0].resolve({version:'v1',plan:{digest:'stale'},value:{name:'Original'},project:{authored:{brief:{products:[{id:'product',source:{path:'prepared.png'}}]}}}});await pending;
    assert.equal(h.updates.length,0,mutation);
    assert.equal(h.context.draft.name,mutation==='edit'?'Newer name':mutation==='campaign'?'Other':'Original');
    assert.equal(h.context.model.version,mutation==='save'?'v2':'v1');
    assert.equal(h.calls.length,1);assert.equal(h.context.page,'production');
  }
});

test('campaign B prepares while A is held; late A success or failure cannot release or replace B',async()=>{
  for(const failure of [false,true]){
    const h=harness(),released=[];let held=null;
    h.context.projectQueue.hold=()=>held={campaign:h.context.draft.campaign_id,base:{}};
    h.context.projectQueue.release=token=>{released.push(token.campaign);if(held===token)held=null;};
    const a=h.run('prepareCurrentPlan()');h.saves.shift().resolve();await flush();
    const old=structuredClone(h.context.draft);
    h.context.draft={...old,campaign_id:'two',name:'Campaign B'};
    h.run('ensureProductionPlan()');assert.equal(h.saves.length,1,'B is not gated by A');
    h.saves.shift().resolve();await flush();assert.equal(h.calls.length,2);
    assert.equal(h.calls[1].url,'/api/motif/campaigns/two/plan');
    if(failure){
      h.calls[0].reject({status:503,message:'A response lost'});await flush();
      h.calls[2].resolve({version:'v1',project:{base:{},authored:{brief:old}}});
    }else h.calls[0].resolve({version:'v1',project:{authored:{brief:old}}});
    await a;
    assert.equal(h.run('currentPreparation().campaign'),'two');
    assert.equal(h.run('currentPreparation().status'),'pending');
    assert.equal(held.campaign,'two');assert.equal(h.context.draft.name,'Campaign B');
    assert.equal(h.updates.length,0);assert.deepEqual(released,['one']);
    h.calls[1].resolve({version:'v1',plan:{digest:'B'},unplanned_draft:false,project:{authored:{brief:structuredClone(h.context.draft)}}});
    await flush();assert.equal(held,null);assert.equal(h.updates.length,1);
    assert.equal(h.context.model.plan.digest,'B');assert.deepEqual(released,['one','two']);
    for(const fn of h.microtasks.splice(0))fn();assert.equal(h.saves.length,0);
    assert.equal(h.calls.filter(call=>call.url.endsWith('/plan')).length,2);
    assert.equal(h.calls.filter(call=>call.url.endsWith('/start')).length,0);
  }
});
test('a changed draft while Save is pending prepares only its later committed basis',async()=>{
  const h=harness();const pending=h.run('prepareCurrentPlan()');h.context.draft.name='Latest';
  h.saves.shift().resolve();await pending;assert.equal(h.calls.length,0);
  h.run('ensureProductionPlan()');h.saves.shift().resolve();await flush();
  assert.equal(h.calls.length,1);h.calls[0].resolve({version:'v1',plan:{digest:'latest'},unplanned_draft:false,project:{authored:{brief:structuredClone(h.context.draft)}}});await flush();
  assert.equal(h.updates.length,1);assert.equal(h.context.draft.name,'Latest');
});
test('an invalid or failed estimate does not loop; a relevant edit or explicit retry permits one new plan',async()=>{
  const h=harness();const pending=h.run('prepareCurrentPlan()');h.saves.shift().resolve();await flush();
  const error={status:422,message:'Attach a readable image',fields:[{path:['products',0,'source','path']}]};
  h.calls[0].reject(error);await pending;
  for(const fn of h.microtasks.splice(0))fn();h.run('ensureProductionPlan()');assert.equal(h.saves.length,0);
  assert.equal(h.run('currentPreparation().error.message'),error.message);
  h.context.draft.name='Changed';h.run('ensureProductionPlan()');assert.equal(h.saves.length,1);
  h.saves.shift().reject(new Error('Save failed'));await flush();
  h.run('ensureProductionPlan()');assert.equal(h.saves.length,0);
  h.run('prepareCurrentPlan(true)');assert.equal(h.saves.length,1);
});

test('planning advances only unchanged retained and open-dialog references, preserving a new upload',()=>{
  const h=harness();h.context.projectBuffers.form_drafts.product_images={product:{path:'old.png'}};
  h.context.modalProduct={id:'product',source:{path:'old.png'},description:'New description'};
  h.context.before=structuredClone(h.context.draft);h.context.after=structuredClone(h.context.draft);
  h.context.after.products[0].source.path='prepared.png';
  h.run('reconcilePreparedSources(before,after)');
  assert.equal(h.context.modalProduct.source.path,'prepared.png');assert.equal(h.context.modalProduct.description,'New description');
  assert.equal(h.context.projectBuffers.form_drafts.product_images.product.path,'prepared.png');
  h.context.modalProduct.source.path='Campaigns/Test-12345678/Assets/Products/new.png';h.run('reconcilePreparedSources(before,after)');
  assert.equal(h.context.modalProduct.source.path,'Campaigns/Test-12345678/Assets/Products/new.png');
});

test('a refused plan releases its own queue hold without starting or accepting a plan',async()=>{
  const h=harness(),token={},released=[];
  h.context.projectQueue.hold=()=>token;h.context.projectQueue.release=owner=>released.push(owner);
  const pending=h.run('prepareCurrentPlan()');h.saves.shift().resolve();await flush();
  h.calls[0].reject({status:422,message:'Invalid color'});await pending;
  assert.deepEqual(released,[token]);assert.equal(h.updates.length,0);assert.equal(h.calls.length,1);
  assert.equal(h.run('currentPreparation().status'),'error');
});

test('a lost plan response acknowledges only its unchanged source references before releasing newer work',async()=>{
  const h=harness();let applied=0;h.context.projectQueue.applied=()=>applied++;
  const pending=h.run('prepareCurrentPlan()');h.saves.shift().resolve();await flush();
  h.calls[0].reject({status:503,message:'Lost plan response'});await flush();
  assert.equal(h.calls[1].url,'/api/motif/campaigns/one');assert.equal(h.calls[1].body,undefined);
  const brief=structuredClone(h.context.draft);brief.products[0].source.path='prepared.png';
  h.context.draft.name='New local typing';
  h.calls[1].resolve({version:'v1',project:{base:{run_id:'accepted',revision_id:'r1',metadata_snapshot_id:'m1'},authored:{brief}}});await pending;
  assert.equal(h.context.draft.name,'New local typing');
  assert.equal(h.context.draft.products[0].source.path,'prepared.png');
  assert.equal(applied,1);assert.equal(h.updates.length,0);assert.equal(h.context.model.unplanned_draft,true);
  assert.equal(h.calls.length,2);assert.equal(h.run('productionPreparation'),null);
});

test('uncertain-plan reconciliation refuses a competing authored version or accepted image revision',async()=>{
  for(const changed of ['version','revision']){
    const h=harness();let applied=0;h.context.projectQueue.applied=()=>applied++;
    const pending=h.run('prepareCurrentPlan()');h.saves.shift().resolve();await flush();h.calls[0].reject({status:503,message:'Lost'});await flush();
    const brief=structuredClone(h.context.draft);brief.products[0].source.path='foreign.png';
    h.calls[1].resolve({version:changed==='version'?'v2':'v1',project:{base:{run_id:'accepted',revision_id:changed==='revision'?'r2':'r1',metadata_snapshot_id:'m1'},authored:{brief}}});await pending;
    assert.equal(applied,0);assert.equal(h.context.draft.products[0].source.path,'old.png');assert.equal(h.run('currentPreparation().status'),'error');
  }
});
