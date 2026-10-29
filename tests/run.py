#!/usr/bin/env python3
"""
Life Hub test suite — run before every push:

    python3 tests/run.py            # everything
    python3 tests/run.py xpenses    # only tests whose name contains "xpenses"

Needs: Python Playwright + Chromium, Node 18+, npm (Chart.js is fetched once into tests/.cache).

Rules for these tests:
  • The network is fully mocked. A catch-all route answers every request; the real
    Cloudflare Worker, Google and CDNs are never contacted.
  • Tests use their OWN inputs, never the widgets' defaults, and check RULES
    (e.g. "donut sum = Your expenses") rather than snapshots, so adding/renaming
    rows or changing defaults does not require editing tests. Only a change in
    how money is calculated should.
"""
import functools, glob, http.server, json, os, re, shutil, socketserver, subprocess, sys, tempfile, threading, time, traceback, urllib.parse

os.environ.setdefault('PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS', '1')   # let routes see service-worker fetches
from playwright.sync_api import sync_playwright

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'tests', '.cache')
PAGES = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, '*.html')))
WORKER_HOST = 'widgets-sync.estebandido80.workers.dev'
KEY  = 'TEST-KEY'
CORS = {'Access-Control-Allow-Origin': '*'}
JSONH = {**CORS, 'Content-Type': 'application/json'}

# Pages whose save function is not global (script wrapped in an IIFE): trigger via the UI
SAVE_BY_CLICK = {'WEEK_PLANNER.html': '#clearbtn'}
# Read-only access to another widget's cloud data (GET only, never POST)
CROSS_READS = {'kals_track.html': {'workout'}}
DRIVE_STATUS_IDS = ['drive-status-bar', 'drive-status', 'statusText']


# ───────────────────────── helpers ─────────────────────────
def read(name): return open(os.path.join(ROOT, name), encoding='utf-8').read()

def chart_js():
    path = os.path.join(CACHE, 'node_modules', 'chart.js', 'dist', 'chart.umd.js')
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        subprocess.run(['npm', 'i', '--silent', '--no-save', '--no-audit', '--no-fund', '--prefix', CACHE, 'chart.js@4.4.1'], check=True)
    return path

def widget_info():
    """Discover cloud key / Drive file / Drive functions from the HTML itself."""
    out = {}
    for f in PAGES:
        s = read(f)
        key = re.search(r"LifeSync\.cloud\('([a-z0-9-]+)'\)", s)
        drv = re.search(r"(?:GD|DRIVE)_FILENAME\s*=\s*'([^']+)'", s)
        sv  = re.search(r"function (\w+)\(\) \{ DRIVE\.save\(\); \}", s)
        ld  = re.search(r"function (\w+)\(\) \{ DRIVE\.load\(\); \}", s)
        out[f] = dict(key=key and key.group(1), drive=drv and drv.group(1), save=sv and sv.group(1), load=ld and ld.group(1))
    return out

def wait_until(pg, fn, timeout=6.0):
    """Poll fn() while letting Playwright process events (time.sleep would freeze the mocked network)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v: return v
        except Exception:
            pass
        pg.wait_for_timeout(100)
    return fn()


class FakeWorker:
    """Mock of worker/widgets-sync.js: /data/<key>, and /hub built from each save's `hub` (KV metadata)."""
    def __init__(self): self.store, self.meta, self.log, self.offline, self.max_body = {}, {}, [], False, None
    def save_meta(self, k, body):
        try: obj = json.loads(body or '')
        except Exception: obj = None
        obj = obj if isinstance(obj, dict) else {}
        meta = {'savedAt': obj.get('savedAt') or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        if isinstance(obj.get('hub'), dict): meta['hub'] = obj['hub']
        self.meta[k] = meta
    def handle(self, route):
        rq = route.request
        if self.offline: return route.abort('internetdisconnected')
        path = urllib.parse.urlparse(rq.url).path
        if rq.method == 'OPTIONS': return route.fulfill(status=204, headers={**CORS, 'Access-Control-Allow-Headers': '*'})
        ok = rq.headers.get('x-secret') == KEY
        self.log.append((rq.method, path, 200 if ok else 401))
        if not ok: return route.fulfill(status=401, body='Unauthorized', headers=CORS)
        if path.startswith('/data/'):
            k = path[6:]
            if rq.method == 'POST':
                if self.max_body and len(rq.post_data or '') > self.max_body:
                    return route.fulfill(status=413, body='Too large', headers=CORS)
                self.store[k] = rq.post_data
                self.save_meta(k, rq.post_data)
                return route.fulfill(status=200, body='OK', headers=CORS)
            return route.fulfill(status=200, body=self.store.get(k, '{}'), headers=JSONH)
        if path == '/hub': return route.fulfill(status=200, body=json.dumps(self.meta), headers=JSONH)
        return route.fulfill(status=200, body='{}', headers=JSONH)   # /price etc.
    def posted(self, k): return json.loads(self.store[k]) if k in self.store else None
    def keys(self, method): return {p[6:] for m, p, _ in self.log if m == method and p.startswith('/data/')}


class FakeDrive:
    def __init__(self): self.files, self.expired, self.writes = {}, False, []
    def handle(self, route):
        rq, u = route.request, route.request.url
        if self.expired: return route.fulfill(status=401, body='{"error":{"code":401}}', headers=JSONH)
        if '/drive/v3/files?q=' in u:
            name = urllib.parse.unquote(u).split('name="')[1].split('"')[0]
            hits = [{'id': i, 'name': f['name']} for i, f in self.files.items() if f['name'] == name]
            return route.fulfill(status=200, body=json.dumps({'files': hits}), headers=JSONH)
        if '/upload/drive/v3/files' in u:
            parts = (rq.post_data_buffer or b'').decode('utf-8', 'ignore').split('\r\n\r\n')
            meta = json.loads(parts[1].split('\r\n--')[0]); content = parts[2].split('\r\n--')[0]
            fid = u.split('/files/')[1].split('?')[0] if '/files/' in u else 'F%d' % len(self.files)
            self.files[fid] = {'name': meta['name'], 'content': content}; self.writes.append(rq.method)
            return route.fulfill(status=200, body=json.dumps({'id': fid}), headers=JSONH)
        if 'alt=media' in u:
            return route.fulfill(status=200, body=self.files[u.split('/files/')[1].split('?')[0]]['content'], headers=JSONH)
        return route.fulfill(status=404, body='{}', headers=JSONH)


def new_context(browser, worker, drive=None, secret=KEY, google_stub=False):
    ctx = browser.new_context()
    chart = chart_js()
    net = {'offline': False}
    def handler(route):
        u = route.request.url
        host = urllib.parse.urlparse(u).hostname or ''
        if net['offline'] and not u.startswith('file:'): return route.abort('internetdisconnected')
        if u.startswith('file:') or host in ('127.0.0.1', 'localhost'): return route.continue_()
        if host == WORKER_HOST: return worker.handle(route)          # never the real Worker
        if host in ('cdnjs.cloudflare.com', 'cdn.jsdelivr.net') and 'chart' in u.lower():
            return route.fulfill(path=chart, content_type='application/javascript', headers=CORS)
        if host == 'www.googleapis.com' and drive: return drive.handle(route)
        if host == 'accounts.google.com': return route.fulfill(status=200, body='', content_type='application/javascript')
        return route.fulfill(status=200, body='{}', headers=JSONH)   # fonts, FX rates, prices, proxies
    ctx.route('**/*', handler)
    init = f"localStorage.setItem('lifehub_sync_secret', {json.dumps(secret)});"
    if google_stub:
        init += "window.google={accounts:{oauth2:{initTokenClient:o=>({requestAccessToken:()=>setTimeout(()=>o.callback({access_token:'TOK'}),5)})}}};"
    ctx.add_init_script(init)
    ctx.go_offline = lambda on: (net.__setitem__('offline', on), ctx.set_offline(on))
    return ctx

def open_page(ctx, url):
    pg = ctx.new_page(); errs = []
    pg.dialog_answer = 'dismiss'                     # tests may switch to 'accept'
    pg.on('pageerror', lambda e: errs.append(str(e)[:160]))
    pg.on('dialog', lambda d: d.accept() if pg.dialog_answer == 'accept' else d.dismiss())
    pg.goto(url if '://' in url else 'file://' + os.path.join(ROOT, url))
    return pg, errs

def drive_status(pg):
    return pg.evaluate("ids => { for (const i of ids) { const e = document.getElementById(i); if (e && e.textContent.trim()) return e.textContent.trim(); } return ''; }", DRIVE_STATUS_IDS)


class Server:
    """Serves a temporary COPY of the repo (tests may modify files) on 127.0.0.1."""
    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix='lifehub-')
        shutil.copytree(ROOT, self.dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git', '.cache', 'node_modules'))
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a): pass
        h = functools.partial(Quiet, directory=self.dir)
        self.httpd = socketserver.ThreadingTCPServer(('127.0.0.1', 0), h)
        self.base = 'http://127.0.0.1:%d/' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self
    def write(self, name, text): open(os.path.join(self.dir, name), 'w').write(text)
    def __exit__(self, *a):
        self.httpd.shutdown(); shutil.rmtree(self.dir, ignore_errors=True)


# ───────────────────────── tests ─────────────────────────
TESTS = []
def test(fn): TESTS.append(fn); return fn

@test
def static_checks(b):
    for f in PAGES:
        s = read(f)
        assert s.count('<head>\n<script src="lib/pwa.js"></script>') == 1, f'{f}: lib/pwa.js must be the first thing in <head>'
        if 'LifeSync.' in s:
            assert 'lib/sync-auth.js' in s and 'lib/sync.js' in s, f'{f}: uses LifeSync without lib/sync-auth.js + lib/sync.js'
        if 'LifeDrive.' in s:
            assert 'lib/drive.js' in s, f'{f}: uses LifeDrive without lib/drive.js'
    for p in PAGES + glob.glob(os.path.join(ROOT, 'lib', '*.js')) + ['sw.js', 'worker/widgets-sync.js']:
        s = open(os.path.join(ROOT, p), encoding='utf-8').read()
        assert 'widget-sync-secret-2024' not in s, f'{p}: old sync secret present'
        assert not re.search(r"""['"]X-Secret['"]\s*:\s*['"][^'"]+['"]""", s), f'{p}: hard-coded X-Secret value'
    sw = read('sw.js')
    assert 'networkFirst(req)' in sw, 'sw.js: same-origin files must stay network-first'

@test
def pages_load_without_errors(b):
    bad = {}
    for f in PAGES:
        ctx = new_context(b, FakeWorker())
        pg, errs = open_page(ctx, f); pg.wait_for_timeout(700)
        if errs: bad[f] = errs
        ctx.close()
    assert not bad, bad

@test
def cloud_sync_load_and_save(b):
    for f, w in widget_info().items():
        if not w['key']: continue
        worker = FakeWorker(); ctx = new_context(b, worker)
        pg, errs = open_page(ctx, f)
        assert wait_until(pg, lambda: w['key'] in worker.keys('GET')), f'{f}: no GET /data/{w["key"]} on load'
        if f in SAVE_BY_CLICK: pg.click(SAVE_BY_CLICK[f])
        else: pg.evaluate('cloudSave()')
        assert wait_until(pg, lambda: w['key'] in worker.keys('POST'), 6), f'{f}: no POST /data/{w["key"]} after save'
        other = ((worker.keys('GET') - CROSS_READS.get(f, set())) | worker.keys('POST')) - {w['key']}
        assert not other, f'{f}: touched other cloud keys {other}'
        assert not errs, f'{f}: {errs}'
        ctx.close()

@test
def hub_reads_every_widget_key(b):
    expected = {w['key'] for w in widget_info().values() if w['key']}
    worker = FakeWorker(); ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html')
    assert wait_until(pg, lambda: expected <= worker.keys('GET')), f'Hub did not read: {expected - worker.keys("GET")}'
    assert not errs, errs
    ctx.close()

@test
def wrong_key_fails_safely_and_keeps_edits(b):
    for f, w in widget_info().items():
        if not w['key']: continue
        worker = FakeWorker(); ctx = new_context(b, worker, secret='WRONG')
        ctx.add_init_script(f"localStorage.setItem('lifesync_synced_{w['key']}', 'synced-before')")   # device synced earlier
        pg, errs = open_page(ctx, f); pg.wait_for_timeout(400)
        if f in SAVE_BY_CLICK: pg.click(SAVE_BY_CLICK[f])
        else: pg.evaluate('cloudSave()')
        assert wait_until(pg, lambda: pg.evaluate(f"localStorage.getItem('lifesync_pending_{w['key']}') !== null"), 6), \
            f'{f}: rejected save was not kept as pending'
        assert worker.store == {}, f'{f}: something was stored with a wrong key'
        assert not errs, f'{f}: {errs}'
        ctx.close()

@test
def no_write_when_opening_without_edits(b):
    for f, w in widget_info().items():
        if not w['key']: continue
        worker = FakeWorker(); ctx = new_context(b, worker)              # 1st session: get a realistic payload
        pg, _ = open_page(ctx, f); pg.wait_for_timeout(400)
        if f in SAVE_BY_CLICK: pg.click(SAVE_BY_CLICK[f])
        else: pg.evaluate('cloudSave()')
        assert wait_until(pg, lambda: w['key'] in worker.store, 6), f'{f}: could not seed cloud data'
        seed = worker.store[w['key']]; ctx.close()
        worker = FakeWorker(); worker.store[w['key']] = seed              # another device opens it, touches nothing
        ctx = new_context(b, worker)
        pg, errs = open_page(ctx, f); pg.wait_for_timeout(6000)
        writes = [p for m, p, _ in worker.log if m == 'POST']
        assert not writes, f'{f}: wrote {len(writes)}× just by opening'
        assert not errs, f'{f}: {errs}'
        ctx.close()

@test
def stale_device_cannot_overwrite_newer_cloud(b):
    worker = FakeWorker(); ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'FIRE_SIM.html'); pg.wait_for_timeout(400)
    pg.evaluate("S.pea = 50; draw(); cloudSave()")                       # this device syncs PEA 50
    assert wait_until(pg, lambda: 'fire-sim' in worker.store)
    newer = json.loads(worker.store['fire-sim']); newer['S']['pea'] = 200
    worker.store['fire-sim'] = json.dumps(newer)                           # another device saves PEA 200
    worker.offline = True; pg.reload(); pg.wait_for_timeout(4500)          # open offline, no edits
    assert pg.evaluate("localStorage.getItem('lifesync_pending_fire-sim')") is None, 'opening offline queued an old copy'
    worker.offline = False; pg.reload(); pg.wait_for_timeout(4500)
    assert json.loads(worker.store['fire-sim'])['S']['pea'] == 200, 'older device overwrote newer cloud data'
    assert pg.evaluate('S.pea') == 200, 'device did not pick up the newer cloud data'
    worker.offline = True; pg.evaluate("S.pea = 77; draw(); cloudSave()"); pg.wait_for_timeout(500)   # real offline edit
    worker.offline = False; pg.reload(); pg.wait_for_timeout(4500)
    assert json.loads(worker.store['fire-sim'])['S']['pea'] == 77, 'a real offline edit was lost'
    assert not errs, errs
    ctx.close()

@test
def drive_backup_save_overwrite_load(b):
    for f, w in widget_info().items():
        if not w['drive']: continue
        drive = FakeDrive(); ctx = new_context(b, FakeWorker(), drive=drive, google_stub=True)
        pg, errs = open_page(ctx, f); pg.wait_for_timeout(400)
        def do(kind):
            fn = w[kind]
            if fn and pg.evaluate(f'typeof {fn} === "function"'): pg.evaluate(f'{fn}()')
            else: pg.click('#drivesavebtn' if kind == 'save' else '#driveloadbtn')
        def status_has(txt): return wait_until(pg, lambda: txt in drive_status(pg), 4)
        do('load'); assert status_has('No file'), f'{f}: load without file → {drive_status(pg)!r}'
        do('save'); assert status_has('Saved'), f'{f}: first save → {drive_status(pg)!r}'
        pg.wait_for_timeout(150); do('save'); pg.wait_for_timeout(300)
        do('load'); assert status_has('Loaded'), f'{f}: load → {drive_status(pg)!r}'
        drive.expired = True; do('save'); assert status_has('Session expired'), f'{f}: expiry → {drive_status(pg)!r}'
        assert drive.writes == ['POST', 'PATCH'], f'{f}: writes {drive.writes} (expected create then overwrite)'
        assert [x['name'] for x in drive.files.values()] == [w['drive']], f'{f}: wrong Drive filename'
        assert all(isinstance(json.loads(x['content']), dict) for x in drive.files.values()), f'{f}: Drive file is not JSON'
        assert not errs, f'{f}: {errs}'
        ctx.close()

@test
def sync_offline_edits_are_never_lost(b):
    with Server() as srv:
        worker = FakeWorker(); worker.store['fire-sim'] = '{"v":"CLOUD-OLD"}'
        ctx = new_context(b, worker)
        pg, errs = open_page(ctx, srv.base + 'README.md')
        pg.add_script_tag(url='/lib/sync-auth.js'); pg.add_script_tag(url='/lib/sync.js')
        C = "LifeSync.cloud('fire-sim')"
        pend = lambda: pg.evaluate("localStorage.getItem('lifesync_pending_fire-sim')")
        posts = lambda: sum(1 for m, _, _ in worker.log if m == 'POST')
        assert pg.evaluate(f"{C}.get().then(d=>d.v)") == 'CLOUD-OLD'          # this device has synced once
        worker.offline = True
        assert pg.evaluate(f"{C}.put({{v:'OFFLINE'}}).then(()=>'ok',()=>'threw')") == 'threw'
        assert pend() == '{"v":"OFFLINE"}', 'failed upload not kept'
        assert pg.evaluate(f"{C}.get().then(()=>'ok',()=>'threw')") == 'threw', 'get while offline must not return cloud data'
        worker.offline = False
        assert pg.evaluate(f"{C}.get().then(d=>d.v)") == 'OFFLINE', 'get after reconnect must return the local edit'
        assert worker.store['fire-sim'] == '{"v":"OFFLINE"}' and pend() is None, 'pending edit not pushed'
        worker.offline = True; pg.evaluate(f"{C}.put({{v:'A'}}).catch(()=>0)"); worker.offline = False
        pg.evaluate(f"Promise.all([LifeSync.flushPending(), {C}.put({{v:'B'}})])")
        assert worker.store['fire-sim'] == '{"v":"B"}' and pend() is None, f'out-of-order upload: {worker.store}'
        n = posts()
        pg.evaluate(f"{C}.put({{v:'B'}})")
        pg.evaluate(f"{C}.put({{v:'B', savedAt: new Date().toISOString()}})")
        assert posts() == n, 'unchanged content (only a timestamp differs) must not be uploaded'
        pg.evaluate(f"{C}.put({{v:'C'}})")
        assert posts() == n + 1 and worker.store['fire-sim'] == '{"v":"C"}', 'a real change must be uploaded'
        assert not errs, errs
        ctx.close()

@test
def sync_size_warning(b):
    with Server() as srv:
        worker = FakeWorker(); worker.max_body = 1000
        worker.store['big-read'] = json.dumps({'x': 'y' * 900})
        ctx = new_context(b, worker)
        pg, errs = open_page(ctx, srv.base + 'README.md')
        pg.add_script_tag(url='/lib/sync-auth.js'); pg.add_script_tag(url='/lib/sync.js')
        pg.evaluate("LifeSync.LIMIT = 1000")
        banner = lambda: pg.evaluate("[...document.querySelectorAll('.lifesync-size-warning')].map(e => e.textContent)")
        pg.evaluate("LifeSync.cloud('small').put({x: 'y'.repeat(500)})")
        assert banner() == [], f'warned below 70%: {banner()}'
        pg.evaluate("LifeSync.cloud('growing').put({x: 'y'.repeat(780)})")
        assert len(banner()) == 1 and 'Still syncing' in banner()[0] and 'growing' in banner()[0], f'no early warning: {banner()}'
        assert 'growing' in worker.store, 'a save between 70% and 100% must still go through'
        pg.evaluate("LifeSync.cloud('too-big').get().then(() => LifeSync.cloud('too-big').put({x: 'y'.repeat(1200)})).catch(() => 0)")
        assert any('NOT syncing' in t and 'too-big' in t for t in banner()), f'no warning when refused: {banner()}'
        pg.evaluate("LifeSync.tryGet('big-read')")
        assert any('big-read' in t for t in banner()), 'large data on load (e.g. Hub) must warn too'
        assert not errs, errs
        ctx.close()

@test
def offline_mode_service_worker(b):
    with Server() as srv:
        srv.write('zz_page.html', '<title>v1</title><script src="lib/pwa.js"></script>')
        ctx = new_context(b, FakeWorker())
        pg, errs = open_page(ctx, srv.base + 'index.html')
        pg.evaluate('navigator.serviceWorker.ready.then(() => 1)'); pg.reload()
        assert wait_until(pg, lambda: pg.evaluate('!!navigator.serviceWorker.controller')), 'service worker not controlling the page'
        for f in ('FIRE_SIM.html', 'XPENSES_CALCULATOR.html', 'zz_page.html'): pg.goto(srv.base + f); pg.wait_for_timeout(600)
        ctx.go_offline(True)
        pg.goto(srv.base + 'FIRE_SIM.html'); pg.wait_for_timeout(800)
        assert pg.evaluate("typeof Chart === 'function' && typeof draw === 'function' && !!document.querySelector('canvas')"), 'FIRE Sim not usable offline'
        pg.goto(srv.base + 'zz_never_opened.html')
        assert 'Offline' in (pg.evaluate("document.querySelector('h1')?.textContent") or ''), 'missing offline fallback page'
        ctx.go_offline(False)
        srv.write('zz_page.html', '<title>v2</title><script src="lib/pwa.js"></script>')
        pg.goto(srv.base + 'zz_page.html')
        assert pg.title() == 'v2', 'a new deploy is hidden behind the cache'
        pg.dialog_answer = 'accept'
        pg.goto(srv.base + 'index.html?nosw'); pg.wait_for_timeout(1200)
        assert pg.evaluate('navigator.serviceWorker.getRegistrations().then(r => r.length)') == 0, '?nosw did not unregister'
        assert pg.evaluate('caches.keys().then(k => k.length)') == 0, '?nosw did not clear caches'
        pg.goto(srv.base + 'FIRE_SIM.html'); pg.wait_for_timeout(800)
        assert pg.evaluate('navigator.serviceWorker.getRegistrations().then(r => r.length)') == 0, 'offline mode came back after ?nosw'
        pg.goto(srv.base + 'index.html?sw'); pg.wait_for_timeout(1200)
        assert wait_until(pg, lambda: pg.evaluate('navigator.serviceWorker.getRegistrations().then(r => r.length)') == 1), '?sw did not re-enable'
        assert not [e for e in errs if 'zz_' not in e], errs
        ctx.close()

@test
def xpenses_calculator_rules(b):
    ctx = new_context(b, FakeWorker())
    pg, errs = open_page(ctx, 'XPENSES_CALCULATOR.html'); pg.wait_for_timeout(500)
    ev = pg.evaluate
    approx = lambda a, e, what: abs(a - e) < 1e-6 or (_ for _ in ()).throw(AssertionError(f'{what}: {a} ≠ {e}'))
    for tab in ('now', 'fire'):
        ev("tab => switchTab(tab)", tab)
        # Own inputs: every line and sub-line gets a distinct value (lines are discovered from the layout)
        ev("""tab => { const s = states[tab]; let i = 0;
            SECTIONS.forEach(sec => layout[sec].forEach(l => { s[l.id] = 100 + 10 * i++; (l.subs || []).forEach(x => { s[x.id] = 7 + 3 * i++; }); }));
            Object.assign(s, { sharedAcc: 1000, salary: 2000, tickets: 200, mel: 41 });
            layout.shared.concat(layout.personal).forEach(l => { if (l.subs) openLines.add(l.id); });
            render(tab); }""", tab)
        r = ev("""tab => {
            const s = states[tab], d = derived(s), g = id => document.getElementById(tab + '-' + id).textContent;
            const val = l => (l.subs && l.subs.length ? l.subs.reduce((t, x) => t + s[x.id], 0) : s[l.id]) / (l.yearly ? 12 : 1);
            return { s, d, shared: layout.shared.map(val), personal: layout.personal.map(val),
              halves: layout.shared.map(l => [g('amt-' + l.id), fmtSmart(val(l) / 2)]),
              sums: layout.shared.concat(layout.personal).filter(l => l.subs && l.subs.length).map(l => ({ id: l.id,
                shown: g('sv-' + l.id), want: fmt(l.subs.reduce((t, x) => t + s[x.id], 0)), buttons: document.querySelectorAll('#' + tab + '-spin-' + l.id + ' button').length })),
              donut: charts[tab].data.datasets[0].data.reduce((a, b) => a + b, 0),
              show: { gross: g('m-gross'), left: g('m-leftover'), pool: g('sa-total'), half: g('half-shared'), pers: g('total-personal') },
              fmt: { gross: fmt(d.gross), left: fmt(d.leftover), pool: fmt(d.sharedPool), half: fmtSmart(d.sharedTotal / 2), pers: fmt(d.personalMonthly) } }; }""", tab)
        d, s = r['d'], r['s']
        approx(d['sharedTotal'], sum(r['shared']), f'[{tab}] shared total = lines (sub-line sums, yearly / 12)')
        approx(d['gross'], sum(r['shared']) / 2 + sum(r['personal']), f'[{tab}] Your expenses = half of shared + personal')
        approx(d['income'], s['salary'] + s['tickets'] + s['mel'], f'[{tab}] income counts the partner (Mel) once')
        approx(d['leftover'], d['income'] - d['gross'], f'[{tab}] left = income − expenses')
        approx(d['sharedPool'], 2 * s['sharedAcc'], f'[{tab}] Revolut pot = 2 × each contribution')
        approx(r['donut'], d['gross'], f'[{tab}] donut adds up to Your expenses')
        assert r['show'] == r['fmt'], f'[{tab}] displayed totals {r["show"]} ≠ {r["fmt"]}'
        assert all(a == b for a, b in r['halves']), f'[{tab}] "Your half" column wrong: {r["halves"]}'
        assert r['sums'] and all(x['shown'] == x['want'] and x['buttons'] == 0 for x in r['sums']), f'[{tab}] sub-line sums wrong: {r["sums"]}'
        # Steppers: a shared line or sub-line costs you half its step, a personal one its full step
        for sec, factor in (('shared', 0.5), ('personal', 1)):
            for pick in ("l => !l.subs && !l.yearly", "l => l.subs && l.subs.length"):
                target = ev(f"sec => {{ const l = layout[sec].find({pick}); return l ? (l.subs ? l.subs[0].id : l.id) : null; }}", sec)
                if not target: continue
                v0 = ev("([tab, id]) => states[tab][id]", [tab, target]); lb = ev("tab => derived(states[tab]).leftover", tab)
                pg.locator(f"#{tab}-spin-{target} button").last.click()
                stp = ev("([tab, id]) => states[tab][id]", [tab, target]) - v0
                assert stp == ev("v => stepFor(v, 1)", v0) and stp > 0, (sec, target, stp)
                approx(lb - ev("tab => derived(states[tab]).leftover", tab), stp * factor, f'[{tab}] {sec} {target} stepper effect')
        # Mel must not change expenses
        before = ev("tab => derived(states[tab]).gross", tab)
        ev("tab => { states[tab].mel += 100; render(tab); }", tab)
        approx(ev("tab => derived(states[tab]).gross", tab), before, f'[{tab}] partner contribution must not change expenses')
        # Weekly hint = monthly × 12 / 52 on weekly lines (and their open sub-lines); none on the others
        wk = ev("""tab => { const s = states[tab], out = [];
            layout.shared.concat(layout.personal).forEach(l => {
              const raw = l.subs && l.subs.length ? l.subs.reduce((t, x) => t + s[x.id], 0) : s[l.id];
              if (!l.yearly) out.push([document.getElementById(tab + '-note-' + l.id).textContent, l.weekly ? fmtWeek(raw) : '']);
              (l.subs || []).forEach(x => out.push([document.getElementById(tab + '-note-' + x.id).textContent, l.weekly && !l.yearly ? fmtWeek(s[x.id]) : '']));
            }); return out; }""", tab)
        assert all(a == b for a, b in wk), f'[{tab}] weekly hints wrong: {[x for x in wk if x[0] != x[1]][:3]}'
        approx(ev("() => weekly(260)"), 60, 'weekly = monthly × 12 / 52')
        # Both boxes collapse and expand
        for box in ('shared', 'personal'):
            first_row = f"#{tab}-box-{box} .erow:not(.erow-head):not(.erow-total) >> nth=0"
            assert pg.locator(first_row).is_visible()
            pg.locator(f"#{tab}-box-{box} .box-title").click(position={'x': 5, 'y': 5})
            assert not pg.locator(first_row).is_visible(), f'[{tab}] {box} box did not collapse'
            pg.locator(f"#{tab}-box-{box} .box-title").click(position={'x': 5, 'y': 5})
            assert pg.locator(first_row).is_visible(), f'[{tab}] {box} box did not expand'
    assert not errs, errs
    ctx.close()

@test
def xpenses_edit_lines(b):
    worker = FakeWorker(); ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'XPENSES_CALCULATOR.html'); wait_until(pg, lambda: 'xpenses-calculator' in worker.keys('GET')); pg.wait_for_timeout(300)
    ev = pg.evaluate
    P = '#pane-now '
    gross = lambda: ev("() => [derived(states.now).gross, derived(states.fire).gross]")
    pg.click(P + '#now-personal-edit')
    first = ev("() => layout.personal[0]")
    # Rename (applies to both tabs), without moving any number
    g0 = gross()
    pg.fill(P + f'input[data-act="rename"][data-id="{first["id"]}"]', 'Assurance habitation'); pg.press(P + f'input[data-act="rename"][data-id="{first["id"]}"]', 'Tab')
    assert ev("() => layout.personal[0].label") == 'Assurance habitation' and gross() == g0
    # Reorder
    pg.click(P + f'button[data-act="down"][data-id="{first["id"]}"]')
    assert ev("() => layout.personal[1].id") == first['id']
    # A first sub-line keeps the line's amount (totals unchanged); a second starts at 0
    plain = ev("() => layout.personal.find(l => !l.subs && !l.yearly && states.now[l.id] > 0).id")
    pg.click(P + f'button[data-act="addsub"][data-id="{plain}"]')
    assert gross() == g0 and len(ev(f"() => layout.personal.find(l => l.id === '{plain}').subs")) == 1
    pg.click(P + f'.ed-addsub[data-id="{plain}"]')
    subs = ev(f"() => layout.personal.find(l => l.id === '{plain}').subs")
    assert len(subs) == 2 and gross() == g0 and ev(f"() => [states.now['{subs[1]['id']}'], states.fire['{subs[1]['id']}']]") == [0, 0]
    # Options: yearly divides by 12
    pg.click(P + f'button[data-act="opts"][data-id="{plain}"]')
    pg.check(P + f'input[data-act="yearly"][data-id="{plain}"]')
    assert ev(f"() => layout.personal.find(l => l.id === '{plain}').yearly") is True and gross()[0] < g0[0]
    pg.uncheck(P + f'input[data-act="yearly"][data-id="{plain}"]'); assert gross() == g0
    # Delete a line, then Undo
    n = ev("() => layout.personal.length"); victim = ev("() => layout.personal[2].id")
    pg.click(P + f'button[data-act="del"][data-id="{victim}"]')
    assert ev("() => layout.personal.length") == n - 1 and gross() != g0
    pg.click('#undo-btn')
    assert ev("() => layout.personal.length") == n and ev("() => layout.personal[2].id") == victim and gross() == g0
    # Add a line: present in both tabs at 0
    pg.click(P + '.ed-addline[data-sec="personal"]')
    nid = ev("() => layout.personal[layout.personal.length - 1].id")
    assert ev(f"() => [states.now['{nid}'], states.fire['{nid}']]") == [0, 0] and gross() == g0
    pg.click(P + '#now-personal-edit')   # Done
    assert pg.is_visible(f'#now-spin-{nid}') and ev("() => switchTab('fire')") is None and pg.is_visible(f'#fire-spin-{nid}')
    # On/off switch: an optional line switched off counts nowhere, only in that tab
    ev("() => { switchTab('now'); const l = layout.shared[0]; l.optional = true; render('now'); render('fire'); }")
    sid = ev("() => layout.shared[0].id")
    g1 = gross(); m = ev("() => lineAmount(states.now, layout.shared[0]) / (layout.shared[0].yearly ? 12 : 1)")
    pg.uncheck(f'#pane-now input[data-act="onoff"][data-id="{sid}"]')
    g2 = gross()
    assert abs((g1[0] - g2[0]) - m / 2) < 1e-6 and g2[1] == g1[1], (g1, g2, m)
    assert abs(ev("() => charts.now.data.datasets[0].data.reduce((a, b) => a + b, 0)") - g2[0]) < 1e-6, 'donut must follow the switch'
    pg.check(f'#pane-now input[data-act="onoff"][data-id="{sid}"]'); assert gross() == g1
    # The layout travels with the data (cloud)
    ev("cloudPush()"); assert wait_until(pg, lambda: (worker.posted('xpenses-calculator') or {}).get('layout', {}).get('personal', [{}])[1].get('label') == 'Assurance habitation', 6)
    # Labels are text, never HTML
    ev("() => { layout.shared[0].label = '<b>x</b>'; render('now'); }")
    assert not pg.query_selector('#now-box-shared .ename b')
    assert not errs, errs
    ctx.close()

@test
def xpenses_old_save_migrates(b):
    # A save from before editable lines (Breakdown in "your part", linked lines already synced)
    old_now = {'salary': 2000, 'tickets': 0, 'mel': 0, 'sharedAcc': 500, 'rent': 1000, 'sharedBar': 2 * (10 + 20 + 30),
               'trkBar': 10, 'trkBarBouffe': 20, 'trkApero': 30, 'lunch': 15, 'trkSaumon': 5, 'trkLegumes': 10, 'trkMercadoExtras': 99}
    payload = json.dumps({'now': old_now, 'fire': dict(old_now)})
    ctx = new_context(b, FakeWorker()); ctx.add_init_script("localStorage.setItem('xpenses_calculator_v1', %s)" % json.dumps(payload))
    pg, errs = open_page(ctx, 'XPENSES_CALCULATOR.html'); pg.wait_for_timeout(500)
    r = pg.evaluate("""() => ({ bar: states.now.trkBar, apero: states.now.trkApero, saumon: states.now.trkSaumon, rent: states.now.rent,
                             barLine: lineAmount(states.now, layout.shared.find(l => l.id === 'sharedBar')),
                             lunch: lineAmount(states.now, layout.personal.find(l => l.id === 'lunch')) })""")
    assert (r['bar'], r['apero'], r['saumon'], r['rent']) == (20, 60, 5, 1000), r
    assert r['barLine'] == old_now['sharedBar'] and r['lunch'] == old_now['lunch'], f'totals changed by the migration: {r}'
    assert pg.evaluate("() => layout.shared.find(l => l.id === 'sharedExtras').optional") is True
    assert not errs, errs
    ctx.close()

@test
def kals_balance_rules(b):
    worker = FakeWorker()
    worker.store['kalstrack'] = json.dumps({'settings': {'budget': 2000}, 'days': {'kals_day_2026-01-05': {'meals': [{'kcal': 1}]}}})   # old KALS format
    ctx = new_context(b, worker)
    ctx.add_init_script("localStorage.setItem('kals_day_2026-01-05', '{}'); localStorage.setItem('kals_settings', '{}');")
    pg, errs = open_page(ctx, 'kals_track.html'); wait_until(pg, lambda: 'kalstrack' in worker.keys('GET')); pg.wait_for_timeout(500)
    ev = pg.evaluate
    # Old day-by-day data is ignored (fresh start), its local keys removed, and nothing is written just by opening
    assert ev("Object.keys(state.weeks).length") == 0, 'old KALS cloud data must not be loaded'
    assert ev("Object.keys(localStorage).filter(k => k.startsWith('kals_') && !k.startsWith('kals_balance')).length") == 0, 'old kals_ keys not removed'
    assert not [1 for m, p, _ in worker.log if m == 'POST'], 'opening must not overwrite the cloud'
    near = lambda a, e, what: abs(a - e) < 1e-6 or (_ for _ in ()).throw(AssertionError(f'{what}: {a} ≠ {e}'))
    # Beer: verres standard = ml × %alc × 0.789 / 10 g
    std = ev("beerStats({bd: 2, fp: 2, bb: 1}, {abvB: 4, abvF: 8}).std")
    near(std, (2 * 250 * 0.04 + 2 * 500 * 0.08 + 330 * 0.04) * 0.789 / 10, 'standard drinks')
    # Wine / champagne / cocktail add to the alcohol (verres std) but not to the beer count or litres
    o = ev("() => { const a = beerStats({bd: 1}, {abvB: 5, abvF: 7}), b = beerStats({bd: 1, wn: 2, ch: 1, ck: 1}, {abvB: 5, abvF: 7}); return { a, b, extra: OTHER_DRINKS }; }")
    want = sum(n * d['ml'] * d['abv'] / 100 * 0.789 / 10 for n, d in zip((2, 1, 1), o['extra']))
    near(o['b']['std'] - o['a']['std'], want, 'other drinks in verres std')
    assert (o['b']['count'], o['b']['litres'], o['b']['others']) == (o['a']['count'], o['a']['litres'], 4), o
    # Maintenance = mean kcal − weight slope (kg/day) × 7700; needs 2 weeks with kcal
    st = {'v': 2, 'settings': {'defLo': 100, 'defHi': 400, 'protKg': 2},
          'weeks': {'2026-03-02': {'w': {'2026-03-02': 80.0, '2026-03-05': 79.7}, 'kcal': 2400, 'prot': 150},
                    '2026-03-09': {'w': {'2026-03-09': 79.3, '2026-03-12': 79.0}, 'kcal': 2600, 'prot': 100}}}
    c = ev("s => { const c = compute(s, []); return c.weeks.map(w => ({maint: w.maint, lo: w.zoneLo, hi: w.zoneHi, status: w.status, pt: w.protTarget, trend: w.trend})); }", st)
    assert c[0]['maint'] is None, 'one week of kcal is not enough for maintenance'
    near(c[1]['maint'], 2500 + 0.1 * 7700, 'maintenance'); near(c[1]['lo'], c[1]['maint'] - 400, 'zone bottom'); near(c[1]['hi'], c[1]['maint'] - 100, 'zone top')
    assert c[1]['status'] == 'below', c[1]
    near(c[1]['pt'], 2 * c[1]['trend'], 'protein target = g/kg × trend weight')
    # Verdict: falling waist → on track; rising waist → warning; < 4 check-ins → waiting
    def verdict(waists):
        ws = {}
        for i, wa in enumerate(waists):
            mon = ev("i => addDays('2026-03-02', 7 * i)", i)
            ws[mon] = {'w': {mon: 80.0}, 'waist': wa}
        return ev("s => compute(s, []).verdict.code", {'v': 2, 'settings': {}, 'weeks': ws})
    assert verdict([90, 89.7, 89.4, 89.1]) == 'good'
    assert verdict([90, 90.3, 90.6, 90.9]) == 'warn'
    assert verdict([90, 89.5, 89]) == 'wait'
    # Check-in form: European input parsed, saved for the chosen week, pushed to the cloud
    pg.click('#btn-open-checkin'); mon = ev('ciMon')
    pg.fill('#ci-days input >> nth=0', '74,4'); pg.fill('#ci-days input >> nth=3', '74.0')
    pg.fill('#ci-kcal', '2.350'); pg.fill('#ci-prot', '128'); pg.fill('#ci-steps', '8.900'); pg.fill('#ci-rhr', '57'); pg.fill('#ci-sleep', '6h52')
    pg.click('#ci-beer button[data-k="fp"][data-s="1"]'); pg.click('#ci-beer button[data-k="fp"][data-s="1"]'); pg.click('#ci-beer button[data-k="bd"][data-s="1"]')
    pg.fill('#ci-waist', '86,5'); pg.click('#waist-minus')
    pg.click('#btn-save')
    e = ev("m => state.weeks[m]", mon)
    assert e['w'] == {mon: 74.4, ev("m => addDays(m, 3)", mon): 74} and e['kcal'] == 2350 and e['steps'] == 8900 and e['sleep'] == 412, e
    assert e['beer'] == {'fp': 2, 'bd': 1} and e['waist'] == 86, e
    assert wait_until(pg, lambda: (worker.posted('kalstrack') or {}).get('weeks', {}).get(mon), 6), 'check-in not synced'
    # Edit the last check-in: it reopens with its saved values, and a correction replaces it
    assert pg.is_visible('#btn-edit-last')
    pg.click('#btn-edit-last')
    assert ev('ciMon') == mon and pg.input_value('#ci-kcal') == '2.350' and pg.input_value('#ci-sleep') == '6h52', 'saved week not reloaded'
    pg.click('#ci-beer button[data-k="bb"][data-s="1"]'); pg.click('#ci-beer button[data-k="wn"][data-s="1"]'); pg.click('#btn-save')
    e2 = ev("m => state.weeks[m]", mon)
    assert e2['beer'] == {'fp': 2, 'bd': 1, 'bb': 1, 'wn': 1} and e2['kcal'] == 2350 and e2['w'] == e['w'], e2
    assert not errs, errs
    ctx.close()

@test
def workout_rings_rules(b):
    ctx = new_context(b, FakeWorker())
    pg, errs = open_page(ctx, 'workout_dashboard.html'); pg.wait_for_timeout(500)
    parse = lambda lines: pg.evaluate("t => parseWorkoutText(t, '2026-10-20', 'Home')", "\n".join(lines))
    lines = ["* 12 PU RG", "* 10 PU Wide RG", "* 10 DOM RG", "* 8 DOM Austr RG", "* 6 BIC RG", "* 8 DIP RG",
             "* 5 DOM RG x3", "* 30s HOLD RG", "* 1m HOLD RG x2", "* 1m30s HOLD RG", "* 20 DOM", "* 15 PU"]
    r = parse(lines); s = r['session']
    plain = parse([l.replace(' RG', '') for l in lines if 'HOLD' not in l])['session']
    for k in ('pu', 'dom', 'dip', 'dwp', 'dad', 'dbc'):
        assert s[k] == plain[k], f'RG must not change {k}: {s[k]} vs {plain[k]}'
    got = (s.get('rpu'), s.get('rdom'), s.get('rdip'), s.get('rhold'))
    assert got == (22, 39, 8, 240), f'rings tallies wrong: {got}'
    assert r['unrecognized'] == [], f"RG lines flagged as unrecognized: {r['unrecognized']}"
    assert not any(k in plain for k in ('rpu', 'rdom', 'rdip', 'rhold')), 'sessions without rings must not store rings fields'
    pg.evaluate("s => { sessions = [s, {...s, d: '2026-10-21'}]; rebuildMonthly(); }", s)
    m = pg.evaluate("monthly[monthly.length - 1]")
    got = (m['rg_pu'], m['rg_dom'], m['rg_dip'], m['rg_hold'])
    assert got == (44, 78, 16, 480), f'monthly rings totals wrong: {got}'
    assert not errs, errs
    ctx.close()

@test
def fire_sim_rules(b):
    ctx = new_context(b, FakeWorker())
    pg, errs = open_page(ctx, 'FIRE_SIM.html'); pg.wait_for_timeout(500)
    ev = pg.evaluate
    ev("for (let i = 1; i <= RET_N; i++) { S['pea-r'+i] = 5; S['cto-r'+i] = 5; } S['cto-sync'] = 0; draw();")
    assert abs(ev("cagr('pea')") - 0.05) < 1e-9, 'equal periods must give that same CAGR'
    ev("for (let i = 1; i <= RET_N; i++) S['pea-r'+i] = i; draw();")
    m = ev("[rateFor('pea',0), rateFor('pea',4), rateFor('pea',5), rateFor('pea',39), rateFor('pea',40), rateFor('pea',43)]")
    assert m == [0.01, 0.01, 0.02, 0.08, 0.09, 0.09], f'year → period mapping wrong: {m}'
    ev("for (let i = 1; i <= RET_N; i++) S['pea-r'+i] = 5; S.startAge = 46; draw();")
    base, base_age = ev("cagr('pea')"), ev("shadowRun(0)")
    ev("S['pea-r1'] = -3; S['pea-r2'] = -3; draw();")
    crash, crash_age = ev("cagr('pea')"), ev("shadowRun(0)")
    assert crash < base, 'a crash period must lower the CAGR'
    assert (crash_age or 999) <= (base_age or 999), f'an early crash must not make capital last longer ({crash_age} vs {base_age})'
    ev("S['cto-sync'] = 1; draw();")
    assert ev("cagr('cto') === cagr('pea')"), 'CTO = PEA switch not applied'
    assert not errs, errs
    ctx.close()
    # Old saves with a single rate are migrated (local storage + scenarios)
    ctx = new_context(b, FakeWorker())
    ctx.add_init_script("""localStorage.setItem('fire-sim-v1', JSON.stringify({ S: { pea: 100, 'pea-r': 6, cto: 45, 'cto-r': 4 },
        scenarios: [{ name: 'old', snap: { S: { pea: 90, 'pea-r': 3, 'cto-r': 2 } }, meta: '' }, null, null] }));""")
    pg, errs = open_page(ctx, 'FIRE_SIM.html'); pg.wait_for_timeout(500)
    r = pg.evaluate("[S['pea-r1'], S['pea-r9'], S['cto-r5'], 'pea-r' in S, scenarios[0].snap.S['pea-r4']]")
    assert r == [6, 6, 4, False, 3], f'old single-rate save not migrated: {r}'
    assert not errs, errs
    ctx.close()


# ── Hub summaries (ROADMAP step 1) ──
import datetime
def periods_now():
    t = datetime.date.today(); mon = t - datetime.timedelta(days=t.weekday())
    return dict(today=t.isoformat(), week=mon.isoformat(), month=t.isoformat()[:7], year=t.isoformat()[:4])

def stats_text(pg, key): return pg.evaluate("id => document.getElementById(id).textContent", 'stats-' + key)

@test
def widgets_send_small_hub_summary(b):
    """Every widget's cloud save carries a `hub` object that fits Cloudflare's 1 KB metadata, without timestamps."""
    for f, w in widget_info().items():
        if not w['key'] or w['key'] == 'lukas-flow': continue      # LUKAS: only with live prices (own test)
        worker = FakeWorker(); ctx = new_context(b, worker)
        pg, errs = open_page(ctx, f)
        wait_until(pg, lambda: w['key'] in worker.keys('GET'))
        if f in SAVE_BY_CLICK: pg.click(SAVE_BY_CLICK[f])
        else: pg.evaluate('cloudSave()')
        assert wait_until(pg, lambda: w['key'] in worker.store, 6), f'{f}: no save'
        hub = worker.posted(w['key']).get('hub')
        assert isinstance(hub, dict), f'{f}: payload has no hub summary'
        size = len(json.dumps({'savedAt': '2026-10-20T10:00:00.000Z', 'hub': hub}, ensure_ascii=False, separators=(',', ':')).encode())
        assert size <= 1000, f'{f}: summary too big for KV metadata ({size} B)'
        assert not re.search(r'savedAt|updatedAt|exportedAt|"at"', json.dumps(hub)), f'{f}: timestamp inside hub breaks the unchanged-save check'
        assert not errs, f'{f}: {errs}'
        ctx.close()

@test
def hub_summary_values(b):
    P = periods_now()
    def saved(f, init, key, save='cloudSave()'):
        worker = FakeWorker(); ctx = new_context(b, worker); ctx.add_init_script(init)
        pg, errs = open_page(ctx, f); wait_until(pg, lambda: key in worker.keys('GET')); pg.wait_for_timeout(300)
        pg.evaluate(save)
        assert wait_until(pg, lambda: key in worker.store, 6), f'{f}: no save'
        assert not errs, f'{f}: {errs}'
        h = worker.posted(key)['hub']; ctx.close(); return h
    # Do it!: open items only, long text cut, everything escaped later by the Hub
    long = 'x' * 80
    h = saved('notes.html', "localStorage.setItem('doit_lcc_v1', JSON.stringify({postits:{today:{items:[{text:'A',done:false},{text:'B',done:true},{text:'%s',done:false}]},week:{items:[]},later:{items:[]}},notes:[]}));" % long, 'doit-lcc')
    assert h['nToday'] == 2 and h['today'][0] == 'A' and len(h['today'][1]) == 40 and h['week'] == [], h
    many = json.dumps([{'text': 'é' * 60 + str(i), 'done': False} for i in range(10)])
    h = saved('notes.html', "localStorage.setItem('doit_lcc_v1', JSON.stringify({postits:{today:{items:%s},week:{items:%s},later:{items:[]}},notes:[]}));" % (many, many), 'doit-lcc')
    assert len(json.dumps(h, ensure_ascii=False, separators=(',', ':')).encode()) <= 800 and h['nToday'] == 10, 'Do it! summary not trimmed to fit'
    # Xpenser: totals anchored to this week / month / year
    data = {P['today']: [{'amt': '10.5'}], P['month'] + '-01': [{'amt': '100'}], P['year'] + '-01-01': [{'amt': '1000'}], '2001-01-01': [{'amt': '5'}]}
    cf = {P['month']: {'salary': 2000, 'partner': '500'}}
    h = saved('XPENSE_TRACKER.html', "localStorage.setItem('depenses_data_v1', %s); localStorage.setItem('depenses_cashflow_v1', %s);" % (json.dumps(json.dumps(data)), json.dumps(json.dumps(cf))), 'xpenser')
    exp_m = 10.5 + (100 if P['month'] + '-01' != P['today'] else 0)
    assert (h['w'], h['m'], h['y'], h['mInc']) == (P['week'], P['month'], P['year'], 2500), h
    assert h['mT'] == exp_m and h['yT'] == exp_m + (1000 if P['year'] + '-01-01' < P['month'] + '-01' else 0), h
    # Workout: month count anchored, last session, best kcal
    h = saved('workout_dashboard.html', "localStorage.setItem('workout_insights_v3', JSON.stringify({sessions:[{d:'2026-01-02',dur:30,kcal:700},{d:'2026-03-04',dur:45,kcal:300}],monthly:[{month:'%s',sessions:7}],ts:Date.now()}));" % P['month'], 'workout')
    assert (h['m'], h['mN'], h['last'], h['dur'], h['best']) == (P['month'], 7, '2026-03-04', 45, 700), h
    # Kals: latest check-in week, last waist, trend weight; nothing for an empty state
    weeks = {'2026-01-05': {'w': {'2026-01-05': 80}, 'waist': 90}, '2026-01-12': {'w': {'2026-01-13': 80}, 'kcal': 2000}}
    h = saved('kals_track.html', "localStorage.setItem('kals_balance_v1', %s);" % json.dumps(json.dumps({'v': 2, 'settings': {}, 'weeks': weeks})), 'kalstrack')
    assert (h['wk'], h['wa'], h['tw'], h['v']) == ('2026-01-12', 90, 80, 'wait') and 'mt' not in h, h
    assert saved('kals_track.html', '', 'kalstrack') == {}, 'empty Kals must send an empty summary'

@test
def fire_sim_rules(b):
    ctx = new_context(b, FakeWorker())
    pg, errs = open_page(ctx, 'FIRE_SIM.html'); pg.wait_for_timeout(500)
    ev = pg.evaluate
    near = lambda a, e, what: abs(a - e) < 1e-6 * max(1, abs(e)) or (_ for _ in ()).throw(AssertionError(f'{what}: {a} ≠ {e}'))
    def setup(extra, p2s, p2i, cols=()):
        ev("""([x, p2s, p2i, cols]) => { Object.assign(S, x); ensureAllP2();
              for (let a = S.startAge + 1; a <= 59; a++) { S['p2s_' + a] = p2s; S['p2i_' + a] = p2i; }
              COL_ENTRIES.forEach(e => { document.getElementById('cb-' + e.id).checked = cols.includes(e.id); }); draw(); }""", [extra, p2s, p2i, list(cols)])
    base = {'pea': 120, 'cto': 40, 'av': 20, 'liv': 30, 'cash': 25, 'salary': 3000, 'lock': 2, 'infl': 3,
            'partner1': 150, 'spend2': 1900, 'side2': 300, 'partner2': 100, 'spend3': 1700, 'side3': 0, 'partner3': 0, 'retraite': 900,
            'are2': 1500, 'areM': 12, 'areDelay': 0, 'pensionAge': 65, 'per': 0}
    # One engine: break-even search, summary and table agree
    setup(base, 1800, 400)
    r = ev("() => ({ shadow: shadowRun(400), last: simulate().lastsUntil, shown: document.getElementById('m-age').textContent })")
    assert r['shadow'] == r['last'] and r['shown'] == (str(r['last']) if r['last'] else '90+'), r
    # Income grows with inflation like spending (amounts are in today's euros)
    rows = ev("() => allRows.map(x => ({ age: x.age, phase: x.phase, inflow: x.inflow, spend: x.spend, y: x.year - 1 }))")
    for x in rows:
        idx = 1.03 ** x['y']
        if x['phase'] == 2: near(x['inflow'], (400 + 150) * 12 * idx, f"P2 income at {x['age']}")
        if x['phase'] == 4: near(x['inflow'], (0 + 0 + 900) * 12 * idx, f"P4 income at {x['age']}")
    # Cash + Livrets run out while PEA/AV/CTO are locked: the lock is broken for what is missing
    # (flagged), spending is never dropped, and 'capital lasts' is the real depletion
    setup({**base, 'cash': 1, 'liv': 0, 'lock': 5, 'infl': 0}, 2500, 0)
    r = ev("""() => { const s = simulate(); return { last: s.lastsUntil, dep: s.depleteAge, early: s.earlyYears, sa: S.startAge,
             uncovered: s.rows.filter(x => x.shortfall > 0).map(x => x.total), shown: document.getElementById('m-short').textContent }; }""")
    assert r['early'] and r['early'][0]['age'] <= r['sa'] + 4 and r['shown'] != 'None', r
    assert r['last'] is None or r['last'] >= r['dep'] - 1, r
    assert all(t == 0 for t in r['uncovered']), f'spending left unpaid while capital remains: {r}'
    # Colombian assets are not locked: they pay before PEA/AV/CTO unlock
    setup({**base, 'cash': 0, 'liv': 0, 'lock': 10, 'infl': 0, 'cop1': 500}, 2500, 0, cols=('cop1',))
    srcs = ev("() => allRows[1].sources.map(s => s.label)")
    assert 'Fondo' in srcs and not ev("() => allRows[1].shortfall"), srcs
    # Withdrawal tax: gross-up so the net covers the need; tax = drawn × gain share × rate
    for rate in (0.186, 0.314):
        d = ev("r => drawFrom(1000, 10000, 4000, r)", rate)
        near(d['netReceived'], 1000, 'net covers the need'); near(d['taxPaid'], d['drawn'] * 0.6 * rate, 'tax on the gain share')
        near(d['newBasis'], 4000 - d['drawn'] * 0.4, 'basis reduced by the principal part')
    assert ev("() => [TAX.pea, TAX.cto]") == [0.186, 0.314], 'tax rates 2026'
    # Old saves load with identical results: ARE 6 × full + 6 × half, no latent gains, no PER, pension at 65
    m = ev("() => migrateState({ pea: 90, cto: 30, salary: 3000 })")
    assert (m['are2'], m['areM'], m['areDelay'], m['pea-basis'], m['cto-basis'], m['per'], m['pensionAge']) == (1500, 12, 0, 90, 30, 0, 65), m
    # ARE: month k of the entitlement is paid in month delay + k; months 1–6 at the first amount
    ev("() => Object.assign(S, { salary: 2700, are2: 2450, areM: 18, areDelay: 5 })")
    y0, y1, y2 = ev("() => [areInYear(0), areInYear(1), areInYear(2)]")
    assert (y0, y1, y2) == (6 * 2700 + 2450, 11 * 2450, 0), (y0, y1, y2)
    # Latent gains: only the share above what was paid in is taxed
    setup({**base, 'pea': 0, 'av': 0, 'liv': 0, 'cash': 0, 'lock': 0, 'infl': 0, 'cto': 100, 'cto-basis': 40, 'per': 0, 'salary': 0, 'are2': 0}, 2000, 0)
    r = ev("() => { const x = allRows[0]; return { tax: x.taxPaid, drawn: x.sources.reduce((t, s) => t + s.amount, 0), r: rateFor('cto', 0) }; }")
    near(r['tax'], r['drawn'] * (1 - 40 / (100 * (1 + r['r']) ** 0.5)) * 0.314, 'CTO tax on the gain share (drawn mid-year)')
    # PER: never drawn before the pension age, drawn after; phases follow the pension age
    setup({**base, 'pea': 0, 'cto': 0, 'av': 0, 'liv': 0, 'cash': 50, 'lock': 0, 'infl': 0, 'per': 100, 'pensionAge': 64, 'retraite': 500}, 2000, 0)
    rows = ev("() => allRows.map(x => ({ age: x.age, phase: x.phase, per: x.sources.some(s => s.label.startsWith('PER')), short: x.shortfall, inflow: x.inflow }))")
    assert not any(x['per'] for x in rows if x['age'] < 64) and any(x['per'] for x in rows if x['age'] >= 64), rows
    assert [x['phase'] for x in rows if x['age'] in (63, 64)] == [3, 4] and next(x for x in rows if x['age'] == 64)['inflow'] == 500 * 12 + 0, rows
    assert ev("() => simulate().lastsUntil") < 64, 'without accessible money before the pension age the plan must fail then, PER untouched'
    # Mid-year withdrawals: balance_end = (balance_start × √(1+r) − withdrawal) × √(1+r)
    zero = {k: 0 for k in ('pea', 'cto', 'av', 'cash', 'per', 'cop1', 'salary', 'are2', 'infl', 'lock', 'partner1')}
    setup({**base, **zero, 'liv': 100, 'liv-r': 4}, 1000, 0)
    near(ev("() => allRows[0].total"), (100000 * 1.04 ** 0.5 - 12000) * 1.04 ** 0.5, 'mid-year withdrawal')
    # Colombian assets in euros grow by (1 + COP return) / (1 + peso depreciation)
    setup({**base, **zero, 'liv': 0, 'cop1': 500, 'cop1-r': 5, 'cop-depr': 3}, 0, 0, cols=('cop1',))
    near(ev("() => allRows[0].total"), ev("() => 500e6 / COP_EUR") * 1.05 / 1.03, 'peso depreciation')
    assert not errs, errs
    ctx.close()

@test
def fire_live_cop_rate(b):
    ctx = new_context(b, FakeWorker())
    pg = ctx.new_page(); errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.route('**/api.frankfurter.dev/**', lambda r: r.fulfill(status=200, body='{"date":"2026-10-22","base":"EUR","quote":"COP","rate":4321.5}', headers=JSONH))
    pg.goto('file://' + os.path.join(ROOT, 'FIRE_SIM.html'))
    assert wait_until(pg, lambda: pg.evaluate('COP_EUR') == 4321.5), 'live COP rate (Frankfurter v2) not applied'
    assert 'fallback' not in pg.evaluate("document.getElementById('cop-rate-display').textContent")
    assert not errs, errs
    ctx.close()

@test
def fire_summary_matches_widget(b):
    worker = FakeWorker(); ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'FIRE_SIM.html'); wait_until(pg, lambda: 'fire-sim' in worker.keys('GET')); pg.wait_for_timeout(300)
    pg.evaluate("COP_EUR = 5000; Object.assign(S, {pea:100,av:10,cto:20,liv:5,cash:1,cop1:50,cop2:0,cop3:100,cop4:0}); document.getElementById('cb-cop1').checked = true; document.getElementById('cb-cop3').checked = false; cloudSave();")
    assert wait_until(pg, lambda: 'fire-sim' in worker.store, 6)
    h = worker.posted('fire-sim')['hub']
    assert (h['fr'], h['co'], h['total']) == (136000, 10000, 146000), f'FIRE summary must use live rate and only ticked assets: {h}'
    assert not errs, errs
    ctx.close()

def seed_hub(worker, P):
    """Summaries for every widget, as the Worker would return them from /hub."""
    at = '2026-10-20T08:00:00Z'
    hubs = {
        'doit-lcc': {'nToday': 2, 'nWeek': 0, 'today': ['<b>bold</b> & co', 'Call bank'], 'week': []},
        'xpenser': {'w': P['week'], 'wT': 42.5, 'm': P['month'], 'mT': 900, 'mInc': 1000, 'y': P['year'], 'yT': 9000},
        'xpenses-calculator': {'now': {'leftover': 850, 'net': 0, 'income': 0}, 'fire': {'leftover': -120, 'net': 0, 'income': 0}},
        'weekplanner': {'week': 1, 'hours': 4.5, 'free': 3},
        'fire-sim': {'total': 216667, 'fr': 150000, 'co': 66667, 'scen': 'Plan <A>'},
        'workout': {'m': P['month'], 'mN': 5, 'best': 520, 'last': '2026-10-18', 'dur': 45},
        'kalstrack': {'wk': '2026-10-12', 'v': 'good', 'vt': 'On <track>', 'tw': 74.2, 'wa': 86, 'mt': 2540},
        'lukas-flow': {'v': 123456, 'gl': -5432, 'glp': -4.2},
    }
    worker.meta = {k: {'savedAt': at, 'hub': h} for k, h in hubs.items()}

@test
def hub_renders_from_summaries_only(b):
    P = periods_now(); worker = FakeWorker(); seed_hub(worker, P)
    ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html')
    assert wait_until(pg, lambda: '850' in stats_text(pg, 'calc')), 'Hub did not render summaries'
    pg.wait_for_timeout(300)
    assert worker.keys('GET') == set(), f'Hub fetched full payloads although summaries exist: {worker.keys("GET")}'
    t = {k: stats_text(pg, k) for k in ('notes', 'tracker', 'calc', 'planner', 'fire', 'workout', 'kals', 'lukas')}
    assert '<b>bold</b> & co' in t['notes'] and not pg.query_selector('#stats-notes b'), 'Do it! text must be escaped'
    assert '43' in t['tracker'], t['tracker']
    assert '90% of income' in t['tracker'], t['tracker']
    assert 'Week 2' in t['planner'] and '4.5h' in t['planner'] and '3 free days' in t['planner'], t['planner']
    assert 'Plan <A>' in t['fire'] and not pg.query_selector('#stats-fire a'), t['fire']
    assert '5 sessions' in t['workout'] and '520 kcal' in t['workout'], t['workout']
    assert 'On <track>' in t['kals'] and '74,2 kg' in t['kals'] and '86,0 cm' in t['kals'] and '2.540 kcal' in t['kals'], t['kals']
    assert not pg.query_selector('#stats-kals track'), 'Kals verdict must be escaped'
    assert '-4.2%' in t['lukas'], t['lukas']
    foot = pg.evaluate("document.getElementById('footer-tracker').textContent")
    assert foot.startswith('Last logged'), f'footer must come from the cloud save time: {foot!r}'
    assert not errs, errs
    ctx.close()

@test
def hub_old_periods_and_old_kals_summary(b):
    P = periods_now(); worker = FakeWorker(); seed_hub(worker, P)
    worker.meta['kalstrack']['hub'] = {'budget': 2000, 'd': '2026-01-05', 'c': 900, 'b': 200, 'w': 1500}   # old day-by-day KALS
    worker.meta['xpenser']['hub'].update(w='2000-01-03', m='2000-01', y='2000')
    worker.meta['workout']['hub']['m'] = '2000-01'
    ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html')
    assert wait_until(pg, lambda: 'weekly check-in' in stats_text(pg, 'kals')), f'old Kals summary must show the start message: {stats_text(pg, "kals")}'
    tr = stats_text(pg, 'tracker').replace(' ', ' ').replace('\xa0', ' ')
    assert tr.count('0 €') == 3 and 'of income' not in tr, f'old-period totals must count as 0: {tr}'
    assert '0 sessions' in stats_text(pg, 'workout')
    assert not errs, errs
    ctx.close()

@test
def hub_falls_back_per_widget(b):
    P = periods_now(); worker = FakeWorker(); seed_hub(worker, P)
    del worker.meta['kalstrack']; worker.meta['weekplanner'] = {'savedAt': '2026-10-20T08:00:00Z'}   # saved before summaries existed
    worker.store['kalstrack'] = json.dumps({'v': 2, 'settings': {}, 'weeks': {}, 'hub': {'wk': '2026-10-12', 'v': 'wait', 'vt': 'Collecting data', 'mt': 2700}})
    worker.store['weekplanner'] = json.dumps({'curWeek': 0, 'weekPlaces': [{'a': {'d': 0, 'dur': 3}}], 'acts': [], 'weekNotes': []})
    ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html')
    assert wait_until(pg, lambda: '2.700 kcal' in stats_text(pg, 'kals')), stats_text(pg, 'kals')
    assert worker.keys('GET') == {'kalstrack', 'weekplanner'}, worker.keys('GET')
    assert '3h' in stats_text(pg, 'planner')
    assert pg.evaluate("document.getElementById('footer-planner').textContent").startswith('Last logged'), 'planner footer must use the cloud save time'
    assert not errs, errs
    ctx.close()

@test
def lukas_hub_saves_on_100_eur_or_daily(b):
    worker = FakeWorker(); ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'lukas_analyzer.html'); wait_until(pg, lambda: 'lukas-flow' in worker.keys('GET')); pg.wait_for_timeout(500)
    ev = pg.evaluate
    posts = lambda: len([1 for m, p, _ in worker.log if m == 'POST' and p == '/data/lukas-flow'])
    ev("localStorage.setItem('lukas_hub_last', JSON.stringify({v:10000, gl:500, glp:5, at: Date.now()}))")
    ev("pricedSnap = {totalValue: 10099.4, totalGL: 520, totalGLPct: 5.2}; maybeHubSave()")
    pg.wait_for_timeout(3600)
    assert posts() == 0, 'a move under €100 within 24 h must not save'
    ev("pricedSnap = {totalValue: 9900, totalGL: 400, totalGLPct: 4}; maybeHubSave()")
    assert wait_until(pg, lambda: posts() == 1, 6), 'a €100 move must save'
    assert worker.posted('lukas-flow')['hub'] == {'v': 9900, 'gl': 400, 'glp': 4}, worker.posted('lukas-flow')
    ev("localStorage.setItem('lukas_hub_last', JSON.stringify({v:9900, gl:400, glp:4, at: Date.now() - 25*3600*1000}))")
    ev("pricedSnap = {totalValue: 9910, totalGL: 410, totalGLPct: 4.1}; maybeHubSave()")
    assert wait_until(pg, lambda: posts() == 2, 6), 'after 24 h any change must save'
    ev("pricedSnap = null; localStorage.setItem('lukas_hub_last', JSON.stringify({v:1, gl:2, glp:3, at: 0}))")
    ev("updateCash(12345)")     # manual edit before prices loaded: keeps the last summary, never a partial one
    assert wait_until(pg, lambda: posts() == 3, 6), 'manual edit must save'
    assert worker.posted('lukas-flow')['hub'] == {'v': 1, 'gl': 2, 'glp': 3}, worker.posted('lukas-flow')
    assert not errs, errs
    ctx.close()


# ── Hub layout: order, pins, More, drag (ROADMAP step 3) ──
ORDER_JS = "[...document.querySelectorAll('#tiles .tile')].map(e => e.dataset.id)"
def seed_ordered(worker, P, layout=None, layout_at='2026-10-20T08:00:00Z'):
    seed_hub(worker, P)
    order = ['kalstrack', 'xpenser', 'workout', 'lukas-flow', 'fire-sim', 'xpenses-calculator', 'weekplanner', 'doit-lcc']
    for i, k in enumerate(order): worker.meta[k]['savedAt'] = '2026-10-20T%02d:00:00Z' % (12 - i)
    if layout is not None: worker.meta['hub-layout'] = {'savedAt': layout_at, 'hub': layout}

@test
def hub_order_pins_and_more(b):
    P = periods_now(); worker = FakeWorker(); seed_ordered(worker, P, {'pins': ['calc', 'notes'], 'more': ['planner', 'strat']})
    del worker.meta['weekplanner']['hub']                                   # More tile without summary
    worker.store['weekplanner'] = json.dumps({'curWeek': 0, 'weekPlaces': [{'a': {'d': 0, 'dur': 7}}], 'acts': [], 'weekNotes': []})
    ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html')
    expect = ['calc', 'notes', 'kals', 'tracker', 'workout', 'lukas', 'fire']
    assert wait_until(pg, lambda: pg.evaluate(ORDER_JS) == expect), f'order: {pg.evaluate(ORDER_JS)}'
    assert pg.evaluate("document.querySelector('#more-count').textContent") == '2'
    assert not pg.query_selector('#more-tiles .tile'), 'More must start collapsed'
    assert 'weekplanner' not in worker.keys('GET'), 'collapsed More tile must not be fetched'
    pg.click('#more-head')
    assert wait_until(pg, lambda: '7h' in (pg.evaluate("document.getElementById('stats-planner')?.textContent") or '')), 'More tile not loaded on expand'
    assert 'weekplanner' in worker.keys('GET')
    assert pg.evaluate("[...document.querySelectorAll('#more-tiles .tile')].map(e => e.dataset.id)") == ['planner', 'strat']
    assert pg.url.endswith('index.html'), 'More toggle must not navigate'
    assert not errs, errs
    ctx.close()

@test
def hub_pin_tap_never_opens_widget(b):
    P = periods_now(); worker = FakeWorker(); seed_ordered(worker, P)
    ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html')
    assert wait_until(pg, lambda: pg.evaluate(ORDER_JS)[0] == 'kals')
    pg.click('.tile-fire .tile-pin')
    pg.wait_for_timeout(200)
    assert pg.url.endswith('index.html'), f'pin tap navigated to {pg.url}'
    assert pg.evaluate(ORDER_JS)[0] == 'fire', 'pinned tile must go on top immediately'
    assert wait_until(pg, lambda: 'hub-layout' in worker.store, 4), 'layout not synced'
    assert worker.posted('hub-layout')['hub'] == {'pins': ['fire'], 'more': []}
    pg.click('.tile-fire .tile-pin')
    assert wait_until(pg, lambda: worker.posted('hub-layout')['hub']['pins'] == [], 4), 'unpin not synced'
    assert pg.evaluate(ORDER_JS)[0] == 'kals'
    writes = [1 for m, p, _ in worker.log if m == 'POST' and p == '/data/hub-layout']
    assert len(writes) == 2, f'expected one write per change, got {len(writes)}'
    assert not errs, errs
    ctx.close()

def long_drag(pg, src_sel, to):
    pg.evaluate("s => document.querySelector(s).scrollIntoView({block: 'center'})", src_sel); pg.wait_for_timeout(100)
    bx = pg.query_selector(src_sel).bounding_box()
    pg.mouse.move(bx['x'] + 40, bx['y'] + 40); pg.mouse.down(); pg.wait_for_timeout(650)
    pg.mouse.move(bx['x'] + 80, bx['y'] + 60, steps=4)
    to(); pg.mouse.up(); pg.wait_for_timeout(300)

@test
def hub_long_press_drag(b):
    P = periods_now(); worker = FakeWorker(); seed_ordered(worker, P, {'pins': ['notes'], 'more': []})
    ctx = new_context(b, worker)
    pg, errs = open_page(ctx, 'index.html'); pg.set_viewport_size({'width': 1070, 'height': 596})
    assert wait_until(pg, lambda: pg.evaluate(ORDER_JS)[0] == 'notes')
    # Workout → right half of the pinned Do it! → pinned after it
    def onto_notes_right():
        r = pg.query_selector('.tile-notes').bounding_box()
        pg.mouse.move(r['x'] + r['width'] * 0.8, r['y'] + r['height'] / 2, steps=8)
    long_drag(pg, '.tile-workout', onto_notes_right)
    assert pg.evaluate(ORDER_JS)[:2] == ['notes', 'workout'], pg.evaluate(ORDER_JS)
    assert pg.url.endswith('index.html'), 'drag opened the widget'
    # Kals → hold at the bottom edge (auto-scroll) → drop on More
    def to_more():
        for _ in range(40):
            pg.mouse.move(500, 590); pg.wait_for_timeout(50)
            r = pg.evaluate("(() => { const b = document.getElementById('more').getBoundingClientRect(); return b.top < innerHeight - 10 ? [b.left, b.top] : null; })()")
            if r: break
        assert r, 'auto-scroll did not reach the More drop zone'
        # The page keeps auto-scrolling while the finger is near the bottom edge, so follow the zone
        # until it is under the pointer (highlighted) — as a person would — then drop.
        for _ in range(40):
            box = pg.evaluate("(() => { const b = document.getElementById('more').getBoundingClientRect(); return [b.left + 40, Math.min(b.top + 20, innerHeight - 5)]; })()")
            pg.mouse.move(box[0], box[1], steps=2); pg.wait_for_timeout(80)
            if pg.evaluate("document.getElementById('more').classList.contains('drop-more')"):
                pg.wait_for_timeout(150)
                if pg.evaluate("document.getElementById('more').classList.contains('drop-more')"): break
        assert pg.evaluate("document.getElementById('more').classList.contains('drop-more')"), 'More drop zone never highlighted'
    long_drag(pg, '.tile-kals', to_more)
    assert 'kals' not in pg.evaluate(ORDER_JS) and pg.evaluate("document.querySelector('#more-count').textContent") == '1', \
        f'Kals not moved to More: {pg.evaluate(ORDER_JS)}'
    # Workout → onto an unpinned tile → unpinned (back to automatic order)
    def onto_unpinned():
        r = pg.query_selector('.tile-fire').bounding_box()
        pg.mouse.move(r['x'] + 40, r['y'] + 40, steps=8)
    long_drag(pg, '.tile-workout', onto_unpinned)
    assert pg.evaluate(ORDER_JS)[0] == 'notes' and pg.evaluate(ORDER_JS).index('workout') > 1
    assert wait_until(pg, lambda: worker.posted('hub-layout') and worker.posted('hub-layout')['hub'] == {'pins': ['notes'], 'more': ['kals']}, 4), worker.posted('hub-layout')
    # A short tap still opens the widget
    pg.evaluate("document.querySelector('.tile-fire').scrollIntoView({block: 'center'})")
    pg.click('.tile-fire .tile-name'); pg.wait_for_url('**/FIRE_SIM.html', timeout=4000)
    assert not errs, errs
    ctx.close()

@test
def hub_layout_newest_copy_wins(b):
    P = periods_now()
    for local_at, cloud_at, want in (('2026-10-20T09:00:00Z', '2026-10-20T08:00:00Z', ['tracker']),
                                     ('2026-10-20T07:00:00Z', '2026-10-20T08:00:00Z', ['lukas'])):
        worker = FakeWorker(); seed_ordered(worker, P, {'pins': ['lukas'], 'more': []}, cloud_at)
        ctx = new_context(b, worker)
        ctx.add_init_script("localStorage.setItem('lifehub_layout', JSON.stringify({pins:['tracker'],more:[],at:'%s'}))" % local_at)
        pg, errs = open_page(ctx, 'index.html')
        assert wait_until(pg, lambda: pg.evaluate(ORDER_JS)[:1] == want), f'{local_at} vs {cloud_at}: {pg.evaluate(ORDER_JS)}'
        pg.wait_for_timeout(300)
        assert pg.evaluate(ORDER_JS)[:1] == want
        assert not errs, errs
        ctx.close()


# ── Leaving a widget right after an edit (bug 2026-10-21: edit not in the Hub / lost on reopen) ──
DEBOUNCED = {'notes.html': "scheduleCfSave()", 'XPENSES_CALCULATOR.html': "cloudSave()", 'FIRE_SIM.html': "cloudSaveDebounced()",
             'workout_dashboard.html': "cloudSave()", 'kals_track.html': "scheduleCloudsave()",
             'lukas_analyzer.html': "debouncedCloudSave()", 'WEEK_PLANNER.html': "#clearbtn"}

# Over http like the real site: under file:// Chromium sometimes does not show the next page a
# localStorage write made just before navigating (measured ~1/30), which made these tests flaky.
@test
def edit_then_leave_at_once_reaches_cloud(b):
    keys = {f: w['key'] for f, w in widget_info().items()}
    assert set(DEBOUNCED) | {'XPENSE_TRACKER.html'} == {f for f, k in keys.items() if k}, 'new widget with cloud sync: add it to DEBOUNCED'
    with Server() as srv:
      for f, trig in DEBOUNCED.items():
        worker = FakeWorker(); ctx = new_context(b, worker)
        pg, errs = open_page(ctx, srv.base + f)
        assert wait_until(pg, lambda: keys[f] in worker.keys('GET')), f'{f}: no load'
        pg.wait_for_timeout(300)
        if trig.startswith('#'): pg.click(trig)
        else: pg.evaluate(trig)                                   # a save is now waiting ~3 s
        assert pg.evaluate(f"localStorage.getItem('lifesync_pending_{keys[f]}')") is not None, f'{f}: edit not kept as pending at once'
        pg.wait_for_timeout(300)
        pg.goto(srv.base + 'index.html')                          # the owner taps the Hub icon
        assert wait_until(pg, lambda: keys[f] in worker.keys('POST'), 5), f'{f}: edit lost when leaving before the save delay'
        assert not errs, f'{f}: {errs}'
        ctx.close()

@test
def edit_left_offline_is_kept_and_pushed(b):
  with Server() as srv:
    worker = FakeWorker(); ctx = new_context(b, worker)
    pg, errs = open_page(ctx, srv.base + 'XPENSES_CALCULATOR.html'); wait_until(pg, lambda: 'xpenses-calculator' in worker.keys('GET'))
    pg.evaluate("states.now.salary = 1000; saveLocal('now'); render('now')")
    assert wait_until(pg, lambda: (worker.posted('xpenses-calculator') or {}).get('now', {}).get('salary') == 1000, 6)
    pg.evaluate("states.now.salary = 7777; saveLocal('now'); render('now')")
    worker.offline = True                                        # the request made while leaving never arrives
    pg.click('a.home-link'); pg.wait_for_url('**/index.html'); pg.wait_for_timeout(800)
    # The tile shows this device's newer summary (salary 7777), not the synced one (salary 1000) — no reliance on defaults
    local = pg.evaluate("LifeSync.localHub('xpenses-calculator').hub.now.leftover")
    assert local != worker.posted('xpenses-calculator')['hub']['now']['leftover'], 'local summary is not the newer edit'
    assert pg.evaluate("v => fmt(v)", local) in stats_text(pg, 'calc'), \
        'Hub must show the unsynced edit from this device'
    worker.offline = False
    pg.goto(srv.base + 'XPENSES_CALCULATOR.html'); pg.wait_for_timeout(1500)
    assert pg.evaluate("states.now.salary") == 7777, 'reopening the widget loaded the older cloud copy over the edit'
    assert worker.posted('xpenses-calculator')['now']['salary'] == 7777, 'edit not pushed after reconnect'
    assert not errs, errs
    ctx.close()

@test
def hub_prefers_newer_summary_of_this_device(b):
    P = periods_now()
    for local_at, want, other in (('2026-10-20T09:00:00Z', '999', '111'), ('2026-10-20T07:00:00Z', '111', '999')):
        worker = FakeWorker(); seed_hub(worker, P)
        worker.meta['xpenses-calculator'] = {'savedAt': '2026-10-20T08:00:00Z', 'hub': {'now': {'leftover': 111}, 'fire': {'leftover': 0}}}
        ctx = new_context(b, worker)
        ctx.add_init_script("localStorage.setItem('lifesync_hub_xpenses-calculator', JSON.stringify({savedAt:'%s', hub:{now:{leftover:999},fire:{leftover:0}}}))" % local_at)
        pg, errs = open_page(ctx, 'index.html')
        assert wait_until(pg, lambda: not pg.query_selector('.tile.loading') and want in stats_text(pg, 'calc')), f'{local_at}: {stats_text(pg, "calc")}'
        pg.wait_for_timeout(300)
        assert want in stats_text(pg, 'calc') and other not in stats_text(pg, 'calc'), stats_text(pg, 'calc')
        assert not errs, errs
        ctx.close()


# ───────────────────────── runner ─────────────────────────
def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ''
    chart_js()
    failed = 0
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for t in TESTS:
            if only and only not in t.__name__: continue
            s = time.time()
            try:
                t(browser); print(f'  ✓ {t.__name__}  ({time.time() - s:.1f}s)')
            except Exception as e:
                failed += 1
                msg = str(e) or e.__class__.__name__
                print(f'  ✗ {t.__name__}\n      {msg}')
                if not isinstance(e, AssertionError): traceback.print_exc(limit=3)
        browser.close()
    if not only or 'worker' in only:
        r = subprocess.run(['node', os.path.join(ROOT, 'tests', 'worker.test.mjs')])
        failed += r.returncode != 0
    print(f"\n{'FAILED' if failed else 'ALL PASSED'} — {failed} failing, {time.time() - t0:.0f}s")
    sys.exit(1 if failed else 0)

if __name__ == '__main__':
    main()
