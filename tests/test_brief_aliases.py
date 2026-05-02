"""Tests for the brief-key alias / unit-conversion / NBR cross-check helper.

`_apply_brief_aliases` is the bridge between EconDelta's indicator IDs
(stable engineering names) and the brief's metric_id conventions
(macro_*, remit_*, fiscal_*, banking_*, food_*). Three behaviours under
test:
  1. 1:1 aliases — copy source value under brief-key name
  2. Unit conversions — apply multiplier (T-Bill/Bond mn → crore)
  3. NBR cross-check — tolerance-bounded average vs mismatch flag
"""
from __future__ import annotations

from aggregate_latest import _apply_brief_aliases


def test_simple_alias_surfaces_brief_key():
    data = {"food_inflation": 8.29, "monthly_remittance": 2.88}
    _apply_brief_aliases(data)
    assert data["macro_cpi_food"] == 8.29
    assert data["remit_monthly_mn"] == 2.88
    # Source key untouched
    assert data["food_inflation"] == 8.29


def test_alias_skips_when_source_missing():
    data = {"food_inflation": 8.29}
    _apply_brief_aliases(data)
    assert "macro_cpi_headline" not in data  # general_inflation absent


def test_alias_does_not_overwrite_preset_brief_key():
    """If a brief_key is already populated (e.g. from a prior pass or a
    hand-override), the alias step leaves it alone."""
    data = {"food_inflation": 8.29, "macro_cpi_food": 99.99}
    _apply_brief_aliases(data)
    assert data["macro_cpi_food"] == 99.99


def test_tbill_outstanding_unit_converts_mn_to_crore():
    """T-Bill outstanding source is BDT million (gsom reports it that way).
    Brief consumers expect BDT crore, so the alias step divides by 10."""
    data = {"treasury_bill_outstanding": 2_004_863.6}
    _apply_brief_aliases(data)
    assert data["tbill_outstanding_cr"] == 200_486.36


def test_tbond_outstanding_unit_converts_mn_to_crore():
    data = {"treasury_bond_outstanding": 5_767_587.2}
    _apply_brief_aliases(data)
    assert data["tbond_outstanding_cr"] == 576_758.72


def test_food_aliases_pass_through_dam_prices():
    data = {
        "food_rice_coarse": 49.0,
        "food_chicken_farm": 164.5,
        "food_sugar_local": 133.5,
    }
    _apply_brief_aliases(data)
    assert data["food_rice_coarse_bdt"] == 49.0
    assert data["food_chicken_farm_bdt"] == 164.5
    assert data["food_sugar_local_bdt"] == 133.5


def test_nbr_cross_check_confirmed_within_tolerance():
    """TBS and DS values within 5% → 'confirmed' + mean. Within 5% allows
    ~one month of drift since one outlet may have a fresher cumulative."""
    data = {
        "nbr_fytd_collected_tbs": 287_862.0,
        "nbr_fytd_collected_dailystar": 285_000.0,  # ~1% off
    }
    _apply_brief_aliases(data)
    assert data["nbr_fytd_cross_check"] == "confirmed"
    assert data["nbr_fytd_collected_cr"] == round((287_862.0 + 285_000.0) / 2, 2)


def test_nbr_cross_check_mismatch_outside_tolerance():
    """TBS and DS values >5% apart → 'mismatch' flag + use larger figure
    (cumulative collection only grows within a fiscal year)."""
    data = {
        "nbr_fytd_collected_tbs": 287_862.0,
        "nbr_fytd_collected_dailystar": 254_000.0,  # ~12% off
    }
    _apply_brief_aliases(data)
    assert data["nbr_fytd_cross_check"].startswith("mismatch_")
    # Larger value wins
    assert data["nbr_fytd_collected_cr"] == 287_862.0


def test_nbr_cross_check_tbs_only():
    data = {"nbr_fytd_collected_tbs": 287_862.0}
    _apply_brief_aliases(data)
    assert data["nbr_fytd_cross_check"] == "tbs_only"
    assert data["nbr_fytd_collected_cr"] == 287_862.0


def test_nbr_cross_check_dailystar_only():
    data = {"nbr_fytd_collected_dailystar": 254_000.0}
    _apply_brief_aliases(data)
    assert data["nbr_fytd_cross_check"] == "dailystar_only"
    assert data["nbr_fytd_collected_cr"] == 254_000.0


def test_nbr_cross_check_neither_source():
    data = {}
    _apply_brief_aliases(data)
    assert "nbr_fytd_collected_cr" not in data
    assert "nbr_fytd_cross_check" not in data


def test_banking_ratio_aliases_propagate_fsar_values():
    data = {"gross_npl_ratio": 35.73, "banking_sector_crar": 1.56}
    _apply_brief_aliases(data)
    assert data["banking_npl_pct"] == 35.73
    assert data["banking_car_pct"] == 1.56
