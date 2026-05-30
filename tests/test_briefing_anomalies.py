from briefing.anomalies import compute_candidates, AnomalyCandidate


def _series(values):
    # newest-first rows, as the reader returns them
    return [{"as_of": f"2026-05-{30-i:02d}", "value": v} for i, v in enumerate(values)]


def test_change_vs_prior_flags_when_breach():
    # call_money jumps 9.34 from 7.10 (Δ=2.24 >= threshold 2.0)
    series = {"call_money_rate": _series([9.34, 7.10, 7.05, 7.00])}
    out = compute_candidates(series, thresholds={"call_money_rate": 2.0}, cadence={"call_money_rate": "daily"})
    ids = {c.candidate_id for c in out}
    assert "call_money_rate:change" in ids
    c = next(c for c in out if c.candidate_id == "call_money_rate:change")
    assert c.severity == "up"
    assert c.value == 9.34
    assert c.metric_id == "call_money_rate"


def test_change_vs_prior_silent_when_within_threshold():
    series = {"call_money_rate": _series([7.20, 7.10])}  # Δ=0.10 < 2.0
    out = compute_candidates(series, thresholds={"call_money_rate": 2.0}, cadence={"call_money_rate": "daily"})
    assert all(c.candidate_id != "call_money_rate:change" for c in out)


def test_zscore_flags_statistical_outlier():
    # trailing points jitter around 5.0 (non-zero stdev), then a spike to 7.0 -> large z-score
    series = {"x": _series([7.0, 5.0, 5.1, 4.9, 5.0, 5.1, 4.9, 5.0, 5.1, 4.9])}
    out = compute_candidates(series, thresholds={"x": 99.0}, cadence={"x": "daily"})  # high thr so only z fires
    assert any(c.candidate_id == "x:zscore" for c in out)


def test_no_threshold_means_no_change_candidate():
    series = {"y": _series([10.0, 1.0])}
    out = compute_candidates(series, thresholds={"y": None}, cadence={"y": "daily"})
    assert all(c.candidate_id != "y:change" for c in out)


def test_too_few_points_no_zscore():
    series = {"z": _series([9.0, 1.0])}
    out = compute_candidates(series, thresholds={"z": None}, cadence={"z": "daily"})
    assert all(c.candidate_id != "z:zscore" for c in out)
