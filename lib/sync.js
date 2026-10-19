/*
 * Life Hub — shared Cloudflare sync client.
 * Requires lib/sync-auth.js (getSyncSecret) to be loaded first.
 *
 *   const CLOUD = LifeSync.cloud('fire-sim');
 *   const data  = await CLOUD.get();       // parsed JSON; throws on network/HTTP error
 *   await CLOUD.put(payload);              // throws unless the Worker answered 2xx
 *   const save  = LifeSync.debounce(fn, 3000);
 *   LifeSync.time()                        // 'HH:MM' (fr-FR), for "Synced at" labels
 *
 * Every widget keeps its own payload shape and status UI; this file owns the
 * URL, the key header, timeouts and HTTP error handling.
 */
(function () {
  const BASE = 'https://widgets-sync.estebandido80.workers.dev';
  const TIMEOUT_MS = 15000;

  async function request(path, init = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const headers = { 'X-Secret': getSyncSecret(), ...(init.headers || {}) };
      const res = await fetch(BASE + path, { ...init, headers, signal: ctrl.signal });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res;
    } finally {
      clearTimeout(timer);
    }
  }

  function cloud(key) {
    const path = '/data/' + key;
    return {
      key,
      url: BASE + path,
      async get() {
        const res = await request(path);
        return res.json();
      },
      async put(payload) {
        await request(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        return true;
      },
    };
  }

  // Hub helper: never throws, returns null on any failure
  async function tryGet(key) {
    try { return await cloud(key).get(); } catch (_) { return null; }
  }

  function debounce(fn, ms) {
    let t = null;
    return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
  }

  const time = () => new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });

  window.LifeSync = { BASE, cloud, tryGet, debounce, time, request };
})();
