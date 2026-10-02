"""Why a monthly source leg wrote nothing: official lag, a guard, or a source gone backwards.

R2 fix 4 (lag window) and fix 8 (categories), shared by every official-source monthly writer:
the macro appender's remittance/imports legs, the yield ladder and the EPB exports writer
(R2 fix H4). Lives in utils so utils/epb_monthly.py can use it without importing
aggregate_latest, which production runs as ``python -m aggregate_latest`` (``__main__``).
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import utils.write_receipts as wr
from utils.write_receipts import record_skip


def accepted_lag_days(metric_id: str) -> int:
    """The lag a monthly source series is allowed before its newest month is stale: the
    sentinel's vintage grace for its cadence (ruling E1; e.g. imports_usd_mn_monthly is
    "quarterly", 165 days, for BB's ~2-month MEI lag). No config entry carries these ids, so
    the scraper map plus the monthly-table rule is exactly what load_cadence_map resolves."""
    from sentinel.cadence import _SCRAPER_CADENCE, GRACE_DAYS_BY_CADENCE, resolve_cadence
    return GRACE_DAYS_BY_CADENCE[resolve_cadence(metric_id, _SCRAPER_CADENCE, from_monthly_table=True)]


def record_official_lag(leg: str, metric_id: str, detail: str, newest_recorded: date | None) -> None:
    """BB lists nothing newer: stated with the newest stored month and the series' accepted
    lag window, so a reader can tell normal publication lag from a stuck listing (R2 fix 4)."""
    record_skip(leg, wr.NO_NEW_OFFICIAL_PERIOD, detail, newest_recorded=newest_recorded,
                lag_window_days=accepted_lag_days(metric_id))


def note_source_leg(leg: str, rows: list[dict], reasons: list[str], *, source: str, metric_id: str,
                    listed: Iterable[date], recorded: Iterable[date], wanted: date) -> None:
    """A parsed official source with nothing new and nothing refused is lag, not a guard --
    unless its newest listed month is older than one already recorded, which lag cannot
    explain (the review-H3 partial table-structure change signature)."""
    for reason in reasons:
        record_skip(leg, wr.WITHHELD_BY_VALIDATION, reason)
    if rows or reasons:
        return
    newest_listed, newest_recorded = max(listed, default=None), max(recorded, default=None)
    if newest_recorded is not None and (newest_listed is None or newest_listed < newest_recorded):
        record_skip(leg, wr.SOURCE_OLDER_THAN_RECORDS,
                    f"{source} lists nothing at or after the newest recorded month {newest_recorded} "
                    f"(newest listed: {newest_listed or 'none'})")
    else:
        record_official_lag(leg, metric_id,
                            f"{source} lists no month after those recorded ({wanted} not listed)",
                            newest_recorded)
