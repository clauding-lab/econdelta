"""Classify a parsed-vs-press pair into a review Candidate, or None.

The strict filter (spec D4): a candidate is emitted only when the press period
is explicit AND the value differs from the parsed value beyond a per-metric
rounding tolerance. This is the flap-killer — undated numbers are discarded.
"""
from __future__ import annotations

from datetime import date

from media_screen.types import Candidate, Extracted


def classify(
    metric_id: str,
    parsed_value: float | None,
    parsed_as_of: date | None,
    ex: Extracted,
    *,
    tolerance: float,
) -> Candidate | None:
    # Rule 1: period MUST be explicit.
    if ex.period is None:
        return None

    # Rule 2 + kind derivation.
    if parsed_as_of is None or ex.period > parsed_as_of:
        kind = "fresher_period"
    elif ex.period == parsed_as_of:
        if parsed_value is not None and abs(ex.value - parsed_value) <= tolerance:
            return None  # same period, within rounding → same number
        kind = "same_period_conflict"
    else:
        return None  # press period is OLDER than what we have → not interesting

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
