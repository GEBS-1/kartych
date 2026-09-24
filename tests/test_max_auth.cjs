const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync('app/static/js/max-auth.js', 'utf8');

function harness({ hash = '', cookie = true, role = '' } = {}) {
  const calls = [], redirects = [], timers = [];
  const status = {}, toast = {};
  const window = { location: { hash, replace: url => redirects.push(url) }, addEventListener() {} };
  const context = {
    window, URLSearchParams,
    document: { body: { dataset: { role } }, documentElement: { classList: { add() {} } }, querySelector: () => status, getElementById: () => toast },
    sessionStorage: { setItem() { throw new Error('Storage unavailable'); } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      return url === '/app/auth' ? { ok: true, json: async () => ({ role: 'business' }) } : { ok: cookie };
    },
    setInterval: fn => { timers.push(fn); return 1; }, clearInterval() {},
  };
  vm.runInNewContext(source, context);
  return { calls, redirects, timers, window, toast };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test('signed launch fragment restores login even without loaded SDK or storage', async () => {
  const state = harness({ hash: '#WebAppData=' + encodeURIComponent('user=encoded&hash=signature') });
  await settle();
  assert.deepEqual(state.calls.map(c => c.url), ['/app/auth', '/app/session']);
  assert.equal(state.calls[0].options.credentials, 'include');
  assert.equal(JSON.parse(state.calls[0].options.body).init_data, 'user=encoded&hash=signature');
  assert.deepEqual(state.redirects, ['/biz']);
});

test('blocked cookies do not cause an infinite auth/redirect loop', async () => {
  const state = harness({ hash: '#WebAppData=signed', cookie: false });
  await settle();
  for (let i = 0; i < 10; i++) state.timers[0]();
  await settle();
  assert.equal(state.calls.length, 2);
  assert.deepEqual(state.redirects, []);
  assert.match(state.toast.textContent, /блокирует сохранение входа/);
});

test('late SDK initialization is retried', async () => {
  const state = harness();
  assert.equal(state.calls.length, 0);
  state.window.WebApp = { initData: 'signed-later' };
  state.timers[0]();
  await settle();
  assert.deepEqual(state.redirects, ['/biz']);
});

test('existing authenticated page stays open', async () => {
  const state = harness({ hash: '#WebAppData=signed', role: 'business' });
  await settle();
  assert.equal(state.calls.length, 0);
  assert.deepEqual(state.redirects, []);
});
