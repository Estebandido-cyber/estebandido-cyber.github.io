# Life Hub

Personal dashboard of self-contained HTML widgets, served by GitHub Pages at
**https://estebandido-cyber.github.io/** and installable as a PWA.

Each widget is one `.html` file (markup + CSS + JS inline). Shared plumbing lives in `lib/`.
Data is stored locally (localStorage), synced automatically to a Cloudflare Worker (KV),
and can be backed up manually to Google Drive.

## Widgets

| Widget | File | Cloud key | Drive backup file | Main localStorage key |
|---|---|---|---|---|
| Hub | `index.html` | reads all keys below | — | `lifehub_theme` |
| Life Command Center (DOit!) | `notes.html` | `doit-lcc` | `doit_lcc_data.json` | `doit_lcc_v1` |
| Xpenser Tracker | `XPENSE_TRACKER.html` | `xpenser` | `xpenser_data.json` | `depenses_data_v1` (+ `_settings_v1`, `_cashflow_v1`, `_notes_v1`) |
| Workout Insights | `workout_dashboard.html` | `workout` | `workout_insights_data.json` | `workout_insights_v3` |
| KALS Track | `kals_track.html` | `kalstrack` | `kalstrack_data.json` | `kals_settings`, `kals_recaps`, `kals_day_*` |
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
| `sync.js` | Cloudflare Worker client. | `LifeSync.cloud(key).get()` / `.put(obj)` (throw on error), `LifeSync.tryGet(key)` (null on error), `LifeSync.request(path, init)`, `LifeSync.debounce(fn, ms)`, `LifeSync.time()` |
| `drive.js` | Google Drive manual backup (Google Identity Services). | `LifeDrive.create({ filename, status, busy, payload, apply, types }).save() / .load()` |

Load order in `<head>`: `sync-auth.js` → `sync.js` → `drive.js` (only if the widget has Drive buttons).

## Backend (`worker/widgets-sync.js`)

Cloudflare Worker `widgets-sync` (`https://widgets-sync.estebandido80.workers.dev`), bound to KV namespace `WIDGET_DATA`.

- `GET /data/<key>` → stored JSON (`{}` if empty) · `POST /data/<key>` → store body (max 1 MB)
- `GET /price?ticker=XXX` → Yahoo Finance chart JSON, 5-min edge cache (used by LUKAS)
- **Daily backups**: the first save of each day (Paris time) copies the previous value to `bak:<key>:<YYYY-MM-DD>`, kept 30 days. Read-only: `GET /backups/<key>` lists dates, `GET /backups/<key>/<date>` returns that copy.
- Auth: header `X-Secret` must equal the Worker secret `SYNC_SECRET` (Settings → Variables and Secrets). **The secret is never committed.**
- CORS: only `https://estebandido-cyber.github.io`

The file in this repo is the source of truth; deploy by pasting it into the Cloudflare editor.

## Hub ↔ widget data

The Hub fetches every cloud key (falling back to localStorage) and renders summary tiles.
Some widgets also write a small summary for the Hub, e.g. Xpenses Calculator → `xpenses_calculator_outputs`
(`{ now|fire: { leftover, net, income } }`).

## External services

- Chart.js 4.4.x (cdnjs / jsDelivr) · Google Fonts
- Frankfurter API (ECB rates, COP/EUR) — no key
- Google Drive API (`drive.file` scope), OAuth client ID in `lib/drive.js` — public by design; only works from the authorized JavaScript origins set in Google Cloud Console

## Adding a widget

1. Create `my_widget.html`. In `<head>` add `<script src="lib/sync-auth.js"></script>` and `<script src="lib/sync.js"></script>` (+ `lib/drive.js` and the GIS script if it needs Drive backup).
2. Pick a cloud key matching `^[a-z0-9-]{1,40}$` and use `const CLOUD = LifeSync.cloud('my-widget')`.
3. Save locally first, then debounce `CLOUD.put(state)` (~3 s). On load, `await CLOUD.get()` and apply if non-empty.
4. Add a tile in `index.html` and a `cfFetch('my-widget')` call in the Hub loader.
5. Add a row to the table above.
