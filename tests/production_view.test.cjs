// Exercise the shipped read-only Production projection with separate draft,
// reviewed-plan, and accepted-output identities. No renderer or provider calls.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

function harness() {
  const context = vm.createContext({
    document: { body: { dataset: { page: 'production', cap: '5' } }, addEventListener() {} },
    structuredClone, damagedFiles: () => false, window: {},
  });
  context.window = context;
  const run = code => vm.runInContext(code, context);
  for (const file of ['api.js', 'producer-repair.js', 'production.js', 'producer-host.js'])
    run(readFileSync(`src/campaign_studio/static/motif/${file}`, 'utf8'));
  run(`
    draft={campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',name:'Campaign',
      markets:{'en-US':{copy:{message:'Hello'}},'es-MX':{copy:{message:'Hola'}}},
      products:['a','b','c'].map((id,index)=>({id,name:'Product '+id,
        source:index===2?{role:'concept',description:'New idea',provenance:{}}:
          {role:'finished_scene',path:'Campaigns/Test-12345678/Assets/Products/'+id+'.jpg',provenance:{}}}))};
    model={version:'v1',concept_preview_version:'v1',concept_previews:{},
      run:{id:'run1',state:'planned'},plan_brief:copy(draft),
      plan:{digest:'exact',generation_products:['c'],generation_requests:{c:{}},findings:[],cost_estimate:{estimated_usd:.1}},
      manifest:null,run_attempts:[]};
    currentBriefValue=()=>copy(draft);
  `);
  const plain = code => JSON.parse(run(`JSON.stringify(${code})`));
  return {run, plain};
}

test('only a current campaign preparation claims work is running',()=>{
  const h=harness();h.run("model.unplanned_draft=true;productionPreparation={campaign:'other',status:'pending'}");
  let html=h.run('productionView(productionContext())');
  assert.match(html,/Waiting for changes to save/);assert.doesNotMatch(html,/Preparing estimate/);
  assert.match(html,/data-pr-action="start" disabled/);
  h.run("productionPreparation={campaign:draft.campaign_id,basis:'earlier local values',status:'pending'}");
  html=h.run('productionView(productionContext())');assert.match(html,/Preparing estimate/);
  assert.match(html,/data-pr-action="start" disabled/);
});

test('reviewed missing concept means one source request, while the same board owns the product actions', () => {
  const h=harness(), view=h.plain('productionContext()');
  assert.equal(view.total,18);
  assert.deepEqual(view.products.map(p=>p.id),['a','b','c']);
  assert.equal(view.products[2].sourceLabel,'Image will be generated.');
  assert.equal(view.products[2].image,null);
  assert.equal(view.products[2].requested,true);
  assert.equal(view.sourceSummary,'2 photos supplied · 1 image will be generated');
  h.run("model.plan.generation_products=['b','c']");
  assert.match(h.run('productionContext().sourceSummary'),/2 images will be generated$/);
  const html=h.run('productionView(productionContext())');
  assert.match(html,/data-pr-action="edit" data-pr-id="c"/);
  assert.equal((html.match(/pr-product-card/g)||[]).length,3);
  assert.doesNotMatch(html,/<table|To create|To update/);
});

test('draft add/remove/replacement displays current products and attachments while old plan stays separate', () => {
  const h=harness();
  h.run(`model.manifest={outputs:[{id:'old-a',product_id:'a',market:'en-US',ratio:'1:1',url:'/old.png'}]};
    model.output_run_id='run1';model.run.state='complete';
    draft.products.shift();draft.products[0].source.path='Campaigns/Test-12345678/Assets/Products/new.jpg';
    draft.products.push({id:'d',name:'New D',source:{role:'concept',provenance:{}}});dirty=true;`);
  const view=h.plain('productionContext()');
  assert.deepEqual(view.products.map(p=>p.id),['b','c','d']);
  assert.equal(view.products[0].image,'/uploads/new.jpg');
  assert.equal(view.plan,null);
  assert.equal(h.run('model.plan.digest'),'exact');
  assert.equal(h.run('model.plan_brief.products[0].id'),'a');
  assert.doesNotMatch(h.run('productionView(productionContext())'),/Product changes are saved/);
});

test('a zero-request replan labels saved concept reuse and saved outputs without calling the new plan complete', () => {
  const h=harness();
  h.run(`model.plan.generation_products=[];model.concept_previews={c:'/saved-master'};
    model.manifest={complete:true,outputs:[{id:'old',product_id:'a',market:'en-US',ratio:'1:1',url:'/old.png'}]};
    model.output_run_id='previous';`);
  const view=h.plain('productionContext()');
  assert.equal(view.products[2].sourceLabel,'Saved generated image');
  assert.equal(view.complete,false);
  assert.equal(view.products[0].outputSummary,'1 saved image');
  assert.equal(view.sourceSummary,'2 photos supplied · 1 saved generated image');
  assert.match(h.run('productionView(productionContext())'),/Generate campaign images/);
});

test('current partial counts actual accepted files and cannot invent format completion', () => {
  const h=harness();
  h.run(`model.run.state='partial';model.output_run_id='run1';model.rule_withheld_outputs=['held'];
    model.file_integrity={damaged_outputs:['damaged']};
    model.manifest={complete:false,outputs:[
      {id:'ready',product_id:'b',market:'es-MX',ratio:'9:16',url:'/actual.png'},
      {id:'held',product_id:'a',market:'en-US',ratio:'1:1',url:null},
      {id:'damaged',product_id:'b',market:'en-US',ratio:'16:9',url:null}]};`);
  const view=h.plain('productionContext()');
  assert.equal(view.products[1].outputSummary,'1 of 2 saved');
  assert.equal(view.products[0].outputSummary,'No saved images');
  assert.equal(view.complete,false);
});

test('active source status uses only current-run progress', () => {
  const h=harness();
  h.run(`model.run.state='running';operation={run_id:'run1',active:true,completed:5,
    attempts:[{product_id:'c',state:'dispatching'}]};`);
  const view=h.plain('productionContext()');
  assert.equal(view.products[2].sourceLabel,'Generating image…');
  assert.equal(view.active,true);
  h.run("operation.run_id='another-run'");
  assert.equal(h.run('productionContext().active'),false);
});

test('saved responses, failed decoding, and uncertain attempts retain distinct source truth', () => {
  const h=harness();
  h.run("model.run.state='partial';model.output_run_id='run1'");
  for (const [attempt,label] of [
    [{state:'received',response_recoverable:true},'Result saved for recovery'],
    [{state:'received',response_recoverable:false,error_kind:'saved_response_decode'},'Image not created'],
    [{state:'unknown'},'Result not confirmed'],
    [{state:'decoded',source_available:true},'Saved generated image'],
  ]) {
    h.run(`model.run_attempts=[{product_id:'c',...${JSON.stringify(attempt)}}]`);
    assert.equal(h.run('productionContext().products[2].sourceLabel'),label);
    assert.equal(h.run('productionContext().products[2].outputSummary'),'No saved images');
  }
});

test('a saved draft cannot reuse outdated concept facts before the guarded refresh', () => {
  const h=harness();
  h.run("model.unplanned_draft=true;model.version='v2';model.concept_previews={c:'/old-master'}");
  assert.equal(h.run('productionContext().products[2].image'),null);
  h.run("model.concept_preview_version='v2';model.concept_previews={c:'/verified-master'}");
  assert.equal(h.run('productionContext().products[2].image'),'/verified-master');
  assert.equal(h.run('productionContext().plan'),null);
});

test('component escapes names and shows supplied photos as source photos', () => {
  const h=harness();
  h.run(`draft.products[0].name='<img onerror="bad()">';dirty=true`);
  const html=h.run('productionView(productionContext())');
  assert.doesNotMatch(html,/<img onerror=/);
  assert.match(html,/&lt;img onerror=/);
  assert.equal((html.match(/src="\/uploads\/a.jpg"/g)||[]).length,1);
});

test('reviewed zero-request plan and empty campaign never offer an unreviewed Start', () => {
  const h=harness();
  h.run(`model.plan.generation_products=[];model.concept_previews={c:'/verified-master'};model.plan.cost_estimate={estimated_usd:0};`);
  let html=h.run('productionView(productionContext())');
  assert.match(html,/2 photos supplied · 1 saved generated image/);
  assert.doesNotMatch(html,/No new images need generation/);
  assert.doesNotMatch(html,/Composition settings|data-pr-input="consent"/);
  assert.match(html,/data-pr-action="start" >Generate campaign images/);
  assert.doesNotMatch(html,/Dispatched requests|automatic paid retries/);
  assert.match(html,/Uses your existing images to create all three formats with your logo and wording\. No AI requests\./);
  assert.match(html,/Estimated AI cost/);
  h.run("model.plan.generation_products=['c']");
  const paid=h.run('productionView(productionContext())');
  assert.match(paid,/Ready to generate\./);
  assert.match(paid,/data-pr-action="start" >Generate campaign images/);
  assert.match(paid,/Exact prompt &amp; settings|Exact prompt & settings/);
  h.run('draft.products=[];dirty=true');
  html=h.run('productionView(productionContext())');
  assert.match(html,/Add your first product/);
  assert.doesNotMatch(html,/data-pr-action="start"|estimated|Product changes are saved/);
});

test('blocking source and brand reasons retain their specific existing field remedy', () => {
  const h=harness();
  h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`blockedProposal={campaign_id:draft.campaign_id,version:model.version,findings:[
    {severity:'blocking',rule:'asset',evidence:'Product photograph cannot be read.',remedy:'Replace it.',context:{product_id:'a',fields:[{path:['products',0,'source'],product_index:0}]}},
    {severity:'blocking',rule:'asset',evidence:'Logo cannot be read.',remedy:'Attach a valid logo.',context:{fields:[{path:['brand','logo_path']}]}}]};wordingNotice=()=>'';`);
  const html=h.run('production()');
  assert.match(html,/Replace the unavailable photo/);
  assert.match(html,/Product photograph cannot be read\. Replace it\./);
  assert.match(html,/data-action="planInput:0"[^>]*>Edit product/);
  assert.match(html,/data-action="planInput:1"[^>]*>Open Campaign/);
  assert.doesNotMatch(html,/data-pr-action="start"/);
});

test('missing photo exposes both exact choices and active work prevents either mutation', () => {
  const h=harness();h.run("draft.products[0].source.path='';model.plan_brief=copy(draft)");
  let html=h.run('productionView(productionContext())');
  assert.match(html,/data-pr-action="choose" data-pr-id="a">Choose existing image/);
  assert.match(html,/data-pr-action="choose-concept" data-pr-id="a">Generate new image/);
  h.run("model.run.state='running';operation={run_id:'run1',active:true}");
  html=h.run('productionView(productionContext())');
  assert.match(html,/data-pr-action="choose" disabled data-pr-id="a"/);
  assert.match(html,/data-pr-action="choose-concept" disabled data-pr-id="a"/);
});

test('empty-photo choices target the same product and select a mode without plan or dispatch', async () => {
  const h=harness();h.run("C={products:draft.products}");
  h.run('productDraft=id=>captureChoice(id);setProductMode=mode=>captureChoice(mode)');
  h.run('dispatchAction=()=>{throw Error("No producer action is admitted by choosing a mode")}');
  h.run('captureChoice=value=>choices.push(value)');h.run('choices=[]');
  await h.run('producerAction({dataset:{prAction:"choose-concept",prId:"a"}})');
  await h.run('producerAction({dataset:{prAction:"choose",prId:"a"}})');
  assert.deepEqual(h.plain('choices'),['a',true,'a',false]);
});

test('returning to existing-image mode restores a retained photo only when no current selection exists', () => {
  const h=harness();
  h.run(`modal='product';modalProduct={id:'a',name:'Original',description:'Keep this description',
    treatment:'concept',source:{role:'concept',description:'Keep this description'}};
    retainedProductPhoto=()=>({source:{role:'finished_scene',path:'retained.jpg'},url:'/retained.jpg'});
    captureProductInputs=()=>{};openModal=()=>{};
    captureProject=()=>{throw Error('Source mode must not capture campaign state')};
    projectChanged=()=>{throw Error('Source mode must not enqueue a save')};`);
  h.run('setProductMode(false)');
  assert.deepEqual(h.plain('modalProduct.source'),{role:'finished_scene',path:'retained.jpg'});
  assert.equal(h.run('modalProduct.image'),'/retained.jpg');
  assert.equal(h.run('modalProduct.description'),'Keep this description');
  h.run("modalProduct.source={role:'finished_scene',path:'replacement.jpg'};modalProduct.image='/replacement.jpg';setProductMode(false)");
  assert.equal(h.run('modalProduct.source.path'),'replacement.jpg');
  h.run('setProductMode(true)');
  assert.equal(h.run('modalProduct.source.path'),'replacement.jpg');
  h.run("modalProduct.source={role:'concept'};retainedProductPhoto=()=>null;setProductMode(false)");
  assert.equal(h.run('modalProduct.treatment'),'reuse');
  assert.equal(h.run('modalProduct.source.path'),undefined);
});
