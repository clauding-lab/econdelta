"""Deterministic zero-rows / high-failure floors (E2.5).

``fetch_all`` and ``parse_all`` return exit 0 regardless of how many indicators
failed, and ``opus_review`` is fail-open on operational errors — so a systemic
fetch outage or a parse that produced almost nothing slips through as a green
run serving yesterday's carried-forward data. These are pure, count-based
verdicts that fire BEFORE and INDEPENDENT of the LLM review: a floor breach is
arithmetic, not a judgement call.
"""
from __future__ import annotations

from dataclasses import dataclass

# A run is systemically broken (not just a few flaky sources) when more than
# this fraction of due indicators fail to fetch. Normal operation loses a handful
# of walled/flaky PDFs; >50% failing means the network/config/host is down.
FETCH_FAILURE_RATE = 0.5

# Parse must produce a snapshot for at least this fraction of due indicators.
# Below it, parsing (or the fetch that feeds it) is systemically broken.
PARSE_MIN_RATE = 0.5


@dataclass(frozen=True)
class FloorVerdict:
    """Result of a floor check: whether it breached, and a human reason."""

    breached: bool
    reason: str


def assess_fetch_floor(
    *, due: int, fetched: int, failure_rate: float = FETCH_FAILURE_RATE
) -> FloorVerdict:
    """Breach when nothing fetched, or more than ``failure_rate`` of due failed."""
    if due <= 0:
        return FloorVerdict(False, "no indicators due")
    failed = max(due - fetched, 0)
    if fetched == 0:
        return FloorVerdict(True, f"0/{due} indicators fetched — total fetch outage")
    if failed / due > failure_rate:
        return FloorVerdict(
            True, f"{failed}/{due} fetches failed (>{failure_rate:.0%}) — systemic, not a flaky source"
        )
    return FloorVerdict(False, f"{fetched}/{due} fetched, {failed} failed (within tolerance)")


def assess_parse_floor(
    *, due: int, produced: int, min_rate: float = PARSE_MIN_RATE
) -> FloorVerdict:
    """Breach when parse produced nothing, or fewer than ``min_rate`` of due."""
    if due <= 0:
        return FloorVerdict(False, "no indicators due")
    if produced == 0:
        return FloorVerdict(True, f"0/{due} snapshots produced — parse yielded nothing")
    if produced / due < min_rate:
        return FloorVerdict(
            True, f"only {produced}/{due} snapshots produced (<{min_rate:.0%}) — systemic"
        )
    return FloorVerdict(False, f"{produced}/{due} snapshots produced")
