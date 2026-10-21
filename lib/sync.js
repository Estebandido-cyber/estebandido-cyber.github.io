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
 * Leaving a page: widgets debounce their uploads, so an edit can be waiting when
 * the owner taps the Hub icon. CLOUD.stage(payload) — called by the widget when it
 * schedules an upload, i.e. at edit time — stores the payload as pending (once this
 * device has synced the key) and its Hub summary right away. put() also marks
 * pending before sending. So an upload cut off by navigation is pushed by the next
 * get() or by the Hub (flushPending on load), and the widget never reloads its older
 * cloud copy over the edit. Writes made while a page unloads are not reliably kept,
 * which is why staging happens at edit time, not on leave. LifeSync.onLeave(fn) runs
 * fn on pagehide / tab hidden; widgets use it to send a waiting upload at once.
 * Requests made while leaving use fetch keepalive (bodies up to 60 KB).
 *
 * Hub summaries: a put() whose payload has a `hub` object also stores
 * { savedAt, hub } in localStorage 'lifesync_hub_<key>', so the Hub can show this
 * device's newest numbers even before the cloud summary list catches up.
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
  const HUB_PREFIX     = 'lifesync_hub_';
  const KEEPALIVE_MAX  = 60000;          // browsers cap keepalive bodies at 64 KB in flight
  let leaving = false;                   // true while the page is being hidden / unloaded
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
      const keepalive = leaving && typeof init.body === 'string' && init.body.length <= KEEPALIVE_MAX;
      const res = await fetch(BASE + path, { ...init, headers, signal: ctrl.signal, ...(keepalive ? { keepalive: true } : {}) });
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
    // Store this device's Hub summary and, for a real change, the payload as pending — synchronously
    function prepare(payload) {
      const body = JSON.stringify(payload);
      const fp = fingerprint(body);
      if (payload && payload.hub && typeof payload.hub === 'object') {
        try {
          localStorage.setItem(HUB_PREFIX + key, JSON.stringify({
            savedAt: typeof payload.savedAt === 'string' ? payload.savedAt : new Date().toISOString(), hub: payload.hub }));
        } catch (_) {}
      }
      const pend = readPending(key);
      const changed = !((pend === null || fingerprint(pend) === fp) && readSynced(key) === fp);
      // Only for keys this device has synced, so a fresh device never queues its defaults over cloud data
      if (changed && readSynced(key) !== null) writePending(key, body);
      else if (!changed && pend !== null) writePending(key, null);   // edited back to what the cloud has
      return { body, fp, changed };
    }
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
      // Record an upload that is about to happen (call when scheduling a debounced save)
      stage(payload) { prepare(payload); },
      put(payload) {
        const { body, fp, changed } = prepare(payload);
        if (!changed) return Promise.resolve(true);   // unchanged → nothing to send
        return enqueue(key, async () => {
          sizeBanner(key, body.length, false);
          try { await post(key, body); }
          catch (e) {
            if (e.status === 413) sizeBanner(key, body.length, true);
            throw e;   // pending (if any) stays for the next get() / flush
          }
          const left = readPending(key);
          if (left !== null && fingerprint(left) === fp) writePending(key, null);   // a newer edit may already be pending
          writeSynced(key, fp);
          return true;
        });
      },
    };
  }

  // Run fn when the page is left or hidden (link to the Hub, app switch, tab close)
  const leaveFns = [];
  function onLeave(fn) { leaveFns.push(fn); }
  function runLeave() {
    leaving = true;
    leaveFns.forEach(fn => { try { fn(); } catch (e) { console.warn('[LifeSync] onLeave', e); } });
    setTimeout(() => { leaving = false; }, 0);   // only requests started now use keepalive
  }
  window.addEventListener('pagehide', runLeave);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') runLeave(); });

  // This device's latest summary for a key: { savedAt, hub } or null
  function localHub(key) { try { return JSON.parse(localStorage.getItem(HUB_PREFIX + key)) || null; } catch (_) { return null; } }

  // Hub helper: never throws, returns null on any failure
  async function tryGet(key) {
    try { return await cloud(key).get(); } catch (_) { return null; }
  }

  function debounce(fn, ms) {
    let t = null;
    return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
  }

  const time = () => new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });

  window.LifeSync = { BASE, cloud, tryGet, debounce, time, request, flushPending, onLeave, localHub, LIMIT: 5000000 };   // LIMIT = Worker MAX_BODY
})();
