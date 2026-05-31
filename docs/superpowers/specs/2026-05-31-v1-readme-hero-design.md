# EconDelta v1.0.0 — README hero, live-site hero, first release

**Date:** 2026-05-31
**Status:** Approved (design + "build + tag now")
**Branch:** `feat/v1-readme-hero` (off `origin/main` @ `177e81c`)

## Goal

Three deliverables, all anchored to the **real, queried** repository numbers:

1. A proper **README** led by a hero badge row showing the live data-point count.
2. The first tagged **v1.0.0** release with release notes.
3. A **hero banner** on the first page of the live PWA (`econdelta.clauding-lab.com`)
   stating what EconDelta is, the (live) number of data points it autonomously
   captures, and how many years of backlog the archive holds.

## Grounded numbers (queried live from Supabase `ssbliukchgibjcjohibi`, 2026-05-31)

| Source | Value | Span |
|---|---|---|
| `metric_history` (daily) | 4,912 rows | 2025-10-01 → 2026-05-31 |
| `metric_history_monthly` (archive) | 5,293 rows | **2012-01-01** → 2026-05-01 |
| **Total data points** | **10,205** | — |
| `metric_definitions` | 71 indicators | — |
| Backlog depth | — | **~14 years (since Jan 2012)** |

**Honesty constraints (load-bearing):**
- The data-points figure is shown **live** (fetched at load), not hardcoded.
- "Autonomously / continuously" describes the *daily pipeline*; the 14-year archive
  was largely **backfilled** — the hero frames these as two distinct claims, never
  lumping all 10K as live-scraped.
- No lifetime run count is claimed (`run_logs` only began 2026-05-04).

## Design

### 1 · Live-site hero (PWA)
A new `<HeroBanner/>` at the **top of `PageLatest`** (route `/`, `pwa/pages/latest.jsx`),
between the global masthead/tape and the existing `<PageHead/>`. Editorial/newspaper
treatment matching the terminal aesthetic: IBM Plex Serif headline, mono eyebrow + ledger,
amber `--accent`, the `Δ` motif, a live-pulse dot, themed via existing tokens, responsive
(320/768/1024/1440), reduced-motion safe.

- **Data:** `bootstrap()` in `lib/supabase-client.js` gains a `fetchRepoStats()` helper
  (two `count=exact` calls + earliest archive `as_of`), stashed on `window.ED_DATA.stats`.
  Wrapped in try/catch — on failure or mock mode the hero falls back to baked constants
  (`10,000+ / 14 yrs / 70`) so it never breaks.
- **Files:** `pages/latest.jsx` (component + mount), `lib/supabase-client.js` (+stats),
  `styles.css` (`.hero` block + breakpoints + reduced-motion). Plus a minor version bump
  `v0.1 → v1.0.0` in `components.jsx` (masthead + sidebar strings).

### 2 · README hero badges (shields.io `endpoint`)
Badge row at the top of `README.md`: **data points · history (14 yrs) · indicators ·
data-updated · GitHub Pages deploy · live-site link**. Live badges read JSON from a
`badges` orphan branch. Existing README body (architecture, data contract) is preserved;
only the intro + badge hero are added.

### 3 · Daily stats Action (`.github/workflows/stats-badges.yml`)
Cron `0 8 * * *` (08:00 UTC = 14:00 BDT, after the daily aggregate) + `workflow_dispatch`.
Runs `scripts/gen_badges.sh`, which reads the **public** anon config from `pwa/config.js`
(no secrets needed), queries the live counts, writes shields `endpoint` JSON, and the
workflow publishes them to the `badges` branch. The `badges` branch is **seeded manually**
during the build (run the script locally + push) so README badges resolve immediately.

### 4 · v1.0.0 release
Build 1–3 → verify (local PWA preview + Playwright desktop/mobile × light/dark; README
render; run `gen_badges.sh`) → adversarial-review workflow over the diff → **merge to main**
(push triggers `pwa-deploy.yml` → hero goes live) → seed `badges` branch → `git tag -a
v1.0.0` + `gh release create` with notes framing 1.0 as "stable data contract + 14-year
archive + public dashboard." Tag is on current `main` (`177e81c`), which already includes
the Opus 4.8 bump (#41) and the briefing pipeline (#39/#46).

## Known open item (explicitly accepted by user, not part of this work)
Today's `aggregate` stage failed twice (13:00 + 13:05 BDT, exit 1, error not captured in
`run_logs`). Parse ran clean on Opus 4.8. Cause unverified — to be investigated separately.
v1.0.0 is being tagged with this known-red aggregate per explicit user direction.

## Out of scope
Fixing the aggregate failure; rewiring the masthead's stale mock date/issue strings;
structured-data panels.
