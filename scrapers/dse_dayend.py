"""DSE DS30 per-ticker daily-close scraper — keeps ``dse_close_<CODE>`` flowing.

Thin DAILY entry-point over the one-time backfill (``scripts/backfill_dse_dayend``).
It computes a short SELF-HEALING look-back window and delegates to
``run_backfill`` for a real, full-30-ticker write to ``metric_history``.

Why a window instead of a single day: DSE trades Sun–Thu, and a timer can miss a
fire (reboot, transient outage). Re-fetching the last few days every run means a
missed Sunday or a long weekend self-heals on the next successful run — upserts
are idempotent on ``(metric_id, as_of)``, so re-writing recent closes is harmless.

The fetch/parse/write machinery lives in ``scripts.backfill_dse_dayend`` and is
reused verbatim (no duplication); this module only owns the daily window + the
run-logging wrapper. The write path passes NO ``url=`` (see AGENTS.md landmine
#22) because ``run_backfill`` already uses ``upsert_metric_history`` cleanly.

Deployed via ``deploy/econdelta-dse-dayend.{service,timer}`` (OnCalendar 23:20
UTC = 05:20 BDT next day — a few minutes after the index scraper at 23:11).
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

from scripts.backfill_dse_dayend import run_backfill

logger = logging.getLogger("dse_dayend")

# Self-healing look-back: re-fetch the last N calendar days every run so a missed
# fire, weekend, or holiday is caught up on the next successful run. 5 days spans
# a Fri+Sat weekend (DSE is closed) plus a missed prior trading day, and always
# overlaps at least one trading day so a run on a non-trading day still has data.
_DAILY_LOOKBACK_DAYS = 5


def compute_window(today: date, lookback_days: int = _DAILY_LOOKBACK_DAYS) -> tuple[date, date]:
    """Return ``(start, end)`` for the daily self-healing window.

    ``end`` is ``today`` and ``start`` is ``today - lookback_days``. The range is
    inclusive of recent trading days; ``run_backfill`` upserts each day under its
    own trading date, so an overlap with earlier runs is a harmless idempotent
    rewrite.
    """
    return today - timedelta(days=lookback_days), today


def main() -> int:
    """Run the daily DS30 close scrape over the self-healing window (real write)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start, end = compute_window(date.today())
    logger.info(
        "DSE day-end daily run: window %s .. %s (self-healing %d-day look-back); "
        "fetching all DS30 tickers live and upserting to metric_history",
        start.isoformat(), end.isoformat(), _DAILY_LOOKBACK_DAYS,
    )
    # codes_override omitted -> run_backfill fetches the 30 DS30 codes live.
    # dry_run/sample_only False -> a real write of the full set (run_backfill
    # prints the ticker x day counts it wrote).
    # notify_on_failure=True -> the daily production path fires a Discord error
    # alert on a total fetch failure, a below-floor partial, or a Supabase write
    # error, instead of failing silently (E1.6 — this was the ONLY scraper with
    # no alerting, the path behind the 24-day silent DSE freeze).
    return run_backfill(
        start=start, end=end, dry_run=False, sample_only=False, notify_on_failure=True,
    )


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("dse_dayend", "econdelta-dse-dayend.service", main))
