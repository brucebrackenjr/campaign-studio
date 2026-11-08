const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const context = vm.createContext({ structuredClone, setTimeout, clearTimeout });
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/studio-autosave.js", "utf8"), context);
  const h = { value: { text: "Original", left: .1 }, calls: [], states: [], accepted: [], version: "v1" };
  h.reply = async () => ({ version: "v2" });
  h.queue = context.compositionSaveQueue({
    capture: () => structuredClone(h.value), delay: 5,
    send: async attempt => {
      attempt.request ||= { version: h.version, value: structuredClone(attempt.captured) };
      h.calls.push(structuredClone(attempt.request));
      return h.reply(attempt);
    },
    accept: async (result, value) => { h.version = result.version; h.accepted.push(value); },
    changed: state => h.states.push(state),
  });
  return h;
}
test("typing pauses save one current metadata request without a preview prerequisite", async () => {
  const h = harness();
  h.value.text = "N"; h.queue.schedule();
  h.value.text = "New wording"; h.queue.schedule();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].value.text, "New wording");
  assert.equal(h.queue.dirty(), false);
  assert.equal(h.states.at(-1).phase, "saved");
  h.queue.stop();
});
test("navigation flush waits for older and newer input, without replacing newer typing", async () => {
  const h = harness(); let release;
  h.reply = () => new Promise(resolve => { release = resolve; });
  h.value.text = "First";
  const leaving = h.queue.flush(); await tick();
  h.value.text = "Second";
  h.reply = async () => ({ version: "v3" });
  release({ version: "v2" });
  assert.equal(await leaving, true);
  assert.deepEqual(h.calls.map(call => call.value.text), ["First", "Second"]);
  assert.deepEqual(h.calls.map(call => call.version), ["v1", "v2"]);
  assert.equal(h.value.text, "Second");
  assert.equal(h.queue.dirty(), false);
});
test("lost acknowledgement retries the same versioned request before saving newer work", async () => {
  const h = harness(); h.value.left = .4;
  h.reply = async () => { throw new Error("Connection lost"); };
  await assert.rejects(h.queue.flush(), /Connection lost/);
  assert.equal(h.value.left, .4); assert.equal(h.queue.dirty(), true);
  h.value.left = .6; h.reply = async () => ({ version: h.calls.length === 2 ? "v2" : "v3" });
  await h.queue.flush();
  assert.deepEqual(h.calls[1], h.calls[0]);
  assert.equal(h.calls[2].version, "v2");
  assert.equal(h.calls[2].value.left, .6);
});
test("a stale-tab refusal keeps edits and does not silently retry or accept", async () => {
  const h = harness(); h.value.text = "Keep this";
  h.reply = async () => { throw Object.assign(new Error("Newer saved work exists"), { status: 409 }); };
  await assert.rejects(h.queue.flush(), /Newer saved/);
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(h.calls.length, 1); assert.equal(h.accepted.length, 0);
  assert.equal(h.value.text, "Keep this"); assert.equal(h.states.at(-1).phase, "conflict");
  h.value.text = "More typing after refusal";
  h.queue.schedule(0);
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(await h.queue.flush(), false, "navigation cannot resend a stale save");
  assert.equal(h.calls.length, 1);
  assert.equal(h.states.at(-1).phase, "conflict");
  assert.equal(h.queue.rebase, undefined);
  assert.equal(h.value.text, "More typing after refusal");
  h.queue.stop();
});
test("a failed save does not report Saved and a corrected retry can save", async () => {
  const h = harness(); h.value.text = "Changed";
  h.reply = async () => { throw Object.assign(new Error("Disk is full"), { status: 507 }); };
  await assert.rejects(h.queue.flush(), /Disk is full/);
  assert.equal(h.states.at(-1).phase, "error");
  assert.equal(h.queue.dirty(), true);
  h.reply = async () => ({ version: "v2" });
  assert.equal(await h.queue.flush(), true);
  assert.equal(h.states.at(-1).phase, "saved");
});

test("metadata acknowledgement refreshes every logical URL and changed text without source reads",()=>{
 const c=vm.createContext({structuredClone,setTimeout,clearTimeout});
 vm.runInContext(readFileSync("src/campaign_studio/static/motif/studio-autosave.js","utf8"),c);
 const old={version:"old",composition_version:"old",can_edit:true,source_urls:{a:"/composition/old/sources/a"},
  manifest:{revision_id:"old",outputs:[{id:"a/en/1x1",url:"/composition/old/images/a",copy:{message:"Before"}},{id:"b/en/1x1",url:null,omission:"missing"}]}};
 const ack={version:"new",composition_version:"new",composition_issues:[],composition_slots:[{id:"a/en/1x1",copy:{message:"After"},width:1080}]};
 const merged=c.mergeStudioAcknowledgement(old,ack);
 assert.equal(merged.manifest.outputs[0].copy.message,"After");
 assert.equal(merged.composition_projection_stale,true);
 assert.equal(merged.manifest.outputs[0].url,"/composition/new/images/a");
 assert.equal(merged.source_urls.a,"/composition/new/sources/a");
 assert.equal(merged.manifest.outputs[1].url,null); assert.equal(merged.can_edit,true);
 assert.equal(old.manifest.outputs[0].copy.message,"Before");
});

test("an early style choice waits for measured geometry before save or navigation capture", async () => {
  const c=vm.createContext({structuredClone,setTimeout,clearTimeout});
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/studio-autosave.js","utf8"),c);
  let value={placement:null,pendingStyle:null},release;const calls=[];
  const geometry=new Promise(resolve=>release=resolve);
  const queue=c.compositionSaveQueue({
    capture:()=>structuredClone(value),
    prepare:async()=>{
      if(value.pendingStyle){await geometry;if(value.pendingStyle)value={placement:{left:.12,font_size:59,color:value.pendingStyle},pendingStyle:null};}
      return true;
    },
    send:async attempt=>{calls.push(structuredClone(attempt.captured));return{};},accept(){},
  });
  value.pendingStyle='#FFFFFF';
  const autosave=queue.flush(),navigation=queue.flush();
  await tick();assert.equal(calls.length,0);assert.equal(queue.dirty(),true);
  release();await Promise.all([autosave,navigation]);
  assert.equal(calls.length,1);assert.deepEqual(calls[0],{placement:{left:.12,font_size:59,color:'#FFFFFF'},pendingStyle:null});
  assert.equal(queue.dirty(),false);
});

test("save acknowledgement removes corrected wording and fit findings while preserving source failures",()=>{
 const c=vm.createContext({structuredClone,setTimeout,clearTimeout});
 vm.runInContext(readFileSync("src/campaign_studio/static/motif/studio-autosave.js","utf8"),c);
 const previous={version:"old",wording_issue:{code:"prohibited_phrase"},manifest:{outputs:[],findings:[{rule:"text_fit",severity:"blocking"},{rule:"rule-guaranteed",context:{phrase:{}}},{rule:"source",severity:"blocking"}]}};
 const next=c.mergeStudioAcknowledgement(previous,{version:"new",composition_slots:[],composition_findings:[],wording_issue:null});
 assert.equal(next.wording_issue,null);
 assert.deepEqual(Array.from(next.manifest.findings,f=>f.rule),["source"]);
 assert.equal(previous.manifest.findings.length,3);
});
