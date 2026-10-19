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
- Keep each widget self-contained: shared code goes in `lib/`, loaded via `<script src="lib/...">` in `<head>`. No build step, no framework.

## Workflow
1. `git pull` before every change (the owner may edit on GitHub or other devices).
2. Edit with exact-match replacements; assert each match is unique.
3. Test (below). Compare against `git show HEAD:<file>` when the change must not alter results.
4. Commit with a descriptive message and push to `main`. Pages deploys in ~1 min; tell the owner to hard-reload (Ctrl/Cmd+Shift+R).

## Testing
No test suite yet. Use headless Chromium (Python Playwright is available):
- `file://` load of the widget, collect `pageerror` events — must be empty.
- Mock network: Chart.js CDN → local `chart.js` from npm; `widgets-sync` → fake 200/401; `accounts.google.com` → empty; `www.googleapis.com` → fake Drive for `lib/drive.js`.
- Pre-seed `localStorage.lifehub_sync_secret` in `add_init_script` to skip the key prompt.
- Assert computed values via `page.evaluate` (e.g. `derived(states.now)`, `S`, `shadowRun(0)`), and screenshot at 390 px and 800 px for layout changes.
- Check light and dark mode when touching CSS.

## Widget notes
- **FIRE_SIM.html** — state `S`, limits `LIM`, render `draw()`, save `saveState()`. PEA/CTO returns are 9 five-year periods `pea-r1..9`, `cto-r1..9` (`cto-sync` copies PEA); horizon 44 years; summary uses CAGR via `cagr()`. Old `pea-r`/`cto-r` migrated by `migrateReturns()`.
- **XPENSES_CALCULATOR.html** — `states.now` / `states.fire` merged over `DEFAULTS`; rows come from `SHARED_DEF` / `PERSONAL_DEF` (adding a row = one def line + default in both tabs + a donut category in `CHART_CATS`). Shared expenses are full bills paid from the joint Revolut pot (2 × `sharedAcc`); "Your expenses" = half of shared + personal. Partner's contribution is income (`mel`). Donut must sum to `derived().gross`.
- **WEEK_PLANNER.html** — script wrapped in an IIFE; functions are not global (trigger via UI in tests).
- **index.html** — Hub tiles; `cfFetch(key)` = `LifeSync.tryGet`.
