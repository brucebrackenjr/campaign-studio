const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const message = "This campaign was updated elsewhere. Refresh to load the latest changes. Unsaved edits in this window will be discarded.";
const load = (context, name) => vm.runInContext(readFileSync(`src/campaign_studio/static/motif/${name}.js`, "utf8"), context);
const tick = () => new Promise(resolve => setImmediate(resolve));

function node(tag = "div") {
  return {
    tag, children: [], dataset: {}, events: {}, hidden: false, disabled: false,
    textContent: "", querySelector(){return null;}, classList: {add(){}, toggle(){}},
    setAttribute(name,value){this[name]=value;},
    addEventListener(name,callback){this.events[name]=callback;}, removeEventListener(){},
    append(...items){this.children.push(...items);}, prepend(item){this.children.unshift(item);},
    replaceChildren(...items){this.children=items;},
  };
}
function environment() {
  const windowEvents = {}, controls = node(), root = node(), status = node();
  status.dataset.studioSave = "";
  const context = vm.createContext({
    structuredClone, setTimeout, clearTimeout, Intl, Date, Promise,
    document: {body:{dataset:{}},querySelector(){return null;},addEventListener(){},removeEventListener(){},createElement:node,createTextNode:text=>({textContent:text})},
    window: {addEventListener(name,callback){windowEvents[name]=callback;}},
    $: selector => selector === '[data-studio-save]' ? status : selector === '[data-studio-recovery]'
      ? controls.children.find(item => 'studioRecovery' in item.dataset) : controls,
    projectQueue:{isDirty:()=>true}, getSaveState:()=>({phase:"conflict",problem:{phase:"conflict"}}),
    studioWorkspace:{root}, localDialog:null, draft:{}, modalView(){},
    openModal(){assert.fail("stale conflict must not open an overlay");},
    api(){assert.fail("Refresh must not read, merge, or resend any draft");},
    refreshCatalog:()=>new Promise(()=>{}),
  });
  let reloads=0, unload;
  const event=()=>({prevented:false,preventDefault(){this.prevented=true;}});
  context.window.location={reload(){reloads++;unload=event();windowEvents.beforeunload?.(unload);}};
  load(context,"save-state");context.MotifSave=context.window.MotifSave;
  load(context,"studio-autosave");
  load(context,"actions");
  return {context,controls,status,windowEvents,event,get reloads(){return reloads;},get unload(){return unload;}};
}
function recoveryRoot() {
  const elements=new Map(),root=node();
  root.contains=()=>true;
  root.querySelector=selector=>elements.get(selector)||null;
  Object.defineProperty(root,'innerHTML',{get(){return this.markup||'';},set(value){
    this.markup=value;elements.clear();
    if(value)for(const selector of ['strong','p','[data-save-recover]'])elements.set(selector,node(selector==='[data-save-recover]'?'button':selector));
  }});
  return root;
}

test("Campaign stale notice has only Refresh and suppresses old service wording", async () => {
  const h=environment(),root=recoveryRoot(); let notify;
  h.context.MotifSave.mountRecovery(root,{
    getSaveState:()=>({phase:'conflict',problem:{phase:'conflict',message:'Another tab changed. Review and replace its work.'}}),
    subscribeSaveState(callback){notify=callback;return()=>{};},
    onSave(){assert.fail('stale save cannot be retried');},
  });
  assert.equal(root.querySelector('p').textContent,message);
  assert.equal(root.querySelector('strong').hidden,true);
  const button=root.querySelector('[data-save-recover]');
  assert.equal(button.textContent,'Refresh');
  assert.equal((root.innerHTML.match(/<button/g)||[]).length,1);
  assert.doesNotMatch(root.innerHTML,/Review|Use saved|Keep editing|Save my/);
  assert.equal(h.context.guardNavigation({action:'openCampaign'}),true);
  assert.equal(h.reloads,0,'navigation must not discard or refresh automatically');
  const ordinary=h.event();h.windowEvents.beforeunload(ordinary);
  assert.equal(ordinary.prevented,true,'ordinary browser closing still warns about unsaved work');
  root.events.click({target:{closest:()=>button}});
  assert.equal(h.reloads,1);
  assert.equal(h.unload.prevented,false,'explicit Refresh has already disclosed discarded edits');
  notify({phase:'conflict',problem:{phase:'conflict',message:'Different backend message'}});
  assert.equal(root.querySelector('p').textContent,message);
});

test("Studio stale notice matches Campaign and Refresh bypasses only intentional unload",()=>{
  const h=environment();
  vm.runInContext('studioSave={dirty:()=>true}',h.context);
  h.context.paintStudioSave({phase:'conflict',error:new Error('Another tab changed. Review latest.')});
  const status=h.controls.children[0],buttons=status.children.filter(child=>child.tag==='button');
  assert.equal(status.children[0].textContent,message);
  assert.equal(status.children.length,2,'no appended stale backend message');
  assert.equal(buttons.length,1);assert.equal(buttons[0].textContent,'Refresh');
  const ordinary=h.event();h.windowEvents.beforeunload(ordinary);assert.equal(ordinary.prevented,true);
  buttons[0].events.click();
  assert.equal(h.reloads,1);assert.equal(h.unload.prevented,false);
});

test("Studio Refresh survives a focused field publishing the paused conflict before click",async()=>{
  const h=environment();
  h.context.value={headline:'Saved headline'};
  let attempts=0;
  h.context.send=async()=>{attempts++;throw Object.assign(new Error('A newer campaign is saved'),{status:409});};
  vm.runInContext(`studioSave=compositionSaveQueue({
    capture:()=>value,send,accept(){throw new Error('A refused save cannot be accepted');},
    changed:paintStudioSave,
  });`,h.context);
  h.context.value={headline:'Unsaved headline'};
  await assert.rejects(vm.runInContext('studioSave.flush()',h.context),/newer campaign/);
  const status=h.controls.children[0],pressedButton=status.children.at(-1);
  assert.equal(pressedButton.textContent,'Refresh');
  // The browser presses Refresh, then blurs the changed text field before
  // delivering click. That field may publish the paused conflict again.
  h.context.value={headline:'More unsaved typing'};
  vm.runInContext('studioSave.schedule();studioSave.conflict(new Error("Stale preview"));',h.context);
  assert.equal(status.children.at(-1),pressedButton,'the pressed button must remain in the DOM until click');
  assert.equal(status.children.length,2,'repeat publications do not duplicate the notice or button');
  pressedButton.events.click();
  assert.equal(h.reloads,1,'the first click refreshes');
  assert.equal(h.unload.prevented,false);
  assert.equal(attempts,1,'blur must not resend the refused save');
});

test("ordinary Campaign and Studio failures retain Try again and their specific cause",async()=>{
  const h=environment(),root=recoveryRoot();let retries=0;
  h.context.MotifSave.mountRecovery(root,{
    getSaveState:()=>({phase:'error',problem:{phase:'error',message:'Free disk space before trying again.'}}),
    subscribeSaveState(){return()=>{};},onSave(){retries++;},
  });
  const button=root.querySelector('[data-save-recover]');
  assert.equal(button.textContent,'Try again');assert.match(root.querySelector('p').textContent,/Free disk space/);
  root.events.click({target:{closest:()=>button}});await tick();assert.equal(retries,1);
  h.context.flushStudioSave=async()=>{retries++;};
  h.context.paintStudioSave({phase:'error',error:new Error('Disk is full.')});
  const status=h.controls.children[0];assert.equal(status.children[1].textContent,'Disk is full.');
  const retry=status.children.at(-1);assert.equal(retry.textContent,'Try again');
  await retry.events.click();assert.equal(retries,2);assert.equal(h.reloads,0);
  const closing=h.event();h.windowEvents.beforeunload(closing);assert.equal(closing.prevented,true);
});

test("failed browser reload restores the ordinary unsaved-work warning",()=>{
  const h=environment();
  h.context.window.location.reload=()=>{throw new Error('Reload unavailable');};
  assert.throws(()=>h.context.MotifSave.refreshPage(),/Reload unavailable/);
  assert.equal(h.context.MotifSave.refreshing(),false);
  const closing=h.event();h.windowEvents.beforeunload(closing);assert.equal(closing.prevented,true);
});


test("Studio autosave is silent and never inserts routine status into the controls",()=>{
  const h=environment(),field=node('textarea');h.controls.append(field);
  for(const phase of ['dirty','saving','saved','dirty']) {
    h.context.paintStudioSave({phase});
    assert.deepEqual(h.controls.children,[field],'ordinary status never enters the controls');
    assert.equal(h.status.textContent,'','there is no routine success indicator');
  }
});


test("Handoff descriptions retain the unload warning while dirty, busy, or failed",()=>{
  const h=environment();h.context.projectQueue={isDirty:()=>false};
  for(const [dirty,busy] of [[true,false],[false,true],[true,true]]) {
    h.context.handoffDescriptionSave={queue:{dirty:()=>dirty,busy:()=>busy}};
    const closing=h.event();h.windowEvents.beforeunload(closing);assert.equal(closing.prevented,true);
  }
  h.context.handoffDescriptionSave={queue:{dirty:()=>false,busy:()=>false}};
  const closing=h.event();h.windowEvents.beforeunload(closing);assert.equal(closing.prevented,false);
});
