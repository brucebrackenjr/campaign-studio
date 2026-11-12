const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
function fixture() {
  const field = { value: '', addEventListener() {} }, status = {}, retry = {};
  const calls = [], c = vm.createContext({ structuredClone, setTimeout, clearTimeout, document:{addEventListener(){}}, page:'handoff',modal:'inspect',C:{id:'campaign',products:[{id:'product'}]},currentProduct:0,currentFormat:0,lang:'es',MARKET:{es:'es-MX'},FMT:[{ratio:'1:1'}],model:{version:'v1',metadata:{creatives:{'product/es-MX/1x1':{version:2}}}},
    $: selector => selector === '#image-description' ? field : selector === '#handoff-description-status' ? status : retry,
    canDescribeCreative:()=>true, saveProject: async () => true, useData: data => { c.model=data; }, render() {}, mergeStudioAcknowledgement:(_,data)=>data,
    api:async(path,body)=>{calls.push({path,body});return {version:'v2',metadata:{creatives:{'product/es-MX/1x1':{version:3}}}};},
  });
  vm.runInContext(fs.readFileSync('src/campaign_studio/static/motif/studio-autosave.js','utf8'),c);
  // Use the actual acknowledgement merger's required host-free replacement.
  vm.runInContext('mergeStudioAcknowledgement=(_,data)=>data',c);
  vm.runInContext(fs.readFileSync('src/campaign_studio/static/motif/review.js','utf8'),c);
  vm.runInContext('beginHandoffDescriptionSave()',c);
  return {c,calls,field,run:code=>vm.runInContext(code,c)};
}
test('Handoff description flush saves current version, product, market, and metadata concurrency token',async()=>{
  const f=fixture();f.field.value='A brass lantern.';
  assert.equal(await f.run('flushHandoffDescriptionSave()'),true);
  assert.equal(f.calls[0].path,'/api/campaigns/campaign/composition/v1/metadata/product/es-MX/1x1');
  assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].body)),{description:'A brass lantern.',mode:'current',expected_version:2});
  assert.equal(f.run('handoffDescriptionSave.queue.dirty()'),false);f.run('endHandoffDescriptionSave()');
});
test('uncertain description retry reuses exact request and leaves newer text pending until accepted',async()=>{
  const f=fixture();f.field.value='First';let reject=true;
  f.c.api=async(path,body)=>{f.calls.push({path,body});if(reject)throw new Error('Response lost');return {version:'v2',metadata:{creatives:{'product/es-MX/1x1':{version:3}}}};};
  assert.equal(await f.run('flushHandoffDescriptionSave()'),false);reject=false;f.field.value='Second';
  assert.equal(await f.run('flushHandoffDescriptionSave()'),true);
  assert.deepEqual(f.calls[1],f.calls[0]);assert.equal(f.calls[2].body.description,'Second');
  assert.match(f.calls[2].path,/composition\/v2/);f.run('endHandoffDescriptionSave()');
});
