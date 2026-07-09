"""Cadence resolution for the freshness sentinel (E2.1)."""
from __future__ import annotations

from sentinel.cadence import (
    GRACE_DAYS_BY_CADENCE,
    load_cadence_map,
    resolve_cadence,
)


def test_grace_tiers_match_agreed_design():
    assert GRACE_DAYS_BY_CADENCE == {
        "daily": 2,
        "weekly": 10,
        "monthly": 45,
        "quarterly": 165,
        "fiscal_year": 400,
    }


def test_config_ids_resolve_to_their_configured_cadence():
    m = load_cadence_map()
    assert m["money_multiplier"] == "monthly"
    assert m["gross_npl_ratio"] == "quarterly"
    assert m["banking_sector_crar"] == "quarterly"
    assert m["tax_revenue"] == "monthly"


def test_brief_alias_inherits_source_cadence():
    m = load_cadence_map()
    # banking_npl_pct is a 1:1 alias of gross_npl_ratio (quarterly).
    assert m["banking_npl_pct"] == "quarterly"
    # macro_cpi_headline aliases general_inflation (monthly).
    assert m["macro_cpi_headline"] == "monthly"


def test_brief_conversion_target_inherits_source_cadence():
    m = load_cadence_map()
    # fiscal_bank_borrow_trn = bank_borrowing_for_deficit_financing (monthly) × 1e-5.
    assert m["fiscal_bank_borrow_trn"] == "monthly"


def test_scraper_only_ids_are_mapped():
    m = load_cadence_map()
    assert m["dsex"] == "daily"
    assert m["lng_price_usd_mmbtu"] == "monthly"
    assert m["usd_bdt_mid"] == "daily"


def test_resolve_falls_back_to_prefix_rules():
    m = load_cadence_map()
    # per-ticker DSE close (not in config)
    assert resolve_cadence("dse_close_GP", m) == "daily"
    # FSR ownership cluster fan-out
    assert resolve_cadence("npl_socb_pct", m) == "quarterly"
    assert resolve_cadence("deposits_pcb_cr", m) == "quarterly"
    # per-tenor call money
    assert resolve_cadence("call_money_rate_7d", m) == "daily"


def test_monthly_table_implies_monthly_when_otherwise_unknown():
    m = load_cadence_map()
    assert resolve_cadence("cpi_headline_monthly", m) == "monthly"
    # a genuinely unknown id seen only in the monthly table still resolves monthly
    assert resolve_cadence("mystery_series", m, from_monthly_table=True) == "monthly"


def test_unknown_id_resolves_to_none():
    m = load_cadence_map()
    assert resolve_cadence("totally_unknown_xyz", m) is None
