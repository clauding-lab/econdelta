"""tests/test_bb_npl_structure_gate.py"""
from datetime import date

TODAY = date(2026, 8, 3)
POS = date(2025, 12, 31)

# The REAL FSR 2025 Table 2.3 figures (billion BDT / percent, verbatim).
GOOD = {
    "npl_rate_sector_agriculture": 29.59, "lending_share_sector_agriculture": 3.88,
    "npl_rate_sector_industrial_mfg": 28.91, "lending_share_sector_industrial_mfg": 51.35,
    "npl_rate_sector_industrial_services": 27.88, "lending_share_sector_industrial_services": 11.49,
    "npl_rate_sector_consumer_credit": 8.01, "lending_share_sector_consumer_credit": 6.83,
    "npl_rate_sector_trade_commerce": 49.88, "lending_share_sector_trade_commerce": 18.16,
    "npl_rate_sector_nbfi": 21.61, "lending_share_sector_nbfi": 0.48,
    "npl_rate_sector_capital_market": 7.35, "lending_share_sector_capital_market": 0.46,
    "npl_rate_sector_other": 22.63, "lending_share_sector_other": 7.36,
    "npl_rate_sub_rmg": 31.15, "npl_rate_sub_construction": 31.54,
    "npl_rate_sub_housing_finance": 13.10, "npl_rate_sub_smc_industries": 24.09,
    "total_bank_advances": 18204.30, "gross_npl_stock": 5570.32,
    "overall_npl_ratio_fsr": 30.60,
}


def _gate(payload, pos=POS):
    from scrapers.bb_npl_structure import validate_extraction
    return validate_extraction(payload, pos, TODAY)


def test_real_fsr_2025_figures_pass():
    assert _gate(dict(GOOD)) == []


def test_missing_required_key_rejects():
    bad = dict(GOOD)
    bad["npl_rate_sector_trade_commerce"] = None
    assert any("npl_rate_sector_trade_commerce" in r for r in _gate(bad))


def test_missing_sub_rate_is_fine():
    ok = dict(GOOD)
    ok["npl_rate_sub_rmg"] = None
    assert _gate(ok) == []


def test_wrong_column_read_fails_weighted_reconciliation():
    bad = dict(GOOD)
    bad["npl_rate_sector_industrial_mfg"] = 48.51  # Share-of-NPLs column
    assert any("weighted" in r for r in _gate(bad))


def test_decimal_slip_in_stock_fails_stock_ratio_check():
    bad = dict(GOOD)
    bad["gross_npl_stock"] = 2000.0  # inside NPL_STOCK_RANGE_BN, wrong vs advances
    assert any("npl stock/advances" in r for r in _gate(bad))


def test_stock_out_of_range_rejects():
    bad = dict(GOOD)
    bad["gross_npl_stock"] = 25000.0  # outside NPL_STOCK_RANGE_BN
    assert any("gross_npl_stock out of range" in r for r in _gate(bad))


def test_nan_overall_ratio_rejects_as_missing():
    bad = dict(GOOD)
    bad["overall_npl_ratio_fsr"] = float("nan")
    assert any(
        "required key missing or non-numeric: overall_npl_ratio_fsr" in r
        for r in _gate(bad)
    )


def test_shares_not_summing_to_100_rejects():
    bad = dict(GOOD)
    bad["lending_share_sector_other"] = 17.36
    assert any("share" in r for r in _gate(bad))


def test_rate_out_of_range_rejects():
    bad = dict(GOOD)
    bad["npl_rate_sub_construction"] = 85.0
    assert any("npl_rate_sub_construction" in r for r in _gate(bad))


def test_advances_out_of_range_rejects():
    bad = dict(GOOD)
    bad["total_bank_advances"] = 1820430.0   # crore slipped in
    assert any("total_bank_advances" in r for r in _gate(bad))


def test_future_position_rejects():
    assert any("position" in r for r in _gate(dict(GOOD), pos=date(2027, 12, 31)))


def test_ancient_position_rejects():
    assert any("position" in r for r in _gate(dict(GOOD), pos=date(2023, 12, 31)))


def test_position_within_widened_age_bound_passes():
    assert _gate(dict(GOOD), pos=date(2025, 6, 30)) == []
