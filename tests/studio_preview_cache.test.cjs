const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture() {
  const reads=[],decodes=[];
  const c=vm.createContext({structuredClone,Map,AbortController,
    document:{body:{dataset:{}},querySelector:()=>({content:'local'})},
    Image:class {decode(){decodes.push(this.src);return this.src==='/broken' ? Promise.reject(Error('decode failed')) : Promise.resolve();}},
    fetch:async(path,options)=>{reads.push({path,options});return {ok:true,json:async()=>({images:[{url:'/verified'}],number:reads.length})};},
  });
  const run=code=>vm.runInContext(code,c);
  run(fs.readFileSync('src/campaign_studio/static/motif/api.js','utf8'));
  run(fs.readFileSync('src/campaign_studio/static/motif/studio-workspace.js','utf8'));
  run("C={id:'campaign',products:[{id:'product'}]};model={version:'one',manifest:{revision_id:'one'}};studioWorkspace={};");
  return {c,run,reads,decodes};
}

test('exact preview responses and decoded layers reuse only the current target and document',async()=>{
  const f=fixture();
  const first=await f.run("api('/api/campaigns/campaign/text/preview',{revision_id:'one',placements:{}})");
  first.images[0].url='/changed-by-caller';
  const next=await f.run("api('/api/campaigns/campaign/text/preview',{revision_id:'one',placements:{}})");
  assert.equal(f.reads.length,1);assert.equal(next.images[0].url,'/verified');
  const image=await f.run("decodedImage('/verified')");
  assert.equal(await f.run("decodedImage('/verified')"),image);assert.equal(f.decodes.length,1);
  for(const change of ["currentFormat=1", "lang='es'", "model.version='two';model.manifest.revision_id='two'", "studioWorkspace={}"]){
    f.run(change);
    await f.run("api('/api/campaigns/campaign/text/preview',{revision_id:'one',placements:{}})");
    await f.run("decodedImage('/verified')");
  }
  assert.equal(f.reads.length,5);assert.equal(f.decodes.length,5);
  f.run('studioWorkspace=null');await f.run("decodedImage('/verified')");await f.run("decodedImage('/verified')");
  assert.equal(f.decodes.length,7,'leaving Studio releases decoded layer reuse');
});

test('changed placement, saved logo replacement version, and stale-tab writes never use an old preview authorization',async()=>{
  const f=fixture();
  for(const placement of [1,2])await f.run(`api('/api/campaigns/campaign/logo/preview',{revision_id:'one',placement:${placement}})`);
  assert.equal(f.reads.length,2);
  f.run("model.version='replacement';model.manifest.revision_id='replacement'");
  await f.run("api('/api/campaigns/campaign/logo/preview',{revision_id:'one',placement:2})");
  assert.equal(f.reads.length,3);
  f.c.fetch=async()=>({ok:false,status:409,json:async()=>({code:'stale',message:'Refresh'})});
  await assert.rejects(f.run("api('/api/campaigns/campaign/logo/save',{revision_id:'one',placement:2})"),e=>e.status===409);
});

test('failed JSON, failed image decode, and aborted preview requests are evicted',async()=>{
  const f=fixture(),path='/api/campaigns/campaign/framing/preview';
  await f.run(`api('${path}',{revision_id:'one'})`);
  await assert.rejects(f.run("decodedImage('/broken')"),/decode failed/);
  await f.run(`api('${path}',{revision_id:'one'})`);
  assert.equal(f.reads.length,2,'a failed layer decode forces a fresh preview request');
  await assert.rejects(f.run("decodedImage('/broken')"),/decode failed/);
  assert.equal(f.decodes.length,2);
  let finish;
  f.c.fetch=async()=>({ok:true,json:()=>new Promise(resolve=>{finish=resolve})});
  f.run('controller=new AbortController()');
  const pending=f.run(`api('${path}',{revision_id:'two'},{signal:controller.signal})`);
  await new Promise(setImmediate);f.run('controller.abort()');finish({images:[]});await pending;
  assert.equal(f.run('studioPreviewCache().requests.size'),0);
  f.c.fetch=async()=>({ok:true,json:async()=>{throw Error('bad JSON')}});
  await assert.rejects(f.run(`api('${path}',{revision_id:'three'})`),/bad JSON/);
  assert.equal(f.run('studioPreviewCache().requests.size'),0);
});

test('preview and decoded-layer retention stay bounded and late target responses cannot repopulate current cache',async()=>{
  const f=fixture();
  for(let index=0;index<12;index++){
    await f.run(`api('/api/campaigns/campaign/text/preview',{placement:${index}})`);
    await f.run(`decodedImage('/layer-${index}')`);
  }
  assert.equal(f.run('studioPreviewCache().requests.size'),4);
  assert.equal(f.run('studioPreviewCache().images.size'),8);
  let finish;f.c.fetch=async()=>({ok:true,json:()=>new Promise(resolve=>{finish=resolve})});
  const pending=f.run("api('/api/campaigns/campaign/logo/preview',{placement:'late'})");
  await new Promise(setImmediate);f.run("currentFormat=2;studioPreviewCache()");finish({images:[]});await pending;
  assert.equal(f.run('studioPreviewCache().requests.size'),0);
});

test('expired cached layer regenerates its preview automatically and aged responses refresh',async()=>{
  const f=fixture();let calls=0;
  f.c.fetch=async()=>({ok:true,json:async()=>({images:[{url:++calls===1?'/broken':'/fresh'}]})});
  assert.equal((await f.run("api('/api/campaigns/campaign/logo/preview',{})")).images[0].url,'/broken');
  assert.equal((await f.run("api('/api/campaigns/campaign/logo/preview',{})")).images[0].url,'/fresh');
  assert.equal(calls,2,'decode failure must regenerate once without requiring a user retry');
  f.run('for(const entry of studioPreviewCache().requests.values())entry.created=0');
  await f.run("api('/api/campaigns/campaign/logo/preview',{})");assert.equal(calls,3);
});

test('image handover after autosave admits only an image decoded for the acknowledged version',async()=>{
  const f=fixture();
  f.c.image={complete:true,naturalWidth:1080,style:{visibility:''},currentSrc:'/current',src:'/current'};
  f.run("modal='text';studioWorkspace.root={querySelector:()=>image};studioWorkspace.surfaceKey=studioPreviewCache().key");
  f.run('studioRetainImage()');assert.equal(f.run('studioRetainedImage()'),f.c.image);
  f.run("model.version='saved';model.manifest.revision_id='saved';studioRetainImage()");
  assert.equal(f.run('studioRetainedImage()'),null,'old surface cannot claim a new document version');
  await f.run("decodedImage('/current')");f.run('studioRetainImage()');
  assert.equal(f.run('studioRetainedImage()'),f.c.image);
  f.run('currentFormat=1;modal="";studioRetainImage()');
  assert.equal(f.run('studioRetainedImage()'),null,'a second teardown must not relabel the old target');
});
