// Unit tests for worker/widgets-sync.js with an in-memory KV. Run: node tests/worker.test.mjs
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const tmp = join(mkdtempSync(join(tmpdir(), 'worker-')), 'worker.mjs');   // .js in repo → load as ES module
writeFileSync(tmp, readFileSync(join(here, '..', 'worker', 'widgets-sync.js')));
const worker = (await import(pathToFileURL(tmp))).default;

function makeEnv() {
  const store = new Map(), puts = [];
  const kv = {
    get: async k => (store.has(k) ? store.get(k) : null),
    put: async (k, v, o = {}) => { puts.push({ k, o }); store.set(k, v); },
    list: async ({ prefix }) => ({ keys: [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name })) }),
  };
  return { env: { SYNC_SECRET: 'K', WIDGET_DATA: kv }, store, puts, kv };
}
const call = (env, method, path, { key = 'K', body } = {}) =>
  worker.fetch(new Request('https://x.dev' + path, { method, headers: key ? { 'X-Secret': key } : {}, body }), env)
    .then(async r => ({ status: r.status, text: await r.text(), origin: r.headers.get('access-control-allow-origin') }));

const RealDate = Date;
const setDay = d => { globalThis.Date = class extends RealDate { constructor(...a) { super(...(a.length ? a : [d + 'T10:00:00Z'])); } static now() { return new RealDate(d + 'T10:00:00Z').getTime(); } }; };

let failed = 0;
async function t(name, fn) {
  try { await fn(); console.log('  ✓ worker: ' + name); }
  catch (e) { failed++; console.log('  ✗ worker: ' + name + '\n      ' + e.message); }
}

await t('auth: missing or wrong key → 401, preflight allowed', async () => {
  const { env } = makeEnv();
  assert.equal((await call(env, 'GET', '/data/fire-sim', { key: null })).status, 401);
  assert.equal((await call(env, 'GET', '/data/fire-sim', { key: 'widget-sync-secret-2024' })).status, 401);
  assert.equal((await call(env, 'OPTIONS', '/data/fire-sim', { key: null })).status, 204);
  assert.equal((await call(env, 'GET', '/data/fire-sim')).origin, 'https://estebandido-cyber.github.io');
});

await t('data: save, load, empty key returns {}, bad keys and big bodies rejected', async () => {
  const { env } = makeEnv();
  assert.equal((await call(env, 'POST', '/data/fire-sim', { body: '{"a":1}' })).status, 200);
  assert.equal((await call(env, 'GET', '/data/fire-sim')).text, '{"a":1}');
  assert.equal((await call(env, 'GET', '/data/never-saved')).text, '{}');
  assert.equal((await call(env, 'GET', '/data/Bad_Key')).status, 400);
  assert.equal((await call(env, 'GET', '/data/bak:fire-sim:2026-01-01')).status, 400);
  assert.equal((await call(env, 'POST', '/data/fire-sim', { body: 'x'.repeat(1_000_001) })).status, 413);
});

await t('backups: one per widget per day, holds the previous value, 30-day TTL', async () => {
  const { env, store, puts } = makeEnv();
  setDay('2026-10-20');
  await call(env, 'POST', '/data/fire-sim', { body: '{"v":"A"}' });            // first ever: nothing to back up
  assert.deepEqual([...store.keys()], ['fire-sim']);
  await call(env, 'POST', '/data/fire-sim', { body: '{"v":"B"}' });            // backs up A
  setDay('2026-10-21');
  puts.length = 0;
  await call(env, 'POST', '/data/fire-sim', { body: '{"v":"C"}' });            // backs up B
  await call(env, 'POST', '/data/fire-sim', { body: '{"v":"D"}' });
  await call(env, 'POST', '/data/fire-sim', { body: '{"v":"E"}' });
  assert.equal(puts.length, 4, 'expected 1 backup write + 3 data writes');
  assert.equal(store.get('bak:fire-sim:2026-10-21'), '{"v":"B"}');
  assert.equal(puts.find(p => p.k.startsWith('bak:')).o.expirationTtl, 30 * 86400);
  const list = JSON.parse((await call(env, 'GET', '/backups/fire-sim')).text);
  assert.deepEqual(list.dates, ['2026-10-21', '2026-10-20']);
  assert.equal((await call(env, 'GET', '/backups/fire-sim/2026-10-20')).text, '{"v":"A"}');
  assert.equal((await call(env, 'GET', '/backups/fire-sim/2025-01-01')).status, 404);
  assert.equal((await call(env, 'POST', '/backups/fire-sim', { body: '{}' })).status, 405);
  assert.equal((await call(env, 'GET', '/backups/fire-sim', { key: null })).status, 401);
  globalThis.Date = RealDate;
});

await t('backups: a failing backup never blocks the save', async () => {
  const { env, store, kv } = makeEnv();
  store.set('fire-sim', '{"v":"OLD"}');
  const get = kv.get;
  kv.get = async k => { if (k.startsWith('bak:')) throw new Error('KV down'); return get(k); };
  const warn = console.warn; console.warn = () => {};
  const r = await call(env, 'POST', '/data/fire-sim', { body: '{"v":"NEW"}' });
  console.warn = warn;
  assert.equal(r.status, 200);
  assert.equal(store.get('fire-sim'), '{"v":"NEW"}');
});

await t('price proxy: ticker validated', async () => {
  const { env } = makeEnv();
  assert.equal((await call(env, 'GET', '/price?ticker=%3Cscript%3E')).status, 400);
});

process.exit(failed ? 1 : 0);
