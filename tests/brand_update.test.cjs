const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function harness() {
  let saves = 0;
  const context = vm.createContext({
    projectBuffers: {form_drafts: {}},
    saveProject: async () => { saves++; return true; },
  });
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/brand-update.js', 'utf8'), context);
  return {context, saves: () => saves};
}

test('Brand has no separate unapplied-image state or leave confirmation', () => {
  const h = harness();
  assert.equal(h.context.brandChangesPending(), false);
  assert.equal(h.context.guardBrandNavigation({kind:'page',target:'review'}), false);
  assert.equal(h.saves(), 0);
});

test('a legacy update action delegates to the current metadata save only', async () => {
  const h = harness();
  assert.equal(await h.context.updateBrandImages(), true);
  assert.equal(h.saves(), 1);
});

test('intentional per-image default-color reset targets stay available to the authored save', () => {
  const h = harness();
  h.context.projectBuffers.form_drafts.brand_color_defaults = [{product_id:'lantern',market:'en-US',ratio:'9:16'}];
  assert.equal(h.context.brandColorTargets()[0].product_id, 'lantern');
});

test('Campaign route presents Brand before Copy and Rules and contains no Update Images control', () => {
  const source = readFileSync('src/campaign_studio/static/motif/brief-flow.js', 'utf8');
  const tabs = source.slice(source.indexOf('tabs = ['), source.indexOf('];',source.indexOf('tabs = [')));
  assert.ok(tabs.indexOf('["brand"') < tabs.indexOf('["copy"'));
  assert.match(source, /products: \["Next: Brand", "tab:brand"\]/);
  assert.match(source, /brand: \["Next: Copy & Rules", "tab:copy"\]/);
  assert.doesNotMatch(source, /"Update Images"/);
});
