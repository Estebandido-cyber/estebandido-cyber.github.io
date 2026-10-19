/*
 * Life Hub — sync secret, kept out of the public repo.
 *
 * The Cloudflare Worker secret is entered once per browser and stored in
 * localStorage. Every widget lives on the same origin, so one entry covers
 * the Hub and all widgets on that device.
 *
 * Load this before any widget script:  <script src="lib/sync-auth.js"></script>
 * Then use:  const CF_SECRET = getSyncSecret();
 */
(function () {
  const KEY  = 'lifehub_sync_secret';
  const SKIP = 'lifehub_sync_skip';
  const HOST = 'widgets-sync.estebandido80.workers.dev';

  const read  = () => { try { return localStorage.getItem(KEY) || ''; } catch (_) { return ''; } };
  const write = v  => { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (_) {} };

  window.getSyncSecret = function () {
    let s = read();
    if (s) return s;
    // Asked at most once per browser session if the user cancels
    try { if (sessionStorage.getItem(SKIP)) return ''; } catch (_) {}
    s = (window.prompt('Life Hub — cloud sync key\n\nPaste your sync key once on this device.\nCancel = work offline (local only).') || '').trim();
    if (s) write(s);
    else { try { sessionStorage.setItem(SKIP, '1'); } catch (_) {} }
    return s;
  };

  // Forget a wrong key: if the Worker answers 401, clear it so the next load asks again
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await origFetch(input, init);
    try {
      const url = typeof input === 'string' ? input : input.url;
      if (res.status === 401 && url.includes(HOST)) {
        write('');
        try { sessionStorage.removeItem(SKIP); } catch (_) {}
        console.warn('Life Hub sync: key rejected — you will be asked again on next load.');
      }
    } catch (_) {}
    return res;
  };

  // Manual reset from the console if ever needed: resetSyncSecret()
  window.resetSyncSecret = () => { write(''); try { sessionStorage.removeItem(SKIP); } catch (_) {} };
})();
