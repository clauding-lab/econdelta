"""Static config for the weekly briefing: which metrics, their thresholds,
cadence and labels — all derived from config/sources-v3.json (the same source
aggregate_latest.py uses to seed metric_definitions).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_V3 = REPO_ROOT / "config" / "sources-v3.json"

# Thesis-bearing series. If any of these is stale, skip the whole briefing.
# Verified against sources-v3.json in Step 0b of the plan.
CORE_METRIC_IDS = frozenset({
    "policy_rate_repo", "policy_rate_sdf", "policy_rate_slf",
    "call_money_rate", "bill_bond_rates", "tbill_182d_yield", "tbill_364d_yield",
    "tbond_5y_yield", "tbond_10y_yield",
    "usd_bdt_exchange_rate", "fx_reserve_gross_and_bpm6",
    "point_to_point_inflation", "gross_npl_ratio",
})

# PR-C (build-brief item 3, AGENTS.md landmine 52): bill_bond_rates/
# tbill_182d_yield/tbill_364d_yield/tbond_5y_yield/tbond_10y_yield left
# config/sources-v3.json entirely (the BB treasury page's two-yield-column
# trap shipped materially wrong bond yields for weeks -- see
# AGENT_LEARNINGS.md's 2026-08-22 entry) and are now derived from
# auction_results by aggregate_latest._derive_daily_yields_from_auctions.
# They remain CORE_METRIC_IDS the briefing must track for freshness -- this
# supplies their cadence/threshold/label directly since sources-v3.json can
# no longer answer for them. Without this, tracked_metric_ids() would stop
# including them entirely, _collect_history() would never even attempt to
# read their metric_history rows, and assess_freshness's "core metric
# entirely absent from history" branch (briefing/freshness.py) would mark
# the briefing core_stale EVERY week, forever -- turning a source-quality
# fix into a permanently-skipped weekly briefing.
#
# CADENCE (Opus review round 1, C1 -- 2026-08-23): bills stay "weekly"
# (91d/182d/364d T-bills genuinely auction roughly weekly, so their real
# auction_results dates comfortably clear briefing/freshness.py's 8-day
# "weekly" window, _STALE_DAYS_BY_CADENCE). tbond_5y_yield/tbond_10y_yield
# are "monthly" (60-day window) instead -- BGTB 5y/10y auctions land
# roughly monthly-to-quarterly, so under honest auction-date `as_of` (this
# PR's whole point) an 8-day window would read core_stale on almost every
# week's briefing (measured: a bond auctioning ~monthly leaves as_of
# 20-30+ days old on a typical Monday, comfortably past 8 days but well
# inside 60). This INTENTIONALLY DIVERGES from sentinel/cadence.py, which
# keeps all 5 ids at "weekly" -- the sentinel pages on a tighter, more
# suspicious freeze window; the briefing's own publish gate needs the
# same monthly/weekly split its other cadences already use (see
# briefing/freshness.py's own monthly=60/quarterly=165 divergence notes)
# so a genuinely-scheduled auction gap doesn't self-inflict a false skip.
_RETIRED_YIELD_IDS_METADATA: dict[str, dict] = {
    "bill_bond_rates": {"cadence": "weekly", "anomaly_threshold": 1.0, "name": "91-Day T-Bill Cut-Off Yield"},
    "tbill_182d_yield": {"cadence": "weekly", "anomaly_threshold": 1.0, "name": "182-Day T-Bill Cut-Off Yield"},
    "tbill_364d_yield": {"cadence": "weekly", "anomaly_threshold": 1.0, "name": "364-Day T-Bill Cut-Off Yield"},
    "tbond_5y_yield": {"cadence": "monthly", "anomaly_threshold": 1.0, "name": "5-Year BGTB Cut-Off Yield"},
    "tbond_10y_yield": {"cadence": "monthly", "anomaly_threshold": 1.0, "name": "10-Year BGTB Cut-Off Yield"},
}


def load_indicators() -> list[dict]:
    return json.loads(SOURCES_V3.read_text())["indicators"]


def tracked_metric_ids(indicators: list[dict]) -> list[str]:
    """Every daily-pipeline indicator id (the data YieldScope surfaces),
    plus the retired-from-config yield ids (see _RETIRED_YIELD_IDS_METADATA)."""
    return [ind["id"] for ind in indicators] + list(_RETIRED_YIELD_IDS_METADATA)


def thresholds_by_metric(indicators: list[dict]) -> dict[str, float | None]:
    out = {ind["id"]: ind.get("anomaly_threshold") for ind in indicators}
    for mid, meta in _RETIRED_YIELD_IDS_METADATA.items():
        out.setdefault(mid, meta["anomaly_threshold"])
    return out


def cadence_by_metric(indicators: list[dict]) -> dict[str, str]:
    out = {ind["id"]: ind.get("cadence", "daily") for ind in indicators}
    for mid, meta in _RETIRED_YIELD_IDS_METADATA.items():
        out.setdefault(mid, meta["cadence"])
    return out


def label_by_metric(indicators: list[dict]) -> dict[str, str]:
    # sources-v3 uses `name` (not `label`) for the human-readable string.
    out = {ind["id"]: ind.get("name") or ind["id"] for ind in indicators}
    for mid, meta in _RETIRED_YIELD_IDS_METADATA.items():
        out.setdefault(mid, meta["name"])
    return out
