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

test('zero-request composition has meaningful progress without allowance or charge filler', () => {
  const h = progressHarness();
  h.context.model.run.allowance = 0;
  h.context.operation = {run_id:'run',active: true, attempted: 0, stage: 'Creating campaign sizes'};
  const html = h.run('progressView()');
  assert.match(html, /No image requests in this run\./);
  assert.match(html, /Creating campaign sizes/);
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

test('settled zero, partial, and complete counts use current files while active counts use live progress', () => {
  for (const available of [0,1,3]) {
    const h = progressHarness();
    h.context.model.manifest.outputs = Array.from({length:3},(_,index)=>({id:String(index),url:index<available?'/saved.png':null}));
    h.context.operation = {run_id:'run',active:false,completed:2,total:9};
    assert.match(h.run('progressView()'), new RegExp(`pr-operation-count[^>]*>${available} <small[^>]*>/ 3 images`));
    h.context.operation.active = true;
    assert.match(h.run('progressView()'), /pr-operation-count[^>]*>2 <small[^>]*>\/ 9 images/);
  }
});

test('only missing recoverable outputs enable recovery; completed decoded attempts do not', () => {
  const h = progressHarness();
  h.context.model.run_attempts = [{product_id: 'one', state: 'decoded', source_available: true}];
  h.context.model.manifest.outputs = [{id: 'one/en-US/1x1', product_id: 'one', url: '/saved.png'}];
  let html = h.run('progressView()');
  assert.doesNotMatch(html, /A provider result is already saved/);
  assert.match(html, /data-action="recover" disabled/);
  h.context.model.local_recovery = {output_ids: ['one/en-US/9x16'], saved_result: true};
  html = h.run('progressView()');
  assert.match(html, /A provider result is already saved/);
  assert.doesNotMatch(html, /data-action="recover" disabled/);
  assert.doesNotMatch(html, /Rebuild the missing format/);
  h.context.model.local_recovery.saved_result = false;
  html = h.run('progressView()');
  assert.match(html, /Recover missing image →/);
  assert.match(html, /Rebuild the missing format using its saved product image\. No new AI request is needed\./);
  h.context.model.local_recovery.output_ids.push('one/en-US/16x9');
  assert.doesNotMatch(h.run('progressView()'), /Recover missing image →/);
});

test('completed damaged output uses single-image recovery copy only for a verified local source', () => {
  const h = progressHarness();
  h.context.model.run.state = 'complete';
  h.context.model.manifest = {complete:true,metrics:{},outputs:[
    {id:'square',product_id:'one',market:'en-US',ratio:'1:1',url:'/square.png'},
    {id:'story',product_id:'one',market:'en-US',ratio:'9:16',url:null},
    {id:'landscape',product_id:'one',market:'en-US',ratio:'16:9',url:'/landscape.png'}]};
  h.context.model.file_integrity = {damaged_outputs:['story']};
  h.context.model.local_recovery = {output_ids:['story'],saved_result:false};
  let html = h.run('production()');
  assert.match(html, /2 of 3 saved/);
  assert.match(html, /data-action="recover"[^>]*>Recover missing image →/);
  assert.match(html, /Rebuild the missing format using its saved product image\. No new AI request is needed\./);
  assert.match(html, /Saved images need recovery/);
  h.context.model.local_recovery.output_ids.push('landscape');
  html = h.run('production()');
  assert.match(html, /Recover saved sources · 0 calls/);
  assert.doesNotMatch(html, /Recover missing image|Rebuild the missing format/);
  h.context.model.local_recovery = {output_ids:['story'],saved_result:true};
  html = h.run('production()');
  assert.match(html, /Recover saved sources · 0 calls/);
  assert.doesNotMatch(html, /Recover missing image|Rebuild the missing format/);
  h.context.model.local_recovery.saved_result = false;
  h.context.model.output_run_id = 'previous-run';
  assert.doesNotMatch(h.run('production()'), /data-action="recover"/);
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
  assert.match(h.run('savedResultNotice()'), /Saved images are ready/);
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
  assert.match(html, /pr-operation-count[^>]*>0 <small/, 'settled count uses the current accepted result');
  assert.match(html, /Images are withheld by current rules\./);
  assert.match(html, /Withheld/);
  assert.match(html, /Edit headline in Studio/);
  assert.match(html, /data-action="recover" disabled/);
  assert.doesNotMatch(h.run('savedResultNotice()'), /Recover saved sources locally|needs attention/);
  h.context.model.manifest.outputs.push({id:'failed',url:null});
  html = h.run('savedResultNotice()');
  assert.match(html, /prohibited-word list/);
  assert.match(html, /Review the available images\. No new request starts automatically/);
  h.context.model.local_recovery.output_ids = ['failed'];
  assert.equal(h.run('canRecoverCurrentOutputs()'), true);
  assert.match(h.run('savedResultNotice()'), /Recover saved sources locally/);
  h.context.model.run = {...h.context.model.run,id:'new-unstarted-plan',state:'planned'};
  assert.equal(h.run('canRecoverCurrentOutputs()'), false, 'old pixels are not a latest-run recovery command');
  assert.match(h.run('savedResultNotice()'), /prohibited-word list/);
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
    assert.match(html, /prohibited-word list/, kind);
    if (kind === 'active' || kind === 'source') assert.equal(h.run('canRecoverCurrentOutputs()'), false, kind);
    if (kind === 'unknown') assert.match(html, /Result not confirmed/);
    if (kind === 'provider') assert.match(html, /Rejected key.*Update API key/s);
    if (kind === 'source') assert.match(html, /Exact unavailable source/);
  }
});

test('rejected and denied credentials keep the existing update action without a dispatch', async () => {
  const h = progressHarness();
  for (const code of ['rejected_key', 'access_denied']) {
    h.context.model.notice = {code, message: code === 'access_denied' ? 'The account lacks image access.' : 'The key was rejected.'};
    const html = h.run('progressView()');
    assert.match(html, /\[updateKey\]Update API key/);
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
  assert.match(html, /\[attempts\]Attempt details/);
  assert.ok(!html.includes("Open setup steps"));
  h.context.model.notice.code = "rejected_key";
  html = h.run("production()");
  assert.match(html, /\[updateKey\]Update API key/);
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
