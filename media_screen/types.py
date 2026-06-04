"""Shared, frozen dataclasses for the media screen."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str            # EconDelta indicator id (alias propagation carries it to brief keys)
    press_names: tuple[str, ...]   # how the press refers to it ("NPL", "default loans", ...)
    tolerance: float          # absolute diff in the metric's unit below which press==parsed
    # Plausible value bounds in the metric's unit — the unit guard. A press number
    # outside this range is rejected (e.g. a Tk-crore amount mislabelled as a % ratio).
    valid_range: tuple[float, float] = (float("-inf"), float("inf"))


@dataclass(frozen=True)
class Extracted:
    indicator_hint: str       # the press_name the extractor matched
    value: float
    period: date | None       # the reporting period the article states (None if absent)
    quote: str                # the exact sentence
    source_url: str
    source_outlet: str


@dataclass(frozen=True)
class Candidate:
    metric_id: str
    parsed_value: float | None
    parsed_as_of: date | None
    press_value: float
    press_as_of: date
    kind: str                 # 'fresher_period' | 'same_period_conflict'
    source_outlet: str
    source_url: str
    source_quote: str
    confidence: str
