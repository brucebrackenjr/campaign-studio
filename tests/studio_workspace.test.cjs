const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const context = vm.createContext({
    document:{body:{classList:{remove() {}}}},
    C:{id:'campaign',products:[{id:'product',name:'Product'}]},currentProduct:0,currentFormat:0,
    FMT:[{ratio:'1:1'},{ratio:'9:16'}],MARKET:{en:'en-US'},formats:[0],lang:'en',modal:'text',decodedImage:async()=>({}),
    wordingReturn:{kind:'text',page:'handoff'},page:'review',
    withProjectApply:operation=>operation(),chooseEditorFormat:async()=> 'apply',
    useData() {},
  });
  const run=code=>vm.runInContext(code,context);
  run(fs.readFileSync('src/campaign_studio/static/motif/studio-workspace.js','utf8'));
  run(`let publications=0,originReturns=0;
    studioWorkspace={campaign:'campaign',product:'product',mode:'text',sequence:0};
    studioBusy=()=>false;studioDirty=()=>true;
    teardownStudioTool=()=>{modal=''};
    mountStudioTool=async mode=>{studioWorkspace.mode=mode;modal=mode==='framing'?'frame':mode};
    returnFromWording=()=>{originReturns++;page='handoff';wordingReturn=null};
    const actualRefreshStudioTargets=refreshStudioTargets;refreshStudioTargets=()=>{};
    applyText=async()=>{publications++;return studioApplied({manifest:{outputs:[{product_id:'product',market:'en-US',ratio:'1:1',url:'/saved.png'}]}},()=>{})};`);
  return{context,run};
}

test('correction Apply-and-switch exits to All products exactly once without origin return',async()=>{
 const f=fixture();assert.equal(await f.run('leaveStudioWorkspace()'),true);
 assert.equal(f.run('publications'),1);assert.equal(f.run('originReturns'),0);
 assert.equal(f.run('studioWorkspace'),null);assert.equal(f.context.wordingReturn,null);
 f.run('finishStudioVisit()'); // Ending an already completed visit is harmless.
});

test('correction Apply-and-switch keeps the shell and follows chosen mode or format',async()=>{
 for(const command of ["switchStudioTarget({mode:'framing'})","switchStudioTarget({format:1})"]){
  const f=fixture(),owner=f.run('studioWorkspace');await f.run(command);
  assert.equal(f.run('studioWorkspace'),owner);assert.equal(f.run('publications'),1);
  assert.equal(f.run('originReturns'),0);assert.equal(owner.continuing,false);
  assert.equal(command.includes('framing')?owner.mode:f.context.currentFormat,command.includes('framing')?'framing':1);
 }
});

test('ordinary correction Apply returns to its Handoff origin',async()=>{
 const f=fixture();await f.run('applyText()');
 assert.equal(f.run('publications'),1);assert.equal(f.run('originReturns'),1);
 assert.equal(f.run('studioWorkspace'),null);assert.equal(f.context.page,'handoff');
});

test('failed correction Apply leaves the draft owner and requested target unchanged',async()=>{
 const f=fixture(),owner=f.run('studioWorkspace');f.run('applyText=async()=>false');
 await f.run("switchStudioTarget({format:1,mode:'framing'})");
 assert.equal(f.run('studioWorkspace'),owner);assert.equal(owner.mode,'text');
 assert.equal(owner.continuing,false);assert.equal(f.context.currentFormat,0);assert.equal(f.run('originReturns'),0);
});

test('an owner replaced while Apply resolves cannot receive the old destination',async()=>{
 const f=fixture();f.run("applyText=async()=>{studioWorkspace={campaign:'another',mode:'inspect',sequence:0};return true}");
 await f.run("switchStudioTarget({format:1,mode:'framing'})");
 assert.equal(f.run('studioWorkspace.campaign'),'another');assert.equal(f.run('studioWorkspace.mode'),'inspect');
 assert.equal(f.context.currentFormat,0);
});

test('unavailable format thumbnails stay compact and retain accessible status through refresh',()=>{
 const f=fixture();f.run('refreshStudioTargets=actualRefreshStudioTargets');f.context.window={};
 f.run(fs.readFileSync('src/campaign_studio/static/motif/studio-shell.js','utf8'));
 const markup=f.context.window.MotifStudioShell.shell({productName:'Product',productNumber:1,productCount:1,
  markets:[],formats:[{id:'1:1',name:'Square',available:false}],format:'1:1',mode:'inspect'});
 assert.match(markup,/aria-label="Square · 1:1 · Image unavailable"/);
 assert.match(markup,/<span class="ws-thumb"><span aria-hidden="true">–<\/span><\/span>/);
 const thumb={innerHTML:''},button={dataset:{wsFormat:'1:1'},setAttribute(key,value){this[key]=value}};
 f.context.FMT=[{ratio:'1:1',name:'Square'}];f.context.MARKET={en:'en-US'};
 f.context.$=(selector)=>selector==='.ws-thumb'?thumb:{};
 f.context.$$=selector=>selector==='[data-ws-mode]'?[]:[button];
 f.context.model={rule_withheld_outputs:['withheld']};f.context.esc=value=>value;
 f.context.outputFor=()=>({id:'withheld'});
 f.run('refreshStudioTargets()');
 assert.equal(button.disabled,false); // Existing rule correction admission remains available.
 assert.equal(button['aria-label'],'Square · 1:1 · Image unavailable');
 assert.equal(thumb.innerHTML,'<span aria-hidden="true">–</span>');
 f.context.outputFor=()=>({id:'missing'});f.run('refreshStudioTargets()');assert.equal(button.disabled,true);
 f.context.outputFor=()=>({id:'ready',url:'/current.png'});f.run('refreshStudioTargets()');
 assert.equal(button.disabled,false);assert.equal(button['aria-label'],'Square · 1:1');
 assert.equal(thumb.innerHTML,'<img src="/current.png" alt="">');
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

test('Logo shows each saved format immediately and only the latest opening may install controls',async()=>{
  const waits=[], views=[], images=[], errors=[];
  const controls={ querySelector:()=>({set textContent(value){errors.push(value)}}), append(){} };
  const root={ classList:{remove(){}} };
  const c=vm.createContext({AbortController,modal:'',textEditor:null,frameSaving:false,logoEditor:null,
    C:{products:[{id:'bag',name:'Bag'}]},currentProduct:0,formats:[0],currentFormat:0,lang:'en',LANG:{en:'English'},
    FMT:[{name:'Square'},{name:'Story'},{name:'Landscape'}],document:{createElement:()=>({addEventListener(){}})},
    MotifStudioShell:{setMode:(_,view)=>views.push(view)},outputFor:(_,lang,format)=>({url:'/saved/'+format}),
    $:selector=>selector.includes('surface')?{replaceChildren:image=>images.push(image.src)}:controls,
    openLogo:(_,format,{isCurrent,signal})=>new Promise((resolve,reject)=>waits.push({format,isCurrent,signal,resolve,reject})),
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
  c.logoEditor={dirty:()=>true};c.modal='logo';c.chooseEditorFormat=async()=> 'keep';
  await run('switchStudioTarget({format:0})');
  assert.equal(c.currentFormat,2);assert.equal(waits.length,3);
  c.logoEditor.busy=true;assert.equal(run('studioBusy()'),true);
});
