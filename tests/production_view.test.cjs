// Exercise the shipped read-only Production projection with separate draft,
// reviewed-plan, and accepted-output identities. No renderer or provider calls.
const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');

function harness() {
  const context = vm.createContext({
    document: { body: { dataset: { page: 'production', cap: '5' } }, addEventListener() {} },
    structuredClone, retainStudioGallery() {}, damagedFiles: () => false, cancelProductUpload() {}, window: {},
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
    currentBriefValue=()=>copy(draft);keyAvailable=true;
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

test('fresh import composition previews cannot masquerade as a ready generation run',async()=>{
  const h=harness();
  h.run("model.run=null;model.composition_version='v1';model.unplanned_draft=false;dirty=false;page='production';modal='';busy=false;let preparations=0;getSaveState=()=>null;prepareCurrentPlan=()=>{preparations++}");
  assert.equal(h.plain('productionContext()').pending,true);
  const html=h.run('productionView(productionContext())');
  assert.match(html,/data-pr-action="start" disabled/);
  assert.doesNotMatch(html,/Ready to generate/);
  h.run('ensureProductionPlan()');assert.equal(h.run('preparations'),1);
  await assert.rejects(h.run('startDisplayedPlan()'),/current plan is not ready/);
  h.run("model.run={id:'new-production-run',state:'planned'};ensureProductionPlan()");
  assert.equal(h.run('preparations'),1,'the resulting run is not prepared again');
  assert.equal(h.plain('productionContext()').pending,false);
  assert.match(h.run('productionView(productionContext())'),/Generate missing images/);
});

test('a completed earlier run cannot start a newly added concept request',async()=>{
  const h=harness();
  h.run("model.run.state='complete';model.unplanned_draft=false;page='production';modal='';busy=false;getSaveState=()=>null;let prepared=0;prepareCurrentPlan=()=>prepared++;");
  assert.equal(h.run('productionContext().pending'),true);
  h.run('ensureProductionPlan()');assert.equal(h.run('prepared'),1);
  await assert.rejects(h.run('startDisplayedPlan()'),/current plan is not ready/);
});

test('changed generated-image inputs explain the replacement before a paid request',()=>{
  const h=harness();
  h.run("model.plan.changed_source_products=[{id:'c',name:'Brass bells'}]");
  const html=h.run('productionView(productionContext())');
  assert.match(html,/The image instructions changed/);
  assert.match(html,/Brass bells: the previous generated image/);
  assert.match(html,/Review the estimate before generating a replacement/);
  h.run('model.plan.changed_source_products=[]');
  assert.doesNotMatch(h.run('productionView(productionContext())'),/The image instructions changed/);
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

test('a source-ready current campaign opens Production without a historical run', () => {
  const h=harness();
  h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`model.run=null;model.output_run_id=null;model.composition_version='v1';
    model.plan.generation_products=[];model.plan.cost_estimate={estimated_usd:0};
    model.concept_previews={c:'/verified-master'};
    model.manifest={complete:true,outputs:draft.products.flatMap(p=>
      ['en-US','es-MX'].flatMap(m=>['1:1','9:16','16:9'].map(r=>({product_id:p.id,market:m,ratio:r,url:'/preview.png'}))))};
    wordingNotice=()=>'';`);
  const html=h.run('production()');
  assert.match(html,/Your source images are available/);
  assert.match(html,/18 campaign previews/);
  assert.match(html,/6 previews/);
  assert.match(html,/finished images are created when you download/i);
  assert.doesNotMatch(html,/data-pr-action="start"|images saved|saved image/);
  assert.equal(h.run('model.run'),null);
});

test('a current campaign with a missing source still offers its exact reviewed request', () => {
  const h=harness();
  h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`model.composition_version='v1';model.manifest={complete:false,outputs:[]};wordingNotice=()=>'';`);
  const html=h.run('production()');
  assert.match(html,/1 new AI request/);
  assert.match(html,/18 images at export/);
  assert.match(html,/data-pr-action="start" >Generate missing images/);
  assert.doesNotMatch(html,/create all three formats|Campaign images are ready/);
});

test('a new campaign without a source or run can open Production while preparation is pending', () => {
  const h=harness();
  h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`model.run=null;model.plan=null;model.composition_version='v1';model.unplanned_draft=true;wordingNotice=()=>'';`);
  const html=h.run('production()');
  assert.match(html,/Waiting for changes to save/);
  assert.match(html,/data-pr-action="start" disabled/);
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
  assert.match(h.run('productionView(productionContext())'),/Prepare source images/);
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
    [{state:'decoded',source_available:true},'Source image received'],
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
  assert.match(html,/data-pr-action="start" >Prepare source images/);
  assert.doesNotMatch(html,/Dispatched requests|automatic paid retries/);
  assert.match(html,/Uses your existing imagery\. No AI requests\./);
  assert.doesNotMatch(html,/Estimated AI cost/);
  h.run("model.plan.generation_products=['c']");
  const paid=h.run('productionView(productionContext())');
  assert.match(paid,/Ready to generate\./);
  assert.match(paid,/data-pr-action="start" >Generate missing images/);
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
  assert.match(html,/data-pr-action="choose" data-pr-id="a">Select existing image/);
  assert.match(html,/data-pr-action="choose-concept" data-pr-id="a">Generate new image/);
  h.run("model.run.state='running';operation={run_id:'run1',active:true}");
  html=h.run('productionView(productionContext())');
  assert.doesNotMatch(html,/data-pr-action="choose(?:-concept)?" data-pr-id=/);
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
    projectChanged=()=>{};`);
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

test('selected generation stays selected while unspecified images offer both starting choices', () => {
  const h=harness();
  let html=h.run('productionView(productionContext())');
  const concept=html.match(/<article class="pr-product-card">[\s\S]*?<\/article>/g)[2];
  assert.match(concept, /pr-concept-art/);
  assert.match(concept, /Image will be generated\./);
  assert.doesNotMatch(concept, /data-pr-action="choose(?:-concept)?"/);
  h.run("draft.products[0].source.path='';model.plan_brief=copy(draft)");
  html=h.run('productionView(productionContext())');
  const panel=html.match(/<aside class="pr-action-panel">[\s\S]*?<\/aside>/)[0];
  assert.match(panel, /Add product images.*Product a.*Choose image/s);
  assert.doesNotMatch(panel, /Try again|Correct this input|schema|data-pr-action="start"/);
});

test('ready generation places cost and the actionable key or generate control before collapsed details', () => {
  const h=harness();
  for(const key of [false,true]) {
    h.run(`keyAvailable=${key}`);
    const html=h.run('productionView(productionContext())');
    const panel=html.match(/<aside class="pr-action-panel">[\s\S]*?<\/aside>/)[0];
    assert.ok(panel.indexOf('$0.10') < panel.indexOf('data-pr-action="'+(key?'start':'key')+'"'));
    assert.ok(panel.indexOf('data-pr-action="'+(key?'start':'key')+'"') < panel.indexOf('<details'));
    assert.match(panel, /<details class="pr-request-details"><summary>Request details/);
    assert.doesNotMatch(panel, /<details[^>]*\bopen\b/);
  }
});

test('replacement key changes the single primary action to Resume without retaining the rejected-key warning', () => {
  const h=harness();h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`model.run.state='partial';model.notice={code:'rejected_key',message:'Old rejected key'};
    model.retry_plan={generation_products:['c']};model.run_attempts=[{product_id:'c',state:'failed',created_at:'2026-11-07T18:00:00Z'}];
    wordingNotice=()=>'';keySavedAt=Date.parse('2026-11-07T17:00:00Z')/1000;`);
  let html=h.run('production()');
  assert.match(html,/data-pr-action="update-key"[^>]*>Update API key/);
  assert.doesNotMatch(html,/data-pr-action="resumeGeneration"/);
  h.run("keySavedAt=Date.parse('2026-11-07T19:00:00Z')/1000");
  html=h.run('production()');
  assert.match(html,/Key updated\. Resume to try again\./);
  assert.match(html,/data-pr-action="resumeGeneration"[^>]*>Resume generation/);
  assert.doesNotMatch(html,/Old rejected key|OpenAI did not accept|data-pr-action="update-key"/);
});

test('Studio missing sources go to Production while source-backed invalid layouts remain repairable', async () => {
  const h=harness();
  h.run(`C={products:draft.products,markets:['en']};model.can_edit=true;model.editable_products={a:true};
    outputFor=()=>({url:null});reviewTab='products';imageMarket='all';lang='en';
    dispatchAction=action=>dispatched.push(action);dispatched=[];render=()=>{};`);
  let html=h.run('studioView()');
  const cards=html.match(/<article class="pr-studio-card">[\s\S]*?<\/article>/g);
  assert.match(cards[0], /data-pr-action="open-product"/);
  assert.match(cards[0], /Adjust this layout in Studio/);
  assert.match(cards[1], /data-pr-action="add-image" data-pr-id="b"/);
  assert.match(cards[1], /pr-add-image-plus/);
  await h.run('producerAction({dataset:{prAction:"open-product",prId:"b"}})');
  assert.deepEqual(h.plain('dispatched'),[{action:'addProductImage:b'}]);
  await h.run('producerAction({dataset:{prAction:"open-product",prId:"a"}})');
  assert.equal(h.run('reviewTab'),'product');
  h.run("reviewTab='overview'");html=h.run('studioView()');
  assert.match(html,/data-pr-action="add-image" data-pr-id="b"/);
  assert.doesNotMatch(html,/No saved Square image/);
});

test('normal incomplete Studio has missing-image actions without a partial-recovery banner', () => {
  const h=harness();h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`C={products:draft.products};model.manifest={complete:false,outputs:[{id:'missing',url:null}]};
    wordingNotice=()=>'';layoutNotice=()=>'';studioView=()=>'<button>Add image</button>';reviewTab='products';`);
  const html=h.run('review()');
  assert.match(html,/Add image/);assert.doesNotMatch(html,/partial saved|Review recovery|Recover locally/);
});

test('switching an existing concept to a photo removes concept-only source fields', () => {
  const h=harness();
  h.run(`modal='product';modalProduct={id:'c',name:'Photo',description:'Description',origin:'Producer',rights:'Owned',
    treatment:'reuse',source:{role:'concept',description:'Obsolete generation instruction',imagery_absent:true,path:'photo.png'}};
    document.querySelector=selector=>selector==='#product-name'?{value:'Photo'}:{value:selector==='#product-description'?'Description':'Producer'};`);
  h.run('captureProductInputs()');
  const source=h.plain('draft.products[2].source');
  assert.equal(source.role,'finished_scene');assert.equal(source.path,'photo.png');
  assert.ok(!('imagery_absent' in source));assert.ok(!('description' in source));
});

test('finished-scene upload guidance states the transparency restriction without changing prepared-product guidance', () => {
  const h=harness();
  const view={product:{name:'Test product',mode:'photo',source:{},treatment:'reuse'},existing:true};
  h.run(`editorTestView=${JSON.stringify(view)}`);
  assert.match(h.run('MotifProducerRepair.productEditor(editorTestView)'), /Use an image with a background and no transparent areas\./);
  h.run("editorTestView.product.treatment='prepared'");
  assert.doesNotMatch(h.run('MotifProducerRepair.productEditor(editorTestView)'), /no transparent areas/);
  h.run("editorTestView.product.mode='concept'");
  assert.doesNotMatch(h.run('MotifProducerRepair.productEditor(editorTestView)'), /no transparent areas/);
});

test('a product-specific validation remedy is shown once without a second generic error panel', () => {
  const h=harness();h.run(readFileSync('src/campaign_studio/static/motif/presentation.js','utf8'));
  h.run(`blockedProposal={campaign_id:draft.campaign_id,version:model.version,findings:[{
    severity:'blocking',rule:'asset',evidence:'This product image has transparent areas.',
    remedy:'Add a background in an image editor, then upload it again, or select a different image.',
    context:{product_id:'a',fields:[{path:['products',0,'source'],product_index:0}]}}]};wordingNotice=()=>'';`);
  const html=h.run('production()'), panel=html.match(/<aside class="pr-action-panel">[\s\S]*?<\/aside>/)[0];
  assert.match(panel,/Product a.*This product image has transparent areas.*Edit product/s);
  assert.equal((panel.match(/transparent areas/g)||[]).length,1);
  assert.doesNotMatch(panel,/Check the campaign details|Try again|Add product images|Correct this input/);
});


test('new generation choice updates the card before plan preparation and survives the saved projection', () => {
  const h=harness();
  h.run("draft.products[0].source={role:'finished_scene',path:''};dirty=true");
  let html=h.run('productionView(productionContext())');
  assert.match(html.match(/<article class="pr-product-card">[\s\S]*?<\/article>/g)[0], /data-pr-action="choose-concept"/);
  h.run("draft.products[0].source={role:'concept',description:'An outdoor product photo',imagery_absent:true}");
  for(const saved of [false,true]) {
    if(saved) h.run("dirty=false;model.version='v2';model.concept_preview_version='v2';model.plan_brief=copy(draft);model.plan.generation_products=['a','c']");
    html=h.run('productionView(productionContext())');
    const card=html.match(/<article class="pr-product-card">[\s\S]*?<\/article>/g)[0];
    assert.match(card, /pr-concept-art/);
    assert.match(card, /Image will be generated/);
    assert.doesNotMatch(card, /data-pr-action="choose(?:-concept)?"/);
  }
});

test('OpenAI key management remains reachable when every source is supplied', () => {
  const h=harness();
  h.run("model.composition_version='v1';model.plan.generation_products=[];model.concept_previews={c:'/source'};model.manifest={complete:true,outputs:[{product_id:'a',url:'/preview'}]}");
  const html=h.run('productionView(productionContext())');
  assert.match(html, /class="pr-key-access"[^]*data-pr-action="key"[^>]*>OpenAI key/);
});
test('key access stays visible during missing-source validation without duplicating the primary key action', () => {
  const h=harness();
  h.run("keyAvailable=false");
  let html=h.run('productionView(productionContext())');
  assert.equal((html.match(/data-pr-action="key"/g)||[]).length,1);
  assert.match(html,/Add OpenAI API key/);
  h.run("draft.products[0].source={role:'finished_scene',path:''};dirty=true");
  html=h.run('productionView(productionContext())');
  assert.match(html,/Add product images/);
  assert.equal((html.match(/data-pr-action="key"/g)||[]).length,1);
  assert.match(html,/OpenAI key/);
});

test('regeneration action is limited to server-admitted generated sources and hidden while active',()=>{
 const h=harness();
 h.run("model.plan.generation_products=[];model.run.state='complete';model.composition_version='v1';model.regeneratable_products=['a'];model.source_urls={a:'old-a',b:'old-b'};model.manifest={outputs:[{id:'a/en-US/1x1',product_id:'a',url:'old-a'}]};");
 let html=h.run('productionView(productionContext())');
 assert.equal((html.match(/data-pr-action="regenerate"/g)||[]).length,1);
 assert.match(html,/data-pr-action="regenerate" data-pr-id="a"/);
 h.run("operation={run_id:'run1',active:true}");
 html=h.run('productionView(productionContext())');assert.doesNotMatch(html,/data-pr-action="regenerate"/);
});

test('regeneration confirmation names target, paid request, preservation, and exact settings',()=>{
 const h=harness();
 h.run("dialog=(title,body,footer)=>title+body+footer;regenerationReview={campaign:draft.campaign_id,product_name:'Only Alpha',background_only:false,cost_estimate:{estimated_usd:.1,pricing_date:'2026-10-29'},request:{model:'saved-model',size:'1920x1920',quality:'high',deadline_seconds:120,prompt:'Actual prompt'}}");
 const html=h.run('regenerationDialog()');
 assert.match(html,/Regenerate image for Only Alpha/);assert.match(html,/Confirm 1 paid request/);
 assert.match(html,/Failure, timeout, or Stop keeps the current image/);assert.match(html,/Other products stay unchanged/);
 assert.match(html,/Actual prompt/);assert.match(html,/zero automatic paid retries/);
 h.run('regenerationReview.confirming=true');assert.match(h.run('regenerationDialog()'),/disabled/);
 h.run('regenerationReview.confirming=false;regenerationReview.uncertain=true');
 const uncertain=h.run('regenerationDialog()');
 assert.match(uncertain,/>Close dialog</);
 assert.doesNotMatch(uncertain,/>Keep current image</);
 assert.match(uncertain,/Closing this dialog does not stop it/);
 assert.match(uncertain,/then use Stop regeneration in Production/);
 assert.match(uncertain,/accepted result may already be in use/);
});


test('uncertain confirmation checks only saved status and retains an explicit paid choice when absent',async()=>{
 const h=harness();
 h.run(`regenerationReview={campaign:draft.campaign_id,version:'v1',product_id:'a',product_name:'Alpha',operation_id:'same-operation',digest:'same-digest',uncertain:true,confirming:false};
 modal='regenerate';page='production';let lookups=[];
 api=async(path,body)=>{lookups.push({path,body});return {found:false,message:'No confirmed request is recorded yet. This check sent no image request.'};};
 openModal=()=>{};refreshProductionStatus=()=>{};`);
 await h.run('confirmRegeneration()');
 assert.equal(h.run('lookups.length'),1);
 assert.match(h.run('lookups[0].path'),/regeneration\/status$/);
 assert.equal(h.run('lookups[0].body.operation_id'),'same-operation');
 assert.equal(h.run('regenerationReview.notRecorded'),true);
 const notice=h.run('unresolvedRegenerationNotice()');
 assert.match(notice,/Check this confirmed request/);
 assert.match(notice,/Review a new paid request/);
 assert.match(notice,/This check sent no image request/);
});
