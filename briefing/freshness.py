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

# STALE_THRESHOLDS_HOURS_BY_CADENCE (aggregate_latest.py) / 24, rounded up —
# except quarterly, monthly, and daily below, which have since diverged from
# that formula for reasons specific to the briefing gate (see each note).
#
# quarterly=165 (was 100, owner-approved 2026-08-01): BD banking releases the
# core tier depends on (QFSAR NPL/CAR) lag by design ~2 quarters, so the old
# 100d window flagged a correctly-dated, on-schedule vintage as stale and
# skipped briefings that should have run. Matches sentinel/cadence.py's
# GRACE_DAYS_BY_CADENCE, which already used 165 for the same reason.
#
# monthly=45 (was 35, owner-approved 2026-08-01, review round 1, corrected
# review round 2): BBS CPI — a core-tier metric — was measured publishing at
# 32/36/53 day lags across three recent vintages, so the old 35d window keeps
# going dark on the slow tail. Matches sentinel/cadence.py's
# GRACE_DAYS_BY_CADENCE, which already used 45 for the same reason.
#
# Worked timeline under the fix (point_to_point_inflation as_of 2026-06-30):
# Mondays 08-03 fresh (34d) and 08-10 fresh (41d — this is the Monday the OLD
# 35d window would have LOST, since 41 > 35) both still generate; 08-17,
# 08-24, and 08-31 are STILL STALE (48d/55d/62d) — 45d does not cover the
# whole gap to the next vintage, only the first one. Recovery lands on 09-07
# ONLY IF July CPI (as_of 2026-07-31) has arrived by then: the three observed
# BBS lags (32/36/53d) put its real arrival at 09-01, 09-05, and 09-22
# respectively, so the two faster lags recover the 09-07 briefing but the
# slowest (53d, arriving 09-22) leaves it dark too — a source-lag gap this
# window was never meant to close, not a pipeline fault.
#
# Trade-off, accepted: widening 35->45 also means the 3 monthly core
# policy_rate_* ids (repo/sdf/slf) now take 46 days of real silence to trip
# the gate instead of 36 before a genuine freeze is caught — aligned with the
# sentinel's own 45d grace, and accepted as the cost of not false-positiving
# on CPI's normal lag. Do NOT widen further without owner sign-off — a wider
# window is a separate decision.
#
# daily=2 (was 1, owner-approved 2026-08-01): under honest Tier-1 source_as_of
# dating (as_of comes from the source, never the run date — AGENTS.md
# landmine 26, PR #97), a Monday 01:00 UTC briefing sits at EXACTLY zero
# margin against a 1-day window — one missed Sunday snapshot (a BD public
# holiday, a transient Sunday scraper miss) silently skipped the whole
# briefing. 2 days buys one real day of slack without hiding a genuine freeze.
_STALE_DAYS_BY_CADENCE = {
    "daily": 2, "weekly": 8, "monthly": 45, "quarterly": 165, "fiscal_year": 400,
}
# NOTE: even a 2-day 'daily' window means a Monday briefing can still
# honestly skip when the freshest daily reading predates Saturday — e.g. a BD
# public holiday run spanning the weekend when BB didn't publish. That's an
# intentional skip (no briefing on stale data), not a bug.
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
