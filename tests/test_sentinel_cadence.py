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


def test_commodity_prices_ids_are_mapped_daily():
    """brent_crude_usd_barrel/wti_crude_usd_barrel/gold_usd_oz are
    aggregate_latest's flatten unit-suffixed keys for the daily (23:08 UTC)
    commodity_prices scraper — previously absent from _SCRAPER_CADENCE despite
    landing fresh rows every day, so the sentinel could never see them."""
    m = load_cadence_map()
    assert m["brent_crude_usd_barrel"] == "daily"
    assert m["wti_crude_usd_barrel"] == "daily"
    assert m["gold_usd_oz"] == "daily"


def test_import_cover_months_has_no_cadence_mapping():
    """Zero rows ever (BB's reserves page has never published it) -- removed
    from _SCRAPER_CADENCE so it correctly resolves to unmapped, not a false
    'daily' cadence with nothing to judge against."""
    m = load_cadence_map()
    assert resolve_cadence("import_cover_months", m) is None


def test_gross_reserves_is_monthly_not_daily():
    """Reclassified with the Tier-1 as_of forgery fix: BB's reserves figure is
    an END-of-month stock, not a daily-moving one. Was 'daily' (2-day grace),
    which would falsely flag a correctly-dated month-old reading as stale."""
    m = load_cadence_map()
    assert m["gross_reserves_usd_bn"] == "monthly"


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


def test_retired_yield_ids_split_bills_weekly_bonds_monthly():
    """Opus review round 1, H2 (blocker): the original PR-C comment assigned
    ALL 5 auction-derived yield ids "weekly" while simultaneously describing
    5y/10y bonds as auctioning "far less often" than bills -- a self-
    contradiction. Bills genuinely auction roughly weekly; 5y/10y BGTB bonds
    auction roughly monthly-to-quarterly and need the wider grace."""
    m = load_cadence_map()
    assert m["bill_bond_rates"] == "weekly"
    assert m["tbill_182d_yield"] == "weekly"
    assert m["tbill_364d_yield"] == "weekly"
    assert m["tbond_5y_yield"] == "monthly"
    assert m["tbond_10y_yield"] == "monthly"


def test_retired_yield_ids_brief_aliases_inherit_the_split_cadence():
    """The scraper-only cadence map merges BEFORE the alias loop (PR-C fix)
    specifically so these aliases see a cadence at all -- pin that the
    bond/bill split survives alias inheritance too."""
    m = load_cadence_map()
    assert m["tbond_bond_5y"] == "monthly"
    assert m["tbond_bond_10y"] == "monthly"
    assert m["tbond_tbill_182d"] == "weekly"
    assert m["tbond_tbill_364d"] == "weekly"
    assert m["tbond_tbill_91d"] == "weekly"
    assert m["tbill_91d_yield_pct"] == "weekly"
