/*
 * Life Hub — registers the offline service worker (sw.js).
 *
 * Per-device switch (any page):
 *   ?nosw → turn offline mode OFF on this device: unregister the worker, delete
 *           its caches, remember the choice (localStorage 'lifehub_nosw').
 *   ?sw   → turn it back ON.
 */
(function () {
  if (!('serviceWorker' in navigator) || location.protocol === 'file:') return;
  const FLAG = 'lifehub_nosw';
  const params = new URLSearchParams(location.search);
  const cleanUrl = () => { const u = new URL(location.href); u.searchParams.delete('nosw'); u.searchParams.delete('sw'); return u.toString(); };
  const setFlag = on => { try { on ? localStorage.setItem(FLAG, '1') : localStorage.removeItem(FLAG); } catch (_) {} };
  const flagged = () => { try { return localStorage.getItem(FLAG) === '1'; } catch (_) { return false; } };

  async function removeAll() {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map(r => r.unregister()));
    if (window.caches) { const keys = await caches.keys(); await Promise.all(keys.map(k => caches.delete(k))); }
    return regs.length;
  }

  if (params.has('nosw')) {
    setFlag(true);
    removeAll().then(n => { alert('Offline mode OFF on this device (' + n + ' worker removed, caches cleared).\nOpen any page with ?sw to turn it back on.'); location.replace(cleanUrl()); });
    return;
  }
  if (params.has('sw')) {
    setFlag(false);
    alert('Offline mode ON for this device.');
    location.replace(cleanUrl());
    return;
  }
  if (flagged()) { removeAll(); return; }   // stay off; also cleans up if a worker came back

  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(e => console.warn('SW registration failed', e));
  });
})();
