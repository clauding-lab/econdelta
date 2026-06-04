"""Classify a parsed-vs-press pair into a review Candidate or a Skip(reason).

classify is a TOTAL function: every tracked figure returns either a Candidate
(needs approval) or a Skip with the reason it was dropped. No `return None`.
"""
from __future__ import annotations

from datetime import date

from media_screen.types import Candidate, Extracted, Skip


def classify(
    metric_id: str,
    parsed_value: float | None,
    parsed_as_of: date | None,
    ex: Extracted,
    *,
    tolerance: float,
    valid_range: tuple[float, float] = (float("-inf"), float("inf")),
) -> Candidate | Skip:
    # Rule 0: value must be plausible for this metric's unit (the unit guard).
    lo, hi = valid_range
    if not (lo <= ex.value <= hi):
        return Skip(metric_id, ex.value, ex.period, "out-of-range")

    # Rule 1: period MUST be explicit.
    if ex.period is None:
        return Skip(metric_id, ex.value, None, "no-period")

    # Rule 2 + kind derivation.
    if parsed_as_of is None or ex.period > parsed_as_of:
        kind = "fresher_period"
    elif ex.period == parsed_as_of:
        if parsed_value is not None and abs(ex.value - parsed_value) <= tolerance:
            return Skip(metric_id, ex.value, ex.period, "matches-current-data")
        kind = "same_period_conflict"
    else:
        return Skip(metric_id, ex.value, ex.period, "older-period")

    return Candidate(
        metric_id=metric_id,
        parsed_value=parsed_value,
        parsed_as_of=parsed_as_of,
        press_value=ex.value,
        press_as_of=ex.period,
        kind=kind,
        source_outlet=ex.source_outlet,
        source_url=ex.source_url,
        source_quote=ex.quote,
        confidence=f"press={ex.value} @ {ex.period} vs parsed={parsed_value} @ {parsed_as_of}",
    )
