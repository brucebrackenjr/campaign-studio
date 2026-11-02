const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
function harness() {
  const c = vm.createContext({ AbortController, MotifBriefFlow: { esc: String, button: (label, action, _class, attr) => `<button ${attr || ''} data-bf-action="${action}">${label}</button>` } });
  vm.runInContext(readFileSync('src/campaign_studio/static/motif/local-dialogs.js', 'utf8'), c);
  const root = { listeners: {}, input: { value: '', focus() {} },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelector(selector) { if (selector === '#bf-local-key') return this.innerHTML.includes('id="bf-local-key"') ? this.input : null; return { focus() {} }; },
    innerHTML: '',
  };
  const background = { inert: false }, events = [];
  const mount = onSubmit => c.MotifLocalDialogs.mount(root, { state: { kind: 'key', phase: 'missing' }, background,
    onSubmit, onGuardedRefusal: message => events.push(['refusal', message]), onClose: () => events.push(['closed']), returnFocus: { focus: () => events.push(['focus']) } });
  const submit = () => { root.input.value = 'dummy-test-only'; return root.listeners.click({ target: { closest: () => ({ disabled: false, dataset: { bfAction: 'key-submit' } }) }, preventDefault() {}, stopPropagation() {} }); };
  return { c, root, background, events, mount, submit };
}
test('known key transport refusals settle before close and preserve exact fixed remedies', async () => {
  for (const [status, code, text] of [
    [403, 'origin', 'this local session'], [403, 'session', 'this local session'],
    [413, 'action_size', 'the request was too large'], [415, 'content_type', 'the request format was unsupported'],
  ]) {
    const h = harness(); h.mount(async () => { throw { status, code }; });
    await h.submit();
    assert.equal(h.root.innerHTML, ''); assert.equal(h.root.input.value, ''); assert.equal(h.background.inert, false);
    assert.deepEqual(h.events.map(x => x[0]), ['focus', 'closed', 'refusal']);
    assert.ok(h.events[2][1].includes(text)); assert.ok(h.events[2][1].includes('No key was saved.'));
    if (status === 403) assert.ok(h.events[2][1].includes('Motif’s local address'));
  }
});
test('different status/code or transport loss remains unconfirmed and does not claim refusal', async () => {
  for (const error of [{ status: 403, code: 'credential_save_uncertain' }, { status: 429, code: 'origin' }, new TypeError('Failed to fetch')]) {
    const h = harness(); const view = h.mount(async () => { throw error; }); await h.submit();
    assert.equal(view.phase, error.code === 'credential_save_uncertain' ? 'uncertain' : 'unknown');
    assert.deepEqual(h.events, []); assert.equal(h.background.inert, true);
  }
});
test('an old settled and torn-down key owner cannot close a newer interaction on late refusal', async () => {
  const h = harness(); let reject;
  const old = h.mount(() => new Promise((_resolve, fail) => { reject = fail; })); const flight = h.submit();
  assert.equal(old.phase, 'saving'); old.close(); assert.equal(old.phase, 'saving'); assert.equal(h.background.inert, true);
  // Controlled host teardown exercises the public lifetime guard; normal Close
  // remains unavailable while saving, as the assertions above establish.
  old.render({ phase: 'missing' }); old.close(); h.events.length = 0;
  const newer = h.mount(async () => ({ saved: true })); const html = h.root.innerHTML;
  reject({ status: 403, code: 'session' }); await flight;
  assert.equal(newer.phase, 'missing'); assert.equal(h.root.innerHTML, html); assert.equal(h.background.inert, true); assert.deepEqual(h.events, []);
});

test('access denial keeps the update form but does not mislabel permissions as an invalid key', () => {
  const h = harness();
  const view = h.mount(async () => ({saved: true}));
  view.render({phase: 'rejected', reason: 'This account cannot access image generation.'});
  assert.match(h.root.innerHTML, /Update your OpenAI API key/);
  assert.match(h.root.innerHTML, /This account cannot access image generation/);
  assert.doesNotMatch(h.root.innerHTML, /OpenAI rejected the configured key/);
  view.render({phase: 'environment'});
  assert.match(h.root.innerHTML, /managed outside Motif/);
  assert.doesNotMatch(h.root.innerHTML, /This account cannot access image generation/);
});
