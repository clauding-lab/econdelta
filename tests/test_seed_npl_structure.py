"""tests/test_seed_npl_structure.py"""
from datetime import date
from unittest.mock import patch


def test_seed_values_are_the_deck_primitives_exactly():
    from scripts.seed_npl_structure import SEED_AS_OF, SEED_SOURCE, SEED_VALUES
    assert SEED_AS_OF == date(2026, 3, 31)
    assert SEED_SOURCE == "bb_via_press_static"
    assert len(SEED_VALUES) == 14
    assert SEED_VALUES["npl_rate_band_lt1cr"] == 15.0
    assert SEED_VALUES["npl_rate_band_1_10cr"] == 26.5
    assert SEED_VALUES["npl_rate_band_10_20cr"] == 45.0
    assert SEED_VALUES["npl_rate_band_20_30cr"] == 36.0
    assert SEED_VALUES["npl_rate_band_30_40cr"] == 39.0
    assert SEED_VALUES["npl_rate_band_40_50cr"] == 45.0
    assert SEED_VALUES["npl_rate_band_gt50cr"] == 42.5
    assert SEED_VALUES["loans_outstanding_band_lt1cr"] == 410_000
    assert SEED_VALUES["loans_outstanding_band_1_10cr"] == 361_000
    assert SEED_VALUES["loans_outstanding_band_gt50cr"] == 576_000
    assert SEED_VALUES["npl_rate_cmsme_overall"] == 34.0
    assert SEED_VALUES["npl_rate_cmsme_cottage"] == 53.0
    assert SEED_VALUES["npl_rate_cmsme_medium"] == 38.0
    assert SEED_VALUES["total_bank_advances"] == 1_784_000
    # Press-taxonomy sector values deliberately ABSENT (spec amendment:
    # the sector family lives in the FSR taxonomy; press cut would orphan).
    # These are REAL METRIC_SPECS ids (fsr=True) — pinning that the seed never
    # writes the FSR-owned sector family, not just that some made-up string
    # is absent (the old ids here weren't in METRIC_SPECS at all).
    for absent in ("npl_rate_sector_consumer_credit", "lending_share_sector_trade_commerce"):
        assert absent not in SEED_VALUES


def test_every_seed_id_is_known_and_seed_only_or_shared_total():
    from scrapers.bb_npl_structure import METRIC_SPECS
    from scripts.seed_npl_structure import SEED_VALUES
    assert set(SEED_VALUES) <= set(METRIC_SPECS)
    for mid in SEED_VALUES:
        assert (not METRIC_SPECS[mid].fsr) or mid == "total_bank_advances"


def test_dry_run_writes_nothing():
    import scripts.seed_npl_structure as seeder
    with patch.object(seeder, "upsert_metric_history") as up, \
         patch.object(seeder, "upsert_metric_definitions_seed") as seed:
        assert seeder.run(execute=False) == 0
    up.assert_not_called()
    seed.assert_not_called()


def test_execute_seeds_definitions_then_history():
    import scripts.seed_npl_structure as seeder
    from scripts.seed_npl_structure import SEED_VALUES
    with patch.object(seeder, "upsert_metric_definitions_seed", return_value=35) as seed, \
         patch.object(seeder, "upsert_metric_history", return_value=14) as up:
        assert seeder.run(execute=True) == 0
    seed.assert_called_once()
    kwargs = up.call_args.kwargs
    assert kwargs["source"] == "bb_via_press_static"
    assert kwargs["as_of"] == date(2026, 3, 31)
    assert "url" not in kwargs
    assert kwargs["data"] == SEED_VALUES
