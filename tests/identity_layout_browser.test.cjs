const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function layout() {
  const events={},frames=[],properties={};
  const rows=[{base:28,factor:25,extra:100,textContent:'🦊'.repeat(20)},
    {base:18,factor:25,extra:100,textContent:'🦊'.repeat(25)}]
    .map(row=>({...row,style:{fontSize:''},hidden:false}));
  let width=334;
  const caret={getBoundingClientRect:()=>({width:10})};
  const heading={querySelectorAll:()=>rows,querySelector:()=>caret,parentElement:{getBoundingClientRect:()=>({width:width+16})}};
  const document={querySelector:s=>s==='.campaign-heading'?heading:{getBoundingClientRect:()=>({height:88})},
    documentElement:{style:{setProperty:(key,value)=>properties[key]=value}},
    fonts:{ready:{then:fn=>events.fontReady=fn},addEventListener:(event,fn)=>events[event]=fn},
    createRange:()=>{let row;return{selectNodeContents:n=>row=n,getBoundingClientRect:()=>({width:(parseFloat(row.style.fontSize)||row.base)*row.factor+row.extra})}}};
  const context=vm.createContext({document,window:{addEventListener:(event,fn)=>events[event]=fn},
    getComputedStyle:row=>row===heading?{paddingLeft:'2',paddingRight:'2',columnGap:'2'}:row===caret?{marginLeft:'0',marginRight:'0'}:{fontSize:String(parseFloat(row.style.fontSize)||row.base)},
    requestAnimationFrame:fn=>{frames.push(fn);return frames.length}});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/identity-layout.js','utf8'),context);
  return{api:context.window.MotifIdentityLayout,rows,events,frames,properties,setWidth:value=>width=value,
    measured:row=>(parseFloat(row.style.fontSize)||row.base)*row.factor+row.extra};
}

test('header measures fallback glyphs, keeps full names, and restores normal fonts when room returns',()=>{
  const h=layout(),original=h.rows.map(row=>row.textContent);
  h.api.fit();
  for(const row of h.rows)assert.ok(h.measured(row)<=332);
  assert.deepEqual(h.rows.map(row=>row.textContent),original);
  assert.equal(h.properties['--motif-header-height'],'88px');
  h.setWidth(1000);h.api.fit();
  assert.deepEqual(h.rows.map(row=>row.style.fontSize),['','']);
  h.rows[0].factor=3;h.rows[1].factor=5;h.rows.forEach(row=>row.extra=0);
  h.setWidth(334);h.api.fit();
  assert.deepEqual(h.rows.map(row=>row.style.fontSize),['','']);
});

test('viewport and completed font loads coalesce without an observer feedback loop',()=>{
  const h=layout();h.events.resize();h.events.resize();h.events.fontReady();h.events.loadingdone();
  assert.equal(h.frames.length,1);h.frames[0]();
  assert.ok(h.rows.every(row=>h.measured(row)<=332));
  h.setWidth(1000);h.events.resize();assert.equal(h.frames.length,2);h.frames[1]();
  assert.deepEqual(h.rows.map(row=>row.style.fontSize),['','']);
});
