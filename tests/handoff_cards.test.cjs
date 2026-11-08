'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = path.resolve(__dirname, '../src/campaign_studio/static/motif');
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context = {
  console, Set, Map, document: {addEventListener(){}}, C: {id:'test', name:'Test campaign', markets:['en'], products:[{id:'sled',name:'Toboggan'},{id:'blanket',name:'Blanket'},{id:'bells',name:'Bells'}]},
  FMT:[{name:'Square',ratio:'1:1'},{name:'Story',ratio:'9:16'},{name:'Landscape',ratio:'16:9'}],
  LANG:{en:'English'}, MARKET:{en:'en-US'}, page:'handoff', filter:'all', pageNum:0, operation:null,
  esc:escape, damagedFiles:()=>0, unavailableSource:()=>false,
  campaignMetricDisplay:()=>({spend:'$0.10',known_spend:true}), descriptionCount:()=>9, total:()=>9,
  btn:(label, action, cls='', attrs='')=>`<button data-action="${escape(action)}" class="${escape(cls)}" ${attrs}>${escape(label)}</button>`,
  badge:(label, cls)=>`<span class="badge ${escape(cls)}">${escape(label)}</span>`,
  notice:(title, text)=>`<aside><strong>${escape(title)}</strong><p>${escape(text)}</p></aside>`,
  creative:(p,l,f)=>`<img class="test-preview" src="/preview/${p}/${l}/${f}" alt="Preview">`,
};
context.window=context;
context.outputFor=(p,l,f)=>context.model.manifest.outputs.find(o=>o.product_id===context.C.products[p].id && o.market===context.MARKET[l] && o.ratio===context.FMT[f].ratio);
vm.createContext(context);
for(const file of ['wording.js','wording-host.js','presentation.js']) vm.runInContext(fs.readFileSync(path.join(source,file),'utf8'),context,{filename:file});
function reset() {
  context.pageNum=0; context.filter='all';
  context.model={manifest:{complete:true,findings:[],outputs:context.FMT.flatMap(f=>context.C.products.map(p=>({id:`${p.id}/en-US/${f.ratio.replace(':','x')}`,product_id:p.id,market:'en-US',ratio:f.ratio,url:'/actual-preview',eligible_for_export:true}))),metrics:{completed_images:9}},wording_issue:null,editable_products:{sled:true,blanket:true,bells:true},rule_withheld_outputs:[]};
}
function phrase(output, term='guaranteed', field='message') {
  return {item:output.id,rule:'banned-promise',severity:'blocking',evidence:'Actual rule match',context:{product_id:output.product_id,product_name:'Toboggan',market:output.market,ratio:output.ratio,phrase:{configured_phrase:term,market:output.market,matches:[{field,value:term,origin:'product_override',path:['products',0,'copy_overrides','en-US',field]}]}}};
}
function setFindings(findings) {
  context.model.manifest.findings=findings;
  context.model.wording_issue=findings.some(f=>f.context?.phrase) ? {code:'prohibited_phrase',matches:findings.filter(f=>f.context?.phrase)} : null;
  context.model.manifest.complete=!findings.some(f=>f.severity==='blocking');
}
const nodeTest = require('node:test');
function test(name, fn) { nodeTest(name, () => { reset(); fn(); }); }
const cards=html=>[...html.matchAll(/<article class="overview-item">([\s\S]*?)<\/article>/g)].map(m=>m[1]);
test('Only the affected output shows a correction, and its real preview stays visible',()=>{
  setFindings([phrase(context.model.manifest.outputs[0])]);
  const rendered=cards(context.overviewView(true));
  assert.equal(rendered.length,6);
  assert.match(rendered[0],/test-preview/);assert.match(rendered[0],/Changes needed/);assert.match(rendered[0],/Change “guaranteed” in the headline before exporting/);
  for(const card of rendered.slice(1)) assert.doesNotMatch(card,/overview-issue/);
  assert.doesNotMatch(context.overviewView(false),/overview-issue/);
});
test('First and third affected products have warnings, while the middle product does not',()=>{
  setFindings([phrase(context.model.manifest.outputs[0]),phrase(context.model.manifest.outputs[2],'guaranteed','cta')]);
  const rendered=cards(context.overviewView(true));
  assert.match(rendered[0],/in the headline/);assert.match(rendered[2],/in the call to action/);assert.doesNotMatch(rendered[1],/overview-issue/);
  assert.equal(rendered.filter(c=>c.includes('overview-issue')).length,2);
});
test('Wording-blocked campaign says Changes needed instead of Partial handoff',()=>{
  setFindings([phrase(context.model.manifest.outputs[0])]);
  const html=context.handoff();
  assert.match(html,/CHANGES NEEDED CAMPAIGN PACKAGE/);assert.match(html,/>Changes needed<\/span>/);
  assert.doesNotMatch(html,/Partial handoff|PARTIAL CAMPAIGN PACKAGE/);
  assert.match(html,/data-action="exportZip"[^>]*disabled/);
});
test('A real layout finding appears on its card and blocks the campaign status',()=>{
  const output=context.model.manifest.outputs[2];
  setFindings([{item:output.id,rule:'text_fit',severity:'blocking',evidence:'The headline extends beyond the image edge.',context:{product_id:output.product_id,market:output.market,ratio:output.ratio}}]);
  assert.match(cards(context.overviewView(true))[2],/The headline extends beyond the image edge/);
  assert.match(context.handoff(),/CHANGES NEEDED CAMPAIGN PACKAGE/);
});
test('Missing source without rule or layout finding is labeled Images missing',()=>{
  context.model.manifest.outputs[1].url=null;context.model.manifest.outputs[1].omission='Prepare the source image in Production.';
  context.model.manifest.complete=false;context.model.manifest.metrics.completed_images=8;
  const html=context.handoff();
  assert.match(html,/PARTIAL CAMPAIGN PACKAGE/);assert.match(html,/Images missing/);assert.doesNotMatch(html,/Partial handoff/);
  assert.doesNotMatch(html,/CHANGES NEEDED CAMPAIGN PACKAGE|overview-issue/);
});
test('Correcting the finding removes the warning on the next render',()=>{
  setFindings([phrase(context.model.manifest.outputs[0])]);assert.match(context.overviewView(true),/overview-issue/);
  setFindings([]);const html=context.handoff();assert.doesNotMatch(html,/overview-issue|Changes needed|Partial handoff|Images missing/);assert.match(html,/All formats included/);
});
test('Rule phrases and layout evidence are HTML-escaped',()=>{
  const attack='<img src=x onerror="alert(1)">';
  setFindings([phrase(context.model.manifest.outputs[0],attack),{item:context.model.manifest.outputs[2].id,rule:'text_fit',severity:'blocking',evidence:attack}]);
  const html=context.overviewView(true);assert.doesNotMatch(html,/<img src=x|onerror="alert/);assert.match(html,/&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});
test('Nonblocking findings do not accuse any output',()=>{
  const finding=phrase(context.model.manifest.outputs[0]);finding.severity='warning';
  context.model.manifest.findings=[finding];assert.doesNotMatch(context.overviewView(true),/overview-issue/);
});
test('Exact output context is supported without leaking to another ratio or market',()=>{
  const finding=phrase(context.model.manifest.outputs[0]);finding.item='external-finding-id';
  setFindings([finding]);const rendered=cards(context.overviewView(true));assert.equal(rendered.filter(c=>c.includes('overview-issue')).length,1);
  finding.context.market='es-MX';assert.doesNotMatch(context.overviewView(true),/overview-issue/);
});
