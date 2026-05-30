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


def load_indicators() -> list[dict]:
    return json.loads(SOURCES_V3.read_text())["indicators"]


def tracked_metric_ids(indicators: list[dict]) -> list[str]:
    """Every daily-pipeline indicator id (the data YieldScope surfaces)."""
    return [ind["id"] for ind in indicators]


def thresholds_by_metric(indicators: list[dict]) -> dict[str, float | None]:
    return {ind["id"]: ind.get("anomaly_threshold") for ind in indicators}


def cadence_by_metric(indicators: list[dict]) -> dict[str, str]:
    return {ind["id"]: ind.get("cadence", "daily") for ind in indicators}


def label_by_metric(indicators: list[dict]) -> dict[str, str]:
    # sources-v3 uses `name` (not `label`) for the human-readable string.
    return {ind["id"]: ind.get("name") or ind["id"] for ind in indicators}
