"""When does an approved media override yield to BB's own pipeline?

Spec D6: any later BB parse — including the SAME period — supersedes the
human-approved press value. The override is a temporary bridge:
  - fresher_period: BB has caught up once its parsed source_as_of reaches (or
    passes) the period the press front-ran.
  - same_period_conflict: BB has genuinely revised once its parsed value for
    that period moves off the baseline that was current at approval. The daily
    re-emission of the IDENTICAL figure is not a revision (that would flap).
"""
from __future__ import annotations

from datetime import date


def is_superseded(
    *,
    kind: str,
    press_as_of: date,
    parsed_baseline: float | None,
    automated_value: float | None,
    automated_as_of: date | None,
    epsilon: float = 1e-9,
) -> bool:
    if kind == "fresher_period":
        return automated_as_of is not None and automated_as_of >= press_as_of
    if kind == "same_period_conflict":
        if automated_value is None or parsed_baseline is None:
            return False
        return abs(automated_value - parsed_baseline) > epsilon
    return False
