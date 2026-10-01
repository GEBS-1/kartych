const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync('app/static/js/max-auth.js', 'utf8');

function harness({ hash = '', cookie = true, role = '', pathname = '/me', search = '', isAdmin = false, webApp } = {}) {
  const calls = [], redirects = [], timers = [];
  const status = {}, toast = {};
  const window = { location: { hash, search, pathname, origin: 'https://kartych.test', replace: url => redirects.push(url), assign: url => redirects.push(url) }, addEventListener() {} };
  if (webApp) window.WebApp = webApp;
  const elements = { toast };
  const context = {
    window, URLSearchParams,
    document: {
      body: { dataset: { role, cabinet: role === 'business' ? 'biz' : 'guest', canEarn: role === 'business' ? '1' : '', canScan: role === 'business' ? '1' : '', isAdmin: isAdmin ? '1' : '' } },
      documentElement: { classList: { add() {} } },
      querySelector: () => status,
      getElementById: (id) => {
        if (id === 'toast') return toast;
        if (!elements[id]) elements[id] = { id, hidden: false, textContent: '', dataset: {} };
        return elements[id];
      },
      addEventListener() {},
    },
    sessionStorage: { setItem() { throw new Error('Storage unavailable'); } },
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

test('launcher with session opens purchase QR from bot payload', async () => {
  const state = harness({
    hash: '#WebAppData=' + encodeURIComponent('start_param=qr&hash=x'),
    role: 'business',
    pathname: '/app',
  });
  await settle();
  assert.equal(state.calls.length, 0);
  assert.deepEqual(state.redirects, ['/biz/scan']);
});

test('guest launcher opens personal QR from bot payload', async () => {
  const state = harness({
    hash: '#WebAppData=' + encodeURIComponent('start_param=qr&hash=x'),
    role: 'client',
    pathname: '/app',
  });
  await settle();
  assert.deepEqual(state.redirects, ['/me/qr']);
});

test('launcher startapp query opens purchase QR', async () => {
  const state = harness({
    role: 'business',
    pathname: '/app',
    search: '?startapp=qr',
  });
  await settle();
  assert.equal(state.calls.length, 0);
  assert.deepEqual(state.redirects, ['/biz/scan']);
});

test('authenticated mini-app on site root opens cabinet', async () => {
  const state = harness({
    hash: '#WebAppData=' + encodeURIComponent('start_param=cabinet&hash=x'),
    role: 'client',
    pathname: '/',
  });
  await settle();
  assert.equal(state.calls.length, 0);
  assert.deepEqual(state.redirects, ['/me']);
});

test('admin launcher opens review queue', async () => {
  const state = harness({
    hash: '#WebAppData=' + encodeURIComponent('start_param=admin&hash=x'),
    role: 'client',
    pathname: '/',
    isAdmin: true,
  });
  await settle();
  assert.deepEqual(state.redirects, ['/admin']);
});

test('browser landing stays open without MAX webapp', async () => {
  const state = harness({ role: 'client', pathname: '/' });
  await settle();
  assert.equal(state.calls.length, 0);
  assert.deepEqual(state.redirects, []);
});

test('MAX without allow calls ready and does not bounce to login', async () => {
  const ready = [];
  const state = harness({
    pathname: '/app',
    webApp: { initData: '', platform: 'web', ready: () => ready.push(1), openLink() {} },
  });
  await settle();
  assert.ok(ready.length >= 1);
  assert.equal(state.calls.length, 0);
  assert.deepEqual(state.redirects, []);
  assert.match(String(state.toast.textContent || ''), /Разрешить/);
});
