# Life Hub — roadmap

Agreed with the owner on 2026-10-20 after a full audit of `index.html`. Work top-down; each step ships separately with tests.
After step 3, send phone/tablet/desktop screenshots for approval before continuing.

## Bugs found in the Hub audit (fixed by step 1 unless noted)
- Calculator and Lukas tiles read only this device's localStorage → empty/stale on other devices.
- FIRE tile ≠ FIRE Sim: hard-coded 4.500 COP/€ (widget uses live Frankfurter rate) and counts Colombian assets even when excluded (`colChecks`).
- Xpenser and Week Plan "Last logged" footers come from this device's localStorage.
- Kals "today" uses UTC (`toISOString`) → wrong day 00:00–02:00 Paris.
- Do it! item text inserted with innerHTML unescaped (a `<` breaks the tile).
- Hub does not refresh when returning from a widget (back button / bfcache) → stale numbers (step 4).
- Bottom bar says "live from local storage" (wrong since cloud sync).
- There is NO tile reordering today; order is fixed in the HTML.

## Steps
1. **Tile summaries (#6)** — each widget adds a small `hub` object to its cloud payload (computed numbers only, NO timestamps inside, or the "skip unchanged saves" fingerprint breaks). Worker gets `GET /hub` returning all summaries + `savedAt` in one response. Hub renders from summaries, falls back to today's per-key fetch for widgets not yet updated. Hub download ~530 KB → ~2 KB. Needs Worker redeploy.
2. **Tile registry** — one list (name, icon, file, cloud key, accent) generates the tiles; removes 9 hand-written tile blocks and duplicated load lists. Visual must stay identical (screenshot compare).
3. **Ordering + pins + "More"** — last saved first (re-ordered on load only, never live); 📌 pinned tiles stay on top in pin order; "Move to More" → collapsed bottom section, not fetched until expanded. Layout synced across devices in new cloud key `hub-layout` (1 write per layout change). Pin/menu buttons must not trigger the tile link.
4. **Auto-refresh + attention badges** — refresh on `pageshow`/`visibilitychange`; amber staleness ("12 days ago"); badges from summaries (budget ≥ 90%, no workout in 5 days, Do it! items left today…). No backend.
5. **Push notifications** (Android phone, tablet, PC) — Worker cron (e.g. 08:00 and 21:00 Paris) evaluates the step-4 rules and sends Web Push; payload-less "ping" + service worker fetches the text from the Worker (avoids push payload encryption). Owner adds a VAPID secret + cron trigger in Cloudflare. Reuses the leftover `notificationclick` in `sw.js`.
6. **Cleanup** — unused functions (`kals_track.html`: `switchLogTab`, `useSuggestion`; `notes.html`: `showSaving` — verify each), duplicated Hub fallbacks, "Coming soon…" Kals placeholder, wrong bottom label, CSS orphaned by the redesign.
7. Theme follows system light/dark by default; manual toggle overrides.
8. Strat tile shows real phase/age from FIRE Sim summary.
9. Mini sparkline per tile (from summaries).
10. Quick actions ("+ Expense", "+ Workout") opening widgets straight in add mode (small hook per widget).

## Devices to design and test for
- **Tablet — main secondary device, used a lot:** OnePlus Pad Go, OxygenOS 14 / Android 14, Chrome. 11.35", 2408×1720 px, 7:5 ratio. **Mostly landscape** → optimize for landscape, still check portrait.
  CSS viewport (to confirm with whatismyviewport.com on the tablet): ~1204×860 if DPR 2, ~1376×983 if DPR 1.75; minus browser bars (~80 px tall). Until confirmed, screenshot both 1204×780 and 1376×900 landscape, plus 860×1124 portrait.
  Landscape has room for 3–4 tiles per row — use it instead of stretching 2.
- **Phone:** Android (no iPhone), 390 px wide.
- **Desktop:** ≥ 1200 px.
- Light and dark mode for every layout change.
