# CLAUDE.md — working on Life Hub

Read `README.md` first for the widget map, cloud keys, Drive filenames and `lib/` APIs.
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
- New feature with math → add a rule test. For layout changes still screenshot at 390 px and 800 px, light and dark.
- In the harness, wait with `wait_until(pg, …)` / `pg.wait_for_timeout`, never `time.sleep` (it freezes the mocked network).

## Widget notes
- **FIRE_SIM.html** — state `S`, limits `LIM`, render `draw()`, save `saveState()`. PEA/CTO returns are 9 five-year periods `pea-r1..9`, `cto-r1..9` (`cto-sync` copies PEA); horizon 44 years; summary uses CAGR via `cagr()`. Old `pea-r`/`cto-r` migrated by `migrateReturns()`.
- **XPENSES_CALCULATOR.html** — `states.now` / `states.fire` merged over `DEFAULTS`; rows come from `SHARED_DEF` / `PERSONAL_DEF` (adding a row = one def line + default in both tabs + a donut category in `CHART_CATS`). Shared expenses are full bills paid from the joint Revolut pot (2 × `sharedAcc`); "Your expenses" = half of shared + personal. Partner's contribution is income (`mel`). Donut must sum to `derived().gross`.
- **workout_dashboard.html** — sessions parsed from pasted calendar text by `parseWorkoutText` → `parseLineExercises` → `classifyExercise`. Rings: an `RG` tag on a PU/DOM/BIC/DIP line keeps its normal bucket/variant and also adds to `rpu`/`rdom`/`rdip`; `N s/m HOLD RG` adds seconds to `rhold`. Rings fields are stored only when > 0; monthly totals `rg_pu/rg_dom/rg_dip/rg_hold`.
- **WEEK_PLANNER.html** — script wrapped in an IIFE; functions are not global (trigger via UI in tests).
- **index.html** — Hub tiles; `cfFetch(key)` = `LifeSync.tryGet`.
