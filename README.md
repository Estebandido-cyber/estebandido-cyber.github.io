# Life Hub

Personal dashboard of self-contained HTML widgets, served by GitHub Pages at
**https://estebandido-cyber.github.io/** and installable as a PWA.

Each widget is one `.html` file (markup + CSS + JS inline). Shared plumbing lives in `lib/`.
Data is stored locally (localStorage), synced automatically to a Cloudflare Worker (KV),
and can be backed up manually to Google Drive.

## Widgets

| Widget | File | Cloud key | Drive backup file | Main localStorage key |
|---|---|---|---|---|
| Hub | `index.html` | `hub-layout` (+ reads `GET /hub`) | — | `lifehub_theme`, `lifehub_layout`, `lifehub_order` |
| Life Command Center (DOit!) | `notes.html` | `doit-lcc` | `doit_lcc_data.json` | `doit_lcc_v1` |
| Xpenser Tracker | `XPENSE_TRACKER.html` | `xpenser` | `xpenser_data.json` | `depenses_data_v1` (+ `_settings_v1`, `_cashflow_v1`, `_notes_v1`) |
| Workout Insights | `workout_dashboard.html` | `workout` | `workout_insights_data.json` | `workout_insights_v3` |
| KALS Balance | `kals_track.html` | `kalstrack` (+ reads `workout`) | `kalstrack_data.json` | `kals_balance_v1` (+ `kals_balance_range`) |
| LUKAS Flow | `lukas_analyzer.html` | `lukas-flow` | `portfolio_dashboard_data.json` | `lukas_flow_snapshot` |
| Xpenses Calculator | `XPENSES_CALCULATOR.html` | `xpenses-calculator` | `xpenses_calculator_data.json` | `xpenses_calculator_v1` |
| FIRE Sim | `FIRE_SIM.html` | `fire-sim` | `fire-sim-data.json` | `fire-sim-v1` |
| Week Plan | `WEEK_PLANNER.html` | `weekplanner` | `weekplan_data.json` | `weekplan_acts`, `weekplan_week0`–`3`, `weekplan_notes`, `weekplan_curweek` |
| FIRE Strategy | `FIRE_strat.html` | — (static) | — | — |

**Do not rename cloud keys or Drive filenames** — existing data and backups are found by these exact names.

## Shared libraries (`lib/`)

| File | Purpose | API |
|---|---|---|
| `sync-auth.js` | Sync key, entered once per browser, stored in localStorage (`lifehub_sync_secret`). Clears itself on HTTP 401. | `getSyncSecret()`, `resetSyncSecret()` |
| `pwa.js` | Registers the offline service worker `sw.js`. Per-device switch: `?nosw` turns offline mode off (and clears caches), `?sw` turns it back on. | — |
| `sync.js` | Cloudflare Worker client. Uploads are kept as `lifesync_pending_<key>` from the moment they are staged/sent until the Worker confirms them and pushed first on the next `get()` or when the browser goes back online, so offline edits are never overwritten by older cloud data. Saves whose content is unchanged since the last sync (ignoring `savedAt`/`exportedAt`/`updatedAt`) are skipped (`lifesync_synced_<key>` fingerprint), so opening a widget never uploads a stale copy. A banner warns when a widget's data passes 70% of the 5 MB limit, or if a save is refused as too large. | `LifeSync.cloud(key).get()` / `.put(obj)` (throw on error) / `.stage(obj)` (at edit time: keep as pending + local Hub summary), `LifeSync.onLeave(fn)` (pagehide / tab hidden), `LifeSync.localHub(key)`, `LifeSync.tryGet(key)` (null on error), `LifeSync.request(path, init)`, `LifeSync.debounce(fn, ms)`, `LifeSync.time()` |
| `drive.js` | Google Drive manual backup (Google Identity Services). | `LifeDrive.create({ filename, status, busy, payload, apply, types }).save() / .load()` |

Load order in `<head>`: `pwa.js` → `sync-auth.js` → `sync.js` → `drive.js` (only if the widget has Drive buttons).

## Offline (`sw.js`)

Same-origin files are **network-first** (a deploy is visible on the next normal reload; the cached copy is only used offline). Versioned CDN files and Google Fonts are cache-first. Cloud sync, Google APIs, prices and FX rates are never cached. Bump `VERSION` in `sw.js` to drop every device's cache.

## Backend (`worker/widgets-sync.js`)

Cloudflare Worker `widgets-sync` (`https://widgets-sync.estebandido80.workers.dev`), bound to KV namespace `WIDGET_DATA`.

- `GET /data/<key>` → stored JSON (`{}` if empty) · `POST /data/<key>` → store body (max 5 MB; `LifeSync.LIMIT` in `lib/sync.js` must match)
- `GET /hub` → `{ <key>: { savedAt, hub } }` for every widget, in one small response (Hub tiles). Built from **KV metadata**: on each `POST /data/<key>` whose body has a top-level `hub` object, the Worker stores `{ savedAt, hub }` as metadata on the same put (no extra write). Metadata max 1 KB; bigger summaries, non-JSON bodies or bodies > 1 MB are saved with `savedAt` only. `list()` is eventually consistent (can lag a save by ~60 s).
- `GET /price?ticker=XXX` → Yahoo Finance chart JSON, 5-min edge cache (used by LUKAS)
- **Daily backups**: the first save of each day (Paris time) copies the previous value to `bak:<key>:<YYYY-MM-DD>`, kept 30 days. Read-only: `GET /backups/<key>` lists dates, `GET /backups/<key>/<date>` returns that copy.
- Auth: header `X-Secret` must equal the Worker secret `SYNC_SECRET` (Settings → Variables and Secrets). **The secret is never committed.**
- CORS: only `https://estebandido-cyber.github.io`

The file in this repo is the source of truth; deploy by pasting it into the Cloudflare editor.

## Hub ↔ widget data

Each widget adds a small `hub` object to its cloud payload (`hubSummary()` in the widget): computed numbers
only, **no timestamps inside** (they would defeat the unchanged-save check in `lib/sync.js`), ≤ ~800 bytes.
Period values are anchored to their period (`{ w: <Monday>, wT, m: 'YYYY-MM', mT, … }`) so the Hub shows 0
for a past week/month instead of stale totals.

**Hub layout** (`hub-layout` = `{ savedAt, pins, more, hub: { pins, more } }`): 📌 pinned tiles first in pin order,
then the rest by last save (newest first), re-ordered only on load/Refresh or by the owner's own actions.
Long-press (~0.45 s) then drag: onto a pinned tile → pinned at that spot; onto an unpinned tile → unpinned;
onto "More" → collapsed section at the bottom, whose tiles get no full-payload fetch until expanded. The layout
arrives through `/hub` (its `hub` copy is the KV metadata); the newer of cloud and this device's copy wins.

The Hub calls `GET /hub` once and paints every tile from the summaries. For a widget that has not saved a
summary yet it falls back to the old path (`cfFetch(key)` full payload, then localStorage). Pending offline
edits are flushed first (`LifeSync.flushPending()`).

| Widget | `hub` fields |
|---|---|
| Do it! | `nToday`, `nWeek`, `today[]`, `week[]` (≤ 5 open items each, ≤ 40 chars, trimmed to fit) |
| Xpenser | `w`, `wT`, `m`, `mT`, `mInc`, `y`, `yT` |
| Xpenses Calculator | `now`/`fire`: `{ leftover, net, income }` |
| Week Plan | `week`, `hours`, `free` (weekdays with < 2 h) |
| FIRE Sim | `total`, `fr`, `co` (live COP/EUR, ticked Colombian assets only), `scen` |
| Workout | `m`, `mN`, `last`, `dur`, `best` |
| Kals | `wk` (Monday of the latest check-in), `v`/`vt` (verdict code/label), `tw` (trend kg), `wa` (waist cm), `mt` (maintenance kcal) |
| LUKAS | `v`, `gl`, `glp` — only from fully priced renders; after a price refresh saved only if the value moved ≥ €100 since this device's last save (`lukas_hub_last`) or that save is > 24 h old and something changed |

## External services

- Chart.js 4.4.x (cdnjs / jsDelivr) · Google Fonts
- Frankfurter API (ECB rates, COP/EUR) — no key
- Google Drive API (`drive.file` scope), OAuth client ID in `lib/drive.js` — public by design; only works from the authorized JavaScript origins set in Google Cloud Console

## Tests

`python3 tests/run.py` — full suite (~1 min, mocked network, needs Python Playwright + Node). See CLAUDE.md.

## Adding a widget

1. Create `my_widget.html`. In `<head>` add `<script src="lib/sync-auth.js"></script>` and `<script src="lib/sync.js"></script>` (+ `lib/drive.js` and the GIS script if it needs Drive backup).
2. Pick a cloud key matching `^[a-z0-9-]{1,40}$` and use `const CLOUD = LifeSync.cloud('my-widget')`.
3. Save locally first, then debounce `CLOUD.put(cloudPayload())` (~3 s). When scheduling the debounced upload call `CLOUD.stage(cloudPayload())`, and add `LifeSync.onLeave(() => { if (timer) { clearTimeout(timer); timer = null; cloudSave(); } })` so tapping the Hub icon right after an edit never loses it. On load, `await CLOUD.get()` and apply if non-empty.
4. Add one entry to `TILES` in `index.html` (id, name, icon, file, cloud key + `hub`/`full`/`paint` functions) and a `.tile-<id>` accent in its CSS. Have the widget send a `hub` summary (see Hub ↔ widget data).
5. Add a row to the table above.
