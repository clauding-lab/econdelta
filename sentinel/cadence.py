"""Resolve each metric_id's reporting cadence and its freshness grace window.

The sentinel needs, for every metric_id it finds in Supabase, the cadence that
governs "how stale is too stale". Cadence lives in several places:

  1. ``config/sources-v3.json`` — authoritative for the ~74 scraped indicators.
  2. ``BRIEF_ALIASES`` / ``BRIEF_CONVERSIONS`` — brief-side keys inherit their
     source metric's cadence (a unit conversion changes the value, not the
     reporting period).
  3. ``DERIVED_DEFINITION_SEEDS`` — runtime-minted metrics (crr/slr utilisation).
  4. Scraper-only ids that never appear in config (dse index, dse_close_*
     tickers, pink-sheet commodities, imf tier-2) — a small explicit map + a
     few prefix rules.
  5. The MONTHLY table itself implies monthly cadence for any ``*_monthly`` id.

Anything it still cannot map is reported (never silently skipped) so it feeds
the E3.1 dedupe/retire decision rather than hiding a real freeze behind a guess.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCES_V3_PATH = REPO_ROOT / "config" / "sources-v3.json"

# Canonical cadence tiers → grace window (days). "daily" is TRADING-day aware
# (2 DSE sessions of lag, handled in freshness.is_breach); the rest are plain
# calendar-day windows. These are the tiers the E2.1 brief agreed.
GRACE_DAYS_BY_CADENCE: dict[str, int] = {
    "daily": 2,           # 2 BD trading days
    "weekly": 10,
    "monthly": 45,
    "quarterly": 165,
    "fiscal_year": 400,
}

# Scraper-produced ids that write metric_history directly (or via aggregate's
# flatten_data) and have NO sources-v3.json entry. Cadence is by construction.
_SCRAPER_CADENCE: dict[str, str] = {
    # bb_forex — daily
    "usd_bdt_mid": "daily",
    "usd_bdt_buy": "daily",
    "usd_bdt_sell": "daily",
    "eur_bdt": "daily",
    "gbp_bdt": "daily",
    "gross_reserves_usd_bn": "daily",
    "import_cover_months": "daily",
    "usd_bdt_exchange_rate": "daily",
    "fx_reserve_gross_and_bpm6": "daily",
    # dse_market index — daily (trading-day aware)
    "dsex": "daily",
    "ds30": "daily",
    "dses": "daily",
    "dsex_change": "daily",
    "dsex_change_pct": "daily",
    "turnover_crore": "daily",
    "total_trades": "daily",
    "advancing": "daily",
    "declining": "daily",
    "unchanged": "daily",
    # world_bank_pink_sheet — monthly
    "lng_price_usd_mmbtu": "monthly",
    "palm_oil_price_usd_mt": "monthly",
    "wheat_price_usd_mt": "monthly",
    # imf tier-2 — monthly (conservative; imf publishes on a slow cadence)
    "imf_eff_outstanding_sdr_mn": "monthly",
    # fiscal_gdp_ratios — annual figures (IMF DataMapper rev / World Bank tax),
    # one row per year. Their sources publish with a structural multi-year lag
    # (WB tax stops at 2021; IMF rev carries no forward projection), so both would
    # breach the fiscal_year grace by design. They are ENFORCED as non-alerting via
    # freshness.ACCEPTED_STALE_METRIC_IDS (routed to the silent `accepted_stale`
    # bucket, never `breaches`) — the cadence here is only for catalog/labeling.
    "rev_gdp_ratio": "fiscal_year",
    "tax_gdp_ratio": "fiscal_year",
}


def _prefix_cadence(metric_id: str) -> str | None:
    """Cadence for fanned-out / derived metric families keyed by id shape.

    Only consulted AFTER the config/alias/scraper maps, so it never overrides an
    authoritative cadence — it only catches ids those maps don't carry (e.g. the
    per-segment ownership clusters, per-tenor call-money keys, per-ticker DSE
    closes).
    """
    if metric_id.endswith("_monthly"):
        return "monthly"
    if metric_id.startswith("dse_close_"):
        return "daily"
    if metric_id.startswith("dse_sector_heat_"):
        return "daily"
    if metric_id.startswith("call_money_rate"):
        return "daily"
    if metric_id.startswith(("crr_utilisation", "slr_utilisation")):
        return "monthly"
    if metric_id.startswith(("npl_", "deposits_")):
        # FSR ownership clusters (npl_socb_pct, deposits_pcb_cr, …). The config
        # ids deposits_of_the_system / deposits_held_with_bb_crr resolve via
        # config first, so this only catches the quarterly ownership fan-out.
        return "quarterly"
    return None


def load_cadence_map(config_path: Path | str = SOURCES_V3_PATH) -> dict[str, str]:
    """Build metric_id → cadence for every id the config + derivations can name.

    Layered so config wins over aliases wins over the scraper map. Ids the map
    doesn't carry are resolved lazily by ``resolve_cadence`` (prefix rules +
    monthly-table fallback).
    """
    cfg = json.loads(Path(config_path).read_text())
    cadence: dict[str, str] = {
        ind["id"]: ind.get("cadence", "daily") for ind in cfg.get("indicators", [])
    }

    # Import lazily so this module stays importable (and unit-testable) without
    # pulling in aggregate_latest's pydantic/util chain unless a real map is built.
    from aggregate_latest import (
        BRIEF_ALIASES,
        BRIEF_CONVERSIONS,
        DERIVED_DEFINITION_SEEDS,
    )

    for brief_key, econ_key in BRIEF_ALIASES.items():
        if econ_key in cadence and brief_key not in cadence:
            cadence[brief_key] = cadence[econ_key]
    for brief_key, (src_key, _mult) in BRIEF_CONVERSIONS.items():
        if src_key in cadence and brief_key not in cadence:
            cadence[brief_key] = cadence[src_key]
    for seed in DERIVED_DEFINITION_SEEDS:
        cadence.setdefault(seed["metric_id"], seed.get("cadence", "monthly"))

    for mid, c in _SCRAPER_CADENCE.items():
        cadence.setdefault(mid, c)

    return cadence


def resolve_cadence(
    metric_id: str,
    cadence_map: dict[str, str],
    *,
    from_monthly_table: bool = False,
) -> str | None:
    """Resolve one metric's cadence, or None if genuinely unknown.

    Args:
        metric_id: the id to resolve.
        cadence_map: the pre-built map from ``load_cadence_map``.
        from_monthly_table: True if this id was seen only in
            ``metric_history_monthly`` — the table itself implies monthly cadence.
    """
    c = cadence_map.get(metric_id)
    if c:
        return c
    c = _prefix_cadence(metric_id)
    if c:
        return c
    if from_monthly_table:
        return "monthly"
    return None
