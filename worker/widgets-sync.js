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
 */

const ALLOWED_ORIGIN = 'https://estebandido-cyber.github.io';
const KEY_RE    = /^[a-z0-9-]{1,40}$/;          // widget keys like fire-sim, xpenser
const TICKER_RE = /^[A-Za-z0-9.=^_-]{1,20}$/;   // e.g. WPEA.PA, EURUSD=X
const MAX_BODY  = 1_000_000;                    // 1 MB per widget

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
      await env.WIDGET_DATA.put(key, body);
      return new Response('OK', { headers: CORS });
    }

    return new Response('Method not allowed', { status: 405, headers: CORS });
  },
};
