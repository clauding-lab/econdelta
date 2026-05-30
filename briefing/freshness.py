"""Tiered data-freshness gate. Pure function — no I/O.

Core series stale  -> skip the whole briefing (don't publish a confident read
                      on stale data; the 'fresh as_of != fresh parse' landmine).
Peripheral stale   -> generate, but record the names so the PWA shows a banner.

A fresh as_of alone is NOT proof of fresh data (carry-forward writes today's
date onto last week's value), so the gate ALSO requires a recent successful
aggregate run (aggregate_ok_recent) for the core tier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# STALE_THRESHOLDS_HOURS_BY_CADENCE (aggregate_latest.py) / 24, rounded up.
_STALE_DAYS_BY_CADENCE = {
    "daily": 1, "weekly": 8, "monthly": 35, "quarterly": 100, "fiscal_year": 400,
}
# NOTE: a 1-day 'daily' window means a Monday briefing honestly skips when the
# freshest daily reading predates Sunday — e.g. BD public holidays when BB
# didn't publish. That's an intentional skip (no briefing on stale data), not a bug.
_DEFAULT_STALE_DAYS = 35


@dataclass(frozen=True)
class FreshnessResult:
    core_stale: bool
    stale_series: list[str]
    data_as_of: date
    reasons: list[str]


def _is_stale(as_of: date, cadence: str, today: date) -> bool:
    window = _STALE_DAYS_BY_CADENCE.get(cadence, _DEFAULT_STALE_DAYS)
    return (today - as_of).days > window


def assess_freshness(latest_as_of_by_metric: dict[str, date],
                     cadence_by_metric: dict[str, str],
                     core_ids: set[str],
                     today: date,
                     aggregate_ok_recent: bool) -> FreshnessResult:
    reasons: list[str] = []
    stale_series: list[str] = []
    core_stale = False

    if not aggregate_ok_recent:
        core_stale = True
        reasons.append("no successful aggregate run within window (possible carry-forward)")

    for metric_id, as_of in latest_as_of_by_metric.items():
        cadence = cadence_by_metric.get(metric_id, "monthly")
        if not _is_stale(as_of, cadence, today):
            continue
        if metric_id in core_ids:
            core_stale = True
            reasons.append(f"core metric stale: {metric_id} (as_of {as_of})")
        else:
            stale_series.append(metric_id)

    # A core metric entirely absent from history (scraper/Supabase gap, or a
    # first run with no data) is at least as dangerous as a stale as_of — never
    # publish a briefing whose core series is simply missing.
    for core_id in core_ids:
        if core_id not in latest_as_of_by_metric:
            core_stale = True
            reasons.append(f"core metric absent from history: {core_id}")

    core_as_ofs = [d for m, d in latest_as_of_by_metric.items() if m in core_ids]
    data_as_of = min(core_as_ofs) if core_as_ofs else today
    return FreshnessResult(core_stale=core_stale, stale_series=sorted(stale_series),
                           data_as_of=data_as_of, reasons=reasons)
