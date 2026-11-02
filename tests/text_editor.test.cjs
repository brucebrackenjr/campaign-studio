const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function fixture() {
  const listeners = [];
  const context = vm.createContext({
    document: { addEventListener() {} },
    $: () => ({ getBoundingClientRect: () => ({ left: 10, top: 20, width: 540, height: 540 }) }),
    listeners,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/campaign_studio/static/motif/text.js'), 'utf8'), context);
  vm.runInContext(`
    let modal = 'text', C = { id: '08639a3c-6df4-53cd-9cf4-e1ce27b66273' };
    textEditor = {
      campaign: C.id, selected: 'message', width: 1080, height: 1080,
      sequence: 2, geometrySequence: 2, pending: false, saving: false,
      placements: {}, input: { cancel() { listeners.push('cancel'); } },
      geometry: { text_blocks: {
        message: { bounds: [100, 100, 500, 150] },
        cta: { bounds: [100, 150, 400, 180] }
      }}
    };
    paintText = () => listeners.push(textEditor.selected);
  `, context);
  return {
    context,
    run: code => vm.runInContext(code, context),
    point(x, y, handle = false) {
      context.event = { clientX: 10 + x / 2, clientY: 20 + y / 2, button: 0, isPrimary: true, target: { closest: selector => handle && selector === '[data-mf-corner]' ? {} : null } };
      vm.runInContext('selectTextAtPointer(event, textEditor)', context);
    },
  };
}

test('actual CTA glyphs select through the expanded Message frame without changing layout', () => {
  const f = fixture();
  f.point(250, 165);
  assert.equal(f.run('textEditor.selected'), 'cta');
  assert.equal(f.run('JSON.stringify(textEditor.placements)'), '{}');
  assert.deepEqual(Array.from(f.context.listeners), ['cancel', 'cta']);
});

test('explicit resize handles keep their selected block and overlap follows CTA draw order', () => {
  const f = fixture();
  f.point(250, 165, true);
  assert.equal(f.run('textEditor.selected'), 'message');
  f.run('textEditor.geometry.text_blocks.cta.bounds = [100, 120, 400, 170]');
  f.point(250, 140);
  assert.equal(f.run('textEditor.selected'), 'cta');
  f.point(450, 140);
  assert.equal(f.run('textEditor.selected'), 'message');
});

test('padding stays a move surface while stale geometry and retired editor owners cannot select', () => {
  const f = fixture();
  f.point(250, 190);
  assert.equal(f.run('textEditor.selected'), 'message');
  f.run('textEditor.pending = true');
  f.point(250, 165);
  assert.equal(f.run('textEditor.selected'), 'message');
  f.run('textEditor.pending = false; textEditor.geometrySequence = 1');
  f.point(250, 165);
  assert.equal(f.run('textEditor.selected'), 'message');
  f.run("textEditor.geometrySequence = 2; const retired = textEditor; textEditor = { ...retired, campaign: '8f57d6d7-9318-564d-b7a8-1b78b4e978b7' }; C.id = '8f57d6d7-9318-564d-b7a8-1b78b4e978b7'");
  f.run('selectTextAtPointer(event, retired)');
  assert.equal(f.run('textEditor.selected'), 'message');
});

function keyboardFixture() {
  const nodes = new Map(), events = {}, timers = [];
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {
      handlers: {}, classList: {add() {}, remove() {}},
      addEventListener(type, fn) { this.handlers[type] = fn; },
      getBoundingClientRect: () => ({width: 540, height: 540}),
    });
    return nodes.get(key);
  };
  const context = vm.createContext({
    document: {addEventListener(type, fn) { const prior = events[type]; events[type] = e => { prior?.(e); fn(e); }; }},
    $: node, structuredClone, copy: structuredClone, AbortController,
    setTimeout(fn) {timers.push(fn); return timers.length;}, clearTimeout() {},
    requestAnimationFrame() {return 1;}, cancelAnimationFrame() {},
    ResizeObserver: class {observe() {} disconnect() {}},
    MotifEditorControls: {copy: {previewing: 'Preparing preview'}},
    C: {id: '292e9e84-eb00-5fdb-87e8-423f5e51fac4'}, modal: 'text',
  });
  const run = code => vm.runInContext(code, context);
  run(fs.readFileSync('src/campaign_studio/static/motif/direct-manipulation.js', 'utf8'));
  run(fs.readFileSync('src/campaign_studio/static/motif/text.js', 'utf8'));
  run(`textEditor = {
    campaign:'292e9e84-eb00-5fdb-87e8-423f5e51fac4', selected:'message', width:1080, height:1080,
    sequence:0, geometrySequence:0, gestureBlock:'message', placements:{},
    pending:false, saving:false, recovery:null, layers:null,
    geometry:{text_blocks:{
      message:{bounds:[100,100,400,160],placement:{left:0.1,top:0.1,width:0.5,font_size:60,font_id:'noto_sans',color:null}},
      cta:{bounds:[100,200,300,240],placement:{left:0.1,top:0.2,width:0.5,font_size:40,font_id:'noto_sans',color:null}}
    }}
  }; paintText=()=>{};positionTextSelection=()=>{};sizeTextCanvas=()=>{};previewText=()=>{};bindText();`);
  const key = (name, corner = null, shift = false) => node('.mf-direct-selection').handlers.keydown({
    key:name,shiftKey:shift,preventDefault() {},stopPropagation() {},
    target:{closest:()=>corner ? {dataset:{mfCorner:corner}} : null},
  });
  return {run,key,events,context};
}

test('rapid direct gestures retain every nudge and resize while wording changes require fresh geometry', () => {
  const f = keyboardFixture();
  f.key('ArrowRight');
  f.run('textEditor.pending=true');
  for (let i=0;i<9;i++) f.key('ArrowRight');
  f.key('ArrowDown',null,true);
  assert.equal(f.run('textEditor.geometry.text_blocks.message.bounds[0]'),110);
  assert.equal(f.run('textEditor.geometry.text_blocks.message.bounds[1]'),110);
  assert.equal(f.run('textEditor.sequence'),11);
  f.key('ArrowRight','se');
  assert.equal(f.run('textEditor.sequence'),12);
  assert.equal(f.run('textEditor.placements.message.font_size'),61);
  f.events.change({target:{matches:selector=>selector==='[data-mf-font]',value:'barlow_condensed'}});
  f.key('ArrowRight');
  assert.equal(f.run('textEditor.sequence'),13);
  assert.equal(f.run('textEditor.placements.message.font_id'),'barlow_condensed');
});

test('pending move keys stay bound to the measured block and retired editor owner', () => {
  const f = keyboardFixture();
  f.key('ArrowRight');
  f.run("textEditor.pending=true;textEditor.selected='cta'");
  f.key('ArrowRight');
  assert.equal(f.run('textEditor.geometry.text_blocks.cta.bounds[0]'),100);
  f.run("const retired=textEditor;endText();textEditor={...retired,selected:'message',campaign:'62220994-1d32-5713-80d2-ac0b6ff0baf3',closing:false};C.id='62220994-1d32-5713-80d2-ac0b6ff0baf3'");
  f.key('ArrowRight');
  assert.equal(f.run('textEditor.geometry.text_blocks.message.bounds[0]'),101);
});

test('fractional text origins stay valid at each canvas edge without changing measured shape', () => {
  const f = keyboardFixture();
  for (const [id, placement, bounds] of [
    ['message', {left:0.004444444444444442,top:0.23166666666666666,width:0.88,font_size:88,font_id:'barlow_condensed',color:'#FFF4DC'}, [5,250,592,332]],
    ['cta', {left:0.015555555555555555,top:0.3316666666666667,width:0.88,font_size:52,font_id:'noto_sans',color:'#FFF4DC'}, [17,358,421,411]],
  ]) {
    f.context.start = {id,placement,bounds,patch:{other:'preserved'},anchor:null};
    assert.equal(f.run('moveTextGesture(start,0,0)'), f.context.start);
    for (const [dx,dy] of [[-2000,-2000],[2000,2000]]) {
      const moved = f.run(`moveTextGesture(start,${dx},${dy})`);
      assert.ok(moved.placement.left >= 0 && moved.placement.left <= 1, id);
      assert.ok(moved.placement.top >= 0 && moved.placement.top <= 1, id);
      assert.ok(moved.bounds[0] >= 0 && moved.bounds[1] >= 0, id);
      assert.ok(moved.bounds[2] <= 1080 && moved.bounds[3] <= 1080, id);
      assert.ok(Math.abs(moved.bounds[0]-bounds[0]-(moved.placement.left-placement.left)*1080)<1e-10);
      assert.ok(Math.abs(moved.bounds[1]-bounds[1]-(moved.placement.top-placement.top)*1080)<1e-10);
      assert.equal(moved.bounds[2]-moved.bounds[0],bounds[2]-bounds[0]);
      assert.equal(moved.bounds[3]-moved.bounds[1],bounds[3]-bounds[1]);
      for (const key of ['width','font_size','font_id','color']) assert.equal(moved.placement[key],placement[key]);
      assert.equal(moved.patch.other,'preserved');
      assert.equal(moved.patch[id],moved.placement);
    }
  }
});

test('unaccepted text layout preserves placement and offers preview retry without finishing', async () => {
  const f = keyboardFixture();
  f.context.imageCleanupReady = async () => true;
  f.context.imageCapacity = async () => false;
  f.context.api = async () => { throw {status:409,code:'apply_not_accepted',message:'Could not apply. Your edits are still here.'}; };
  f.run(`textEditor.preview={displayed:true,body:{},result:{digest:'preview',impact:{render_outputs:['one']}}};
    textEditor.placements.message={left:0.2};`);
  assert.equal(await f.run('applyText()'),false);
  assert.equal(f.run('textEditor.placements.message.left'),0.2);
  assert.equal(f.run('textEditor.recovery'),'retry');
  assert.equal(f.context.modal,'text');
});


test('each corner scales the font and wrap column together about the opposite glyph corner', () => {
  const f = keyboardFixture();
  f.context.start = {id:'message',placement:{left:.34,top:.24,width:.42,font_size:54,font_id:'barlow_condensed',color:'#B5542F'},bounds:[367,259,773,309],patch:{cta:{font_size:30}},anchor:null};
  for (const corner of ['nw','ne','sw','se']) {
    const east=corner.endsWith('e'),south=corner.startsWith('s');
    const value=f.run(`resizeTextGesture(start,'${corner}',${east?50:-50},${south?20:-20})`);
    const scale=value.placement.font_size/54;
    assert.ok(scale>1);
    assert.equal(value.placement.width,.42*scale);
    assert.equal(value.bounds[east?0:2],f.context.start.bounds[east?0:2]);
    assert.equal(value.bounds[south?1:3],f.context.start.bounds[south?1:3]);
    assert.ok(Math.abs((value.bounds[2]-value.bounds[0])/406-scale)<1e-12);
    assert.ok(Math.abs((value.bounds[3]-value.bounds[1])/50-scale)<1e-12);
    assert.equal(value.patch.cta.font_size,30);
    assert.equal(value.placement.font_id,'barlow_condensed');
    assert.equal(value.placement.color,'#B5542F');
  }
});

test('unused CTA wrapping width does not impose an invisible enlargement limit', () => {
  const f=keyboardFixture();
  f.context.start={id:'cta',placement:{left:.06,top:.3,width:.88,font_size:38,font_id:'noto_sans',color:null},bounds:[65,324,165,360],patch:{},anchor:null};
  const value=f.run("resizeTextGesture(start,'se',300,100)");
  assert.ok(value.placement.font_size>38*2);
  assert.equal(value.placement.width,1);
  assert.ok(value.bounds[2]<1080&&value.bounds[3]<1080);
  for (const corner of ['nw','ne','sw','se']) {
    const east=corner.endsWith('e'),south=corner.startsWith('s');
    const edge=f.run(`resizeTextGesture(start,'${corner}',${east?5000:-5000},${south?5000:-5000})`);
    assert.ok(edge.bounds[0]>=0&&edge.bounds[1]>=0&&edge.bounds[2]<=1080&&edge.bounds[3]<=1080);
    assert.ok(edge.placement.left>=0&&edge.placement.top>=0);
  }
});
