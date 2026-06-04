# Media-screen daily report to #thebrief — design

**Date:** 2026-06-04
**Status:** Approved (brainstorm) — revised after 5-lens adversarial review
**Author:** AI agent under Adnan's direction

## Problem

The media screen (`scrapers/media_screen.py`) only sends a Discord notification when it finds candidates. On a real 0-candidate run, `format_digest([])` returns `None` and the live path is `if digest is not None: notify(...)` (media_screen.py:191-192) — so **nothing is posted**. The operator gets no signal that the screen ran, what it looked at, or why nothing changed. A silent failure looks identical to a silent success.

Separately, the report should land in **#thebrief** (the channel Copotron watches for approve/reject replies), not EconDelta's ops/alerts channel.

**Bundled bug fix (found in review — see below):** the digest numbers candidates by list position, but the approve/reject loop interprets that number as the `media_review.id`. They only coincided on the first-ever run. This spec fixes it as part of the report rewrite, because it touches the same files and the feature is moot without a working approve loop.

## Goals

1. **Every real run posts a report to #thebrief** — including when there is nothing to approve.
2. The report states **what was observed** (articles checked, per outlet) and, for each tracked-metric figure, **why it was skipped** (out-of-range / no-period / matches-current-data / older-period / already-in-review).
3. When a change **is** needed, the report carries the approve/reject request, and **`approve N` targets the real `media_review.id`** (not the list position).
4. Route the report to **#thebrief**; keep EconDelta's operational *error* alerts on the existing ops channel.

## Non-goals

- No change to collection, extraction, or classification *thresholds* (the return-type of `classify` does change — see blast radius).
- No Copotron-composed messages — EconDelta posts via webhook; Copotron stays the reply-handler.
- No per-article/per-number full dump (rejected for noise; "observed + why" only, with a length cap).
- Dry-run does **not** post to Discord.

## Bundled bug fix — digest number N must equal `media_review.id`

**Bug:** `digest.py:14,16` numbers candidates by `enumerate` index (`i+1`) and instructs *"Reply `approve N`"*, but `media_screen/decide.py` + Copotron treat N as the `media_review.id`. `insert_media_review_rows` (supabase_writer.py:542) returns an `int` count, so the real ids never reach the digest. Worked once only because the first rows had ids 1, 2.

**Fix:** `insert_media_review_rows` returns the inserted rows' **ids** (`Prefer: return=representation`, select `id`). `run_screen` **inserts kept candidates first, then formats the report** using each row's real id. `format_report` numbers each candidate line by its id, and the instruction reads `Reply: approve <id>`.

## Design

### Data flow (note the reordered insert → format)

```
collect 6 TBS + 6 Daily Star articles            (unchanged)
  → extract figures                              (unchanged)
  → classify each figure that maps to a tracked metric:
        Candidate                 (a real fresher/conflicting figure → needs approval)
        Skip(metric_id, value, period, reason)   (dropped, with the reason)
  → within-run dedup: collapse duplicate Candidates and Skips
        (same metric_id + period) across multiple articles
  → drop_already_open(candidates, open_rows) → kept   (helper UNCHANGED: pure filter)
        deduped = [c for c in candidates if c not in kept]   (computed in run_screen)
        → each deduped becomes Skip(reason="already-in-review")
  → INSERT kept candidates  → returns their media_review ids   (return=representation)
  → format_report(candidates_with_ids, skips, n_tbs, n_ds)  → ALWAYS (title, message, fields)
  → live: require MEDIA_SCREEN_WEBHOOK_URL; notify(report, webhook_url=that) → #thebrief
  → dry-run: build + print report (candidates shown as "(dry-run — not queued)", no ids); no post
```

### Skip reasons (all FIVE classify/dedup paths — no `return None` may survive)

`classify` becomes a total function `Candidate | Skip`. The reasons map 1:1 to existing `return None` paths (confirmed in review — no new comparison logic needed):
- `out-of-range` — value outside `valid_range` (filter.py:27, the unit guard).
- `no-period` — no explicit reporting period (filter.py:31).
- `matches-current-data` — same period, value within tolerance of current `metric_history` (filter.py:38).
- `older-period` — press period **older** than what we already have (filter.py:41) — *this was the unlisted 5th path.*
- `already-in-review` — emitted by `run_screen` after dedup (a valid candidate already pending/rejected in `media_review`).

Reasons are **bare string literals** (a module-level `frozenset` for tests to assert against) — no Enum (used once each; YAGNI). The range-guard-before-period ordering (test_media_precision.py:31) must be preserved: a value-out-of-range AND period-None figure returns `Skip("out-of-range")`, not `"no-period"`.

### Components touched

| File | Change |
|---|---|
| `media_screen/types.py` | Add frozen `Skip(metric_id, value: float, period: date \| None, reason: str)`. |
| `media_screen/filter.py` (`classify`) | Return `Candidate \| Skip` (never `None`); each of the 5 paths attaches its reason. |
| `media_screen/dedup.py` | **Unchanged signature** — stays a pure filter returning the kept list. |
| `scrapers/media_screen.py` (`run_screen`) | `if c is not None` → `if isinstance(c, Candidate)` (a `Skip` is truthy — must not be inserted); collect skips; within-run dedup; compute `deduped` from `candidates - kept`; insert kept → ids; build+notify report every live run; require the #thebrief webhook. |
| `media_screen/digest.py` | Replace `format_digest` with `format_report(candidates_with_ids, skips, n_tbs, n_ds)` — `candidates_with_ids` = list of `(media_review_id, Candidate)` pairs (id is `None` on dry-run). **Always** returns `(title, message, fields)`; numbers candidates by real id; ISO period rendering; grouped/ordered skip lines; length cap; `fields[:10]`. |
| `utils/supabase_writer.py` (`insert_media_review_rows`) | Return inserted rows' **ids** (`return=representation`) instead of a count. Update its callers/tests. |
| `utils/notifier.py` (`notify`) | Add optional `webhook_url: str \| None = None`; empty/whitespace treated as unset; given non-empty → post there. Unchanged callers keep current behaviour. |

### Message format

Periods render as **ISO `YYYY-MM-DD`** (matches existing code/tests; no new formatter); a `None` period renders `(no period)`. `notify` prepends `"<emoji> EconDelta — "` to the title and sets embed color from `level` — so the in-message title must **not** duplicate an emoji/brand. Level: **`info`** for no-change, **`warning`** for needs-approval.

*No change (level=info):*
> **Media screen — no change** · 2026-06-04
> Checked 12 articles (6 TBS, 6 Daily Star).
> • NPL ratio — 32.26 @ 2026-03-31 → matches current data, skipped
> ✅ Nothing needs approval.

*Change needed (level=warning):*
> **Media screen — 1 needs approval** · 2026-06-04
> Checked 11 articles (5 TBS, 6 Daily Star).
> **#42 gross_npl_ratio** [fresher_period] — press **30.10** @ 2026-06-30 vs current 32.26 @ 2026-03-31
> _"…NPL eased to 30.10% at end-June…"_ <url>
> Reply: approve 42 · reject 42   ← the real media_review.id
> (skips, if any, listed below)

**Ordering & limits:**
- Skips grouped by reason (expected bucket: `already-in-review`, `matches-current-data`, `older-period`; eyeball bucket: `out-of-range`, `no-period`), then sorted by `metric_id` — deterministic across runs.
- Cap the rendered skip list to **12 lines (one per collected article max) with a "…and M more" footer**, and assert the embed description stays < 4096 chars before posting; keep `fields[:10]` (≤25-field limit).
- **0 articles collected** (all source fetches failed) → a distinct message *"Collected 0 articles — all sources failed"* so a scraping outage is not reported as a benign no-figures day.
- **No tracked figures** (skips==0, candidates==0, articles>0) → *"No tracked figures in today's articles. No change needed."* (must NOT render skip scaffolding).

### Routing (require the var — no silent ops fallback)

- `run_screen` reads `MEDIA_SCREEN_WEBHOOK_URL` itself. If **unset/empty → log a warning and skip the post** (do NOT pass `webhook_url=None`, which would fall through to the ops `DISCORD_WEBHOOK_URL` and silently misroute the report to the wrong channel). The run still returns 0.
- If set, post the report there. Operational **errors** in `run_screen` (open-review read fail, insert fail) keep going to the default `DISCORD_WEBHOOK_URL` (ops). On a hard failure the report itself is not built — **the ops-channel error is the signal**; #thebrief silence + an ops error = "screen errored" (documented, accepted).

### Edge cases

- **Within-run duplicate metric** — the same figure quoted in several articles collapses to one Candidate / one Skip (key: `metric_id` + `period`). Tie-break: prefer the candidate carrying a `source_quote`, else first in document order.
- **Webhook set-but-invalid** (typo/revoked) — `notify` raises/returns False; log at error level *distinct from "unset"* so a dead webhook is visible. Run still returns 0.
- **notify `(level, title)` 3600s dedup** (notifier.py:50-58) — per-process (resets each systemd fire), so the daily cadence is safe. The heartbeat title is near-constant; the date lives in the message body. Documented so a future change doesn't accidentally persist the dedup map and self-suppress.
- **Timer retry/double-fire** (`Restart=on-failure`, `StartLimitBurst=3`) — a same-day re-run after a partial success re-posts a report, and a previously-inserted candidate now reads as `already-in-review`. Accepted/ documented; not gated.

### Dry-run

Builds the report and prints it to stdout (candidates shown as *"(dry-run — not queued)"* with no real id and no approve line; skips/heartbeat shown). Posts nothing.

## Testing (assert behavior, not shape; include mutation checks)

- **`classify`** — each of the 5 paths returns `Skip` with the **exact** `reason`; a genuine figure returns a `Candidate`. Rewrite the existing `is None` assertions (test_media_filter.py:15,31; test_media_precision.py:22,33) to assert `isinstance(.., Skip)` + `.reason`. Preserve the range-before-period order test (`reason == "out-of-range"`).
- **`format_report`** — per reason, the message contains BOTH the human phrase (e.g. "matches current data") AND the metric_id + value; the no-change heartbeat and the no-tracked-figures variant are distinguishable (assert the specific phrases); the candidate case contains `approve` + `reject` + the real id + press value (port test_media_digest.py:19-20). Replace test_media_digest.py:13 (`format_digest([]) is None`).
- **`run_screen`** — `test_zero_candidates_still_posts_heartbeat`: mock notify to a recorder, assert **exactly one** call whose message contains "no change"; *mutation:* re-introducing a `if report is not None` gate must turn it red. **Routing:** assert the report call's `webhook_url == MEDIA_SCREEN_WEBHOOK_URL` and the error calls do not pass it; *mutation:* dropping `webhook_url=` must fail. **Dry-run:** notify called **zero** times AND stdout contains the report. **already-in-review:** an open-row match → not inserted AND the message shows that metric with reason `already-in-review`.
- **`insert_media_review_rows`** — returns the inserted ids; assert the digest integer equals the inserted `media_review.id`, not the loop index.
- **`notify`** — `mock_post.call_args.args[0] == given_url` when `webhook_url` passed; `None`/empty falls back to `DISCORD_WEBHOOK_URL`; unchanged callers unaffected.
- **Migrate** the existing assertions listed above (they break on the signature changes) and grep for all `classify` / `format_digest` / `insert_media_review_rows` callers before changing signatures (only `run_screen` today).

## Rollout (re-sequenced — Copotron first)

1. **Repoint Copotron to #thebrief first:** update Copotron's watched channel + the Hetzner `~/CLAUDE.md` block (currently "media-screen channel") → **#thebrief**; confirm Copotron has access; verify a real `approve 999` round-trip from #thebrief (safe no-op) **before** any report routing lands.
2. Add `MEDIA_SCREEN_WEBHOOK_URL` to the **real** env scaffolding: root `.env.example` (alongside `DISCORD_WEBHOOK_URL`) and the heredoc in `deploy/install.sh:46`; hand-add it to `/etc/econdelta.env` on the ExonVPS (install.sh skips an existing env file).
3. Deploy the code; the next 21:30 BDT run posts to #thebrief.
4. Update `docs/media-screen-copotron-wiring.md` to reference #thebrief and the `approve <id>` (real id) instruction format — keep ONE instruction format across `digest.py`, this spec, and the wiring doc.

## Inputs required at build time

- The #thebrief Discord **webhook URL**.
- Confirmation Copotron has **access to #thebrief**.
