/*
 * Life Hub service worker — offline support.
 *
 * Strategy (chosen so a deploy is never hidden behind a stale cache):
 *   • Same-origin files (pages, lib/*.js, manifest, icons): NETWORK FIRST.
 *     Online you always get the latest version; the cached copy is used only offline.
 *   • Versioned CDN files (Chart.js) and Google Fonts: CACHE FIRST (URLs are immutable).
 *   • Everything else (Cloudflare sync, Google APIs, prices, FX rates): not touched.
 *
 * Emergency off-switch: open any page with ?nosw (handled in lib/pwa.js).
 * Bump VERSION to drop all cached copies on every device.
 */
const VERSION = 'lifehub-v1';
const CDN_HOSTS = ['cdnjs.cloudflare.com', 'cdn.jsdelivr.net', 'fonts.googleapis.com', 'fonts.gstatic.com'];

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

const cacheable = res => res && (res.ok || res.type === 'opaque');

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    const res = await fetch(request);
    if (cacheable(res)) cache.put(request, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(request, { ignoreSearch: true });
    if (hit) return hit;
    if (request.mode === 'navigate') {
      return new Response(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<div style="font-family:system-ui,sans-serif;padding:2rem;max-width:30rem">' +
        '<h1>Offline</h1><p>This page has not been opened on this device yet, so there is no offline copy.</p>' +
        '<p><a href="./index.html">← Back to Life Hub</a></p></div>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    }
    throw err;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  if (cacheable(res)) cache.put(request, res.clone());
  return res;
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin === self.location.origin) {
    if (url.pathname.endsWith('/sw.js')) return;
    event.respondWith(networkFirst(req));
  } else if (CDN_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(req));
  }
  // anything else: browser default (no caching by the service worker)
});

// Kept from the previous sw.js
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow('./index.html'));
});
