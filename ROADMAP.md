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
- Found in step 1, NOT fixed (widget data change): `kals_track.html` keys days by UTC (`dateKey` uses `toISOString`), so food logged 00:00–02:00 Paris lands on the previous day. The Hub uses the Paris date.

- Fixed 2026-10-21 (before step 4): an edit followed by tapping the Hub icon within the 3–4 s upload delay never reached the cloud, the Hub showed the old value, and reopening the widget loaded the older cloud copy over the edit (lost). Now uploads are staged as pending at edit time and sent on leave; the Hub prefers this device's newer summary (the cloud summary list can lag ~1 min).

## Steps
1. ✅ **Tile summaries (#6)** (done 2026-10-20) — each widget adds a small `hub` object to its cloud payload (computed numbers only, NO timestamps inside, or the "skip unchanged saves" fingerprint breaks). Worker gets `GET /hub` returning all summaries + `savedAt` in one response. Hub renders from summaries, falls back to today's per-key fetch for widgets not yet updated. Hub download ~530 KB → ~2 KB. Needs Worker redeploy.
2. ✅ **Tile registry** (done 2026-10-20) — one list (name, icon, file, cloud key, accent) generates the tiles; removes 9 hand-written tile blocks and duplicated load lists. Visual must stay identical (screenshot compare).
3. ✅ **Ordering + pins + "More"** (done 2026-10-20; owner chose long-press drag + 📌 corner icon, Do it! as a normal single tile, dense grid) — last saved first (re-ordered on load only, never live); 📌 pinned tiles stay on top in pin order; "Move to More" → collapsed bottom section, not fetched until expanded. Layout synced across devices in new cloud key `hub-layout` (1 write per layout change). Pin/menu buttons must not trigger the tile link.
4. **Auto-refresh + attention badges** — refresh on `pageshow`/`visibilitychange`; amber staleness ("12 days ago"); badges from summaries (budget ≥ 90%, no workout in 5 days, Do it! items left today…). No backend.
5. **Push notifications** (Android phone, tablet, PC) — Worker cron (e.g. 08:00 and 21:00 Paris) evaluates the step-4 rules and sends Web Push; payload-less "ping" + service worker fetches the text from the Worker (avoids push payload encryption). Owner adds a VAPID secret + cron trigger in Cloudflare. Reuses the leftover `notificationclick` in `sw.js`.
6. **Cleanup** — unused functions (`kals_track.html`: `switchLogTab`, `useSuggestion`; `notes.html`: `showSaving` — verify each), duplicated Hub fallbacks, "Coming soon…" Kals placeholder, wrong bottom label, CSS orphaned by the redesign.
7. Theme follows system light/dark by default; manual toggle overrides.
8. Strat tile shows real phase/age from FIRE Sim summary.
9. Mini sparkline per tile (from summaries).
10. Quick actions ("+ Expense", "+ Workout") opening widgets straight in add mode (small hook per widget).

## Devices to design and test for (exact browser viewports, measured by the owner)
| Device | Viewport (CSS px) | Notes |
|---|---|---|
| **OnePlus Pad Go** tablet, Android 14 / OxygenOS 14, Chrome, 11.35" | **1070 × 596 landscape** (main use) · ~764 × 900 portrait (derived, DPR ≈ 2.25) | Used a lot, mostly landscape. Only **596 px tall**: keep the header compact and tiles short so the first row(s) fit without scrolling; use the width for 3–4 tiles per row. Still check portrait. |
| **Pixel 8** phone, Android 17, Chrome | **411 × 784** | Daily use. No iPhone. |
| Desktop | ≥ 1200 px wide | |

Every layout change: screenshot all three (tablet landscape first, plus tablet portrait), light and dark.
