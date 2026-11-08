// Execute the shipped disclosure/recovery controller with deferred local responses.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const { readFileSync } = require("node:fs");
function harness() {
  const pending = [],
    opened = [],
    polls = [],
    el = { checked: true };
  const context = vm.createContext({
    campaignIdentity: () => ({name: "Campaign", brand: "Brand"}),
    document: {addEventListener() {}},
    draft: { campaign_id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4" },
    dirty: false,
    currentBriefValue: () => ({products:[],markets:{}}),
    FMT: [], LANG: {},
    damagedFiles: () => false,
    compactUSD: value => `$${value.toFixed(2)}`,
    page: "production",
    operation: null,
    C: {
      id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4",
      name: "Campaign",
      products: [{ id: "one", name: "One", treatment: "concept" }],
    },
    model: {
      run: { id: "run", allowance: 1 },
      plan: { digest: "digest", generation_requests: {}, generation_products: [], findings: [] },
      run_attempts: [],
    },
    esc: (v) =>
      String(v ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;"),
    dialog: (title, body, footer) => `${title}\n${body}\n${footer}`,
    btn: (label, action) => `[${action}]${label}`,
    notice: (title, body) => `${title} ${body}`,
    api: (url, body) =>
      new Promise((resolve, reject) =>
        pending.push({ url, body, resolve, reject }),
      ),
    $: () => el,
    modal: "",
    keyAvailable: true,
    openKeySetup: async rejected => opened.push(rejected ? "rejected-key" : "missing-key"),
    withBriefPreviewPaused: (action, current = () => true) => current() ? action() : undefined,
    openModal: (name) => {
      context.modal = name;
      opened.push(name);
    },
    closeModal: () => {
      context.modal = "";
    },
    render() {},
    refreshProductionStatus() {},
    updateURL() {},
    scrollTo() {},
    isCurrentRun: (id, run) =>
      id === context.draft.campaign_id && run === context.model.run.id,
    useData: (value) => {
      context.model = value;
      context.draft = value.value;
    },
    poll: async (...args) => polls.push(args),
  });
  context.window = context;
  vm.runInContext(
    readFileSync(
      "src/campaign_studio/static/motif/storage-components.js",
      "utf8",
    ),
    context,
  );
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/production.js", "utf8"),
    context,
  );
  for (const file of ["wording.js", "wording-host.js"])
    vm.runInContext(readFileSync(`src/campaign_studio/static/motif/${file}`, "utf8"), context);
  return {
    context,
    pending,
    opened,
    polls,
    el,
    run: (code) => vm.runInContext(code, context),
  };
}
const retry = {
  run_id: "run",
  digest: "digest",
  generation_products: ["one"],
  previous_attempts: 1,
  allowance: 2,
  cost_estimate: { estimated_usd: 0.1 },
};

function progressHarness() {
  const h = harness();
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/presentation.js', 'utf8'), h.context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/producer-repair.js', 'utf8'), h.context);
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/producer-host.js', 'utf8'), h.context);
  Object.assign(h.context, {
    currentBriefValue: () => h.context.model.plan_brief, busy:false,
    head: () => '', badge: text => text, productLimit: 5,
    FMT: [{ratio:'1:1'},{ratio:'9:16'},{ratio:'16:9'}], LANG: {en:'English (United States)'}, assetURL:()=>null,
     damagedFiles: () => h.context.model.file_integrity?.damaged_outputs?.length || 0,
    unavailableSource: () => false,
    btn: (label, action, style, attributes) => `<button data-action="${action}" ${attributes || ''}>${label}</button>`,
    notice: (title, body, style, action, label) => `${title} ${body} ${action ? `[${action}]${label}` : ''}`,
  });
  Object.assign(h.context.model, {
    plan_brief: {products:[{id:'one',name:'One',source:{role:'concept'}}],markets:{'en-US':{}}},
    output_run_id: 'run', manifest: {outputs: [], metrics: {}},
    local_recovery: {output_ids: [], saved_result: false},
  });
  Object.assign(h.context.model.plan, {outputs: 3, generation_products: ['one'], findings:[]});
  return h;
}

test('active dispatch, terminal uncertainty, and earlier unknown attempts retain distinct copy', () => {
  const h = progressHarness();
  h.context.model.run.state = 'running';
  h.context.operation = {run_id:'run',active: true, attempts: [{product_id: 'one', state: 'dispatching'}], attempted: 1};
  let html = h.run('progressView()');
  assert.match(html, /Generating image…/);
  assert.doesNotMatch(html, /result was not confirmed|may have completed and may be charged/);
  h.context.operation.active = false;
  html = h.run('progressView()');
  assert.match(html, /Result not confirmed/);
  assert.match(html, /The image request could not be confirmed\./);
  assert.match(html, /The request result was not confirmed\. A charge may still apply\./);
  h.context.operation.active = true;
  h.context.operation.attempts.unshift({product_id: 'one', state: 'unknown'});
  html = h.run('progressView()');
  assert.match(html, /The request result was not confirmed/);
  assert.match(html, /Generating image…/);
});

test('a decoded first source remains available while the next product is generating', () => {
  const h = progressHarness();
  h.context.model.run.state='running';
  h.context.model.plan_brief.products.push({id:'two',name:'Two',source:{role:'concept'}});
  h.context.operation={run_id:'run',active:true,sources_completed:1,sources_total:2,
    attempts:[{product_id:'one',state:'decoded',source_available:true,response_recoverable:true},
      {product_id:'two',state:'dispatching',source_available:false,response_recoverable:false}]};
  assert.equal(h.run('productionContext().products[0].sourceLabel'),'Source image received');
  assert.equal(h.run('productionContext().products[0].image'),null);
  const html = h.run('progressView()');
  assert.match(html, /Source image received/);
  assert.doesNotMatch(html, /sources\/one\/1x1/);
  h.context.model.source_urls={one:'/verified-source'};
  assert.equal(h.run('productionContext().products[0].image'),'/verified-source');
  assert.equal(h.run('productionContext().products[1].sourceLabel'),'Generating image…');
  assert.match(h.run('progressView()'), /1 <small[^>]*>\/ 2 source images/);
});

test('each source placeholder retains its own waiting, unknown, or failed status', () => {
  const h = progressHarness();
  h.context.model.run.state='partial';
  h.context.model.plan_brief.products=[
    {id:'received',name:'Received',source:{role:'concept'}},
    {id:'unknown',name:'Unknown',source:{role:'concept'}},
    {id:'failed',name:'Failed',source:{role:'concept'}},
    {id:'unstarted',name:'Unstarted',source:{role:'concept'}},
  ];
  h.context.operation={run_id:'run',active:false,sources_completed:1,sources_total:4,
    attempts:[{product_id:'received',state:'decoded',source_available:true},
      {product_id:'unknown',state:'unknown',source_available:false},
      {product_id:'failed',state:'failed',source_available:false}]};
  const html=h.run('progressView()');
  const cards=html.match(/<article class="pr-product-card">[\s\S]*?<\/article>/g);
  assert.equal(cards.length,4);
  for (const [index,label] of ['Source image received','Result not confirmed','Image not created','Image will be generated.'].entries()) {
    assert.ok(cards[index].includes(label));
    assert.doesNotMatch(cards[index], /<img /);
  }
});

test('zero-request source preparation has progress without allowance or charge filler', () => {
  const h = progressHarness();
  h.context.model.run.allowance = 0;
  h.context.operation = {run_id:'run',active: true, attempted: 0, stage: 'Preparing source images'};
  const html = h.run('progressView()');
  assert.match(html, /No image requests in this run\./);
  assert.match(html, /Preparing source images/);
  assert.doesNotMatch(html, /0 of 0|0 requests attempted|0 remaining allowance|This count is not a confirmed charge|reviewed plan and available allowance|may still incur charges/);
});

test('retry progress uses one received attempted and remaining pair, including zero and fallback', () => {
  const h = progressHarness();
  h.context.model.run.allowance = 2;
  h.context.operation = {run_id:'run',active: true, attempted: 2, remaining: 1,
    attempts: [{state:'unknown'}, {state:'dispatching'}]};
  let html = h.run('progressView()');
  assert.match(html, /This run: 2 of 3 requests used · 1 remaining/);
  assert.match(html, /The request result was not confirmed\. A charge may still apply/);
  h.context.operation = {run_id:'run',active: false, attempted: 3, remaining: 0,
    attempts: [{state:'unknown'}, {state:'decoded'}, {state:'decoded'}]};
  html = h.run('progressView()');
  assert.match(html, /This run: 3 of 3 requests used · 0 remaining/);
  h.context.operation = null;
  assert.match(h.run('progressView()'), /This run: 0 of 2 requests used · 2 remaining/);
  h.context.operation = {run_id:'run',active: true, attempted: 0, remaining: 0};
  assert.match(h.run('progressView()'), /No image requests in this run/);
  assert.equal(h.context.model.run.allowance, 2, 'display does not change request admission');
});

test('active and settled progress counts source images separately from format coverage', () => {
  for (const active of [true,false]) {
    const h = progressHarness();
    h.context.model.manifest.outputs = Array.from({length:30},(_,i)=>({id:String(i),url:'/preview.png'}));
    h.context.operation = {run_id:'run',active,completed:30,total:30,sources_completed:2,sources_total:5};
    const html = h.run('progressView()');
    assert.match(html, /pr-operation-count[^>]*>2 <small[^>]*>\/ 5 source images/);
    assert.doesNotMatch(html, /30 images|Creating campaign images|Rebuild the missing format/);
  }
  const h = progressHarness();
  h.context.model.source_urls = {one:'/source.png',obsolete:'/unrelated.png'};
  h.context.operation = {run_id:'old-run',sources_completed:5,sources_total:5};
  assert.match(h.run('progressView()'), /pr-operation-count[^>]*>1 <small[^>]*>\/ 1 source images/);
});

test('recovery refers to retained source imagery, never a final format', () => {
  const h = progressHarness();
  h.context.model.run_attempts = [{product_id:'one',state:'decoded',source_available:true}];
  h.context.model.source_urls = {one:'/source.png'};
  h.context.model.manifest.outputs = [{id:'one/en-US/1x1',product_id:'one',url:'/preview.png'}];
  assert.doesNotMatch(h.run('progressView()'), /data-action="recover"/);
  h.context.model.source_urls = {};
  h.context.model.local_recovery = {output_ids:['one/en-US/1x1','one/en-US/9x16','one/en-US/16x9'],saved_result:true};
  const html = h.run('progressView()');
  assert.match(html, /A provider result is already saved/);
  assert.match(html, /Recover saved source images · 0 calls/);
  assert.doesNotMatch(html, /Recover missing image|Rebuild the missing format|data-action="recover" disabled/);
  h.context.model.run.state = 'complete';
  h.context.model.composition_version = 'v1';
  h.context.model.manifest.complete = false;
  assert.match(h.run('production()'), /0 <small>\/ 1 source images/);
  assert.doesNotMatch(h.run('production()'), /images saved|Saved images need recovery/);
  h.context.model.output_run_id = 'previous-run';
  assert.doesNotMatch(h.run('production()'), /data-action="recover"/);
});

test('partial handoff explains that export creates final formats from available sources', () => {
  const h = progressHarness();
  Object.assign(h.context, {
    campaignMetricDisplay:()=>({}), handoffLogoNotice:()=>'', descriptionCount:()=>0,
    total:()=>2, overviewView:()=>'', exportBlocked:()=>false,
  });
  h.context.C.markets=['en-US'];
  h.context.model.manifest={complete:false,metrics:{completed_images:1},outputs:[
    {id:'available',product_id:'one',url:'/preview.png'},
    {id:'missing',product_id:'two',url:null},
  ]};
  const html = h.run('handoff()');
  assert.match(html, /Export creates finished images from the available sources and current saved settings/);
  assert.doesNotMatch(html, /Nothing is generated by export|Export does not create imagery/);
});

test('rule status intersects current unavailable outputs and separates other omissions without double counts', () => {
  const h = progressHarness();
  h.context.model.manifest = {complete:false,metrics:{completed_images:1},outputs:[
    {id:'withheld',url:null}, {id:'other',url:null}, {id:'available',url:'/image.png'}]};
  h.context.model.rule_withheld_outputs = ['withheld','withheld','available','old-result'];
  assert.equal(h.run('JSON.stringify(currentRuleWithholding())'), '["withheld"]');
  assert.equal(h.run('otherUnavailableOutputs().length'), 1);
  assert.equal(h.run('partialExportOmissions()'), 'This saved version has 1 image withheld by current rules and 1 other unavailable image.');
  h.context.model.manifest.outputs.splice(1,1);
  assert.equal(h.run('partialExportOmissions()'), 'This saved version has 1 image withheld by current rules.');
  h.context.model.manifest.outputs[0].url = '/restored.png';
  h.context.model.manifest.complete = true;
  assert.equal(h.run('wordingExportBlocked()'), false);
});

test('rules-only and mixed progress retain independent causes and only actionable recovery', () => {
  const h = progressHarness();
  h.context.head = title => title;
  h.context.model.run.state = 'partial';
  h.context.model.manifest = {complete:false,metrics:{completed_images:0},outputs:[{id:'blocked',url:null}]};
  h.context.model.rule_withheld_outputs = ['blocked'];
  h.context.model.wording_issue = {code:'prohibited_phrase',matches:[{rule:'rule-1',context:{product_id:'one',product_name:'One',market:'en-US',phrase:{configured_phrase:'Always',market:'en-US',matches:[{field:'message',value:'Always here',origin:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'}]}}}]};
  h.context.operation = {run_id:'run',active:false,completed:1,total:3};
  let html = h.run('progressView()');
  assert.match(html, /pr-operation-count[^>]*>0 <small/, 'the source count is independent of format coverage');
  assert.match(html, /Images are withheld by current rules\./);
  assert.match(html, /withheld/);
  assert.match(html, /Edit headline in Studio/);
  assert.doesNotMatch(html, /data-action="recover"/);
  h.context.model.manifest.outputs.push({id:'failed',url:null});
  h.context.model.local_recovery.output_ids = ['failed'];
  assert.equal(h.run('canRecoverCurrentOutputs()'), true);
  h.context.model.run = {...h.context.model.run,id:'new-unstarted-plan',state:'planned'};
  assert.equal(h.run('canRecoverCurrentOutputs()'), false, 'old pixels are not a latest-run recovery command');
});

test('withholding cannot conceal active, unknown, stopped, source, or integrity problems', () => {
  for (const kind of ['active','unknown','stopped','source','damage','provider']) {
    const h = progressHarness(); h.context.head = title => title;
    h.context.model.run.state = kind === 'stopped' ? 'stopped' : 'partial';
    h.context.model.manifest = {complete:false,metrics:{completed_images:0},outputs:[{id:'blocked',url:null}]};
    h.context.model.rule_withheld_outputs = ['blocked'];
  h.context.model.wording_issue = {code:'prohibited_phrase',matches:[{rule:'rule-1',context:{product_id:'one',product_name:'One',market:'en-US',phrase:{configured_phrase:'Always',market:'en-US',matches:[{field:'message',value:'Always here',origin:'292e9e84-eb00-5fdb-87e8-423f5e51fac4'}]}}}]};
    h.context.model.local_recovery.output_ids = ['blocked'];
    if (kind === 'active') h.context.operation = {run_id:'run',active:true,attempted:0,remaining:0};
    if (kind === 'unknown') h.context.model.run_attempts = [{state:'unknown'}];
    if (kind === 'source') { h.context.operation = {notice_code:'source_unavailable',notice:'Exact unavailable source'}; h.context.unavailableSource = () => true; }
    if (kind === 'damage') h.context.model.file_integrity = {damaged_outputs:['blocked']};
    if (kind === 'provider') h.context.model.notice = {code:'rejected_key',message:'Rejected key'};
    assert.equal(h.run('rulesOnlyResult()'), false, kind);
    const html = h.run('progressView()');
    assert.match(html, /Edit headline in Studio/, kind);
    if (kind === 'active' || kind === 'source') assert.equal(h.run('canRecoverCurrentOutputs()'), false, kind);
    if (kind === 'unknown') assert.match(html, /The image request could not be confirmed/);
    if (kind === 'provider') assert.match(html, /OpenAI did not accept this key.*Update API key/s);
    if (kind === 'source') assert.match(html, /Exact unavailable source/);
  }
});

test('rejected and denied credentials keep the existing update action without a dispatch', async () => {
  const h = progressHarness();
  for (const code of ['rejected_key', 'access_denied']) {
    h.context.model.notice = {code, message: code === 'access_denied' ? 'The account lacks image access.' : 'The key was rejected.'};
    const html = h.run('progressView()');
    assert.match(html, /data-pr-action="update-key"[^>]*>Update API key/);
    assert.doesNotMatch(html, /Open setup steps/);
    assert.equal(h.run('showProductionIssue(model.notice.code, model.notice.message)'), true);
  }
  assert.deepEqual(h.opened, ['rejected-key', 'rejected-key']);
  assert.equal(h.pending.length, 0);
});
test("retry review refreshes facts; deliberate Generate submits the exact cumulative allowance without a checkbox", async () => {
  const h = harness();
  const reviewing = h.run("reviewRetryPlan()");
  assert.equal(h.pending[0].body, undefined);
  h.pending.shift().resolve({
    value: { campaign_id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4" },
    run: { id: "run", allowance: 1 },
    plan: { digest: "digest", generation_requests: {}, generation_products: [], findings: [] },
    retry_plan: retry,
  });
  await reviewing;
  assert.equal(h.pending.length, 0);
  assert.equal(h.context.page, "production");
  h.context.model.unplanned_draft = true;
  await assert.rejects(h.run("startDisplayedPlan()"), /plan is not ready/);
  assert.equal(h.pending.length, 0);
  h.context.model.unplanned_draft = false;
  h.el.checked = false; // No checkbox gates the explicit Generate action.
  const start = h.run("startDisplayedPlan()");
  assert.deepEqual(JSON.parse(JSON.stringify(h.pending[0].body)), {
    run_id: "run",
    digest: "digest",
    allowance: 2,
    retry: true,
  });
  h.pending.shift().resolve({ disposition: "started" });
  await start;
  assert.equal(h.polls.length, 1);
});
test("newer unsaved typing survives retry-plan refresh and blocks starting that proposal", async () => {
  const h = harness();
  h.context.dirty = true;
  h.context.draft.message = "Typed";
  const reviewed = h.run("reviewRetryPlan()");
  h.pending.shift().resolve({
    value: { campaign_id: "292e9e84-eb00-5fdb-87e8-423f5e51fac4" },
    run: { id: "run" },
    plan: { digest: "digest", generation_requests: {}, generation_products: [], findings: [] },
    retry_plan: retry,
  });
  await assert.rejects(reviewed, /draft is preserved/);
  assert.equal(h.context.draft.message, "Typed");
  assert.equal(h.run("activeRetry()"), null);
});
test("authoritative duplicate Start opens recovery without another call or automatic poll", async () => {
  const h = harness();
  const start = h.run("startDisplayedPlan()");
  h.pending
    .shift()
    .resolve({ disposition: "already_active", active: true, run_id: "run" });
  await start;
  assert.deepEqual(h.opened, ["duplicate-start"]);
  assert.equal(h.pending.length, 0);
  assert.equal(h.polls.length, 0);
  assert.equal(h.context.operation.run_id, "run");
});
test("a recorded failure is presented once, and a newer failure remains independently visible", () => {
  const h = harness();
  h.context.model.run_attempts = [
    { created_at: "one", error_kind: "APIConnectionError", state: "unknown" },
  ];
  h.context.operation = {
    notice_code: "unknown_request",
    notice: "Remote outcome unknown",
  };
  h.run("showNewProductionFailure();showNewProductionFailure()");
  assert.deepEqual(h.opened, ["network-unavailable"]);
  h.context.model.run_attempts.push({
    created_at: "two",
    error_kind: "http_401",
    state: "failed",
  });
  h.context.operation = { notice_code: "rejected_key", notice: "Rejected" };
  h.run("showNewProductionFailure()");
  assert.equal(h.opened.at(-1), "rejected-key");
});
test("a different busy operation is not presented as this run already active", () => {
  const h = harness();
  assert.equal(
    h.run("showProductionIssue('busy', 'Another operation is active')"),
    false,
  );
  assert.equal(h.opened.length, 0);
});
test("billing and temporary rate refusals retain the server remedy in existing recovery", () => {
  for (const code of ["billing_limit", "rate_limited"]) {
    const h = harness();
    h.context.failureCode = code;
    h.context.failureMessage = code === "billing_limit"
      ? "Review OpenAI billing. Another app allowance does not resolve this block."
      : "Wait before deliberately reviewing another attempt.";
    assert.equal(h.run("showProductionIssue(failureCode, failureMessage)"), true);
    assert.deepEqual(h.opened, ["provider-unavailable"]);
    const html = h.run("productionRecoveryModal()");
    assert.ok(html.includes(h.context.failureMessage));
    assert.ok(html.includes("Review saved work"));
    assert.equal(h.pending.length, 0);
  }
});
test("production record remains usable before the first image revision exists", () => {
  const h = harness();
  h.context.model.manifest = null;
  h.context.model.plan.outputs = 6;
  h.context.model.run_attempts = [{ state: "received" }];
  h.context.campaignMetricDisplay = () => ({ attempts: "1", spend: "Unknown" });
  h.context.total = () => 6;
  h.context.treatment = () => "Concept";
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"),
    h.context,
  );
  const html = h.run("activityView()");
  assert.match(html, /0 \/ 6/);
  assert.match(html, />1<\/div>\s*<div>Campaign generation attempts/);
  assert.match(html, /Not recorded/);
  assert.match(html, /Unknown/);
  assert.match(html, /Attempt & source details/);
  assert.match(html, /\[attempts\]/);
});
test("retry failure remedy follows the recorded cause, not generic credential setup", () => {
  const h = progressHarness();
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/presentation.js", "utf8"),
    h.context,
  );
  Object.assign(h.context, {
    currentBriefValue: () => h.context.model.plan_brief, busy:false,
    head: () => "",
    total: () => 6,
    art: () => "",
    treatment: () => "Concept",
    unavailableSource: () => false,
    planFindings: () => "",
    notice: (title, body, style, action, label) => `[${action}]${label}`,
    keyAvailable: true,
  });
  h.context.C.markets = ["en"];
  Object.assign(h.context.model.plan, {
    generation_products: ["one"],
    findings: [],
  });
  h.run(`retryContext=${JSON.stringify({ campaign: "292e9e84-eb00-5fdb-87e8-423f5e51fac4", ...retry })}`);
  h.context.model.notice = {
    code: "unknown_request",
    message: "Outcome unknown",
  };
  let html = h.run("production()");
  assert.match(html, /data-action="attempts"[^>]*>Attempt details/);
  assert.ok(!html.includes("Open setup steps"));
  h.context.model.notice.code = "rejected_key";
  html = h.run("production()");
  assert.match(html, /data-pr-action="update-key"[^>]*>Update API key/);
});
test("exact settings use the displayed current output plan and escape prompt text without changing line breaks", () => {
  const h = harness();
  h.context.page = "review";
  h.context.model.output_plan = {
    generation_requests: {
      one: {
        model: "pinned",
        size: "1920x1920",
        quality: "high",
        background: "opaque",
        output_format: "png",
        n: 1,
        max_retries: 0,
        deadline_seconds: 120,
        prompt: "Exact <script>\nSecond line",
      },
    },
  };
  h.context.model.plan.generation_requests = {
    wrong: { prompt: "CURRENT_CANARY" },
  };
  const html = h.run("settingsModal()");
  assert.match(html, /Exact &lt;script&gt;\nSecond line/);
  assert.ok(!html.includes("CURRENT_CANARY"));
  assert.ok(!html.includes("<script>"));
});
test("blocked proposal cannot survive a different saved brief version or campaign", () => {
  const h = harness();
  h.context.model.version = "v1";
  h.run("blockedProposal={campaign_id:'292e9e84-eb00-5fdb-87e8-423f5e51fac4',version:'v1'}");
  assert.ok(h.run("activeBlockedProposal()"));
  h.context.model.version = "v2";
  assert.equal(h.run("activeBlockedProposal()"), null);
  h.context.model.version = "v1";
  h.context.draft.campaign_id = "62220994-1d32-5713-80d2-ac0b6ff0baf3";
  assert.equal(h.run("activeBlockedProposal()"), null);
});
test("local readiness response cannot replace a dialog opened while its check was pending", async () => {
  const h = harness();
  const check = h.run("checkReadiness()");
  h.context.modal = "edit";
  h.pending.shift().resolve({ ready: false, key_available: false, checks: [] });
  await check;
  assert.equal(h.context.modal, "edit");
  assert.deepEqual(h.opened, ["setup"]);
});

test("Local workspace shows escaped location and decimal storage policy without secrets", () => {
  const h = harness();
  h.run(
    `readiness={storage:{location:'/workspace/<example>',used_bytes:1250000000,file_count:12,free_bytes:23000000000,budget_bytes:10000000000,reserve_bytes:1000000000,over_budget:true,location_notice:'Explicit workspace'}}`,
  );
  const rendered = h.run("storageDetails()");
  assert.match(rendered, /1\.25 GB/);
  assert.match(rendered, /&lt;example&gt;/);
  assert.match(rendered, /1.25 GB of 10 GB/);
  assert.match(rendered, /Free-disk reserve/);
  assert.doesNotMatch(rendered, /class="notice block"/);
  assert.doesNotMatch(rendered, /GiB|MiB|OPENAI_API_KEY/);
});

test("missing-key Start opens setup without consent, confirmation, or automatic continuation", async () => {
  const h = harness();
  h.context.model.plan.generation_requests = { one: { prompt: "Already planned" } };
  h.context.keyAvailable = false;
  h.el.checked = false;
  await h.run("startDisplayedPlan()");
  assert.deepEqual(h.opened, ["missing-key"]);
  assert.equal(h.pending.length, 0);
  assert.equal(h.polls.length, 0);
});


test("saving a replacement key dismisses only the older authentication failure", () => {
  const h = harness();
  h.context.keySavedAt = Date.parse("2026-11-08T08:00:01Z") / 1000;
  h.context.model.notice = { code: "rejected_key", message: "Old key rejected" };
  h.context.model.run_attempts = [{state: "failed", created_at: "2026-11-08T08:00:00Z"}];
  assert.equal(h.run("productionCredentialState().keyUpdated"), true);
  assert.equal(h.run("productionCredentialState().credentialIssue"), null);
  h.run("showNewProductionFailure()");
  assert.deepEqual(h.opened, []);
  h.context.model.run_attempts.push({state: "failed", created_at: "2026-11-08T08:00:02Z"});
  assert.equal(h.run("productionCredentialState().credentialIssue"), "rejected_key");
  h.run("showNewProductionFailure()");
  assert.deepEqual(h.opened, ["rejected-key"]);
  h.context.keyAvailable = false;
  assert.equal(h.run("productionCredentialState().keyUpdated"), false);
});

test('active source generation keeps only progress and meaningful stop above collapsed details', () => {
  const h=progressHarness();
  h.context.model.run.state='running';
  h.context.operation={run_id:'run',active:true,sources_completed:0,sources_total:1,attempted:1,remaining:0,
    stage:'Generating One',stoppable:false,attempts:[{product_id:'one',state:'dispatching'}]};
  let panel=h.run('progressView()').match(/<aside class="pr-action-panel">[\s\S]*?<\/aside>/)[0];
  assert.match(panel,/Generating One/);assert.match(panel,/0 <small>\/ 1 source images/);
  assert.doesNotMatch(panel,/partialReview|retryPlan|data-action="recover"|Hand off|Review available/);
  assert.doesNotMatch(panel,/data-action="stop"/);
  assert.match(panel,/<details class="pr-request-details"><summary>/);
  h.context.operation.stoppable=true;
  panel=h.run('progressView()').match(/<aside class="pr-action-panel">[\s\S]*?<\/aside>/)[0];
  assert.match(panel,/data-action="stop"/);
  assert.ok(panel.indexOf('data-action="stop"') < panel.indexOf('<details'));
});

test("restarted environment key allows deliberate resume without claiming a saved or verified key", () => {
  const h = harness();
  h.context.keySavedAt = null;
  h.context.keyLoadedAt = Date.parse("2026-11-08T08:00:01Z") / 1000;
  h.context.model.notice = { code: "rejected_key", message: "Previous attempt rejected" };
  h.context.model.run_attempts = [{state:"failed",created_at:"2026-11-08T08:00:00Z"}];
  assert.equal(h.run("productionCredentialState().keyReloaded"), true);
  assert.equal(h.run("productionCredentialState().keyUpdated"), false);
  assert.equal(h.run("productionCredentialState().credentialIssue"), null);
  h.run("showNewProductionFailure()");
  assert.deepEqual(h.opened, []);
  assert.equal(h.pending.length, 0);
  h.context.model.run_attempts.push({state:"failed",created_at:"2026-11-08T08:00:02Z"});
  assert.equal(h.run("productionCredentialState().keyReloaded"), false);
  assert.equal(h.run("productionCredentialState().credentialIssue"), "rejected_key");
  h.context.keyAvailable = false;
  assert.equal(h.run("productionCredentialState().keyReloaded"), false);
});


test('active generation shows elapsed time and motion outside collapsed request details', () => {
  const h=progressHarness();
  h.context.model.run.state='running';
  h.context.operation={run_id:'run',active:true,sources_completed:2,sources_total:3,elapsed_seconds:12.8,
    attempts:[{product_id:'one',state:'dispatching'}]};
  let html=h.run('progressView()');
  assert.ok(html.indexOf('12s elapsed') < html.indexOf('<details'));
  assert.match(html, /pr-operation-progress is-active/);
  assert.match(html, /pr-working-indicator/);
  assert.match(html, /2 <small>\/ 3 source images/);
  h.context.operation.elapsed_seconds=14.1;
  html=h.run('progressView()');
  assert.match(html, /14s elapsed/);
  assert.match(html, /2 <small>\/ 3 source images/);
  h.context.operation.active=false;
  html=h.run('progressView()');
  assert.doesNotMatch(html, /pr-operation-progress is-active|pr-working-indicator/);
});
