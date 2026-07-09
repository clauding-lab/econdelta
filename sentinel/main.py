"""Freshness sentinel entry logic — reads both tables, posts one digest.

I/O lives here; the classification logic lives in ``freshness.py`` (pure, so it
retro-tests against synthetic data). Invoked as ``python -m sentinel`` under
``econdelta-sentinel.service`` (daily ~13:30 BDT / 07:30 UTC), wrapped by
``wrap_run`` so it writes ``run_logs (source='freshness_sentinel')`` — the
dead-man's-switch The Brief's off-box heartbeat watches.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.calendar import load_holidays
from utils.notifier import notify
from utils.supabase_reader import SupabaseReadError, fetch_all_freshness_rows

from .cadence import load_cadence_map
from .freshness import assess
from .report import HEARTBEAT_WEEKDAY, format_digest, should_send

logger = logging.getLogger("sentinel")

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLIDAYS_PATH = REPO_ROOT / "config" / "holidays_2026.json"

# Bangladesh is a fixed UTC+6 (no DST); cadence math is done on the BDT date.
_BD_TZ = timezone(timedelta(hours=6))


def main() -> int:
    """Run one sentinel pass. Returns 0 on a clean run, 1 if the read failed.

    A read failure returns 1 so run_logs records ``fail`` (and the off-box
    heartbeat notices) — a sentinel that can't see the data must not look
    healthy. Breaches themselves are NOT a failure of the sentinel: it did its
    job, so it returns 0 and reports them in the digest.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    now = datetime.now(timezone.utc)
    today = now.astimezone(_BD_TZ).date()

    try:
        holidays = load_holidays(HOLIDAYS_PATH)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("holidays load failed (%s); daily staleness uses weekends only", e)
        holidays = None

    try:
        cadence_map = load_cadence_map()
    except Exception as e:  # noqa: BLE001
        # load_cadence_map reads sources-v3.json AND lazily imports aggregate_latest
        # — a malformed config, a KeyError on a bad indicator entry, or a broken
        # aggregate_latest import would otherwise crash the sentinel here with
        # run_logs=fail but NO Discord alert (the exact silent-freeze class this
        # sentinel exists to kill). Mirror the read-failure guard below: alert
        # loudly and fail so the off-box heartbeat notices — a sentinel that can't
        # build its cadence map cannot judge freshness and must not look healthy.
        logger.error("sentinel cadence-map load failed: %s", e)
        notify(
            "error",
            "Freshness sentinel — cadence map failed",
            f"Could not build the metric→cadence map (sources-v3.json / "
            f"aggregate_latest); freshness UNKNOWN this run. {type(e).__name__}: {e}",
        )
        return 1

    try:
        rows_daily = fetch_all_freshness_rows("metric_history")
        rows_monthly = fetch_all_freshness_rows("metric_history_monthly")
    except SupabaseReadError as e:
        logger.error("sentinel read failed: %s", e)
        notify(
            "error",
            "Freshness sentinel — read failed",
            f"Could not read history tables; freshness UNKNOWN this run. {type(e).__name__}: {e}",
        )
        return 1

    report = assess(
        rows_daily=rows_daily,
        rows_monthly=rows_monthly,
        cadence_map=cadence_map,
        today=today,
        holidays=holidays,
        now=now,
    )
    logger.info(
        "sentinel: %d checked — %d breach, %d fresh, %d unmapped",
        report.total, len(report.breaches), len(report.fresh), len(report.unmapped),
    )
    for m in report.breaches:
        logger.warning(
            "STALE %s (%s) last as_of=%s age=%sd", m.metric_id, m.cadence,
            m.latest_as_of, m.age_days,
        )

    is_heartbeat = today.weekday() == HEARTBEAT_WEEKDAY
    if should_send(report, is_heartbeat_day=is_heartbeat):
        level, title, message, fields = format_digest(report)
        notify(level, title, message, fields)
    else:
        logger.info("no breaches and not heartbeat day — staying silent (run_logs proves liveness)")

    return 0
