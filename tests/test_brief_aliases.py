"""Tests for the brief-key alias / unit-conversion helper.

`_apply_brief_aliases` is the bridge between EconDelta's indicator IDs
(stable engineering names) and the brief's metric_id conventions
(macro_*, remit_*, fiscal_*, banking_*, food_*). Two behaviours under
test:
  1. 1:1 aliases — copy source value under brief-key name
  2. Unit conversions — apply multiplier (T-Bill/Bond mn → crore)
"""
from __future__ import annotations

from aggregate_latest import _apply_brief_aliases


def test_simple_alias_surfaces_brief_key():
    data = {"food_inflation": 8.29, "general_inflation": 8.58}
    _apply_brief_aliases(data)
    assert data["macro_cpi_food"] == 8.29
    assert data["macro_cpi_headline"] == 8.58
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


def test_fiscal_nbr_collected_converts_crore_to_trillion():
    """EconDelta tax_revenue is BDT crore; brief renders BDT trillion.
    1 trillion = 100,000 crore → multiplier 0.00001."""
    data = {"tax_revenue": 119478.0}  # ~BDT 1.19 trillion
    _apply_brief_aliases(data)
    assert data["fiscal_nbr_collected_trn"] == 1.19


def test_fiscal_govt_borrow_converts_crore_to_trillion():
    data = {"domestic_borrowing_for_budget_deficit": 67913.54}
    _apply_brief_aliases(data)
    assert data["fiscal_govt_borrow_trn"] == 0.68


def test_remit_monthly_converts_bn_to_mn():
    """EconDelta monthly_remittance is USD bn; brief expects USD mn.
    1 bn = 1000 mn → multiplier 1000."""
    data = {"monthly_remittance": 2.88949}  # 2.89bn USD
    _apply_brief_aliases(data)
    assert data["remit_monthly_mn"] == 2889.49


def test_remit_fy_converts_bn_to_mn():
    data = {"fy_remittance": 26.5}
    _apply_brief_aliases(data)
    assert data["remit_fy_mn"] == 26500.0


def test_macro_credit_growth_aliases_yoy_pct_source():
    """Phase 3.3: a dedicated YoY scrape (private_sector_credit_yoy_pct)
    lands as macro_credit_growth — NOT the absolute private_sector_credit
    crore amount, which has wrong units and was the original bug."""
    data = {
        "private_sector_credit_yoy_pct": 7.2,
        "private_sector_credit": 1_773_829.7,  # absolute BDT crore — must NOT leak in
    }
    _apply_brief_aliases(data)
    assert data["macro_credit_growth"] == 7.2  # YoY % source, not absolute crore


def test_macro_credit_growth_skips_when_yoy_source_missing():
    """If the YoY scrape didn't produce a value, the brief renders null —
    we DO NOT fall back to the absolute amount."""
    data = {"private_sector_credit": 1_773_829.7}
    _apply_brief_aliases(data)
    assert "macro_credit_growth" not in data


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


def test_nbr_fytd_collected_cr_from_tax_revenue():
    """tax_revenue (BB PDF, deterministic) is the canonical source for
    nbr_fytd_collected_cr after the 2026-05-25 retirement of news
    corroborators (TBS, Daily Star)."""
    data = {"tax_revenue": 287_862.59}
    _apply_brief_aliases(data)
    assert data["nbr_fytd_collected_cr"] == 287_862.59
    assert data["nbr_fytd_cross_check"] == "single_source_tax_revenue"


def test_nbr_fytd_collected_cr_absent_when_tax_revenue_missing():
    data = {}
    _apply_brief_aliases(data)
    assert "nbr_fytd_collected_cr" not in data
    assert "nbr_fytd_cross_check" not in data


def test_legacy_news_corroborator_keys_are_ignored():
    """nbr_fytd_collected_tbs / nbr_fytd_collected_dailystar were retired
    upstream; if they leak in from an old snapshot they must NOT influence
    nbr_fytd_collected_cr."""
    data = {
        "tax_revenue": 287_862.59,
        "nbr_fytd_collected_tbs": 0.0,
        "nbr_fytd_collected_dailystar": 203_000.0,
    }
    _apply_brief_aliases(data)
    assert data["nbr_fytd_collected_cr"] == 287_862.59
    assert data["nbr_fytd_cross_check"] == "single_source_tax_revenue"


def test_banking_ratio_aliases_propagate_fsar_values():
    data = {"gross_npl_ratio": 35.73, "banking_sector_crar": 1.56}
    _apply_brief_aliases(data)
    assert data["banking_npl_pct"] == 35.73
    assert data["banking_car_pct"] == 1.56


def test_nbr_decomposition_components_convert_crore_to_bn():
    """Phase 3.2: NBR articles report VAT/IT/Customs in BDT crore;
    brief's §12 expects BDT bn. 1 bn = 100 crore → multiplier 0.01."""
    data = {
        "nbr_vat_collected_cr":     112_500.0,  # ~1.125 lakh crore
        "nbr_it_collected_cr":       95_300.0,
        "nbr_customs_collected_cr":  64_700.0,
    }
    _apply_brief_aliases(data)
    assert data["nbr_vat_bn"] == 1125.0
    assert data["nbr_it_bn"] == 953.0
    assert data["nbr_customs_bn"] == 647.0


def test_nbr_decomposition_skips_when_components_missing():
    """If an article didn't report a component, the conversion skips it
    entirely — brief renders null for that card rather than fabricating."""
    data = {"nbr_vat_collected_cr": 112_500.0}  # IT and customs absent
    _apply_brief_aliases(data)
    assert data["nbr_vat_bn"] == 1125.0
    assert "nbr_it_bn" not in data
    assert "nbr_customs_bn" not in data


def test_dse_sector_heat_flattens_to_per_sector_numeric_keys():
    """Phase 3.1: parser emits dict[sector, pct]; aggregate splits it into
    8 numeric keys (one per sector) so Supabase metric_history persists each.
    Brief reconstructs the dict from these keys via the history client."""
    data = {
        "dse_sector_heat": {
            "Banks":   -1.40,
            "NBFI":    -1.10,
            "Textile":  0.98,
            "Pharma":  -1.24,
            "Fuel":    -1.20,
            "Telecom": -0.16,
            "Food":    -0.54,
            "IT":       1.15,
        },
    }
    _apply_brief_aliases(data)
    assert data["dse_sector_heat_banks"] == -1.40
    assert data["dse_sector_heat_nbfi"] == -1.10
    assert data["dse_sector_heat_textile"] == 0.98
    assert data["dse_sector_heat_pharma"] == -1.24
    assert data["dse_sector_heat_fuel"] == -1.20
    assert data["dse_sector_heat_telecom"] == -0.16
    assert data["dse_sector_heat_food"] == -0.54
    assert data["dse_sector_heat_it"] == 1.15
    # Original dict still present (latest.json local consumers can use it)
    assert data["dse_sector_heat"]["Banks"] == -1.40


def test_dse_sector_heat_flatten_skips_non_numeric_entries():
    """Defensive: a malformed dict entry is dropped, others still flatten."""
    data = {
        "dse_sector_heat": {"Banks": -1.40, "Pharma": "n/a", "IT": 1.15},
    }
    _apply_brief_aliases(data)
    assert data["dse_sector_heat_banks"] == -1.40
    assert data["dse_sector_heat_it"] == 1.15
    assert "dse_sector_heat_pharma" not in data


def test_dse_sector_heat_flatten_noop_when_indicator_absent():
    data = {"some_other_key": 1.0}
    _apply_brief_aliases(data)
    assert all(not k.startswith("dse_sector_heat_") for k in data)


def test_call_money_rate_flattens_to_per_tenor_keys_and_promotes_1d():
    """``call_money_rate`` parser emits dict[tenor, rate] for 4 tenors
    (1D/7D/14D/90D); aggregate splits it into 4 numeric keys so Supabase
    metric_history persists each, AND promotes the 1D (overnight) rate to
    the scalar ``call_money_rate`` itself. BB convention: "call money
    rate" without modifier means overnight. The promotion makes the
    existing ``banking_call_money_rate`` brief alias actually work."""
    data = {
        "call_money_rate": {"1D": 9.50, "7D": 9.75, "14D": 10.10, "90D": 10.50},
    }
    _apply_brief_aliases(data)
    # 4 per-tenor scalars
    assert data["call_money_rate_1d"] == 9.50
    assert data["call_money_rate_7d"] == 9.75
    assert data["call_money_rate_14d"] == 10.10
    assert data["call_money_rate_90d"] == 10.50
    # 1D promoted to the headline scalar — dict replaced
    assert data["call_money_rate"] == 9.50
    # Brief alias bridges the headline scalar via BRIEF_ALIASES
    assert data["banking_call_money_rate"] == 9.50


def test_call_money_rate_flatten_idempotent_per_tenor():
    """If a per-tenor key was hand-set upstream (e.g. by a future direct
    parser), the flatten step leaves it alone."""
    data = {
        "call_money_rate": {"1D": 9.50, "7D": 9.75, "14D": 10.10, "90D": 10.50},
        "call_money_rate_1d": 8.00,  # pre-set
    }
    _apply_brief_aliases(data)
    assert data["call_money_rate_1d"] == 8.00  # untouched
    assert data["call_money_rate_7d"] == 9.75
    assert data["call_money_rate_14d"] == 10.10
    assert data["call_money_rate_90d"] == 10.50


def test_call_money_rate_flatten_skips_non_numeric_tenor():
    """Defensive: a malformed tenor entry (string, None) is dropped, the
    other tenors still flatten and the 1D promotion still runs."""
    data = {
        "call_money_rate": {"1D": 9.50, "7D": "n/a", "14D": None, "90D": 10.50},
    }
    _apply_brief_aliases(data)
    assert data["call_money_rate_1d"] == 9.50
    assert data["call_money_rate_90d"] == 10.50
    assert "call_money_rate_7d" not in data
    assert "call_money_rate_14d" not in data
    # 1D still promoted
    assert data["call_money_rate"] == 9.50


def test_call_money_rate_already_scalar_is_left_untouched():
    """If a future direct PDF scraper writes ``call_money_rate`` as a
    scalar directly, the flatten step is a no-op (isinstance dict check
    short-circuits) — the upstream value wins."""
    data = {"call_money_rate": 9.50}
    _apply_brief_aliases(data)
    assert data["call_money_rate"] == 9.50
    # No per-tenor keys minted
    assert all(not k.startswith("call_money_rate_") for k in data)
    # Brief alias still bridges the scalar
    assert data["banking_call_money_rate"] == 9.50


def test_call_money_rate_flatten_noop_when_indicator_absent():
    data = {"some_other_key": 1.0}
    _apply_brief_aliases(data)
    assert "call_money_rate" not in data
    assert all(not k.startswith("call_money_rate_") for k in data)


def test_npl_by_ownership_flattens_to_four_percent_scalars():
    """S10: the ``pdf_fsr_ownership_cluster`` parser emits a 4-key segment dict;
    aggregate fans it out into npl_<segment>_pct scalars so Supabase
    metric_history persists each. NPL is a percent (no level)."""
    data = {
        "npl_by_ownership": {
            "socb": 44.7,
            "pcb": 9.3,
            "fcb": 5.0,
            "specialised": 13.1,
        },
    }
    _apply_brief_aliases(data)
    assert data["npl_socb_pct"] == 44.7
    assert data["npl_pcb_pct"] == 9.3
    assert data["npl_fcb_pct"] == 5.0
    assert data["npl_specialised_pct"] == 13.1
    # SOCB plausibly highest (exit criterion).
    assert data["npl_socb_pct"] == max(
        data["npl_socb_pct"], data["npl_pcb_pct"],
        data["npl_fcb_pct"], data["npl_specialised_pct"],
    )
    # Original dict still present for local latest.json consumers.
    assert data["npl_by_ownership"]["socb"] == 44.7


def test_deposits_by_ownership_flattens_to_four_crore_scalars():
    """S10: deposits cluster fans out into deposits_<segment>_cr LEVELS in BDT
    crore (NOT shares — the donut computes shares downstream)."""
    data = {
        "deposits_by_ownership": {
            "socb": 365000.0,
            "pcb": 1180000.0,
            "fcb": 58000.0,
            "specialised": 44000.0,
        },
    }
    _apply_brief_aliases(data)
    assert data["deposits_socb_cr"] == 365000.0
    assert data["deposits_pcb_cr"] == 1180000.0
    assert data["deposits_fcb_cr"] == 58000.0
    assert data["deposits_specialised_cr"] == 44000.0


def test_both_ownership_clusters_yield_eight_scalars():
    """All 8 fanned-out scalars (4 NPL + 4 deposits) land from one pass."""
    data = {
        "npl_by_ownership": {"socb": 40.0, "pcb": 8.0, "fcb": 4.0, "specialised": 12.0},
        "deposits_by_ownership": {
            "socb": 350000.0, "pcb": 1100000.0, "fcb": 55000.0, "specialised": 42000.0,
        },
    }
    _apply_brief_aliases(data)
    minted = {
        "npl_socb_pct", "npl_pcb_pct", "npl_fcb_pct", "npl_specialised_pct",
        "deposits_socb_cr", "deposits_pcb_cr", "deposits_fcb_cr",
        "deposits_specialised_cr",
    }
    assert minted <= data.keys()
    assert all(isinstance(data[k], float) for k in minted)


def test_ownership_cluster_flatten_skips_non_numeric_segment():
    """Defensive: a malformed segment entry is dropped; others still flatten."""
    data = {
        "npl_by_ownership": {"socb": 44.7, "pcb": "n/a", "fcb": None, "specialised": 13.1},
    }
    _apply_brief_aliases(data)
    assert data["npl_socb_pct"] == 44.7
    assert data["npl_specialised_pct"] == 13.1
    assert "npl_pcb_pct" not in data
    assert "npl_fcb_pct" not in data


def test_ownership_cluster_flatten_idempotent():
    """A per-segment key hand-set upstream is left untouched."""
    data = {
        "npl_by_ownership": {"socb": 44.7, "pcb": 9.3, "fcb": 5.0, "specialised": 13.1},
        "npl_socb_pct": 99.0,  # pre-set
    }
    _apply_brief_aliases(data)
    assert data["npl_socb_pct"] == 99.0  # untouched
    assert data["npl_pcb_pct"] == 9.3


def test_ownership_cluster_flatten_noop_when_absent():
    data = {"some_other_key": 1.0}
    _apply_brief_aliases(data)
    assert all(
        not k.startswith(("npl_", "deposits_socb", "deposits_pcb")) for k in data
    )


def test_multi_tenor_yield_aliases_feed_yield_curve():
    """Phase 2.3 V5: brief's §07 builder reads tbond_tbill_{182,364}d and
    tbond_bond_{5y,10y}; EconDelta scrapes them as tbill_182d_yield etc.
    The aliases bridge the names so the yield curve chart receives every
    tenor."""
    data = {
        "tbill_182d_yield": 10.20,
        "tbill_364d_yield": 10.55,
        "tbond_5y_yield": 11.10,
        "tbond_10y_yield": 11.42,
    }
    _apply_brief_aliases(data)
    assert data["tbond_tbill_182d"] == 10.20
    assert data["tbond_tbill_364d"] == 10.55
    assert data["tbond_bond_5y"] == 11.10
    assert data["tbond_bond_10y"] == 11.42
