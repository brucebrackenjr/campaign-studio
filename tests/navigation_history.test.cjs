const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  let listener, position = 0;
  const entries = [{ state: null, path: '/' }];
  const location = { pathname: '/', href: '/' };
  const history = {
    get state() { return entries[position].state; },
    replaceState(state, _, path) { entries[position] = { state: structuredClone(state), path }; location.pathname = location.href = path; },
    pushState(state, _, path) { entries.splice(++position); entries.push({ state: structuredClone(state), path }); location.pathname = location.href = path; },
    go(delta) { position += delta; location.pathname = location.href = entries[position].path; queueMicrotask(() => listener({ state: history.state })); },
  };
  const c = vm.createContext({ history, location, window: { addEventListener: (_, fn) => listener = fn },
    page: 'library', draft: { campaign_id: 'a' }, tab: 'campaign', lang: 'en', reviewTab: 'products', selected: [0,1], formats: [0,0], imageMarket: 'all', pageNum: 0, filter: 'all', studioWorkspace: null,
    render() {}, toast(message) { throw new Error(message); }, flushCurrentWork: async () => true,
    openCampaign: async (id, page) => { c.draft = { campaign_id: id }; c.page = page; },
    navigate: async page => { c.page = page; },
  });
  vm.runInContext(fs.readFileSync('src/campaign_studio/static/motif/navigation.js', 'utf8'), c);
  const run = code => vm.runInContext(code, c);
  run('recordNavigation()');
  return { c, run, history, location, entries };
}
test('history records routes and restores Back/Forward view without adding entries', async () => {
  const f = fixture();
  f.run("page='campaign';recordNavigation();retainNavigationView();page='review';reviewTab='images';pageNum=1;recordNavigation()");
  f.c.tab = 'copy';
  f.history.go(-1); await tick(); await tick();
  assert.equal(f.c.page, 'campaign'); assert.equal(f.location.pathname, '/campaigns/a/campaign');
  f.history.go(1); await tick(); await tick();
  assert.equal(f.c.page, 'review'); assert.equal(f.c.reviewTab, 'images'); assert.equal(f.c.pageNum, 1); assert.equal(f.c.tab, 'copy');
  assert.equal(f.entries.length, 3);
});
test('Back restores original URL while a save is pending, then traverses after success', async () => {
  const f = fixture(); f.run("page='campaign';recordNavigation();page='handoff';recordNavigation()");
  let finish; f.c.navigate = page => new Promise(resolve => finish = () => { f.c.page = page; resolve(); });
  f.history.go(-1); await tick();
  assert.equal(f.location.pathname, '/campaigns/a/handoff'); assert.equal(f.c.page, 'handoff');
  finish(); await tick(); await tick();
  assert.equal(f.location.pathname, '/campaigns/a/campaign'); assert.equal(f.c.page, 'campaign');
});
test('refused navigation retains URL, editing state, and forward history', async () => {
  const f = fixture(); f.run("page='campaign';recordNavigation();page='review';recordNavigation()");
  f.c.navigate = async () => {}; // Existing save/conflict guard refused departure.
  f.history.go(-1); await tick(); await tick();
  assert.equal(f.location.pathname, '/campaigns/a/studio'); assert.equal(f.c.page, 'review');
  assert.equal(f.history.state.motifIndex, 2); assert.equal(f.entries.length, 3);
});
