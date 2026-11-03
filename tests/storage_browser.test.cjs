// Exercise the shipped storage controller across separate commit and resume
// responses. Native controls, focus, and layout have independent browser proof.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
const flush = () => new Promise((resolve) => setImmediate(resolve));

test("deletion inventory describes actual PNGs and campaign settings without history counts", () => {
  const context = vm.createContext({ window: {} });
  vm.runInContext(
    readFileSync("src/campaign_studio/static/motif/storage-components.js", "utf8"),
    context,
  );
  const view = context.window.MotifStorage.getView("delete-confirm", {
    campaignName: "Current saved name",
    estimatedReclaimBytes: 1024,
    sourceFilesToRemove: 2,
    outputPNGs: 6,
    packages: 1,
    unmanagedFiles: 0,
  });
  assert.match(view.body, /Campaign brief and settings/);
  assert.match(view.body, /Saved images<\/span><b>6 PNGs/);
  assert.doesNotMatch(view.body, /versions|review information|undefined/);
});

function harness() {
  const requests = [],
    views = [],
    notices = [];
  const context = vm.createContext({
    draft: { campaign_id: "80324ca6-5874-5f29-9614-82eaa05dd506", name: "Chosen campaign" },
    modal: null,
    modalProduct: null,
    model: {},
    dirty: true,
    baseline: "saved",
    operation: null,
    page: "campaign",
    document: { activeElement: null },
    $: () => ({ childNodes: [], style: {}, replaceChildren() {}, focus() {} }),
    invalidatePreview() {},
    forgetProject(owner) { context.forgottenOwner = owner; },
    endFraming() {},
    updateURL() {},
    render() {},
    refreshCatalog: async () => {},
    openModal(value) {
      context.modal = value;
      views.push(value);
    },
    closeModal() {
      context.modal = null;
    },
    toast: (message) => notices.push(message),
    hasPendingNavigationWork: () => true,
    MotifStorage: { mb: (bytes) => String(bytes) },
    api: (url, body) =>
      new Promise((resolve, reject) => {
        requests.push({ url, body, resolve, reject });
      }),
  });
  const run = (code) => vm.runInContext(code, context);
  run(readFileSync("src/campaign_studio/static/motif/storage.js", "utf8"));
  run(
    "deletion = {campaign:'80324ca6-5874-5f29-9614-82eaa05dd506',name:'Chosen campaign',inventory:{token:'server-token'}}",
  );
  return { run, requests, context, views, notices };
}

test("deletion resumes only its committed identity and ignores repeated confirmation", async () => {
  const h = harness();
  const saving = h.run("confirmDeletion()");
  await h.run("confirmDeletion()");
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].body.token, "server-token");
  assert.notEqual(h.run("storageView?.state"), "delete-pending");
  h.requests[0].resolve({ state: "committed", cleanup_id: "approved-id" });
  await flush();
  assert.equal(h.run("storageView.state"), "delete-pending");
  assert.equal(
    h.requests[1].url,
    "/api/campaigns/80324ca6-5874-5f29-9614-82eaa05dd506/cleanup/approved-id/retry",
  );
  h.requests[1].resolve({ state: "complete", removed_bytes: 42 });
  await saving;
  assert.equal(h.context.draft, null);
  assert.equal(h.context.forgottenOwner, "80324ca6-5874-5f29-9614-82eaa05dd506");
  assert.match(h.notices[0], /42/);
});

test("lost confirmation response checks state before allowing an exact pending resume", async () => {
  const h = harness();
  const saving = h.run("confirmDeletion()");
  h.requests[0].reject({
    status: 503,
    code: "local_storage",
    message: "Response publication failed",
  });
  await saving;
  assert.equal(h.run("storageView.state"), "delete-uncertain");
  assert.equal(h.requests.length, 1);
  const checking = h.run("checkDeletion()");
  assert.equal(h.requests[1].body, undefined);
  h.requests[1].resolve({
    campaign_state: "pending",
    pending: [{ kind: "campaign", id: "committed" }],
  });
  await checking;
  assert.equal(h.run("storageView.state"), "delete-cleanup-pending");
  const resume = h.run("resumeDeletion()");
  assert.equal(
    h.requests[2].url,
    "/api/campaigns/80324ca6-5874-5f29-9614-82eaa05dd506/cleanup/committed/retry",
  );
  h.requests[2].resolve({ state: "pending" });
  await resume;
  assert.equal(h.context.draft.campaign_id, "80324ca6-5874-5f29-9614-82eaa05dd506");
  assert.equal(h.notices.length, 0);
});

test("precommit rejection never resumes or claims deletion and preserves current drafts", async () => {
  const h = harness();
  const saving = h.run("confirmDeletion()");
  h.requests[0].reject({
    status: 409,
    code: "cleanup_changed",
    message: "Campaign changed.",
  });
  await saving;
  assert.equal(h.run("storageView.state"), "delete-changed");
  assert.equal(h.requests.length, 1);
  assert.equal(h.context.dirty, true);
  assert.equal(h.context.draft.name, "Chosen campaign");
  assert.equal(h.notices.length, 0);
});

test("completed duplicate resume reconciles deleted status instead of looping on retry", async () => {
  const h = harness();
  h.run("deletion.id='already-completed'");
  const resume = h.run("resumeDeletion()");
  h.requests[0].reject({ status: 404, code: "not_found", message: "Campaign not found" });
  await flush();
  assert.equal(h.requests[1].url, "/api/campaigns/80324ca6-5874-5f29-9614-82eaa05dd506/cleanup/status");
  assert.equal(h.requests[1].body, undefined);
  h.requests[1].reject({status:404, code:"not_found", message:"Campaign not found"});
  await resume;
  assert.equal(h.context.draft, null);
  assert.equal(h.context.modal, null);
  assert.deepEqual(h.notices, ["Campaign deleted."]);
});

test("same-name context is stable across activity order and excludes only the current chooser ID", () => {
  const h = harness();
  h.context.window = h.context;
  h.context.addEventListener = () => {};
  h.context.document.addEventListener = () => {};
  h.context.esc = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;");
  h.run(readFileSync("src/campaign_studio/static/motif/studio.js", "utf8"));
  h.run(readFileSync("src/campaign_studio/static/motif/storage-components.js", "utf8"));
  h.context.saved = [
    { id: "c6ff7987-ac90-5520-a18a-9cddb419fbe3", name: "Field Notes", brand: "HOLD", products: 1, markets: 2, updated: "2026-10-30T12:00:00Z" },
    { id: "8fca7c2c-091a-5bfe-ace8-b6af9c0e801c", name: "Field Notes", brand: "HOLD", products: 1, markets: 2, updated: "2026-10-30T12:00:00Z" },
  ];
  h.context.draft = { campaign_id: "c6ff7987-ac90-5520-a18a-9cddb419fbe3", name: "Field Notes", brand: { name: "HOLD" }, products: [{}], markets: { "en-US": {}, "es-MX": {} } };
  const original = JSON.stringify(h.context.saved);
  assert.equal(h.run("campaignDistinction(saved[0])"), "Same name · 2 of 2");
  assert.equal(h.run("campaignDistinction(saved[1])"), "Same name · 1 of 2");
  h.run("showCampaignChooser()");
  assert.match(h.run("storageView.data.campaignSummary"), /^Same name · 2 of 2 · HOLD/);
  assert.equal(h.run("storageView.data.otherCampaigns.length"), 1);
  assert.equal(h.run("storageView.data.otherCampaigns[0].id"), "8fca7c2c-091a-5bfe-ace8-b6af9c0e801c");
  assert.match(h.run("storageView.data.otherCampaigns[0].summary"), /^Same name · 1 of 2 · HOLD/);
  assert.equal(JSON.stringify(h.context.saved), original);
  h.context.saved.reverse();
  h.context.saved[0].updated = "2026-10-31T12:00:00Z";
  assert.equal(h.run("campaignDistinction(saved[0])"), "Same name · 1 of 2");
  h.context.saved = [h.context.saved[1]];
  h.run("showCampaignChooser()");
  assert.equal(h.run("storageView.data.otherCampaigns.length"), 0);
  const alone = h.run("MotifStorage.getView(storageView.state,storageView.data).body");
  assert.match(alone, /No other saved campaigns yet/);
  assert.doesNotMatch(alone, /menu-popover|Same name/);
  assert.equal(h.context.draft.name, "Field Notes");
  for (const [value, expected] of [
    [{saved_expected_images: 0, thumbnail_expected: true, output_expected: false}, "Images not produced yet"],
    [{saved_expected_images: 6, output_expected: false}, "Image unavailable"],
    [{thumbnail_expected: true, output_expected: false}, "Image unavailable"],
    [{thumbnail_expected: true, output_expected: true}, "Image unavailable"],
    [{}, "Image unavailable"],
    [{thumbnail_expected: false, damaged_images: 1}, "Image unavailable"],
  ]) {
    h.context.card = value;
    assert.equal(h.run("workbenchEmptyImage(card)"), expected);
  }
});

test("typed color-profile cause uses escaped role-specific recovery without changing attachment or fields", () => {
  for (const logo of [false, true]) {
    const h = harness();
    h.context.window = h.context;
    h.run(readFileSync("src/campaign_studio/static/motif/storage-components.js", "utf8"));
    h.context.draft.brand = { logo_path: "Projects/Test-12345678/Assets/Products/logo.png" };
    h.context.draft.audience = "Typed audience";
    h.context.modalProduct = { name: "Typed product", source: { path: "Projects/Test-12345678/Assets/Products/product.jpg" } };
    const before = JSON.stringify([h.context.draft, h.context.modalProduct]);
    h.context.logo = logo;
    h.run("imageInputFailure({input_image:{kind:'color_profile'},message:'UNKNOWN_MESSAGE'}, {name:'folder/<profile>.png',size:20},logo,draft)");
    const view = h.run("MotifStorage.getView(storageView.state,storageView.data)");
    assert.equal(view.title, "Use an sRGB copy.");
    assert.match(view.body, /&lt;profile&gt;\.png/);
    assert.doesNotMatch(view.body, /UNKNOWN_MESSAGE|folder\/|may be damaged/);
    assert.match(view.body, /PNG or JPEG/);
    assert.match(view.body, /typed fields and previous attachment are unchanged/);
    assert.match(view.footer, /Keep my work/);
    assert.match(view.footer, /Choose another image/);
    assert.equal(view.initialFocus, "return-product");
    assert.equal(JSON.stringify([h.context.draft, h.context.modalProduct]), before);
    h.run("imageInputFailure({input_image:{kind:'decode_failure'}}, {name:'bad.png',size:20},logo,draft)");
    assert.equal(h.run("MotifStorage.getView(storageView.state,storageView.data).title"), "We couldn’t open this image.");
    assert.equal(h.requests.length, 0);
  }
});

test("image refusal restores the current connected trigger only for its still-current owner", async () => {
  for (const current of [true, false]) {
    const h=harness();let focus=0;
    h.context.queueMicrotask=queueMicrotask;
    h.context.trigger={isConnected:true,focus(){focus++;}};
    h.context.draft.brand={logo_path:"owned-logo.png"};
    h.run("imageInputFailure({input_image:{kind:'decode_failure'}},{name:'bad.png',size:20},true,draft,()=>trigger)");
    if (!current) h.run("draft={campaign_id:'62220994-1d32-5713-80d2-ac0b6ff0baf3'}");
    h.run('returnFromStorage()');await flush();
    assert.equal(focus,current?2:0);
    assert.equal(h.context.modal,null);
  }
});
