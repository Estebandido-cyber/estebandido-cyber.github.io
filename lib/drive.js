/*
 * Life Hub — shared Google Drive backup (manual Save / Load buttons).
 * Requires the Google Identity Services script on the page:
 *   <script src="https://accounts.google.com/gsi/client" async defer></script>
 *
 *   const DRIVE = LifeDrive.create({
 *     filename: 'fire-sim-data.json',
 *     status:   (msg, type) => {...},     // widget's own status UI; type = working | ok | err
 *     busy:     on => {...},              // enable/disable the Save/Load buttons
 *     payload:  () => ({...}),            // what to save
 *     apply:    data => {...},            // restore + re-render; throw if data is invalid
 *     types:    { err: 'warn' },          // optional: map type names to the widget's CSS classes
 *   });
 *   DRIVE.save();  DRIVE.load();
 *
 * One Google sign-in per page; the token and file id are cached until Google rejects them.
 */
(function () {
  const CLIENT_ID = '453884215011-bnuqib76c5lt7vr87g6e5sd59q7r2cip.apps.googleusercontent.com';
  const SCOPE     = 'https://www.googleapis.com/auth/drive.file';
  const API       = 'https://www.googleapis.com';

  let token = null;  // shared by every LifeDrive instance on the page

  class AuthError extends Error {}

  function signIn() {
    if (token) return Promise.resolve(token);
    return new Promise((resolve, reject) => {
      if (!window.google?.accounts?.oauth2) return reject(new Error('Google Sign-in not loaded yet — try again'));
      const client = google.accounts.oauth2.initTokenClient({
        client_id: CLIENT_ID,
        scope: SCOPE,
        callback: resp => {
          if (resp.error) return reject(new Error('Sign-in failed: ' + resp.error));
          token = resp.access_token;
          resolve(token);
        },
        error_callback: err => reject(new Error('Sign-in cancelled' + (err?.type ? ' (' + err.type + ')' : ''))),
      });
      client.requestAccessToken();
    });
  }

  async function api(url, init = {}) {
    const res = await fetch(url, { ...init, headers: { ...(init.headers || {}), Authorization: 'Bearer ' + token } });
    if (res.status === 401) { token = null; throw new AuthError('Session expired'); }
    if (!res.ok) throw new Error('Drive HTTP ' + res.status);
    return res;
  }

  const time = () => new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });

  function create(opts) {
    const { filename, payload, apply } = opts;
    const t = { working: 'working', ok: 'ok', err: 'err', ...(opts.types || {}) };
    const status = (msg, type) => { try { opts.status && opts.status(msg, t[type]); } catch (_) {} };
    const busy   = on => { try { opts.busy && opts.busy(on); } catch (_) {} };
    let fileId = null;

    async function findFile() {
      if (fileId) return fileId;
      const q = encodeURIComponent(`name="${filename}" and trashed=false`);
      const res = await api(`${API}/drive/v3/files?q=${q}&fields=files(id,name)`);
      const json = await res.json();
      fileId = json.files && json.files.length ? json.files[0].id : null;
      return fileId;
    }

    async function run(verb, work) {
      busy(true);
      try {
        if (!token) status('Connecting to Google…', 'working');
        await signIn();
        status(verb === 'save' ? 'Saving to Google Drive…' : 'Loading from Google Drive…', 'working');
        await work();
      } catch (e) {
        console.error('[Drive] ' + verb + ':', e);
        if (e instanceof AuthError) { fileId = null; status(`Session expired — click ${verb === 'save' ? 'Save' : 'Load'} again`, 'err'); }
        else if (/^(Sign-in|Google Sign-in|No file)/.test(e.message)) status(e.message, 'err');
        else status(verb === 'save' ? 'Save failed — try again' : 'Load failed — try again', 'err');
      } finally {
        busy(false);
      }
    }

    function save() {
      return run('save', async () => {
        const id = await findFile();
        const form = new FormData();
        form.append('metadata', new Blob([JSON.stringify({ name: filename, mimeType: 'application/json' })], { type: 'application/json' }));
        form.append('file', new Blob([JSON.stringify(payload(), null, 2)], { type: 'application/json' }));
        const url = `${API}/upload/drive/v3/files${id ? '/' + id : ''}?uploadType=multipart`;
        const res = await api(url, { method: id ? 'PATCH' : 'POST', body: form });
        const json = await res.json();
        if (!id) fileId = json.id;
        status('✓ Saved to Google Drive — ' + time(), 'ok');
      });
    }

    function load() {
      return run('load', async () => {
        const id = await findFile();
        if (!id) throw new Error('No file found in Drive — save first');
        const res = await api(`${API}/drive/v3/files/${id}?alt=media`);
        apply(await res.json());
        status('✓ Loaded from Google Drive — ' + time(), 'ok');
      });
    }

    return { save, load };
  }

  window.LifeDrive = { create };
})();
