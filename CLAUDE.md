# CLAUDE.md — working on Life Hub

Read `README.md` first for the widget map, cloud keys, Drive filenames and `lib/` APIs, and `ROADMAP.md` for agreed upcoming work.
This repo is published as-is by GitHub Pages: **everything committed is public**.

## Owner preferences
- Replies in the language the owner writes in (usually Spanish). Code, comments and commits in English.
- Direct and concise. No fluff.
- **Analyze before building.** For anything non-trivial, restate the request, flag ambiguities and trade-offs, and wait for confirmation. Small explicit edits ("add a row X, default 20, step 1") can go straight through.
- **Surgical changes only.** Touch what was asked; don't reformat or "clean up" unrelated code.
- **Bug-first:** when something looks wrong, find the cause in the code before blaming cache or deployment. If behavior is by design, say so and offer options.
- Same change in every place it applies (e.g. Xpenses Calculator has two tabs, Now and FIRE, built from one template — check both).
- Numbers: euros, European formatting (`€1.234`, `€7,50`).

## Hard rules
- **Never commit secrets.** The sync key lives only in the Cloudflare secret `SYNC_SECRET` and each browser's localStorage. Grep for it before committing.
- **Never change cloud keys or Drive filenames** (see README table) — data is looked up by name.
- **Never write to the real Worker/KV from tests.** Route `**/widgets-sync.*/**` to a mock in every browser test.
- When changing a saved data shape, add a migration so old localStorage, cloud and Drive payloads still load (see `migrateReturns` in `FIRE_SIM.html`). Unknown keys must not break loading.
- Every page must include `<script src="lib/pwa.js"></script>` first in `<head>`. Never make same-origin files cache-first in `sw.js` (a deploy must never hide behind a stale cache). In browser tests over http, set `PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS=1` so `context.route` also mocks requests made by the service worker.
- Keep each widget self-contained: shared code goes in `lib/`, loaded via `<script src="lib/...">` in `<head>`. No build step, no framework.

## Workflow
1. `git pull` before every change (the owner may edit on GitHub or other devices).
2. Edit with exact-match replacements; assert each match is unique.
3. Test (below). Compare against `git show HEAD:<file>` when the change must not alter results.
4. Commit with a descriptive message and push to `main`. Pages deploys in ~1 min; tell the owner to hard-reload (Ctrl/Cmd+Shift+R).

## Testing
**Run `python3 tests/run.py` before every push — it must end with `ALL PASSED`.** (~1 min; `python3 tests/run.py xpenses` runs a subset.)
- `tests/run.py`: Playwright checks (all pages load, cloud sync per widget, wrong key, Drive backup, offline edits, service worker, Xpenses and FIRE calculation rules). `tests/worker.test.mjs`: Worker unit tests.
- Network is fully mocked by one catch-all route; the real Worker/Google/CDNs are never contacted. Chart.js is fetched once into `tests/.cache` (gitignored).
- Tests use their own inputs and check rules, not defaults or labels. Adding/renaming rows or changing defaults needs no test change; changing how money is calculated does — update the rule in the same commit and say so in the commit message.
- New feature with math → add a rule test. For layout changes screenshot at the owner's real viewports: **tablet 1070×596 landscape (main)** and ~764×900 portrait (OnePlus Pad Go), **phone 411×784** (Pixel 8), desktop ≥ 1200 px — light and dark. The tablet is only 596 px tall in landscape.
- In the harness, wait with `wait_until(pg, …)` / `pg.wait_for_timeout`, never `time.sleep` (it freezes the mocked network).
- Tests that navigate between pages and rely on localStorage must run over http (`with Server() as srv`), not `file://`: under file:// Chromium sometimes hides a write made just before navigating from the next page (~1/30), which makes tests flaky.
- Check new tests for flakiness: run them 10–20× in a loop before pushing.

## Widget notes
- **FIRE_SIM.html** — state `S`, limits `LIM`, render `draw()`, save `saveState()`. PEA/CTO returns are 9 five-year periods `pea-r1..9`, `cto-r1..9` (`cto-sync` copies PEA); horizon 44 years; summary uses CAGR via `cagr()`. Old `pea-r`/`cto-r` migrated by `migrateReturns()`. The projection lives ONLY in `simulate(p2Side)` (table, chart, summary and break-even all use it): monthly amounts are today's euros and both spending and income (side, partner, pension) grow with inflation; if Cash + Livrets (+ ticked 🇨🇴) run out while PEA/AV/CTO are locked, those pay what is missing anyway, flagged "(early)" (`earlyYears`); spending is never dropped. `lastsUntil` = depletion (first year spending can't be paid because the capital is gone). Lock applies to PEA/AV/CTO only. ARE by month via `areInYear(y)` (`salary` = months 1–6 net, `are2` after, `areM` months, `areDelay` différé); cost basis `pea-basis`/`cto-basis`; `per` locked until `pensionAge` (never early); phases P3 = 60…pensionAge−1, P4 = pensionAge+. Each year: half-year growth → withdrawals → half-year growth (mid-year draws). 🇨🇴 grows in EUR by (1+COP return)/(1+`cop-depr`). Live COP rate from Frankfurter **v2** (`api.frankfurter.dev/v2/rate/eur/cop`; v1 is ECB-only, no COP). `draw(true)` = silent (no toast) for programmatic redraws. Old saves upgraded by `migrateState()` to values that keep results identical.
- **XPENSES_CALCULATOR.html** — `states.now` / `states.fire` merged over `DEFAULTS`; rows come from `SHARED_DEF` / `PERSONAL_DEF` (adding a row = one def line + default in both tabs + a donut category in `CHART_CATS`). Shared expenses are full bills paid from the joint Revolut pot (2 × `sharedAcc`); "Your expenses" = half of shared + personal. Partner's contribution is income (`mel`). Donut must sum to `derived().gross`. Breakdown (`TRACKER_GROUPS`, amounts = owner's part): a row with `to: '<line key>'` feeds that line via `syncLinked()` (run at the start of `render` and `hubSummary`): Shared line = 2 × sum (50/50), Personal line = sum; linked lines have no stepper. Rows without `to` are info only.
- **workout_dashboard.html** — sessions parsed from pasted calendar text by `parseWorkoutText` → `parseLineExercises` → `classifyExercise`. Rings: an `RG` tag on a PU/DOM/BIC/DIP line keeps its normal bucket/variant and also adds to `rpu`/`rdom`/`rdip`; `N s/m HOLD RG` adds seconds to `rhold`. Rings fields are stored only when > 0; monthly totals `rg_pu/rg_dom/rg_dip/rg_hold`.
- **kals_track.html** (KALS Balance) — one weekly check-in per Monday key in `state.weeks` (`w` = {date: kg}, `kcal`, `prot`, `steps`, `rhr`, `sleep` in minutes, `beer` counts `bd/bb/bp/fd/fb/fp` = blonde/forte × demi/bouteille/pinte, `waist`). Everything shown comes from `compute(state, workoutSessions)`: trend = 10 %/day EMA; maintenance = mean kcal − weight slope (kg/day) × 7700 over 4 weeks (≥ 2 kcal weeks, ≥ 4 weigh-ins over ≥ 10 days); verdict over 8 weeks (waist slope, reps/session). Reads Workout sessions read-only (`GET /data/workout`, local fallback). Old day-by-day format (`v` ≠ 2) is ignored on purpose (fresh start, 2026-10).
- **WEEK_PLANNER.html** — script wrapped in an IIFE; functions are not global (trigger via UI in tests).
- **index.html** — Hub. Tiles are generated from the `TILES` registry (one entry per tile); data from `GET /hub` summaries, per-widget `cfFetch(key)` (= `LifeSync.tryGet`) only as fallback.
