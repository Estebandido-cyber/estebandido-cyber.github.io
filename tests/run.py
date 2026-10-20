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
    def __init__(self): self.store, self.log, self.offline, self.max_body = {}, [], False, None
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
                return route.fulfill(status=200, body='OK', headers=CORS)
            return route.fulfill(status=200, body=self.store.get(k, '{}'), headers=JSONH)
        return route.fulfill(status=200, body='{}', headers=JSONH)   # /price etc.
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
        other = (worker.keys('GET') | worker.keys('POST')) - {w['key']}
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
    for tab in ('now', 'fire'):
        # Own inputs: every row gets a distinct value (rows are discovered, so new rows are covered automatically)
        pg.evaluate("""tab => {
            const s = states[tab];
            SHARED_DEF.forEach((d, i) => s[d.key] = 100 + 10 * i);
            PERSONAL_DEF.forEach((d, i) => s[d.key] = 12 + 12 * i);
            Object.assign(s, { sharedAcc: 1000, salary: 2000, tickets: 200, mel: 41 });
            render(tab); }""", tab)
        r = pg.evaluate("""tab => {
            const s = states[tab], d = derived(s), g = id => document.getElementById(tab + '-' + id).textContent;
            return { s, d,
              shared:   SHARED_DEF.map(x => ({ v: s[x.key], shown: g('amt-' + x.key) })),
              personal: PERSONAL_DEF.map(x => ({ v: s[x.key], yearly: !!x.yearly })),
              donut:    charts[tab].data.datasets[0].data.reduce((a, b) => a + b, 0),
              show: { gross: g('m-gross'), left: g('m-leftover'), year: g('m-year'), pool: g('sa-total'), saLeft: g('sa-left'), half: g('half-shared') },
              fmt: { gross: fmt(d.gross), left: fmt(d.leftover), year: fmt(d.leftover * 12), pool: fmt(d.sharedPool),
                     saLeft: (d.sharedLeft < 0 ? '−' : '') + fmt(Math.abs(d.sharedLeft)), half: fmtSmart(d.sharedTotal / 2) },
              halves: SHARED_DEF.map(x => fmtSmart(s[x.key] / 2)) }; }""", tab)
        s, d = r['s'], r['d']
        shared_total = sum(x['v'] for x in r['shared'])
        personal = sum(x['v'] / 12 if x['yearly'] else x['v'] for x in r['personal'])
        approx = lambda a, e, what: abs(a - e) < 1e-6 or (_ for _ in ()).throw(AssertionError(f'[{tab}] {what}: {a} ≠ {e}'))
        approx(d['sharedTotal'], shared_total, 'shared total = sum of shared rows')
        approx(d['gross'], shared_total / 2 + personal, 'Your expenses = half of shared + personal')
        approx(d['income'], s['salary'] + s['tickets'] + s['mel'], 'income counts the partner (Mel) once')
        approx(d['leftover'], d['income'] - d['gross'], 'left at end of month = income − expenses')
        approx(d['sharedPool'], 2 * s['sharedAcc'], 'Revolut pot = 2 × each contribution')
        approx(d['sharedLeft'], d['sharedPool'] - shared_total, 'left in Revolut = pot − shared bills')
        approx(r['donut'], d['gross'], 'donut adds up to Your expenses')
        for k in ('gross', 'left', 'year', 'pool', 'saLeft', 'half'):
            assert r['show'][k] == r['fmt'][k], f'[{tab}] displayed {k} {r["show"][k]!r} ≠ {r["fmt"][k]!r}'
        assert [x['shown'] for x in r['shared']] == r['halves'], f'[{tab}] "Your half" column wrong'
        # Mel must not change expenses; a shared stepper must move "Left at end of month" by half its step
        before = pg.evaluate("tab => derived(states[tab])", tab)
        pg.evaluate("tab => { states[tab].mel += 100; render(tab); }", tab)
        after = pg.evaluate("tab => derived(states[tab])", tab)
        approx(after['gross'], before['gross'], 'partner contribution must not change expenses')
        first = pg.evaluate("() => SHARED_DEF[0]")
        lb = pg.evaluate("tab => derived(states[tab]).leftover", tab)
        pg.evaluate("tab => switchTab(tab)", tab)
        pg.locator(f"#{tab}-spin-{first['key']} button").last.click()
        la = pg.evaluate("tab => derived(states[tab]).leftover", tab)
        approx(lb - la, first['step'] / 2, 'a shared stepper moves Left at end of month by half its step')
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
