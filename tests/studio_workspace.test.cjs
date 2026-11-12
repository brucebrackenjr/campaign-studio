const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const context = vm.createContext({
    framingPan:null,framingDrawing:false,logoEditor:null,studioSave:null,model:{},clearTimeout(){},
    document:{body:{classList:{remove() {}}}},
    C:{id:'campaign',products:[{id:'product',name:'Product'}]},currentProduct:0,currentFormat:0,
    FMT:[{ratio:'1:1'},{ratio:'9:16'}],MARKET:{en:'en-US'},formats:[0],lang:'en',modal:'text',decodedImage:async()=>({}),
    wordingReturn:{kind:'text',page:'handoff'},page:'review',textEditor:{recovery:null},framingRecovery:null,
    withProjectApply:operation=>operation(),chooseEditorFormat:async()=> 'apply',
    useData() {},
  });
  const run=code=>vm.runInContext(code,context);
  run(fs.readFileSync('src/campaign_studio/static/motif/studio-workspace.js','utf8'));
  run(`let publications=0,originReturns=0;
    studioWorkspace={campaign:'campaign',product:'product',mode:'text',sequence:0};
    studioBusy=()=>false;studioDirty=()=>true;flushStudioSave=async()=>{publications++;return true};
    teardownStudioTool=()=>{modal=''};
    mountStudioTool=async mode=>{studioWorkspace.mode=mode;modal=mode==='framing'?'frame':mode};
    returnFromWording=()=>{originReturns++;page='handoff';wordingReturn=null};
    const actualRefreshStudioTargets=refreshStudioTargets;refreshStudioTargets=()=>{};
    applyText=async()=>{publications++;return studioApplied({manifest:{outputs:[{product_id:'product',market:'en-US',ratio:'1:1',url:'/saved.png'}]}},()=>{})};`);
  return{context,run};
}

test('Studio location retains stable IDs and the selected tool without an authored save',()=>{
 const f=fixture(),states=[];
 f.context.history={state:{motifIndex:3,view:{page:'review'}},replaceState(state,_,url){this.state=state;states.push({state,url})}};
 f.context.location={href:'/campaigns/campaign/studio'};
 f.context.currentFormat=1;f.context.lang='es';
 f.run("studioWorkspace.mode='text';studioWorkspace.scene={selected:'cta'};retainStudioNavigation()");
 assert.deepEqual(JSON.parse(JSON.stringify(f.context.history.state.studio)),{
   campaign:'campaign',product:'product',language:'es',format:1,mode:'text',block:'cta',
 });
 assert.equal(f.context.history.state.motifIndex,3);
 assert.equal(states[0].url,'/campaigns/campaign/studio');
 assert.equal(f.run('publications'),0);
 f.run('finishStudioVisit()');
 assert.equal(f.context.history.state.studio,undefined);
 assert.equal(f.context.history.state.motifIndex,3);
});

test('refresh restores product by ID after reordering, with format, language, tool, and text block',async()=>{
 const f=fixture(),opened=[];
 f.context.C={id:'campaign',markets:['en','es'],products:[{id:'other'},{id:'product'}]};
 f.context.outputFor=(product,language,format)=>{
   assert.equal(product,1);assert.equal(language,'es');assert.equal(format,1);return {url:'/current.png'};
 };
 f.context.openStudioWorkspace=async mode=>{
   opened.push({mode,product:f.context.currentProduct,format:f.context.currentFormat,language:f.context.lang});
   f.run("studioWorkspace={scene:{setMode(mode,block){restoredBlock=block;restoredMode=mode;}}}");
 };
 f.run('var restoredBlock,restoredMode;');
 assert.equal(await f.run("restoreStudioNavigation({campaign:'campaign',product:'product',language:'es',format:1,mode:'text',block:'cta'})"),true);
 assert.deepEqual(opened,[{mode:'text',product:1,format:1,language:'es'}]);
 assert.equal(f.run('restoredMode'),'text');assert.equal(f.run('restoredBlock'),'cta');
 assert.equal(f.run('publications'),0);
});

test('refresh refuses a deleted product, different campaign, invalid selection, or unavailable output',async()=>{
 const f=fixture();f.context.C.markets=['en'];f.context.outputFor=()=>({url:'/current.png'});
 f.context.canEditProduct=()=>false;
 let opened=0;f.context.openStudioWorkspace=async()=>opened++;
 const valid={campaign:'campaign',product:'product',language:'en',format:0,mode:'logo',block:'message'};
 for(const change of [{campaign:'other'},{product:'deleted'},{language:'es'},{format:-1},{format:1.5},{format:3},{mode:'unknown'},{block:'unknown'}]){
   f.context.savedLocation={...valid,...change};
   assert.equal(await f.run('restoreStudioNavigation(savedLocation)'),false);
 }
 f.context.savedLocation=valid;f.context.page='handoff';
 assert.equal(await f.run('restoreStudioNavigation(savedLocation)'),false);
 f.context.page='review';f.context.outputFor=()=>({url:null});
 assert.equal(await f.run('restoreStudioNavigation(savedLocation)'),false);
 f.context.outputFor=()=>null;
 assert.equal(await f.run('restoreStudioNavigation(savedLocation)'),false);
 assert.equal(opened,0);
});

test('autosave flush before navigation exits to All products exactly once without origin return',async()=>{
 const f=fixture();assert.equal(await f.run('leaveStudioWorkspace()'),true);
 assert.equal(f.run('publications'),1);assert.equal(f.run('originReturns'),0);
 assert.equal(f.run('studioWorkspace'),null);assert.equal(f.context.wordingReturn,null);
 f.run('finishStudioVisit()'); // Ending an already completed visit is harmless.
});

test('autosave flush before switching keeps the shell and follows chosen mode or format',async()=>{
 for(const command of ["switchStudioTarget({mode:'framing'})","switchStudioTarget({format:1})"]){
  const f=fixture(),owner=f.run('studioWorkspace');await f.run(command);
  assert.equal(f.run('studioWorkspace'),owner);assert.equal(f.run('publications'),1);
  assert.equal(f.run('originReturns'),0);assert.equal(!!owner.continuing,false);
  assert.equal(command.includes('framing')?owner.mode:f.context.currentFormat,command.includes('framing')?'framing':1);
 }
});

test('ordinary correction Apply returns to its Handoff origin',async()=>{
 const f=fixture();await f.run('applyText()');
 assert.equal(f.run('publications'),1);assert.equal(f.run('originReturns'),1);
 assert.equal(f.run('studioWorkspace'),null);assert.equal(f.context.page,'handoff');
});

test('failed autosave leaves the draft owner and requested target unchanged',async()=>{
 const f=fixture(),owner=f.run('studioWorkspace');f.run('flushStudioSave=async()=>false');
 await f.run("switchStudioTarget({format:1,mode:'framing'})");
 assert.equal(f.run('studioWorkspace'),owner);assert.equal(owner.mode,'text');
 assert.equal(!!owner.continuing,false);assert.equal(f.context.currentFormat,0);assert.equal(f.run('originReturns'),0);
});

test('an owner replaced while autosave resolves cannot receive the old destination',async()=>{
 const f=fixture();f.run("flushStudioSave=async()=>{studioWorkspace={campaign:'another',mode:'inspect',sequence:0};return true}");
 await f.run("switchStudioTarget({format:1,mode:'framing'})");
 assert.equal(f.run('studioWorkspace.campaign'),'another');assert.equal(f.run('studioWorkspace.mode'),'inspect');
 assert.equal(f.context.currentFormat,0);
});

test('unavailable format thumbnails stay compact and retain accessible status through refresh',async()=>{
 const f=fixture();f.run('refreshStudioTargets=actualRefreshStudioTargets');f.context.window={};
 f.run(fs.readFileSync('src/campaign_studio/static/motif/studio-shell.js','utf8'));
 const markup=f.context.window.MotifStudioShell.shell({productName:'Product',productNumber:1,productCount:1,
  markets:[],formats:[{id:'1:1',name:'Square',available:false}],format:'1:1',mode:'inspect'});
 assert.match(markup,/aria-label="Square · 1:1 · Image unavailable"/);
 assert.doesNotMatch(markup,/data-studio-save|ws-autosave/);
 assert.match(markup,/<span class="ws-thumb"><span aria-hidden="true">–<\/span><\/span>/);
 const thumb={innerHTML:'',replaceChildren(image){this.image=image}},button={dataset:{wsFormat:'1:1'},setAttribute(key,value){this[key]=value},removeAttribute(){}};
 f.context.Image=class {decode(){return Promise.resolve()}};
 f.context.FMT=[{ratio:'1:1',name:'Square'}];f.context.MARKET={en:'en-US'};
 f.context.$=(selector)=>selector==='.ws-thumb'?thumb:{};
 f.context.$$=selector=>selector==='[data-ws-mode]'?[]:[button];
 f.context.model={rule_withheld_outputs:['withheld']};f.context.esc=value=>value;f.context.canEditProduct=()=>true;
 f.context.outputFor=()=>({id:'withheld'});
 f.run('refreshStudioTargets()');
 assert.equal(button.disabled,false); // Existing rule correction admission remains available.
 assert.equal(button['aria-label'],'Square · 1:1 · Image unavailable');
 assert.equal(thumb.innerHTML,'<span aria-hidden="true">–</span>');
 f.context.canEditProduct=()=>false;f.context.outputFor=()=>({id:'missing'});f.run('refreshStudioTargets()');assert.equal(button.disabled,true);
 f.context.outputFor=()=>({id:'ready',url:'/current.png'});f.run('refreshStudioTargets()');
 assert.equal(button.disabled,false);assert.equal(button['aria-label'],'Square · 1:1');
 await new Promise(setImmediate);assert.equal(thumb.image.src,'/current.png');
});


test('prepared-source framing refusal preserves the current Inspect image without mounting obsolete controls',async()=>{
  const notices=[],context=vm.createContext({C:{products:[{treatment:'prepared'}]},currentProduct:0,toast:message=>notices.push(message)});
  const run=code=>vm.runInContext(code,context);
  run(fs.readFileSync('src/campaign_studio/static/motif/studio-workspace.js','utf8'));
  const owner={mode:'inspect',root:{acceptedImage:'unchanged'}};
  context.owner=owner;
  run("studioWorkspace=owner;teardownStudioTool=()=>{throw Error('Inspect must stay mounted')}");
  await run("mountStudioTool('framing')");
  assert.equal(run('studioWorkspace'),owner);
  assert.equal(owner.mode,'inspect');assert.equal(owner.root.acceptedImage,'unchanged');
  assert.equal(notices.length,1);assert.match(notices[0],/Product placement is not adjustable/);
});


test('saved image handover waits for decode and retains the same owner without mounting controls',async()=>{
 const f=fixture(),owner=f.run('studioWorkspace');f.context.wordingReturn=null;
 let decode,adopted=0,read=0;f.context.decodedImage=()=>new Promise(resolve=>{decode=resolve});f.context.useData=()=>read++;
 f.context.accept=output=>{adopted++;assert.equal(output.url,'/saved.png')};
 const task=f.run("studioApplied({manifest:{outputs:[{product_id:'product',market:'en-US',ratio:'1:1',url:'/saved.png'}]}},accept)");
 assert.equal(adopted,0);assert.equal(read,0);assert.equal(f.run('studioWorkspace'),owner);
 decode({});assert.equal(await task,true);assert.equal(adopted,1);assert.equal(read,1);assert.equal(f.run('studioWorkspace'),owner);
});

test('retired Studio owner cannot adopt a late accepted-image decode',async()=>{
 const f=fixture();f.context.wordingReturn=null;let decode,adopted=0;f.context.decodedImage=()=>new Promise(resolve=>{decode=resolve});f.context.accept=()=>adopted++;
 const task=f.run("studioApplied({manifest:{outputs:[{product_id:'product',market:'en-US',ratio:'1:1',url:'/saved.png'}]}},accept)");
 f.run("studioWorkspace={campaign:'other',mode:'inspect'}");decode({});assert.equal(await task,false);assert.equal(adopted,0);
});

test('Logo delegates immediate panel mounting and only the latest opening may accept verified context',async()=>{
  const waits=[], views=[], images=[], errors=[];
  const controls={ querySelector:()=>({set textContent(value){errors.push(value)}}), append(){} };
  const root={ classList:{remove(){}} };
  const c=vm.createContext({AbortController,modal:'',textEditor:null,frameSaving:false,logoEditor:null,framingPan:null,framingDrawing:false,studioSave:null,clearTimeout(){},beginStudioSave(){},flushStudioSave:async()=>true,
    model:{version:'v1'},C:{products:[{id:'bag',name:'Bag'}]},currentProduct:0,formats:[0],currentFormat:0,lang:'en',LANG:{en:'English'},
    FMT:[{name:'Square'},{name:'Story'},{name:'Landscape'}],document:{createElement:()=>({addEventListener(){}})},
    MotifStudioShell:{setMode:(_,view)=>views.push(view)},outputFor:(_,lang,format)=>({url:'/saved/'+format}),
    $:selector=>selector.includes('surface')?{replaceChildren:image=>images.push(image.src)}:controls,
    openLogo:(_,format,{isCurrent,signal})=>{images.push('/saved/'+format);views.push({target:'English · '+c.FMT[format].name});return new Promise((resolve,reject)=>waits.push({format,isCurrent,signal,resolve,reject}));},
  });
  const run=code=>vm.runInContext(code,c);
  run(fs.readFileSync('src/campaign_studio/static/motif/studio-workspace.js','utf8'));
  c.root=root;
  run("studioWorkspace={root,sequence:0,mode:'inspect'};refreshStudioTargets=()=>{}");
  const first=run("mountStudioTool('logo')");
  assert.deepEqual(images,['/saved/0']); assert.equal(run('studioBusy()'),false);
  const second=run('switchStudioTarget({format:1})');await new Promise(setImmediate);
  const third=run('switchStudioTarget({format:2})');await new Promise(setImmediate);
  assert.deepEqual(images,['/saved/0','/saved/1','/saved/2']);
  assert.deepEqual(views.map(v=>v.target),['English · Square','English · Story','English · Landscape']);
  assert.deepEqual(waits.map(w=>w.isCurrent()),[false,false,true]);
  assert.deepEqual(waits.map(w=>w.signal.aborted),[true,true,false]);
  waits[1].reject(Error('stale failure'));waits[0].resolve();await Promise.all([first,second]);
  assert.deepEqual(errors,[]);assert.equal(run('studioWorkspace.logoLoad.signal.aborted'),false);
  waits[2].resolve();await third;assert.equal(run('studioWorkspace.logoLoad'),null);
  c.logoEditor={dirty:()=>true};c.modal='logo';c.flushStudioSave=async()=>false;
  await run('switchStudioTarget({format:0})');
  assert.equal(c.currentFormat,2);assert.equal(waits.length,3);
  c.logoEditor.busy=true;assert.equal(run('studioBusy()'),true);
});


test('navigation retries pending metadata through the same save queue',async()=>{
 const f=fixture();let retries=0;f.context.textEditor.recovery='apply';
 f.context.flushStudioSave=async()=>{retries++;return true};
 assert.equal(await f.run('resolveStudioChanges()'),true);assert.equal(retries,1);
 assert.equal(f.run('publications'),0);
});

test('source-backed invalid layout keeps every repair tool and format reachable; missing source stays blocked', () => {
  const f=fixture();f.run('refreshStudioTargets=actualRefreshStudioTargets');
  const modes=['inspect','framing','logo','text'].map(wsMode=>({dataset:{wsMode}}));
  const square={dataset:{wsFormat:'1:1'},setAttribute(key,value){this[key]=value}},thumb={innerHTML:''};
  f.context.FMT=[{ratio:'1:1',name:'Square'}];f.context.outputFor=()=>({id:'product/en-US/1x1',url:null});
  f.context.model={rule_withheld_outputs:[]};f.context.canEditProduct=()=>true;f.context.canEditLogo=()=>true;
  f.context.$=selector=>selector==='.ws-thumb'?thumb:{};
  f.context.$$=selector=>selector==='[data-ws-mode]'?modes:[square];
  f.run('refreshStudioTargets()');
  assert.ok(modes.every(button=>!button.disabled));assert.equal(square.disabled,false);
  assert.equal(f.run('studioTextAvailable()'),true);
  f.context.canEditProduct=()=>false;f.context.canEditLogo=()=>false;
  f.run('refreshStudioTargets()');
  assert.ok(modes.every(button=>button.disabled));assert.equal(square.disabled,true);
  assert.equal(f.run('studioTextAvailable()'),false);
});

test('leaving Studio refreshes layout findings once without another save and rejects obsolete reads or newer typing',async()=>{
 for(const scenario of ['current','newer typing','newer model','newer owner','different version']){
  const f=fixture(),owner=f.run('studioWorkspace'),pending=[];let adopted=0;
  f.context.model={version:'v2',composition_version:'v2',composition_projection_stale:true,manifest:{complete:false}};
  f.context.projectQueue={isDirty:()=>false};f.context.studioSave={dirty:()=>false,conflict(){}};
  f.context.MotifSave={conflictMessage:'Refresh to load the latest changes.'};
  f.context.api=url=>new Promise(resolve=>pending.push({url,resolve}));
  f.context.useData=data=>{adopted++;f.context.model=data};
  const leaving=f.run('leaveStudioWorkspace()');await Promise.resolve();await Promise.resolve();await Promise.resolve();
  assert.equal(pending.length,1);assert.equal(pending[0].url,'/api/motif/campaigns/campaign');
  if(scenario==='newer typing')f.context.studioSave.dirty=()=>true;
  if(scenario==='newer model')f.context.model={version:'v3'};
  if(scenario==='newer owner')f.run("studioWorkspace={campaign:'other'}");
  pending[0].resolve({value:{campaign_id:'campaign'},composition_version:scenario==='different version'?'v3':'v2',manifest:{complete:true}});
  assert.equal(await leaving,scenario==='current');assert.equal(adopted,scenario==='current'?1:0);
  if(scenario==='current'){assert.equal(f.run('studioWorkspace'),null);assert.equal(f.context.model.manifest.complete,true);}
  else if(scenario!=='newer owner')assert.equal(f.run('studioWorkspace'),owner);
  assert.equal(f.run('publications'),1,'Read-only refresh must not add a save');
 }
});

test('edited canvas stays visible across a version change but never crosses product or format targets',()=>{
  const f=fixture(),owner=f.run('studioWorkspace'),painted=[];
  const edited={tagName:'CANVAS',hidden:false,style:{},width:1080,height:1080};
  let held=null,appended=null;
  const slot={querySelector:selector=>selector==='#text-canvas'?edited:selector==='.ws-preview-continuity'?held:null,append:node=>{held=appended=node}};
  owner.root={querySelector:selector=>selector==='[data-ws-slot="surface"]'?slot:selector==='.ws-preview-continuity'?held:null};
  f.context.FMT=[{ratio:'1:1',w:1080,h:1080},{ratio:'9:16',w:1080,h:1920}];
  f.context.document.createElement=()=>({setAttribute(){},remove(){held=null},getContext:()=>({drawImage:(...args)=>painted.push(args)})});
  f.context.model={composition_version:'before'};
  f.run('studioWorkspace.visualTarget=studioVisualTarget();studioRetainSurface()');
  assert.equal(painted.length,1);assert.equal(painted[0][0],edited);
  const retained=owner.retainedSurface;
  f.context.model.composition_version='after';
  f.run('studioShowRetainedSurface()');
  assert.equal(appended,retained.node,'Only presentation survives; the versioned request cache still changes');
  f.run('studioRetainSurface()');assert.equal(owner.retainedSurface,retained,'Rapid tool switches keep one current bitmap');
  f.run('studioPreviewReady()');assert.equal(held,null);assert.equal(owner.retainedSurface,null);
  f.run('studioRetainSurface()');f.context.currentFormat=1;appended=null;
  f.run('studioShowRetainedSurface()');assert.equal(appended,null);assert.equal(owner.retainedSurface,null);
  f.context.currentFormat=0;f.run('studioRetainSurface()');f.context.C.products[0].id='different';
  f.run('studioShowRetainedSurface()');assert.equal(appended,null);assert.equal(owner.retainedSurface,null);
});

test('format switching paints the destination thumbnail without moving or refreshing it',async()=>{
  const f=fixture(),owner=f.run('studioWorkspace'),draws=[];
  const image={complete:true,naturalWidth:1080,src:'/previous-document/story.png'};
  const button={querySelector:selector=>{assert.equal(selector,'.ws-thumb img');return image}};
  const canvas={setAttribute(){},getContext:()=>({drawImage:(...args)=>draws.push(args)})};
  f.context.document.createElement=tag=>{assert.equal(tag,'canvas');return canvas};
  f.context.FMT=[{ratio:'1:1',w:1080,h:1080},{ratio:'9:16',w:1080,h:1920}];
  f.context.outputFor=()=>({url:'/current-document/story.png',copy:{message:'Story headline'}});
  f.context.button=button;
  f.run(`studioWorkspace.thumbnails=new Map([[button,{
    target:JSON.stringify([C.id,C.products[0].id,lang,1]),
    signature:studioThumbnailSignature(outputFor(),1)
  }]])`);
  await f.run('switchStudioTarget({format:1})');
  assert.equal(f.context.currentFormat,1);
  assert.equal(owner.retainedSurface.node,canvas);
  assert.equal(owner.retainedSurface.target,f.run('studioVisualTarget()'));
  assert.deepEqual(draws,[[image,0,0,1080,1920]]);
  assert.equal(button.querySelector('.ws-thumb img'),image);
  assert.equal(image.src,'/previous-document/story.png','A document version alone must not reload an unchanged thumbnail');
});

test('destination continuity never uses an outdated, different-target, or unloaded thumbnail',()=>{
  for(const scenario of ['outdated','other market','unloaded']){
    const f=fixture();f.context.outputFor=()=>({url:'/current.png'});
    const image={complete:scenario!=='unloaded',naturalWidth:1080};
    const button={querySelector:()=>image};f.context.button=button;
    f.run(`studioWorkspace.thumbnails=new Map([[button,{
      target:JSON.stringify([C.id,C.products[0].id,lang,1]),
      signature:studioThumbnailSignature(outputFor(),1)
    }]])`);
    if(scenario==='outdated')f.run("studioWorkspace.thumbnails.get(button).signature='old settings'");
    if(scenario==='other market')f.run("studioWorkspace.thumbnails.get(button).target='other market'");
    f.context.document.createElement=()=>{throw Error('Must not copy this image')};
    assert.equal(f.run('studioFormatSurface(1)'),null,scenario);
  }
});

test('a format still loading keeps the current canvas, and a newer selection cancels the delayed switch',async()=>{
  for(const cancel of [false,true]){
    const f=fixture(),owner=f.run('studioWorkspace');let decoded;
    const ready=new Promise(resolve=>decoded=resolve);
    const state={target:JSON.stringify(['campaign','product','en',1]),pending:'/story.png',ready};
    owner.thumbnails=new Map([[{},state]]);
    f.context.destination={node:{pixels:'story'},target:state.target};
    f.run('studioFormatSurface=()=>null');
    const switching=f.run('switchStudioTarget({format:1})');
    await new Promise(setImmediate);
    assert.equal(f.context.currentFormat,0,'Keep the displayed format until its replacement is decoded');
    assert.equal(f.context.modal,'text','The existing editor remains mounted');
    if(cancel)await f.run('switchStudioTarget({format:0})');
    f.run('studioFormatSurface=()=>destination');decoded();await switching;
    assert.equal(f.context.currentFormat,cancel?0:1);
    assert.equal(f.run('publications'),cancel?1:2,'Changes entered while waiting must also flush before switching');
    assert.equal(owner.retainedSurface,cancel?undefined:f.context.destination);
  }
});

test('thumbnail refresh decodes only changed format inputs and cannot adopt an obsolete target',async()=>{
 const f=fixture(),loads=[],buttons=[0,1].map(()=>({removeAttribute(){}}));
 f.context.FMT=[{ratio:'1:1'},{ratio:'9:16'}];f.context.MARKET={en:'en-US',es:'es-MX'};
 f.context.model={output_brief:{brand:{logo_path:'logo.png',text_color:'#ffffff'},products:[{id:'product',source:{path:'photo.png'},framing:{'en-US':{}}}]}};
 const outputs=[0,1].map(i=>({id:'image'+i,url:'/v1/'+i,copy:{message:'Hello'}}));
 const original=outputs.map(o=>({complete:true,naturalWidth:100,getAttribute:()=>o.url}));
 const thumbs=original.map(image=>({image,querySelector(){return this.image},replaceChildren(next){this.image=next},set innerHTML(value){this.image=null;this.markup=value}}));
 f.context.Image=class {constructor(){loads.push(this)}decode(){return new Promise((resolve,reject)=>{this.resolve=()=>{this.complete=true;this.naturalWidth=100;resolve()};this.reject=reject})}};
 f.context.outputFor=(_,__,format)=>outputs[format];
 f.context.buttons=buttons;f.context.thumbs=thumbs;f.context.outputs=outputs;
 const refresh=()=>f.run('for(let i=0;i<2;i++)refreshStudioThumbnail(buttons[i],thumbs[i],outputs[i],i,outputs[i].url)');
 refresh();assert.equal(loads.length,0);
 f.context.model.output_brief.products[0].framing['en-US']['1:1']={zoom:1.2};
 outputs.forEach((o,i)=>o.url='/v2/'+i);refresh();
 assert.equal(loads.length,1);assert.equal(loads[0].src,'/v2/0');
 assert.deepEqual(thumbs.map(t=>t.image),original,'the previous decoded images remain visible during decode');
 loads[0].resolve();await new Promise(setImmediate);
 assert.equal(thumbs[0].image,loads[0]);assert.equal(thumbs[1].image,original[1]);
 outputs.forEach((o,i)=>o.url='/v3/'+i);refresh();assert.equal(loads.length,1,'a version-only change does not rerequest either image');
 f.context.model.output_brief.products[0].framing['en-US']['1:1']={zoom:1.3};refresh();
 const pending=loads.at(-1);assert.equal(thumbs[0].image,loads[0]);
 f.context.model.output_brief.products[0].framing['en-US']['1:1']={zoom:1.4};outputs[0].url='/v4/0';refresh();
 const current=loads.at(-1);pending.resolve();await new Promise(setImmediate);
 assert.equal(thumbs[0].image,loads[0],'a late older edit cannot replace the visible thumbnail');
 current.resolve();await new Promise(setImmediate);assert.equal(thumbs[0].image,current);
 const beforeShared=loads.length;
 outputs.forEach((o,i)=>{o.copy.message='Shared headline';o.url='/v5/'+i});refresh();
 assert.equal(loads.length,beforeShared+2,'shared wording refreshes every affected format');
 loads.slice(-2).forEach(load=>load.resolve());await new Promise(setImmediate);
 f.context.lang='es';outputs.forEach((o,i)=>o.url='/spanish/'+i);refresh();
 assert.deepEqual(thumbs.map(t=>t.image),[null,null],'the old market must not remain under new labels');
 const spanish=loads.slice(-2);f.run("studioWorkspace={campaign:'other',product:'other'}");
 spanish.forEach(load=>load.resolve());await new Promise(setImmediate);
 assert.deepEqual(thumbs.map(t=>t.image),[null,null],'retired owner cannot adopt late images');
});

test('failed thumbnail decode retains the current target image and permits a fresh authorized retry',async()=>{
 const f=fixture(),loads=[],shown={complete:true,naturalWidth:100,getAttribute:()=>'/v1'};
 f.context.model={output_brief:{products:[{id:'product',framing:{'en-US':{}}}]}};
 const thumb={image:shown,querySelector(){return this.image},replaceChildren(image){this.image=image}},button={removeAttribute(){delete this.title}};
 f.context.Image=class {constructor(){loads.push(this)}decode(){return new Promise((resolve,reject)=>{this.resolve=resolve;this.reject=reject})}};
 let output={url:'/v1',copy:{message:'Before'}};f.context.outputFor=()=>output;
 f.context.thumb=thumb;f.context.button=button;f.context.output=output;
 f.run('refreshStudioThumbnail(button,thumb,output,0,output.url)');
 output={url:'/v2',copy:{message:'After'}};f.context.output=output;
 f.run('refreshStudioThumbnail(button,thumb,output,0,output.url)');loads[0].reject(Error('decode failed'));await new Promise(setImmediate);
 assert.equal(thumb.image,shown);assert.match(button.title,/could not be updated/);
 f.run('refreshStudioThumbnail(button,thumb,output,0,output.url)');assert.equal(loads.length,2);
 loads[1].resolve();await new Promise(setImmediate);assert.equal(thumb.image,loads[1]);assert.equal(button.title,undefined);
});


test('Escape after a wording correction refreshes the final Handoff projection before returning',async()=>{
 const f=fixture(),checks=[];f.context.projectQueue=null;
 f.context.model={version:'v2',composition_projection_stale:true};
 f.context.api=async()=>{checks.push('projection');return {version:'v2',value:{campaign_id:'campaign'},manifest:{complete:true}}};
 f.context.useData=data=>{f.context.model=data;checks.push('adopt')};
 await f.run('cancelStudioTool()');
 assert.deepEqual(checks,['projection','adopt']);assert.equal(f.context.model.manifest.complete,true);
 assert.equal(f.context.page,'handoff');assert.equal(f.run('originReturns'),1);assert.equal(f.run('studioWorkspace'),null);
});
