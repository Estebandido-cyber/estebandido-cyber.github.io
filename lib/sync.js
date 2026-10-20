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
 *
 * Offline safety: if put() fails (offline, timeout, 4xx/5xx) the payload is kept
 * in localStorage as "pending". The next get() for that key pushes the pending
 * payload first instead of downloading the older cloud copy, so edits made
 * offline are never overwritten. Pending payloads are also pushed when the
 * browser comes back online.
 *
 * Unchanged saves are skipped: the fingerprint of the last content known to be
 * in the cloud (from a successful get or put) is kept per key; a put() with the
 * same content (ignoring top-level savedAt/exportedAt/updatedAt) sends nothing.
 * This stops "save on open" from uploading — or, offline, queueing — a device's
 * older copy over newer cloud data. A failed upload is only kept as pending once
 * this device has synced that key at least once.
 *
 * Size warning: the Worker refuses saves over LIMIT. When a widget's data
 * passes WARN_AT of that (on load or save), or a save is refused as too large,
 * a banner tells the owner — years before it would actually fail.
 */
(function () {
  const BASE = 'https://widgets-sync.estebandido80.workers.dev';
  const TIMEOUT_MS = 15000;
  const PENDING_PREFIX = 'lifesync_pending_';
  const SYNCED_PREFIX  = 'lifesync_synced_';
  const VOLATILE = ['savedAt', 'exportedAt', 'updatedAt'];   // change on every save, not real edits
  const WARN_AT = 0.7;
  const warned = {};

  function sizeBanner(key, length, refused) {
    const limit = window.LifeSync.LIMIT, pct = Math.round(length / limit * 100), mb = n => (n / 1e6).toFixed(1);
    if (!refused && (length < limit * WARN_AT || length > limit)) return;   // over the limit: wait for the refusal
    console.warn('[LifeSync] ' + key + ': ' + mb(length) + ' MB = ' + pct + '% of the ' + mb(limit) + ' MB sync limit');
    if (!document.body) return;
    const text = refused
      ? '\u26A0 "' + key + '" data is ' + mb(length) + ' MB \u2014 over the ' + mb(limit) + ' MB cloud sync limit, so it is NOT syncing. Your data is safe on this device. Ask Claude to split it by year.'
      : '\u26A0 "' + key + '" data is ' + mb(length) + ' MB (' + pct + '% of the ' + mb(limit) + ' MB cloud sync limit). Still syncing fine \u2014 plan to split it by year.';
    let el = warned[key];
    if (el && (el.dataset.refused === '1' || !refused)) return;             // one banner per key; a refusal upgrades it
    if (!el) {
      el = document.createElement('div');
      el.className = 'lifesync-size-warning';
      el.setAttribute('role', 'alert');
      el.style.cssText = 'position:fixed;left:12px;right:12px;bottom:12px;z-index:99999;padding:10px 40px 10px 14px;border-radius:10px;' +
        'font:13px/1.4 system-ui,sans-serif;background:#fff4d6;color:#5a3d00;border:1px solid #e0b44a;box-shadow:0 4px 16px rgba(0,0,0,.15)';
      el.appendChild(document.createElement('span'));
      const x = document.createElement('button');
      x.textContent = '\u00D7'; x.setAttribute('aria-label', 'Dismiss');
      x.style.cssText = 'position:absolute;right:8px;top:6px;border:0;background:none;font-size:18px;color:inherit;cursor:pointer';
      x.onclick = () => el.remove();
      el.appendChild(x); document.body.appendChild(el);
      warned[key] = el;
    }
    el.dataset.refused = refused ? '1' : '0';
    el.firstChild.textContent = text;
  }


  const readSynced  = key => { try { return localStorage.getItem(SYNCED_PREFIX + key); } catch (_) { return null; } };
  const writeSynced = (key, fp) => { try { localStorage.setItem(SYNCED_PREFIX + key, fp); } catch (_) {} };

  // cyrb53 — fast 53-bit string hash
  function hash(str) {
    let h1 = 0xdeadbeef, h2 = 0x41c6ce57;
    for (let i = 0; i < str.length; i++) {
      const c = str.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 2654435761); h2 = Math.imul(h2 ^ c, 1597334677);
    }
    h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
    h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
    return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36) + ':' + str.length;
  }
  function fingerprint(body) {
    try {
      const o = JSON.parse(body);
      if (o && typeof o === 'object' && !Array.isArray(o)) { VOLATILE.forEach(k => delete o[k]); return hash(JSON.stringify(o)); }
    } catch (_) {}
    return hash(body);
  }

  const readPending  = key => { try { return localStorage.getItem(PENDING_PREFIX + key); } catch (_) { return null; } };
  const writePending = (key, json) => {
    try { json === null ? localStorage.removeItem(PENDING_PREFIX + key) : localStorage.setItem(PENDING_PREFIX + key, json); } catch (_) {}
  };

  async function request(path, init = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const headers = { 'X-Secret': getSyncSecret(), ...(init.headers || {}) };
      const res = await fetch(BASE + path, { ...init, headers, signal: ctrl.signal });
      if (!res.ok) { const e = new Error('HTTP ' + res.status); e.status = res.status; throw e; }
      return res;
    } finally {
      clearTimeout(timer);
    }
  }

  // One queue per key: uploads for a widget always reach the cloud in the order
  // they were issued, so an older pending payload can never land after a newer save.
  const queues = {};
  function enqueue(key, job) {
    const run = (queues[key] || Promise.resolve()).catch(() => {}).then(job);
    queues[key] = run;
    return run;
  }
  const post = (key, body) => request('/data/' + key, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
  });

  // Push the pending payload for one key (read at send time, so it is the newest)
  const flushKey = key => enqueue(key, async () => {
    const body = readPending(key);
    if (body === null) return null;
    await post(key, body);
    if (readPending(key) === body) writePending(key, null);
    writeSynced(key, fingerprint(body));
    return body;
  });

  // Push every pending payload (called when the browser comes back online)
  async function flushPending() {
    let keys = [];
    try { keys = Object.keys(localStorage).filter(k => k.startsWith(PENDING_PREFIX)); } catch (_) {}
    for (const k of keys) { try { await flushKey(k.slice(PENDING_PREFIX.length)); } catch (_) {} }
  }
  window.addEventListener('online', flushPending);

  function cloud(key) {
    const path = '/data/' + key;
    return {
      key,
      url: BASE + path,
      async get() {
        if (readPending(key) !== null) {
          // Local edits never reached the cloud: push them instead of loading older data.
          // Throws if still offline, so the widget keeps its (newer) local state.
          const pushed = await flushKey(key);
          if (pushed !== null) return JSON.parse(pushed);
        }
        const res = await request(path);
        const text = await res.text();
        sizeBanner(key, text.length, false);
        writeSynced(key, fingerprint(text));
        return JSON.parse(text);
      },
      put(payload) {
        const body = JSON.stringify(payload);
        const fp = fingerprint(body);
        return enqueue(key, async () => {
          if (readPending(key) === null && readSynced(key) === fp) return true;   // unchanged → nothing to send
          sizeBanner(key, body.length, false);
          try { await post(key, body); }
          catch (e) {
            if (e.status === 413) sizeBanner(key, body.length, true);
            if (readSynced(key) !== null) writePending(key, body);   // keep real edits of a key this device has synced
            throw e;
          }
          writePending(key, null);   // queue order ⇒ any pending left is older than this save
          writeSynced(key, fp);
          return true;
        });
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

  window.LifeSync = { BASE, cloud, tryGet, debounce, time, request, flushPending, LIMIT: 5000000 };   // LIMIT = Worker MAX_BODY
})();
