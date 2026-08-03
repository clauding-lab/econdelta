"""scrapers/bb_npl_structure.py

Banking-structure NPL metrics.

FSR-written family (annual): sector-wise NPL distribution from BB's
Financial Stability Report Table 2.3 — 8 top-level sectors (rate + share
of lending), 4 sub-sector rates, total advances and gross NPL stock.
One LLM extraction pass over a deterministic slice of the document, hard
arithmetic gate (full reconciliation), all-or-nothing upsert.

Seed-only family (no scheduled source — press/parliament disclosures):
band-wise NPL rates/outstandings and CMSME segment rates, written once by
scripts/seed_npl_structure.py with source "bb_via_press_static".

These ids are deliberately NOT in config/sources-v3.json and must never
join briefing.config.CORE_METRIC_IDS (owner decision: non-gating) nor
leave sentinel ACCEPTED_STALE (structural source lag). See the spec
amendment: docs/superpowers/specs/2026-08-03-bb-npl-structure-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass

SOURCE_LABEL = "BB FSR"
SEED_ONLY_SOURCE_NOTE = "bb_via_press_static"


@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: str      # "percent" | "amount_bdt_crore"
    family: str    # sector_rate | sector_share | sub_rate | total | band_rate | band_outstanding | cmsme
    fsr: bool      # True = written by this scraper from the FSR; False = seed-only


_SECTORS = {
    "agriculture": "Agriculture",
    "industrial_mfg": "Industrial (Manufacturing)",
    "industrial_services": "Industrial (Services)",
    "consumer_credit": "Consumer Credit",
    "trade_commerce": "Trade and Commerce (Commercial Loans)",
    "nbfi": "Credit to NBFI",
    "capital_market": "Loans to Capital Market",
    "other": "Other Loans",
}

METRIC_SPECS: dict[str, MetricSpec] = {}
for _key, _name in _SECTORS.items():
    METRIC_SPECS[f"npl_rate_sector_{_key}"] = MetricSpec(
        f"NPL rate — {_name}", "percent", "sector_rate", True)
    METRIC_SPECS[f"lending_share_sector_{_key}"] = MetricSpec(
        f"Share of lending — {_name}", "percent", "sector_share", True)
METRIC_SPECS.update({
    "npl_rate_sub_rmg": MetricSpec("NPL rate — RMG", "percent", "sub_rate", True),
    "npl_rate_sub_construction": MetricSpec("NPL rate — construction loans", "percent", "sub_rate", True),
    "npl_rate_sub_housing_finance": MetricSpec("NPL rate — housing finance", "percent", "sub_rate", True),
    "npl_rate_sub_smc_industries": MetricSpec(
        "NPL rate — other industries (small, medium and cottage)", "percent", "sub_rate", True),
    "total_bank_advances": MetricSpec(
        "Total loans and advances of the banking sector", "amount_bdt_crore", "total", True),
    "gross_npl_stock": MetricSpec(
        "Gross non-performing loans of the banking sector", "amount_bdt_crore", "total", True),
    # --- seed-only: band-wise + CMSME (press/parliament disclosures) ---
    "npl_rate_band_lt1cr": MetricSpec("NPL rate — loans under Tk 1 crore", "percent", "band_rate", False),
    "npl_rate_band_1_10cr": MetricSpec("NPL rate — loans Tk 1-10 crore", "percent", "band_rate", False),
    "npl_rate_band_10_20cr": MetricSpec("NPL rate — loans Tk 10-20 crore", "percent", "band_rate", False),
    "npl_rate_band_20_30cr": MetricSpec("NPL rate — loans Tk 20-30 crore", "percent", "band_rate", False),
    "npl_rate_band_30_40cr": MetricSpec("NPL rate — loans Tk 30-40 crore", "percent", "band_rate", False),
    "npl_rate_band_40_50cr": MetricSpec("NPL rate — loans Tk 40-50 crore", "percent", "band_rate", False),
    "npl_rate_band_gt50cr": MetricSpec("NPL rate — loans above Tk 50 crore", "percent", "band_rate", False),
    "loans_outstanding_band_lt1cr": MetricSpec(
        "Outstanding loans — under Tk 1 crore", "amount_bdt_crore", "band_outstanding", False),
    "loans_outstanding_band_1_10cr": MetricSpec(
        "Outstanding loans — Tk 1-10 crore", "amount_bdt_crore", "band_outstanding", False),
    "loans_outstanding_band_gt50cr": MetricSpec(
        "Outstanding loans — above Tk 50 crore", "amount_bdt_crore", "band_outstanding", False),
    "npl_rate_cmsme_overall": MetricSpec("NPL rate — CMSME overall", "percent", "cmsme", False),
    "npl_rate_cmsme_cottage": MetricSpec("NPL rate — cottage industry", "percent", "cmsme", False),
    "npl_rate_cmsme_medium": MetricSpec("NPL rate — medium enterprise", "percent", "cmsme", False),
})

# LLM payload keys: every FSR-written id + the check-only overall ratio.
# overall_npl_ratio_fsr is NEVER stored — gross_npl_ratio (QFSAR-sourced)
# owns the overall series; the FSR figure is a different vintage.
FSR_EXTRACTION_KEYS: tuple[str, ...] = tuple(
    m for m, s in METRIC_SPECS.items() if s.fsr
) + ("overall_npl_ratio_fsr",)

REQUIRED_EXTRACTION_KEYS: frozenset[str] = frozenset(
    m for m, s in METRIC_SPECS.items() if s.fsr and s.family != "sub_rate"
) | {"overall_npl_ratio_fsr"}


def build_definitions_rows() -> list[dict]:
    """metric_definitions seed rows (first-insert-wins — right on day one).

    cadence "fiscal_year" is truth-in-labeling for annual-with-lag series;
    the sentinel additionally carries every id in ACCEPTED_STALE_METRIC_IDS.

    grace_days=400 is included because ``utils.supabase_writer._normalize_definition``
    passes unknown keys through untouched (it merges caller fields over its
    defaults with no allow-list) and the live ``metric_definitions`` table has
    a ``grace_days`` column the writer's own default set doesn't cover. These
    rows are annual-with-lag (FSR is published well over a year in arrears),
    so the freshness grace window must be wide from the first insert.
    """
    return [
        {
            "metric_id": mid,
            "label": spec.label,
            "domain": "money_market",
            "unit": spec.unit,
            "cadence": "fiscal_year",
            "source": SOURCE_LABEL if spec.fsr else SEED_ONLY_SOURCE_NOTE,
            "grace_days": 400,
        }
        for mid, spec in METRIC_SPECS.items()
    ]
