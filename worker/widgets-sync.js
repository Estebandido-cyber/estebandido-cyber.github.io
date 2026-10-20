/*
 * Cloudflare Worker: widgets-sync
 * Paste into Workers & Pages → widgets-sync → Edit code → Deploy.
 *
 * Requires (Settings → Variables and Secrets):
 *   SYNC_SECRET  (type: Secret)  — the sync key. Never commit it.
 *   WIDGET_DATA  (KV binding)    — already configured.
 *
 * Routes:
 *   GET  /data/<key>          → stored JSON ('{}' if empty)
 *   POST /data/<key>          → store body
 *   GET  /price?ticker=XXX    → Yahoo Finance chart JSON (used by Lukas)
 *   GET  /backups/<key>       → { key, dates: ['YYYY-MM-DD', …] } newest first
 *   GET  /backups/<key>/<date>→ the backed-up JSON for that day
 *
 * Daily backups: on the first save of each day (Europe/Paris), the value that
 * is about to be overwritten is copied to 'bak:<key>:<date>' and auto-deleted
 * after BACKUP_DAYS. So the backup dated D = how the widget looked before
 * its first save on day D. Backup keys contain ':' and can't be reached via /data/.
 */

const ALLOWED_ORIGIN = 'https://estebandido-cyber.github.io';
const KEY_RE    = /^[a-z0-9-]{1,40}$/;          // widget keys like fire-sim, xpenser
const TICKER_RE = /^[A-Za-z0-9.=^_-]{1,20}$/;   // e.g. WPEA.PA, EURUSD=X
const MAX_BODY  = 1_000_000;                    // 1 MB per widget
const BACKUP_DAYS = 30;
const DATE_RE   = /^\d{4}-\d{2}-\d{2}$/;

function today() {
  try { return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Paris' }).format(new Date()); }
  catch (_) { return new Date().toISOString().slice(0, 10); }
}

// Copy the current value to bak:<key>:<today> once per day, before it is overwritten
async function backupBeforeWrite(kv, key) {
  const bakKey = `bak:${key}:${today()}`;
  if (await kv.get(bakKey) !== null) return;          // already backed up today
  const current = await kv.get(key);
  if (!current || current === '{}') return;            // nothing worth keeping
  await kv.put(bakKey, current, { expirationTtl: BACKUP_DAYS * 86400 });
}

function cors() {
  return {
    'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, X-Secret, Accept',
    'Access-Control-Max-Age': '86400',
    'Vary': 'Origin',
  };
}

// Constant-time comparison so the key can't be guessed by timing
function safeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const CORS = cors();

    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });

    if (!env.SYNC_SECRET || !safeEqual(request.headers.get('X-Secret') || '', env.SYNC_SECRET)) {
      return new Response('Unauthorized', { status: 401, headers: CORS });
    }

    const url = new URL(request.url);

    // ── Price proxy for Lukas ──
    if (url.pathname === '/price') {
      const ticker = url.searchParams.get('ticker') || '';
      if (!TICKER_RE.test(ticker)) return new Response('Bad ticker', { status: 400, headers: CORS });
      const yahoo = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(ticker)}?interval=1d&range=1d`;
      const r = await fetch(yahoo, {
        headers: { 'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json' },
        cf: { cacheTtl: 300, cacheEverything: true },   // 5-min edge cache
      });
      return new Response(await r.text(), {
        status: r.status,
        headers: { ...CORS, 'Content-Type': 'application/json' },
      });
    }

    // ── Daily backups (read-only) ──
    if (url.pathname.startsWith('/backups/')) {
      if (request.method !== 'GET') return new Response('Method not allowed', { status: 405, headers: CORS });
      const [key, date] = url.pathname.slice('/backups/'.length).replace(/\/$/, '').split('/');
      if (!KEY_RE.test(key || '')) return new Response('Bad key', { status: 400, headers: CORS });
      if (date === undefined) {
        const list = await env.WIDGET_DATA.list({ prefix: `bak:${key}:` });
        const dates = list.keys.map(k => k.name.split(':')[2]).sort().reverse();
        return new Response(JSON.stringify({ key, dates }), { headers: { ...CORS, 'Content-Type': 'application/json' } });
      }
      if (!DATE_RE.test(date)) return new Response('Bad date', { status: 400, headers: CORS });
      const value = await env.WIDGET_DATA.get(`bak:${key}:${date}`);
      if (value === null) return new Response('No backup for that date', { status: 404, headers: CORS });
      return new Response(value, { headers: { ...CORS, 'Content-Type': 'application/json' } });
    }

    // ── Widget data ──
    if (!url.pathname.startsWith('/data/')) return new Response('Not found', { status: 404, headers: CORS });
    const key = url.pathname.slice('/data/'.length).replace(/\/$/, '');
    if (!KEY_RE.test(key)) return new Response('Bad key', { status: 400, headers: CORS });

    if (request.method === 'GET') {
      const value = await env.WIDGET_DATA.get(key);
      return new Response(value || '{}', { headers: { ...CORS, 'Content-Type': 'application/json' } });
    }

    if (request.method === 'POST') {
      const body = await request.text();
      if (body.length > MAX_BODY) return new Response('Too large', { status: 413, headers: CORS });
      try { await backupBeforeWrite(env.WIDGET_DATA, key); }
      catch (e) { console.warn('backup failed', key, e); }   // never block the save
      await env.WIDGET_DATA.put(key, body);
      return new Response('OK', { headers: CORS });
    }

    return new Response('Method not allowed', { status: 405, headers: CORS });
  },
};
