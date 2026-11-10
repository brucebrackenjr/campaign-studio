// Shipped host adapter tests; actual pointer/layout and saved-pixel proof are
// exercised separately in the connected browser fixture.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('all logo attachment entry points share labels and disabled state during upload and review',()=>{
  const context=vm.createContext({});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/direct-manipulation.js','utf8'),context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8').replace('const api={mount:','const api={Editor,mount:'),context);
  const controls=[{},{}],nodes=new Map();
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{dataset:{},style:{},classList:{toggle(){}},setAttribute(){},getAttribute(){return''},getBoundingClientRect(){return{width:400,height:400}}});return nodes.get(selector)};
  const editor=Object.create(context.MotifLogo.Editor.prototype);
  Object.assign(editor,{open:true,asset:null,o:{embedded:true},p:{x:.2,y:.2,width:.2},default:{},target:{width:1080,height:1080},dirty:()=>false,
    root:{matches:()=>false,querySelector:node,querySelectorAll:selector=>selector==='[data-lg="replace"]'?controls:[]}});
  for(const asset of [null,{width:100,height:100,url:'/logo',filename:'logo.png'}]) {
    editor.asset=asset;
    for(const phase of ['loading','busy','reviewing','reviewRequired','uploading']) {
      for(const key of ['loading','busy','reviewing','reviewRequired','uploading'])editor[key]=key===phase;
      editor.paint();
      for(const control of controls){assert.equal(control.disabled,true,phase);assert.equal(control.textContent,asset?'Replace logo':'Attach logo');}
    }
    for(const key of ['loading','busy','reviewing','reviewRequired','uploading'])editor[key]=false;
    editor.paint();for(const control of controls)assert.equal(control.disabled,false);
    assert.equal(node('.mf-logo-photo').hidden,true);
    assert.equal(node('.mf-logo-copy').hidden,true);
    editor.o.savedDisplay={compositeUrl:'/retained.png'};editor.valid=null;
    editor.paint();assert.equal(node('.mf-logo-composite').hidden,false);
    assert.equal(node('.mf-logo-composite').src,'/retained.png');

  }
});

test("small-logo selection expands screen controls without changing artwork or resize anchors", () => {
  const context=vm.createContext({});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/direct-manipulation.js','utf8'),context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8'),context);
  const g=context.MotifLogo.geometry;
  for(const [width,height] of [[1080,1080],[1080,1920],[1920,1080]]) {
    const target={width,height},canvas={width:333.375,height:333.375*height/width};
    for(const aspect of [1,4,1/4,12,1/12]) {
      const asset={width:120*aspect,height:120};
      const placement=g.constrain({x:.45,y:.45,width:0},target,asset);
      const original=JSON.stringify(placement),d=g.dimensions(placement,target,asset);
      const rect={left:d.x*canvas.width,top:d.y*canvas.height,width:d.width*canvas.width,height:d.height*canvas.height};
      const frame=g.selectionFrame(rect,canvas);
      assert.equal(frame.width,64);assert.equal(frame.height,64);
      assert.equal(JSON.stringify(placement),original);
      assert.equal(placement.width*width,Math.max(1,aspect));
      for(const corner of ['nw','ne','sw','se']) {
        const grown=g.resize(placement,corner,corner.includes('w')?-10:10,corner.includes('n')?-10:10,target,asset);
        const anchor=p=>({x:p.x*width+(corner.includes('w')?p.width*width:0),y:p.y*height+(corner.includes('n')?p.width*width/aspect:0)});
        assert.ok(Math.abs(anchor(grown).x-anchor(placement).x)<1e-9);
        assert.ok(Math.abs(anchor(grown).y-anchor(placement).y)<1e-9);
      }
      for(const left of [0,canvas.width-rect.width])for(const top of [0,canvas.height-rect.height]) {
        const edge=g.selectionFrame({...rect,left,top},canvas);
        assert.ok(edge.left>=0&&edge.top>=0);
        assert.ok(edge.left+edge.width<=canvas.width&&edge.top+edge.height<=canvas.height);
      }
    }
  }
});

test("logo X and Cancel discard transient work and retire only their local session", async () => {
  const context = vm.createContext({ clearTimeout, cancelAnimationFrame() {}, document: { body: {} } });
  const source = readFileSync("src/campaign_studio/static/motif/logo-controls.js", "utf8");
  vm.runInContext(source.replace("const api={mount:", "const api={Editor,mount:"), context);
  const Editor = context.MotifLogo.Editor;
  for (const action of ["dismiss", "close"]) {
    const events = [];
    const editor = Object.create(Editor.prototype);
    Object.assign(editor, { open: true, busy: false, reviewing: false, seq: 1,
      initial: { x: 0, y: 0, width: 0.1 }, p: { x: 0.2, y: 0.3, width: 0.2 },
      pendingAsset: false, root: { removeEventListener() {}, innerHTML: "editor" },
      input: { destroy() { events.push("input detached"); } },
      o: { onClose: value => events.push(value) },
    });
    editor[action]();
    assert.equal(editor.open, false);
    assert.equal(editor.tearingDown, true);
    assert.equal(editor.seq, 2);
    assert.equal(events[0], "input detached");
    assert.equal(events[1].cancelled, true);
    editor[action]();
    await flush();
    assert.equal(events.length, 2, "Teardown and its callback happen only once");
  }
});

function harness() {
  const target = {
    campaignId: "292e9e84-eb00-5fdb-87e8-423f5e51fac4",
    runId: "run",
    revisionId: "before",
    productId: "product",
    market: "en-US",
    ratio: "1:1",
    width: 1080,
    height: 1080,
  };
  const body = {
    target,
    asset: { reference: "owned.png" },
    placement: { x: 0.1, y: 0.2, width: 0.3 },
    mode: "placement",
    previewDigest: "preview",
  };
  const calls = [],
    dialogs = [];
  const context = vm.createContext({
    structuredClone,
    JSON,
    Promise,
    draft: { brand: { logo_path: "old.png" } },
    baseline: JSON.stringify({ brand: { logo_path: "old.png" } }),
    model: { image_cleanup: { pending: [], } },
    api: async (url, payload) => {
      calls.push({ url, payload });
      return context.reply(url, payload);
    },
    decodedImage: async () => ({ naturalWidth: 1080, naturalHeight: 1080 }),
    reply() {
      throw Error("Unexpected request");
    },
    MotifStorage: {
      getView: (state) => ({ title: state, body: "body", footer: "footer" }),
    },
  });
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/logo.js", "utf8"),
    context,
  );
  const run = (code) => vm.runInContext(code, context);
  context.body = body;
  context.logoHostDialog = async (title) => {
    dialogs.push(title);
    return context.choice || "cancel";
  };
  context.refreshAfterLogo = (data) => {
    context.refreshed = data;
  };
  context.logoTarget = (data) => ({
    ...target,
    revisionId: data.manifest.revision_id,
  });
  run(
    "logoSession={target:body.target,previewKey:logoPreviewKey(body),previewDigest:body.previewDigest}",
  );
  const current = (digest = "preview", base = "before") => ({
    output_run_id: "run",
    manifest: {
      revision_id: "after",
      edit_receipt: {
        kind: "logo",
        base_revision: base,
        preview_digest: digest,
      },
      outputs: Array(12).fill({product_id: "product", market: "en-US", ratio: "1:1", url: "/saved.png"}),
      metrics: { completed_images: 12 },
    },
  });
  return { run, context, body, calls, dialogs, current };
}

test("logo preview does not resolve before the actual PNG decodes", async () => {
  const h = harness();
  let decode,
    finished = false;
  h.context.reply = () => ({
    digest: "preview",
    images: [
      {
        url: "actual.png",
        layers: { base_url: "base.png", copy_url: "copy.png", bounds: [1, 2, 31, 42], default_bounds: [1, 2, 31, 42] },
      },
    ],
    impact: { available_outputs: 12, expected_outputs: 12, fitted_count: 0 },
    findings: [],
  });
  const decoded=[];
  h.context.decodedImage = url => {
    decoded.push(url);
    return url === "actual.png" ? new Promise(resolve => { decode=resolve; }) : Promise.resolve({naturalWidth:1080,naturalHeight:1080});
  };
  const pending = h.context.logoPreview(h.body).then((result) => {
    finished = true;
    return result;
  });
  await flush();
  assert.equal(finished, false);
  decode({ naturalWidth: 1080, naturalHeight: 1080 });
  const result=await pending;
  assert.equal(result.compositeUrl, "actual.png");
  assert.equal(result.previewUrl, "base.png");
  assert.equal(result.copyOverlayUrl, "copy.png");
  assert.deepEqual(decoded,["actual.png","base.png","copy.png"]);
  h.context.decodedImage = async () => {
    throw Error("PNG failed");
  };
  await assert.rejects(h.context.logoPreview(h.body), /PNG failed/);
});

for (const mode of ["placement", "replacement"])
  test(`${mode} lost response reconciles only its published receipt`, async () => {
    const h = harness();
    h.body.mode = mode;
    h.run("logoSession.previewKey=logoPreviewKey(body)");
    h.context.reply = (url) => {
      if (url.endsWith("/apply")) throw Error("Response lost");
      return h.current();
    };
    const result = await h.context.applyLogo(h.body);
    assert.equal(result.target.revisionId, "after");
    assert.equal(h.calls.filter((c) => c.url.endsWith("/apply")).length, 1);
    assert.equal(h.context.refreshed.manifest.revision_id, "after");
  });

test("an unrelated later revision never confirms an uncertain logo save", async () => {
  const h = harness();
  h.context.reply = (url) => {
    if (url.endsWith("/apply")) throw Error("Response lost");
    return h.current("other", "after");
  };
  await assert.rejects(h.context.applyLogo(h.body), /Response lost/);
  assert.equal(h.context.refreshed, undefined);
  assert.equal(h.calls.length, 2);
});

test("known unapplied logo outcomes are not reconciled against an unrelated receipt", async () => {
  for (const code of ["no_changes", "apply_not_accepted"]) {
    const h = harness();
    h.context.reply = () => { throw { status: 409, code }; };
    await assert.rejects(h.context.applyLogo(h.body), e => e.code === code);
    assert.equal(h.calls.length, 1);
    assert.equal(h.context.refreshed, undefined);
  }
});

test("logo no-op preserves its preview while real conflict, uncertainty, and disposed responses stay distinct", async () => {
  const context = vm.createContext({ clearTimeout });
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/editor-controls.js', 'utf8'), context);
  const source = readFileSync('src/campaign_studio/static/motif/logo-controls.js', 'utf8');
  vm.runInContext(source.replace('const api={mount:', 'const api={Editor,mount:'), context);
  for (const outcome of ['no-op', 'not-accepted', 'generic-409', 'conflict', 'transport', 'disposed', 'changed-target']) {
    let reject, focus = 0, painted = 0, sub = 0;
    const editor = Object.create(context.MotifLogo.Editor.prototype);
    const preview = { signature: 'current', digest: 'exact' };
    Object.assign(editor, { open: true, busy: false, pendingAsset: true, p: {x: 0.1},
      target: {campaignId: '8fca7c2c-091a-5bfe-ace8-b6af9c0e801c', revisionId: 'one'}, asset: {reference: 'owned.png'}, valid: preview,
      signature: () => 'current', dirty: () => true, closeSub() {},
      paint() { painted++; }, root: {querySelector: () => ({focus() {focus++;}})},
      o: {onSave: () => new Promise((_resolve, fail) => {reject = fail;})},
      requireReview(reason) {this.reviewRequired = true;this.reason = reason;},
      conflict() {this.reviewRequired = true;this.reason = 'conflict';},
      showSub() {sub++;},
    });
    const task = editor.save();
    const before = painted;
    if (outcome === 'disposed') editor.open = false;
    if (outcome === 'changed-target') editor.signature = () => 'new target';
    reject(outcome === 'transport' ? new TypeError('Failed to fetch') : {
      status: 409, code: outcome === 'not-accepted' ? 'apply_not_accepted' : outcome === 'generic-409' ? 'state' : outcome === 'conflict' ? 'conflict' : 'no_changes',
    });
    await task;
    if (['disposed', 'changed-target'].includes(outcome)) {
      assert.equal(painted, before);
      assert.equal(focus, 0);
    } else if (['no-op','not-accepted'].includes(outcome)) {
      assert.equal(editor.valid, preview);
      assert.equal(editor.pendingAsset, true);
      assert.equal(editor.reviewRequired, undefined);
      assert.equal(editor.busy, false);
      assert.match(editor.message, outcome === 'no-op' ? /No changes were applied/ : /problem saving.*previous image is unchanged.*edits are still here/);
      assert.equal(focus, 1);
      assert.equal(sub, 0);
    } else {
      assert.equal(editor.valid, null);
      assert.equal(editor.reviewRequired, true);
      assert.equal(editor.reason, outcome === 'conflict' ? 'conflict' : 'uncertain');
    }
  }
});

test("shared capacity host captures campaign context without a logo session and releases only its own modal", async () => {
  for (const hasLogo of [false, true]) {
    const underlying = { inert: false }, events = {}, focus = [];
    const prior = { isConnected: true, focus() { focus.push("editor"); } };
    const cancel = { focus() { focus.push("notice"); } };
    const overlay = {
      querySelector: () => cancel,
      querySelectorAll: () => [cancel],
      addEventListener: (name, handler) => { events[name] = handler; },
      remove() { this.removed = true; },
    };
    const root = { children: [underlying], querySelector: () => prior, append(node) { this.appended = node; } };
    const body = {};
    const context = vm.createContext({
      C: { name: "Captured campaign" }, esc: String, queueMicrotask, studioWorkspace: null,
      $: () => root,
      document: { body, activeElement: hasLogo ? prior : body, createElement: () => overlay },
    });
    let inert = false;
    Object.defineProperty(underlying, "inert", {
      get: () => inert,
      set(value) {
        inert = value;
        if (value) context.document.activeElement = { focus() { focus.push("body"); } };
      },
    });
    vm.runInContext(readFileSync("src/campaign_studio/static/motif/logo.js", "utf8"), context);
    vm.runInContext(`logoSession=${hasLogo ? "{target:{campaignName:'Captured campaign'}}" : "null"}`, context);
    const result = context.logoHostDialog("Capacity", "Measured values", "Back", async (action, show) => {
      if (action !== "workspace") return;
      context.C.name = "Later campaign";
      show("Readiness", "Local checks", "Back");
      return false;
    });
    await flush();
    assert.equal(underlying.inert, true);
    assert.match(overlay.innerHTML, /Captured campaign/);
    const choose = action => events.click({ target: { closest: () => ({ dataset: { logoHost: action } }) }, stopPropagation() {} });
    choose("workspace");
    await flush();
    assert.match(overlay.innerHTML, /Captured campaign/);
    assert.doesNotMatch(overlay.innerHTML, /Later campaign/);
    if (hasLogo) {
      prior.isConnected = false;
      let stopped = false, prevented = false;
      events.keydown({ key: "Escape", stopPropagation() { stopped = true; }, preventDefault() { prevented = true; } });
      assert.equal(stopped && prevented, true);
    } else choose("cancel");
    assert.equal(await result, "cancel");
    assert.equal(overlay.removed, true);
    assert.equal(underlying.inert, false);
    assert.equal(focus.includes("editor"), !hasLogo, "A detached prior editor must not take focus");
  }
});

test("pending image cleanup delegates the exact host flow and never resumes Apply", async () => {
  const h = harness();
  h.context.model.image_cleanup.pending = [{ id: "cleanup", kind: "retirement" }];
  h.context.openImageCleanup = async value => {
    h.dialogs.push(value);
    h.context.model.image_cleanup.pending = [];
  };
  await assert.rejects(h.context.applyLogo(h.body), e => e.handled && e.commitState === "not_started");
  assert.equal(h.dialogs.length, 1);
  assert.equal(h.calls.length, 0, "Closing successful cleanup never resends the image Apply");
});

test("capacity refusal uses the structured existing dialog and never reports a save", async () => {
  const h = harness();
  h.context.reply = () => {
    throw {
      code: "storage_capacity",
      capacity: {
        kind: "workspace_budget",
        used_bytes: 9,
        budget_bytes: 10,
        projected_bytes: 2,
        free_bytes: 100,
        reserve_bytes: 1,
      },
    };
  };
  await assert.rejects(
    h.context.applyLogo(h.body),
    (e) => e.handled === true && e.commitState === "not_started",
  );
  assert.deepEqual(h.dialogs, ["storage-budget"]);
  assert.equal(h.calls.length, 1);
});

test("unknown capacity or network failures retain uncertainty rather than handled success", async () => {
  const h = harness();
  h.context.reply = () => {
    throw Error("Offline");
  };
  await assert.rejects(h.context.applyLogo(h.body), /Offline/);
  assert.equal(h.dialogs.length, 0);
  assert.equal(h.calls.length, 2);
});

test("known preview refusal keeps named context and cannot enable Save", async () => {
  const h = harness();
  h.run("logoSession.previewSequence=0");
  h.context.reply = () => {
    throw {
      status: 409,
      message: "Chalk · es-MX · 9:16 is unavailable. Use local recovery.",
    };
  };
  const result = await h.context.previewLogoEditor(h.body);
  assert.equal(result.blocked, true);
  assert.equal(result.digest, null);
  assert.equal(result.reviewRequired, false);
  assert.match(result.message, /Chalk · es-MX · 9:16/);
  assert.equal(h.run("logoSession.previewDigest"), null);
});

test("only confirmed revision or Brief conflicts require latest-version review", async () => {
  const h = harness();
  h.run("logoSession.previewSequence=0");
  for (const code of [
    "logo_stale_revision",
    "stale_brief",
    "busy",
    "state",
    "saved_images_unavailable",
  ]) {
    h.context.reply = () => {
      throw { status: 409, code, message: "Specific safe service guidance" };
    };
    const result = await h.context.previewLogoEditor(h.body);
    assert.equal(
      result.reviewRequired,
      ["logo_stale_revision", "stale_brief"].includes(code),
    );
  }
});

test("late earlier preview cannot replace the latest decoded save identity", async () => {
  const h = harness(),
    pending = [];
  h.run("logoSession.previewSequence=0");
  h.context.logoPreview = (body) =>
    new Promise((resolve) => pending.push({ body, resolve }));
  const first = h.context.previewLogoEditor(h.body);
  const newer = { ...h.body, placement: { x: 0.4, y: 0.5, width: 0.2 } };
  const second = h.context.previewLogoEditor(newer);
  pending[1].resolve({ digest: "latest" });
  await second;
  pending[0].resolve({ digest: "older" });
  await first;
  assert.equal(h.run("logoSession.previewDigest"), "latest");
  assert.equal(
    h.run("logoSession.previewKey"),
    h.context.logoPreviewKey(newer),
  );
});

test('explicit resize floor preserves old small artwork outside resize and yields to canvas room', () => {
  const context=vm.createContext({});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/direct-manipulation.js','utf8'),context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8'),context);
  const g=context.MotifLogo.geometry;
  for(const [width,height] of [[1080,1080],[1080,1920],[1920,1080]])for(const aspect of [1,4,.25,100,.01]) {
    const t={width,height},a={width:100*aspect,height:100};
    const p=g.constrain({x:.4,y:.4,width:0},t,a),w=p.width*width;
    assert.equal(g.move(p,1,1,t,a).width,p.width);
    assert.equal(g.constrain(p,t,a).width,p.width);
    for(const corner of ['nw','ne','sw','se']) {
      const sx=corner.includes('w')?-1:1,sy=corner.includes('n')?-1:1;
      assert.equal(JSON.stringify(g.resize(p,corner,0,0,t,a)),JSON.stringify(p));
      const resized=g.resize(p,corner,sx,sy/aspect,t,a);
      assert.ok(Math.abs(resized.width*width-Math.max(w+1,1,aspect,54*Math.min(1,aspect)))<1e-8);
      const anchor=p=>[p.x*width+(sx<0?p.width*width:0),p.y*height+(sy<0?p.width*width/aspect:0)];
      assert.ok(anchor(p).every((v,i)=>Math.abs(v-anchor(resized)[i])<1e-8));
    }
    if(w<54) {
      const near={...p,x:1-p.width,y:1-p.width*width/aspect/height};
      const fit=g.resize(near,'se',10,10,t,a);
      assert.ok(Math.abs(fit.width-p.width)<1e-8);
    }
    const same=g.fitReplacement(p,t,a,a);
    assert.equal(same.width,p.width);
  }
});

test('replacement fit records the automatic reduction until deliberate arrangement', async () => {
  const context=vm.createContext({queueMicrotask,requestAnimationFrame:()=>1});
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/direct-manipulation.js','utf8'),context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/editor-controls.js','utf8'),context);
  const source=readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8');
  vm.runInContext(source.replace('const api={mount:', 'const api={Editor,mount:'),context);
  const Editor=context.MotifLogo.Editor,g=context.MotifLogo.geometry;
  const target={campaignId:'8fca7c2c-091a-5bfe-ace8-b6af9c0e801c',runId:'r',revisionId:'v',productId:'p',market:'en-US',ratio:'1:1',width:1080,height:1080};
  const old={reference:'old',width:400,height:100},next={reference:'new',width:120,height:1440};
  const start={x:.6,y:.6,width:.3},editor=Object.create(Editor.prototype);
  Object.assign(editor,{target,asset:old,savedAsset:old,p:structuredClone(start),initial:structuredClone(start),open:true,seq:0,uploadSeq:0,
    o:{onSelectFile:async()=>next},root:{querySelector:()=>({focus(){}})},paint(){},validate:async()=>{}});
  await editor.selectFile({});
  const snapshot={replacementFit:structuredClone(editor.replacementFit),placement:structuredClone(editor.p)};
  assert.deepEqual(snapshot.replacementFit,start);
  assert.deepEqual(JSON.parse(JSON.stringify(snapshot.placement)),JSON.parse(JSON.stringify(g.fitReplacement(start,target,old,next))));
  const oldSignature=editor.signature();editor.p.x-=.01;editor.changed();
  assert.equal(editor.replacementFit,null);
  assert.notEqual(editor.signature(),oldSignature);
  // A replacement which needs no reduction has no fit claim.
  editor.p={x:.1,y:.1,width:.01};editor.o.onSelectFile=async()=>old;
  await editor.selectFile({});assert.equal(editor.replacementFit,null);
  const h=harness(),body={...h.body,asset:next,mode:'replacement',placement:snapshot.placement,replacementFit:start};
  assert.deepEqual(JSON.parse(JSON.stringify(h.context.logoBody(body).pre_fit_placement)),{left:.6,top:.6,width:.3});
  assert.notEqual(h.context.logoPreviewKey(body),h.context.logoPreviewKey({...body,replacementFit:undefined}));
});


test("late replacement preview cannot focus a reopened editor or a newer upload", async () => {
  const context = vm.createContext({});
  vm.runInContext(readFileSync("src/campaign_studio/static/motif/direct-manipulation.js", "utf8"), context);
  const source = readFileSync("src/campaign_studio/static/motif/logo-controls.js", "utf8");
  vm.runInContext(source.replace("const api={mount:", "const api={Editor,mount:"), context);
  for (const boundary of ["closed", "newer upload", "same owner"]) {
    let release, entered, focused = 0;
    const gate = new Promise(resolve => release = resolve);
    const validating = new Promise(resolve => entered = resolve);
    const editor = Object.create(context.MotifLogo.Editor.prototype);
    Object.assign(editor, {
      target: { campaignId: "8fca7c2c-091a-5bfe-ace8-b6af9c0e801c", width: 1080, height: 1080 },
      asset: { reference: "old", width: 200, height: 80 },
      p: { x: 0.6, y: 0.6, width: 0.1 }, open: true, uploadSeq: 0,
      o: { onSelectFile: async () => ({ reference: "next", width: 80, height: 200 }) },
      paint() {}, root: { querySelector: () => ({ focus() { focused++; } }) },
      validate: async () => { entered(); await gate; },
    });
    const selecting = editor.selectFile({});
    await validating;
    if (boundary === "closed") editor.open = false;
    if (boundary === "newer upload") editor.uploadSeq++;
    release(); await selecting;
    assert.equal(focused, boundary === "same owner" ? 1 : 0, boundary);
    assert.equal(editor.asset.reference, "next");
  }
});


test("logo upload busy refusal preserves its draft and only notifies the current owner", async () => {
  for (const stale of [false, true]) {
    const h = harness(), notices = []; let finish;
    h.context.$ = () => ({content: "session-token"});
    h.context.modal = "logo";
    h.context.toast = message => notices.push(message);
    h.context.fetch = () => new Promise(resolve => {finish = resolve;});
    const before = h.run("JSON.stringify(logoSession)");
    const request = h.context.selectLogoFile({name: "logo.jpg", type: "image/jpeg"}, h.body.target);
    if (stale) h.run("logoSession={target:{campaignId:'2f1c5e33-3f82-55cf-b832-2f2d9785ce1b'}}");
    finish({ok: false, status: 409, json: async () => ({code: "busy", message: "Another campaign operation is already active."})});
    await assert.rejects(request, e => e.handled === true && e.commitState === "not_started");
    assert.deepEqual(notices, stale ? [] : ["Another campaign operation is already active."]);
    assert.equal(h.run("JSON.stringify(logoSession)"), stale ? '{"target":{"campaignId":"2f1c5e33-3f82-55cf-b832-2f2d9785ce1b"}}' : before);
  }
});
test("logo image validation still takes precedence over a contradictory busy marker", async () => {
  const h = harness(); h.context.$ = () => ({content: "session-token"});
  h.context.fetch = async () => ({ok: false, status: 409, json: async () => ({code: "busy", input_image: {kind: "decode_failure"}})});
  await assert.rejects(h.context.selectLogoFile({type: "image/jpeg", size: 12}, h.body.target), e => e.kind === "decode_failure" && !e.handled);
});


test("logo parsed guarded refusals use action notices while unreadable responses retain uncertainty", async () => {
  for (const [status, code] of [[403, "origin"], [403, "session"], [413, "action_size"], [415, "content_type"], [503, "local_storage"]]) {
    const h = harness(), notices = []; h.context.$ = () => ({content: "session-token"}); h.context.modal = "logo"; h.context.toast = text => notices.push(text);
    h.context.fetch = async () => ({ok: false, status, json: async () => ({code, message: "Sanitized local refusal"})});
    await assert.rejects(h.context.selectLogoFile({type: "image/jpeg"}, h.body.target), e => e.handled === true && e.commitState === "not_started");
    assert.deepEqual(notices, ["Sanitized local refusal"]);
  }
  for (const failAt of ["fetch", "json"]) {
    const h = harness(); h.context.$ = () => ({content: "session-token"}); const failure = new TypeError("Controlled missing response");
    h.context.fetch = async () => {if (failAt === "fetch") throw failure; return {ok: false, status: 503, json: async () => {throw failure;}};};
    await assert.rejects(h.context.selectLogoFile({type: "image/jpeg"}, h.body.target), e => e === failure && !e.handled);
  }
});

test('persistent Logo distinguishes successful Apply, Cancel, and a caller-owned switch', async () => {
  for (const scenario of ['apply','cancel','switch','retired','substantive-change']) {
    const modes=[];let callbacks;
    const context=vm.createContext({
      page:'review',modal:'',lastFocus:null,C:{id:'campaign',products:[{id:'product'}]},
      model:{value:{audience:'Original'},version:'before',manifest:{revision_id:'revision'}},lang:'en',MARKET:{en:'en-US'},FMT:[{ratio:'1:1'}],
      studioWorkspace:{root:{classList:{add() {}}}},document:{activeElement:{}},
      canEditLogo:()=>true,outputFor:()=>({url:'/image'}),$:()=>({inert:false}),
      MotifLogo:{mount(options){callbacks=options;return{}}},mountProjectViews() {},
      refreshCatalog() {},refreshStudioTargets() {},mountStudioTool:async mode=>modes.push(mode),
    });
    const run=code=>vm.runInContext(code,context);
    run(readFileSync('src/campaign_studio/static/motif/logo.js','utf8'));
    let release;context.holdContext=()=>new Promise(resolve=>{release=resolve});
    run("logoTarget=()=>({campaignId:'campaign',revisionId:'revision',draftVersion:'before'});logoContext=holdContext");
    const opening=run('openLogo(0,0)');await flush();
    assert.equal(callbacks.loading,true,'real Logo panel mounts before source context resolves');
    assert.equal(callbacks.savedDisplay.compositeUrl,'/image');
    context.model.version='acknowledged';
    if(scenario==='substantive-change')context.model.value.audience='Changed';
    release({});await opening;
    assert.equal(run('logoSession.target.draftVersion'),scenario==='substantive-change'?'before':'acknowledged');
    if(scenario!=='cancel')callbacks.onSaved();
    if(scenario==='switch')context.studioWorkspace.continuing=true;
    if(scenario==='retired')context.studioWorkspace={root:{}};
    if(!['apply','substantive-change'].includes(scenario))callbacks.onClose({cancelled:scenario==='cancel'});await flush();
    assert.deepEqual(modes,scenario==='cancel'?['inspect']:[]);
  }
});


test('successful logo Apply retains the editor and new asset while retiring preview authorization',async()=>{
 const context=vm.createContext({});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8').replace('const api={mount:', 'const api={Editor,mount:'),context);
 const editor=Object.create(context.MotifLogo.Editor.prototype);let resolve,closed=0;
 Object.assign(editor,{open:true,busy:false,pendingAsset:true,p:{x:.2,y:.3,width:.2},initial:{x:.1,y:.1,width:.1},
 target:{revisionId:'before'},asset:{reference:'new.png',width:400,height:90},valid:{signature:'current',digest:'old',resolvedRect:{left:216,top:324,width:216,height:49}},
 signature:()=> 'current',closeSub(){},paint(){},close(){closed++},o:{onSave:()=>new Promise(done=>resolve=done)}});
 const task=editor.save();assert.equal(editor.busy,true);assert.equal(editor.pendingAsset,true);
 resolve({target:{revisionId:'after'},imageUrl:'/saved.png'});await task;
 assert.equal(closed,0);assert.equal(editor.open,true);assert.equal(editor.busy,false);assert.equal(editor.dirty(),false);
 assert.equal(editor.target.revisionId,'after');assert.equal(editor.savedAsset.reference,'new.png');assert.equal(editor.valid.compositeUrl,'/saved.png');assert.equal(editor.valid.digest,null);assert.equal(editor.valid.signature,null);
 editor.p.x+=.01;assert.equal(editor.dirty(),true);await editor.save();assert.equal(editor.busy,false,'retired digest cannot authorize another Apply');
});

test('unified Logo subdialogs remain interactive and restore prior sibling ownership on close and teardown',()=>{
 const node=()=>({inert:false,attributes:{},setAttribute(k,v){this.attributes[k]=v},getAttribute(k){return this.attributes[k]??null},removeAttribute(k){delete this.attributes[k]},querySelector(){return{focus(){}}},remove(){this.removed=true}});
 const context=vm.createContext({document:{createElement:node},queueMicrotask:fn=>fn(),clearTimeout,cancelAnimationFrame(){}});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8').replace('const api={mount:', 'const api={Editor,mount:'),context);
 for(const end of ['closeSub','close']){
  const a=node(),b=node();b.inert=true;b.setAttribute('aria-hidden','false');const root=node();root.children=[a,b];root.append=el=>{root.overlay=el};root.removeEventListener=()=>{};
  const editor=Object.create(context.MotifLogo.Editor.prototype);Object.assign(editor,{root,o:{onMount(){},onUnmount(){}},target:{campaignName:'Campaign'},open:true,seq:0,p:{},initial:{}});
  editor.showSub('Confirm','Body','Buttons');assert.equal(root.inert,false);assert.equal(root.overlay.inert,false);assert.equal(a.inert,true);assert.equal(b.inert,true);
  editor[end](false);assert.equal(root.overlay.removed,true);assert.equal(a.inert,false);assert.equal(a.getAttribute('aria-hidden'),null);assert.equal(b.inert,true);assert.equal(b.getAttribute('aria-hidden'),'false');
 }
});


test("logo preview from before its own acknowledgement cannot raise a stale notice", async()=>{
  const context=vm.createContext({clearTimeout});
  const source=readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8');
  vm.runInContext(source.replace('const api={mount:', 'const api={Editor,mount:'),context);
  let resolve;
  const editor=Object.create(context.MotifLogo.Editor.prototype);
  Object.assign(editor,{open:true,seq:0,target:{},asset:{reference:'logo'},p:{x:.1},
    signature:()=> 'same',paint(){},o:{onPreview:()=>new Promise(done=>{resolve=done;}),onConflict(){assert.fail('obsolete preview must be ignored');}}});
  const pending=editor.validate();
  editor.seq++; // Autosave acceptance retires the prior preview's sequence.
  resolve({reviewRequired:true,code:'conflict'}); await pending;
  assert.equal(editor.reviewRequired,undefined);
  assert.equal(editor.valid,null);
});

test('Logo opens a source-backed output with blocked text layout, but never admits an unavailable source', () => {
 const context=vm.createContext({FMT:[{ratio:'1:1',name:'Square',w:1080,h:1080}],LANG:{en:'English'}});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo.js','utf8'),context);
 const data={value:{campaign_id:'campaign'},output_brief:{name:'Campaign',products:[{id:'product',name:'Lantern'}]},manifest:{revision_id:'version',outputs:[{id:'product/en-US/1x1',product_id:'product',market:'en-US',ratio:'1:1',url:null}]},logo_editable_products:{product:true}};
 assert.equal(context.logoTarget(data,'product','en-US','1:1').revisionId,'version');
 data.logo_editable_products.product=false;
 assert.throws(()=>context.logoTarget(data,'product','en-US','1:1'),/not available/);
});

test('opening Logo reaches its verified source context when the composite preview is blocked', async () => {
 const context=vm.createContext({
  C:{id:'campaign',products:[{id:'product'}]},lang:'en',MARKET:{en:'en-US'},FMT:[{ratio:'1:1',name:'Square',w:1080,h:1080}],LANG:{en:'English'},
  canEditLogo:()=>true,outputFor:()=>({url:null}),
  document:{activeElement:{}},lastFocus:null,modal:"",studioWorkspace:{root:{}},MotifLogo:{mount:()=>({})},mountProjectViews(){},
  model:{value:{campaign_id:'campaign'},output_brief:{name:'Campaign',products:[{id:'product',name:'Lantern'}]},manifest:{revision_id:'version',outputs:[{product_id:'product',market:'en-US',ratio:'1:1',url:null}]},logo_editable_products:{product:true}},
 });
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo.js','utf8'),context);
 let called=0;
 context.sourceContext=async(data,target)=>{called++;assert.equal(target.productId,'product');return{};};
 vm.runInContext('logoContext=sourceContext',context);
 await context.openLogo(0,0,{isCurrent:()=>false});
 assert.equal(called,1,'source-backed context is reached without requiring a completed composite');
 context.canEditLogo=()=>false;
 await assert.rejects(context.openLogo(0,0),/available current image/);
 assert.equal(called,1,'an unavailable source never reaches the context request');
});


test('a missing campaign logo opens an actionable attachment prompt without a preview request',async()=>{
 let options,ready=0;
 const context=vm.createContext({
  C:{id:'campaign',products:[{id:'product'}],logo:null},lang:'en',MARKET:{en:'en-US'},FMT:[{ratio:'1:1',name:'Square',w:1080,h:1080}],LANG:{en:'English'},
  canEditLogo:()=>true,outputFor:()=>({url:'/current-image'}),
  document:{activeElement:{}},lastFocus:null,modal:'',studioWorkspace:{root:{}},
  MotifLogo:{mount:value=>{options=value;return{}}},mountProjectViews(){},studioPreviewReady(){ready++;},
  model:{value:{campaign_id:'campaign'},version:'version',output_brief:{name:'Campaign',brand:{logo_path:''},products:[{id:'product',name:'Lantern'}]},manifest:{revision_id:'version',outputs:[{product_id:'product',market:'en-US',ratio:'1:1',url:'/current-image'}]},logo_editable_products:{product:true}},
  api:()=>assert.fail('An absent logo needs no positioning preview'),decodedImage:()=>assert.fail('An absent logo needs no decoded layers'),
 });
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo.js','utf8'),context);
 await context.openLogo(0,0);
 assert.equal(options.loading,false);assert.equal(options.savedDisplay.compositeUrl,'/current-image');
 assert.equal(typeof options.onSelectFile,'function');assert.equal(ready,1);
});


test('first logo preview installs drag layers, while obsolete responses cannot replace them', async()=>{
 const context=vm.createContext({});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8').replace('const api={mount:','const api={Editor,mount:'),context);
 const nodes={'.mf-logo-photo':{},'.mf-logo-copy':{}};
 let resolve;
 const editor=Object.create(context.MotifLogo.Editor.prototype);
 Object.assign(editor,{open:true,seq:0,target:{},asset:{reference:'logo'},p:{x:.1},signature:()=> 'same',paint(){},root:{querySelector:s=>nodes[s]},o:{onPreview:()=>new Promise(done=>resolve=done)}});
 const first=editor.validate();resolve({previewUrl:'/base',copyOverlayUrl:'/copy'});await first;
 assert.equal(nodes['.mf-logo-photo'].src,'/base');assert.equal(nodes['.mf-logo-copy'].src,'/copy');
 const obsolete=editor.validate();editor.seq++;resolve({previewUrl:'/old-base',copyOverlayUrl:'/old-copy'});await obsolete;
 assert.equal(nodes['.mf-logo-photo'].src,'/base');assert.equal(editor.o.copyOverlayUrl,'/copy');
});


test('failed first logo preview can retry without reopening or losing placement', async()=>{
 const context=vm.createContext({});
 vm.runInContext(readFileSync('src/campaign_studio/static/motif/logo-controls.js','utf8').replace('const api={mount:','const api={Editor,mount:'),context);
 const nodes={'.mf-logo-photo':{},'.mf-logo-copy':{}};
 let fail=true;
 const editor=Object.create(context.MotifLogo.Editor.prototype);
 Object.assign(editor,{open:true,seq:0,target:{},asset:{reference:'logo'},p:{x:.2},signature:()=> 'same',paint(){},root:{querySelector:s=>nodes[s]},o:{onPreview:async()=>{if(fail)throw Error('offline');return{previewUrl:'/base',copyOverlayUrl:'/copy'};}}});
 await editor.validate();
 assert.equal(editor.previewFailed,true);assert.equal(editor.p.x,.2);
 editor.o.onPreview=async()=>({blocked:true,message:'Service unavailable'});await editor.validate();assert.equal(editor.previewFailed,true);
 editor.o.onPreview=async()=>({previewUrl:'/base',copyOverlayUrl:'/copy'});fail=false;await editor.validate();
 assert.equal(editor.previewFailed,false);assert.equal(editor.o.previewUrl,'/base');assert.equal(editor.o.copyOverlayUrl,'/copy');assert.equal(editor.p.x,.2);
});
