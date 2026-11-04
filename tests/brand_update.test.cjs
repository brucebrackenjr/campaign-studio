const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function harness() {
  const brand = {id:'brand',name:'Brand',logo_path:'logo.png',logo_provenance:{label:'Owner'},palette:['#FFFFFF','#112233'],text_color:'#FFFFFF'};
  const product = {id:'p',framing:{'en-US':{'1:1':{text_placements:{message:{color:'#112233',left:0.2,font_id:'noto_sans'}}}}}};
  const accepted = {brand,products:[product]};
  const c = vm.createContext({
    copy:structuredClone, model:{manifest:{revision_id:'revision'},output_run_id:'run',output_brief:structuredClone(accepted),version:'version'},
    MotifBriefFlow:{normalizeHex:value=>typeof value==='string'&&/^#?[0-9a-f]{6}$/i.test(value.trim())?'#'+value.trim().replace(/^#/,'').toUpperCase():null},
    draft:{campaign_id:'campaign',...structuredClone(accepted),audience:'Unrelated unfinished edit'},
    page:'campaign',tab:'brand',modal:null,projectBuffers:{form_drafts:{fields:{'brief/campaign/en':{audience:'Keep this'}}}},
    toast(){},refreshBriefFlow(){},markDirty(){},saveDraft:async()=>{},saveProject:async()=>true,
    briefBrandColorProblem:()=>'',
    withBriefPreviewPaused:fn=>fn(),withProjectApply:fn=>fn(),
    currentBriefValue:()=>c.draft, openModal:name=>c.modal=name, closeModal:()=>c.modal=null,
    api:async()=>{throw Error('Unexpected API call')},
  });
  const run = code=>vm.runInContext(code,c);
  run(readFileSync('src/campaign_studio/static/motif/brand-update.js','utf8'));
  return {c,run};
}
function queueHarness() {
  const h=harness(),writes=[],events=[];
  Object.assign(h.c,{structuredClone,setTimeout:()=>1,clearTimeout(){},modalView:()=>'',
    dirty:false,studioWorkspace:null,pendingNav:null,reviewTab:'products',updateURL(){},scrollTo(){}});
  h.run(readFileSync('src/campaign_studio/static/motif/project.js','utf8'));
  const actions=readFileSync('src/campaign_studio/static/motif/actions.js','utf8');
  h.run(actions.slice(0,actions.indexOf('document.addEventListener("keydown"')));
  h.c.toast=()=>{};h.c.render=()=>{};
  h.c.openModal=name=>{h.c.modal=name;events.push(name);};h.c.closeModal=()=>h.c.modal=null;
  h.c.sendAuthored=async(id,body)=>{
    writes.push(structuredClone(body));
    if(writes.length>=2) return h.c.settleFollowup(body);
    return {version:'uploaded-saved',saved_at:'now'};
  };
  h.c.settleFollowup=async()=>({version:'applied-saved',saved_at:'later'});
  h.run(`projectOwner='campaign';projectQueue=projectSaveQueue({
    capture:()=>({brief:copy(draft),...copy(projectBuffers)}),restore(){},
    send:sendAuthored,read:async()=>{},acknowledged:ack=>model.version=ack.version});
    projectQueue.open('campaign',{version:'version',base:{run_id:'run',revision_id:'revision'},
      authored:{brief:copy(draft),...copy(projectBuffers)}},{restoreState:false});`);
  let applied;
  const didApply=new Promise(resolve=>applied=resolve);
  h.c.api=async()=>({ok:true});
  h.c.useData=()=>{
    h.c.draft.brand.logo_path='Runs/Inputs/accepted-logo.png';
    h.c.model.output_brief=structuredClone(h.c.draft);
    h.run(`projectQueue.applied({version:'image-applied',base:{run_id:'run',revision_id:'new-revision'},
      authored:{brief:copy(draft),...copy(projectBuffers)}})`);
    applied();
  };
  h.c.draft.brand.logo_path='Assets/Brand/uploaded-logo.png';
  return {...h,writes,events,didApply};
}

test('Update and continue settles the real queue after a logo snapshot change before the ordinary navigation guard',async()=>{
  const h=queueHarness();let resolveFollowup;
  h.c.settleFollowup=()=>new Promise(resolve=>resolveFollowup=resolve);
  h.run('guardBrandNavigation({kind:"page",target:"review"})');
  const continuing=h.run('finishBrandNavigation()');
  await Promise.race([h.didApply,continuing.then(()=>assert.fail('navigation completed before the image update'))]);
  await new Promise(setImmediate);
  assert.equal(h.writes.length,2,'the accepted logo path needs its existing follow-up Save');
  assert.equal(h.c.page,'campaign','navigation waits for the Save acknowledgement');
  assert.equal(h.events.includes('unsaved'),false,'do not replace the explicit continue choice with a second dialog');
  assert.equal(h.writes[1].authored.brief.brand.logo_path,'Runs/Inputs/accepted-logo.png');
  assert.equal(h.writes[1].authored.brief.audience,'Unrelated unfinished edit');
  resolveFollowup({version:'applied-saved',saved_at:'later'});await continuing;
  assert.equal(h.c.page,'review');assert.equal(h.c.modal,null);
  assert.equal(h.run('projectQueue.isDirty()'),false);
  assert.equal(h.events.includes('unsaved'),false);
  h.c.draft.audience='A later unsaved change';
  await h.run('navigate("handoff")');
  assert.equal(h.c.modal,'unsaved','the general guard still protects genuinely new edits');
  assert.equal(h.c.page,'review');
});

test('post-update Save failure or conflict retains the destination and retries without applying images again',async()=>{
  for(const conflict of [false,true]) {
    const h=queueHarness();let applies=0;
    h.c.api=async()=>{applies++;return {ok:true};};
    h.c.settleFollowup=async()=>{throw Object.assign(new Error(conflict?'Newer changes were saved elsewhere.':'Save connection failed.'),
      conflict?{code:'authored_conflict',status:409}:{});};
    h.run('guardBrandNavigation({kind:"page",target:"review"})');
    await h.run('finishBrandNavigation()');
    assert.equal(h.c.page,'campaign');assert.equal(h.c.modal,'brand-navigation');
    assert.equal(h.run('brandDestination.target'),'review');assert.equal(applies,1);
    assert.match(h.run('brandUpdateError'),conflict?/Newer changes/:/connection failed/);
    assert.equal(h.c.draft.brand.logo_path,'Runs/Inputs/accepted-logo.png');
    if(!conflict) {
      h.c.settleFollowup=async()=>({version:'applied-saved',saved_at:'later'});
      await h.run('finishBrandNavigation()');
      assert.equal(h.c.page,'review');assert.equal(applies,1);
      assert.equal(h.run('projectQueue.isDirty()'),false);
    } else {
      const writes=h.writes.length;
      await h.run('finishBrandNavigation()');
      assert.equal(h.c.page,'campaign');assert.equal(h.c.modal,'brand-navigation');
      assert.equal(h.writes.length,writes,'a paused conflict cannot be retried as an ordinary Save');
      assert.equal(applies,1);
    }
  }
});
test('an invalid follow-up draft keeps its specific message and destination without a navigation attempt',async()=>{
  const h=harness();
  h.c.draft.brand.logo_path='new.png';
  h.c.api=async()=>({ok:true});
  h.c.useData=()=>{h.c.model.output_brief=structuredClone(h.c.draft);};
  h.c.saveProject=async()=>false;
  h.c.getSaveState=()=>({validationMessage:'Shorten the headline to 120 characters.'});
  h.c.navigate=()=>assert.fail('invalid work must remain editable on Brand');
  h.run('guardBrandNavigation({kind:"page",target:"review"})');
  await h.run('finishBrandNavigation()');
  assert.equal(h.c.modal,'brand-navigation');
  assert.equal(h.run('brandDestination.target'),'review');
  assert.equal(h.run('brandUpdateError'),'Shorten the headline to 120 characters.');
  assert.equal(h.c.draft.audience,'Unrelated unfinished edit');
});
test('Brand leave guard includes saved-but-unapplied changes, not unrelated draft edits',()=>{
  const h=harness();
  assert.equal(h.run('brandChangesPending()'),false);
  h.c.draft.brand.text_color='#112233';
  assert.equal(h.run('guardBrandNavigation({kind:"tab",target:"copy"})'),true);
  assert.equal(h.c.modal,'brand-navigation');
  assert.equal(h.run('brandDestination.target'),'copy');
  h.c.page='production';
  assert.equal(h.run('guardBrandNavigation({kind:"page",target:"review"})'),false);
});
test('discard restores Brand and explicit color resets, preserving unrelated edits and layout',async()=>{
  const h=harness();
  h.c.draft.brand.text_color='#112233'; h.c.draft.brand.logo_path='new.png';
  const placement=h.c.draft.products[0].framing['en-US']['1:1'].text_placements.message;
  placement.color=null; placement.left=0.3;
  h.c.projectBuffers.form_drafts.brand_color_defaults=[{product_id:'p',market:'en-US',ratio:'1:1'}];
  h.c.projectBuffers.form_drafts.fields['brief/brand/en']={'text-color':'#112233','copy-headline':'Keep headline'};
  let saved=0;h.c.saveDraft=async()=>saved++;
  await h.run('discardBrandChanges()');
  assert.equal(h.c.draft.brand.logo_path,'logo.png');
  assert.equal(h.c.draft.brand.text_color,'#FFFFFF');
  assert.equal(placement.color,'#112233');
  assert.equal(placement.left,0.3);
  assert.equal(h.c.draft.audience,'Unrelated unfinished edit');
  assert.equal(h.c.projectBuffers.form_drafts.fields['brief/brand/en']['copy-headline'],'Keep headline');
  assert.equal(saved,1);
  assert.equal(h.run('brandChangesPending()'),false);
});
test('Update sends guarded local Brand request and waits before continuing',async()=>{
  const h=harness(),events=[];
  h.c.draft.brand.logo_path='new.png';
  h.c.api=async(url,body)=>{events.push([url,body]);return {ok:true}};
  h.c.useData=(data,options)=>{events.push(['accepted',data.ok,options.applied]);h.c.model.output_brief=structuredClone(h.c.draft)};
  h.c.navigate=async target=>events.push(['navigate',target]);
  h.run('guardBrandNavigation({kind:"page",target:"handoff"})');
  await h.run('finishBrandNavigation()');
  assert.equal(events[0][0],'/api/motif/campaigns/campaign/brand/apply');
  assert.equal(events[0][1].run_id,'run');assert.equal(events[0][1].revision_id,'revision');
  assert.equal(events[0][1].version,'version');
  assert.deepEqual(events.slice(1),[['accepted',true,true],['navigate','handoff']]);
  assert.equal(h.run('brandUpdating'),false);
});
test('failed update keeps the producer on Brand with edits and destination retained',async()=>{
  const h=harness();h.c.draft.brand.logo_path='new.png';
  h.c.api=async()=>{throw Error('Saved images changed in another tab. Reopen them.')};
  h.c.navigate=()=>assert.fail('must not leave after failed update');
  h.run('guardBrandNavigation({kind:"page",target:"review"})');
  await h.run('finishBrandNavigation()');
  assert.equal(h.c.modal,'brand-navigation');
  assert.equal(h.c.draft.brand.logo_path,'new.png');
  assert.equal(h.run('brandDestination.target'),'review');
  assert.match(h.run('brandUpdateError'),/another tab/);
});
test('campaign chooser New campaign resolves pending Brand changes before creating a campaign',async()=>{
  const h=harness();
  h.run(readFileSync('src/campaign_studio/static/motif/storage.js','utf8'));
  h.c.guardNavigation=()=>false;
  h.c.draft.brand.text_color='#112233';
  await h.run('dispatchStorage("new")');
  assert.equal(h.c.modal,'brand-navigation');
  assert.equal(h.run('brandDestination.action'),'new');
  await h.run('finishBrandNavigation(true)');
  assert.equal(h.c.modal,'new');
  assert.equal(h.c.draft.brand.text_color,'#FFFFFF');
  assert.equal(h.c.draft.audience,'Unrelated unfinished edit');
});
test('Update waits for a pending logo upload instead of accepting the previous logo',async()=>{
  const h=harness();h.c.draft.brand.text_color='#112233';
  h.c.briefLogoUploadPending=()=>true;
  h.c.api=()=>assert.fail('must not apply while the selected logo is still uploading');
  assert.equal(await h.run('updateBrandImages()'),false);
  assert.match(h.run('brandUpdateError'),/logo upload/);
  assert.equal(h.run('brandUpdating'),false);
});
test('unresolved Brand colors cannot submit from the button or leave overlay, and correction permits Update',async()=>{
  const h=harness();let calls=0, problem='Add #61177C to the palette to use it as the default.';
  h.c.draft.brand.text_color='#61177C';
  h.c.briefBrandColorProblem=()=>problem;
  h.c.api=async()=>{calls++;return {ok:true};};
  h.c.dialog=(title,body,buttons)=>body+buttons;
  h.c.notice=message=>message;
  h.c.btn=(label,action,kind,attrs='')=>`<button data-action="${action}" ${attrs}>${label}</button>`;
  h.c.useData=()=>{h.c.model.output_brief=structuredClone(h.c.draft);};
  h.c.navigate=async target=>{h.c.page=target;};
  assert.equal(await h.run('updateBrandImages()'),false);
  assert.equal(calls,0);assert.equal(h.run('brandUpdating'),false);
  h.run('guardBrandNavigation({kind:"page",target:"review"})');
  const html=h.run('brandNavigationDialog()');
  assert.match(html,/Add #61177C to the palette/);
  assert.match(html,/data-action="brandContinue" disabled/);
  assert.doesNotMatch(html,/data-action="(?:close|brandDiscard)" disabled/);
  await h.run('finishBrandNavigation()');
  assert.equal(calls,0);assert.equal(h.c.page,'campaign');
  assert.equal(h.run('brandDestination.target'),'review');
  problem='';h.c.draft.brand.palette.push('#61177C');
  assert.doesNotMatch(h.run('brandNavigationDialog()'),/data-action="brandContinue" disabled/);
  await h.run('finishBrandNavigation()');
  assert.equal(calls,1);assert.equal(h.c.page,'review');
});
