from briefing import config


def test_loaders_cover_known_core_ids():
    indicators = config.load_indicators()
    thr = config.thresholds_by_metric(indicators)
    cad = config.cadence_by_metric(indicators)
    # call_money_rate is a real daily money-market indicator with a threshold
    assert "call_money_rate" in thr
    assert thr["call_money_rate"] is not None
    assert cad["call_money_rate"] in {"daily", "weekly", "monthly", "quarterly", "fiscal_year"}


def test_core_ids_are_subset_of_tracked():
    indicators = config.load_indicators()
    tracked = config.tracked_metric_ids(indicators)
    assert config.CORE_METRIC_IDS <= set(tracked)
