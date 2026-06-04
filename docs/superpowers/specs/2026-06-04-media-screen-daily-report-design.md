# Media-screen daily report to #thebrief — design

**Date:** 2026-06-04
**Status:** Approved (brainstorm) — pending spec review
**Author:** AI agent under Adnan's direction

## Problem

The media screen (`scrapers/media_screen.py`) only sends a Discord notification when it finds candidates. On a real 0-candidate run, `format_digest([])` returns `None` and the live path is `if digest is not None: notify(...)` (media_screen.py:191-192) — so **nothing is posted**. The operator gets no signal that the screen ran, what it looked at, or why nothing changed. Confidence in a silent daily job is low, and a silent failure looks identical to a silent success.

Separately, the notification currently posts to a single webhook (`DISCORD_WEBHOOK_URL`, notifier.py:66) pointed at the EconDelta ops/alerts channel. The operator wants the media-screen report in **#thebrief** (the channel Copotron watches for approve/reject replies).

## Goals

1. **Every real run posts a report to #thebrief** — including when there is nothing to approve.
2. The report states **what was observed** (articles checked, per outlet) and, for each tracked-metric figure it saw, **why it was skipped** (out-of-range / no-period / matches-current-data / already-in-review).
3. When a change **is** needed, the same report carries the approve/reject request (today's behaviour, preserved).
4. Route the report to **#thebrief**; keep EconDelta's operational *error* alerts on the existing ops channel.

## Non-goals

- No change to collection, extraction, classification thresholds, or the supersede/apply logic.
- No Copotron-composed messages — EconDelta posts via webhook; Copotron stays the reply-handler (decision from brainstorm).
- No per-article/per-number full dump (rejected for noise; "observed + why" only).
- Dry-run does **not** post to Discord (unchanged safety).

## Design

### Data flow

```
collect 6 TBS + 6 Daily Star articles  (unchanged)
  → extract figures  (unchanged)
  → classify each figure that maps to a tracked metric:
        Candidate            (a real fresher/conflicting figure → needs approval)
        Skip(metric_id, value, period, reason)   (dropped, with the reason)
  → dedup: a Candidate already open in media_review → becomes Skip(reason="already-in-review")
  → format_report(candidates, skips, article_counts)  → ALWAYS returns (title, message, fields)
  → live run: notify(report, webhook_url=MEDIA_SCREEN_WEBHOOK_URL)   ← fires every run
  → dry-run:  print report to stdout, no post
```

### Skip reasons

Emitted by `classify` (it already evaluates each):
- `out-of-range` — value outside the metric's `valid_range` (the unit guard).
- `no-period` — no explicit reporting period in the extraction.
- `matches-current-data` — same period + value within rounding tolerance of the current `metric_history` value (not fresher, not conflicting).

Emitted by `run_screen` after dedup:
- `already-in-review` — an otherwise-valid candidate that is already a pending/rejected `media_review` row.

### Components touched

| File | Change |
|---|---|
| `media_screen/types.py` | Add frozen `Skip` dataclass: `metric_id: str`, `value: float`, `period: str \| None`, `reason: str` (one of the four reasons; define a `SkipReason` string-enum/constants). |
| `media_screen/filter.py` (`classify`) | Return `Candidate \| Skip` instead of `Candidate \| None`. Each early-return path attaches its reason. |
| `media_screen/dedup.py` (`drop_already_open`) | Return `(kept_candidates, deduped_candidates)` so run_screen can record the deduped ones as `Skip("already-in-review")`. |
| `media_screen/digest.py` | Replace `format_digest` with `format_report(candidates, skips, n_tbs, n_ds)` that **always** returns `(title, message, fields)`. |
| `scrapers/media_screen.py` (`run_screen`) | Collect candidates + skips; build the report every run; live run always notifies (to the #thebrief webhook); dry-run prints the report. |
| `utils/notifier.py` (`notify`) | Add optional `webhook_url: str \| None = None` param; when given, post there instead of `DISCORD_WEBHOOK_URL`. Unchanged callers keep current behaviour. |

### Message format

**No change:**
> **📊 Media screen — no change** · 04 Jun 2026
> Checked 12 articles (6 TBS, 6 Daily Star).
> • NPL ratio — 32.26% @ Mar-2026 → matches current data, skipped
> ✅ Nothing needs approval.

**Change needed:**
> **🟠 Media screen — 1 needs approval** · 04 Jun 2026
> Checked 11 articles (5 TBS, 6 Daily Star).
> **1. gross_npl_ratio** [fresher_period] — press **30.10** @ Jun-2026 vs current 32.26 @ Mar-2026
> _"…NPL eased to 30.10% at end-June…"_ <url>
> Reply `approve 1` or `reject 1`.

If no tracked figures were found at all: "No tracked figures in today's articles. No change needed." (still posted).

### Routing

- New env `MEDIA_SCREEN_WEBHOOK_URL` = the #thebrief webhook. The daily **report** posts there.
- Operational **errors** in `run_screen` (open-review read failure, media_review insert failure) keep posting to the default `DISCORD_WEBHOOK_URL` (ops channel) — these are operator alerts, not brief content.
- If `MEDIA_SCREEN_WEBHOOK_URL` is unset, `notify` reuses its existing "no webhook → log + skip" behaviour (the run still succeeds).
- Copotron side (deploy step, not code): repoint Copotron's watched channel and the Hetzner `~/CLAUDE.md` approve/reject block from "media-screen channel" → **#thebrief**, and confirm Copotron has access to #thebrief.

### Dry-run

Prints the full report (including the no-change heartbeat) to stdout and posts nothing — so the report can be previewed safely. Current dry-run candidate-print is folded into this.

## Testing

- `classify` returns the correct `Skip(reason=...)` for each of out-of-range, no-period, matches-current-data; returns a `Candidate` for a genuine fresher/conflicting figure.
- `drop_already_open` returns both kept and deduped lists; deduped become `already-in-review` skips.
- `format_report`: (a) 0-candidate heartbeat with skip lines, (b) no-tracked-figures case, (c) candidates case with approve/reject lines — all return a non-None message.
- `run_screen` (mocked notifier): notify fires **even when candidates == 0**, targeting the media-screen webhook; operational error paths still target the default webhook.
- Dry-run prints the report and does **not** call notify.
- `notify(webhook_url=...)` posts to the given URL; unchanged callers post to `DISCORD_WEBHOOK_URL`.

## Rollout

1. Add `MEDIA_SCREEN_WEBHOOK_URL` to `deploy/econdelta.env.example` and to `/etc/econdelta.env` on the ExonVPS (the #thebrief webhook).
2. Deploy the code; the next 21:30 BDT run posts to #thebrief.
3. Repoint Copotron's watch + Hetzner `~/CLAUDE.md` block to #thebrief; verify a real approve/reject round-trips from #thebrief.
4. Update `docs/media-screen-copotron-wiring.md` to reference #thebrief.

## Inputs required at build time

- The #thebrief Discord **webhook URL**.
- Confirmation Copotron has **access to #thebrief**.
