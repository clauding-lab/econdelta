"""One-shot migration script: config/sources-v2.json → config/sources-v3.json.

Reads every indicator from the v2 registry and augments it with v3-required fields:
  - domain
  - parse.deterministic
  - parse.value_type
  - parse.valid_range
  - parse.llm_prompt
  - anomaly_threshold

The META dict is the hand-curated mapping from v2 indicator id → augmentation values.
DEFAULT_META is the fallback for any id not explicitly listed.

Usage:
    python scripts/build_sources_v3.py \\
        --in config/sources-v2.json \\
        --out config/sources-v3.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Hand-curated augmentation per indicator id
# ---------------------------------------------------------------------------

DEFAULT_META: dict[str, Any] = {
    "domain": "macro",
    "deterministic": "pdf_table_row",
    "value_type": "amount_bdt_crore",
    "valid_range": [0.0, 100_000_000.0],
    "llm_prompt": "pdf_table_row.txt",
    "anomaly_threshold": 0.05,
}

META: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------
    # inflation
    # ------------------------------------------------------------------
    "point_to_point_inflation": {
        "domain": "inflation",
        "deterministic": "pdf_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 50.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 1.0,
    },
    "general_inflation": {
        "domain": "inflation",
        "deterministic": "pdf_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 50.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 1.0,
    },
    "food_inflation": {
        "domain": "inflation",
        "deterministic": "pdf_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 50.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 1.0,
    },
    "non_food_inflation": {
        "domain": "inflation",
        "deterministic": "pdf_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 50.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 1.0,
    },
    # ------------------------------------------------------------------
    # government_finance
    # ------------------------------------------------------------------
    "budget_opex_of_the_fy_vs_utilization": {
        "domain": "government_finance",
        "deterministic": "pdf_table_total",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 1_000_000.0],
        "llm_prompt": "pdf_table_total.txt",
        "anomaly_threshold": 0.10,
    },
    "budget_adpex_of_the_fy_vs_utilization": {
        "domain": "government_finance",
        "deterministic": "pdf_table_total",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 500_000.0],
        "llm_prompt": "pdf_table_total.txt",
        "anomaly_threshold": 0.10,
    },
    "tax_revenue": {
        "domain": "government_finance",
        "deterministic": "pdf_component",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 500_000.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "non_tax_revenue": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 100_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "total_revenue_budget_vs_actual": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 600_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "tax_gdp_ratio": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 30.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.5,
    },
    "rev_gdp_ratio": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 40.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.5,
    },
    "foreign_borrowing_for_budget_deficit": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 200_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.10,
    },
    "domestic_borrowing_for_budget_deficit": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 400_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.10,
    },
    "bank_borrowing_for_deficit_financing": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 400_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.10,
    },
    "non_bank_borrowing_for_deficit_financing": {
        "domain": "government_finance",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 200_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.10,
    },
    # ------------------------------------------------------------------
    # money_market
    # ------------------------------------------------------------------
    "treasury_bill_outstanding": {
        "domain": "money_market",
        "deterministic": "html_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 500_000.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "treasury_bond_outstanding": {
        "domain": "money_market",
        "deterministic": "html_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 2_000_000.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "bill_bond_rates": {
        "domain": "money_market",
        "deterministic": "html_table_row",
        "value_type": "percent",
        "valid_range": [0.0, 25.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 1.0,
    },
    "policy_rate_slf_sdf": {
        "domain": "money_market",
        "deterministic": "html_footer_ticker",
        "value_type": "percent",
        "valid_range": [0.5, 25.0],
        "llm_prompt": "html_footer_ticker.txt",
        "anomaly_threshold": 1.0,
    },
    "call_money_rate": {
        "domain": "money_market",
        "deterministic": "html_call_money",
        "value_type": "percent",
        "valid_range": [0.0, 25.0],
        "llm_prompt": "html_call_money.txt",
        "anomaly_threshold": 2.0,
    },
    "gsec_auction": {
        "domain": "money_market",
        "deterministic": "html_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 50_000.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    # gsec_maturity removed: source URL was placeholder "Own Data Hub"
    # (data was meant to come from an internal data hub not yet built).
    # Re-add when an internal source or BB URL is identified.
    "interbank_repo_data": {
        "domain": "money_market",
        "deterministic": "html_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 100_000.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    # ------------------------------------------------------------------
    # forex_and_reserves
    # ------------------------------------------------------------------
    "usd_bdt_exchange_rate": {
        "domain": "forex_and_reserves",
        "deterministic": "html_footer_ticker",
        "value_type": "rate",
        "valid_range": [80.0, 200.0],
        "llm_prompt": "html_footer_ticker.txt",
        "anomaly_threshold": 0.02,
    },
    "fx_buy_sale_from_market": {
        "domain": "forex_and_reserves",
        "deterministic": "html_table_row",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 5.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "fx_reserve_gross_and_bpm6": {
        "domain": "forex_and_reserves",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 100.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    # ------------------------------------------------------------------
    # monetary_aggregates
    # ------------------------------------------------------------------
    "private_sector_credit": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_component",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 100_000_000.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "deposits_of_the_system": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 30_000_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "currency_outside_bank": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 5_000_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "broad_money": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_component",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 30_000_000.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "reserve_money": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_component",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 10_000_000.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "deposits_held_with_bb_crr": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 5_000_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "money_multiplier": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_table_row",
        "value_type": "ratio",
        "valid_range": [1.0, 20.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "excess_liquid_asset_total_minimum": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_table_row",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 5_000_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "nsc_outstanding": {
        "domain": "monetary_aggregates",
        "deterministic": "pdf_component",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 5_000_000.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    # ------------------------------------------------------------------
    # external_sector
    # ------------------------------------------------------------------
    "monthly_export": {
        "domain": "external_sector",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 10.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "fy_export": {
        "domain": "external_sector",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 60.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.10,
    },
    "categorywise_export": {
        "domain": "external_sector",
        "deterministic": "pdf_table_row",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 60.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.10,
    },
    "monthly_import": {
        "domain": "external_sector",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 10.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "monthly_import_lc_opening": {
        # BB MEI Import Payments report values are USD millions, not billions.
        # Sonnet correctly extracted 6346.29 (~$6.3B/mo); old config rejected it.
        "domain": "external_sector",
        "deterministic": "pdf_table_row",
        "value_type": "amount_usd_mn",
        "valid_range": [0.0, 20_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "monthly_import_lc_settlement": {
        "domain": "external_sector",
        "deterministic": "pdf_table_row",
        "value_type": "amount_usd_mn",
        "valid_range": [0.0, 20_000.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "fy_import_lc": {
        "domain": "external_sector",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 100.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.10,
    },
    "categorywise_fy_import_breakdown": {
        "domain": "external_sector",
        "deterministic": "pdf_table_row",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 100.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.10,
    },
    "monthly_remittance": {
        "domain": "external_sector",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 5.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
    "fy_remittance": {
        "domain": "external_sector",
        "deterministic": "pdf_component",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 30.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.10,
    },
    "remittance_by_country": {
        "domain": "external_sector",
        "deterministic": "pdf_table_row",
        "value_type": "amount_usd_bn",
        "valid_range": [0.0, 10.0],
        "llm_prompt": "pdf_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    "bop_summary": {
        "domain": "external_sector",
        "deterministic": "html_table_row",
        "value_type": "amount_usd_bn",
        "valid_range": [-20.0, 20.0],
        "llm_prompt": "html_table_row.txt",
        "anomaly_threshold": 0.05,
    },
    # ------------------------------------------------------------------
    # macro
    # ------------------------------------------------------------------
    "gdp": {
        "domain": "macro",
        "deterministic": "pdf_component",
        "value_type": "amount_bdt_crore",
        "valid_range": [0.0, 100_000_000.0],
        "llm_prompt": "pdf_component.txt",
        "anomaly_threshold": 0.05,
    },
}

# Publication index pages that require discover="latest_pdf_link"
_PDF_INDEX_SUFFIXES = ("/3/11", "/5/27", "/3/58")


def _needs_discover(url: str | None) -> bool:
    """Return True if the URL is a BB publication index page."""
    if not url:
        return False
    return any(url.rstrip("/").endswith(suffix) for suffix in _PDF_INDEX_SUFFIXES)


def _build_fetch(primary: dict[str, Any]) -> dict[str, Any]:
    """Construct the v3 fetch block from a v2 primary source entry."""
    fetch: dict[str, Any] = {
        "type": primary.get("type", "html"),
        "url": primary.get("url", ""),
    }
    task = primary.get("task")
    if task:
        fetch["task"] = task
    if _needs_discover(fetch["url"]):
        fetch["discover"] = "latest_pdf_link"
    return fetch


def _augment(ind: dict[str, Any]) -> dict[str, Any]:
    """Return a new v3 indicator dict augmented from a v2 indicator dict."""
    ind_id: str = ind["id"]
    meta = META.get(ind_id, DEFAULT_META)

    primary: dict[str, Any] = ind.get("primary") or {}
    alternate = ind.get("alternate")
    fallback = ind.get("fallback")

    result: dict[str, Any] = {
        "id": ind_id,
        "name": ind["name"],
        "domain": meta["domain"],
        "cadence": ind.get("cadence", "monthly"),
        "fetch": _build_fetch(primary),
        "parse": {
            "deterministic": meta["deterministic"],
            "llm_prompt": meta["llm_prompt"],
            "value_type": meta["value_type"],
            "valid_range": meta["valid_range"],
        },
        "anomaly_threshold": meta["anomaly_threshold"],
    }

    # Preserve alternate/fallback verbatim (may be None)
    if alternate is not None:
        result["alternate"] = alternate
    if fallback is not None:
        result["fallback"] = fallback

    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Migrate sources-v2.json → sources-v3.json")
    p.add_argument("--in", dest="in_path", required=True, help="Path to sources-v2.json")
    p.add_argument("--out", dest="out_path", required=True, help="Destination path for sources-v3.json")
    args = p.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path)

    v2: dict[str, Any] = json.loads(in_path.read_text())
    v2_indicators: list[dict[str, Any]] = v2["indicators"]

    v3_indicators = [_augment(ind) for ind in v2_indicators]

    v3: dict[str, Any] = {
        "version": "3.0",
        "generated_from": v2.get("version", "2.0"),
        "indicators": v3_indicators,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(v3, indent=2, ensure_ascii=False) + "\n")

    n = len(v3_indicators)
    print(f"Wrote {n} indicators to {out_path}")


if __name__ == "__main__":
    main()
