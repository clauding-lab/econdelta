"""Digest formatting + send-gating for the freshness sentinel (E2.1)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sentinel.freshness import FreshnessReport, MetricFreshness
from sentinel.report import HEARTBEAT_WEEKDAY, format_digest, should_send


def _breach(mid, cadence="daily", age=30):
    return MetricFreshness(
        metric_id=mid, cadence=cadence, latest_as_of=date(2026, 6, 1),
        latest_ingested_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        age_days=age, breach=True, tables=("metric_history",),
    )


def _fresh(mid):
    return MetricFreshness(
        metric_id=mid, cadence="daily", latest_as_of=date(2026, 7, 3),
        latest_ingested_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        age_days=1, breach=False, tables=("metric_history",),
    )


def test_should_send_on_any_breach():
    report = FreshnessReport(breaches=[_breach("dsex")])
    assert should_send(report, is_heartbeat_day=False) is True


def test_should_stay_silent_when_fresh_and_not_heartbeat():
    report = FreshnessReport(fresh=[_fresh("dsex")])
    assert should_send(report, is_heartbeat_day=False) is False


def test_should_send_heartbeat_when_fresh_on_heartbeat_day():
    report = FreshnessReport(fresh=[_fresh("dsex")])
    assert should_send(report, is_heartbeat_day=True) is True


def test_breach_digest_is_a_warning_listing_each_metric():
    report = FreshnessReport(
        breaches=[_breach("dsex", "daily", 23), _breach("lng_price_usd_mmbtu", "monthly", 185)],
        fresh=[_fresh("usd_bdt_mid")],
    )
    level, title, message, fields = format_digest(report)
    assert level == "warning"
    assert "2 stale" in title
    assert "dsex" in message
    assert "lng_price_usd_mmbtu" in message
    assert fields["Breached"] == "2"
    assert fields["Fresh"] == "1"


def test_heartbeat_digest_is_info_all_fresh():
    report = FreshnessReport(fresh=[_fresh("a"), _fresh("b")])
    level, title, message, _fields = format_digest(report)
    assert level == "info"
    assert "all 2 fresh" in title


def test_heartbeat_names_unmapped_dedupe_candidates():
    unmapped = MetricFreshness(
        metric_id="dse_dsex_close", cadence=None, latest_as_of=date(2026, 6, 1),
        latest_ingested_at=None, age_days=33, breach=False, tables=("metric_history",),
    )
    report = FreshnessReport(fresh=[_fresh("a")], unmapped=[unmapped])
    _level, _title, message, fields = format_digest(report)
    assert "dse_dsex_close" in message
    assert fields["Unmapped"] == "1"


def test_breach_digest_caps_line_count():
    report = FreshnessReport(breaches=[_breach(f"m{i}", "daily", 100 - i) for i in range(40)])
    _level, _title, message, _fields = format_digest(report)
    assert "…and 15 more" in message  # 40 breaches, cap 25


def test_heartbeat_weekday_is_sunday():
    assert HEARTBEAT_WEEKDAY == 6


# --- 2026-08-08 chart-feeding grouping (landmine 50) -------------------------


def test_chart_feeding_breaches_are_listed_first_under_their_own_heading():
    """The exact incident this reorders the digest to prevent: a
    chart-feeding freeze (a reader-visible chart) must never sit buried
    below lower-stakes internal-parity breaches."""
    report = FreshnessReport(
        breaches=[
            _breach("internal_parity_metric", "quarterly", 200),
            _breach("remittance_usd_mn_monthly", "monthly", 60),
        ],
    )
    _level, _title, message, _fields = format_digest(report)
    chart_idx = message.index("CHART-FEEDING")
    remit_idx = message.index("remittance_usd_mn_monthly")
    other_idx = message.index("internal_parity_metric")
    assert chart_idx < remit_idx < other_idx


def test_chart_feeding_group_is_worst_first_within_itself():
    # freshness.assess sorts report.breaches worst-first BEFORE format_digest
    # ever sees it -- construct the input already in that order (oldest/
    # worst first) the way assess() would, and assert format_digest
    # PRESERVES it within the chart-feeding group (does not re-sort or
    # reverse it).
    report = FreshnessReport(
        breaches=[
            _breach("remittance_usd_mn_monthly", "monthly", 90),   # worst
            _breach("gross_reserves_usd_bn_monthly", "monthly", 50),
        ],
    )
    _level, _title, message, _fields = format_digest(report)
    assert message.index("remittance_usd_mn_monthly") < message.index("gross_reserves_usd_bn_monthly")


def test_no_chart_feeding_breaches_omits_the_heading_entirely():
    """Degrades to the EXACT prior single-list digest shape when nothing
    chart-feeding is stale — no structural change for the common case."""
    report = FreshnessReport(breaches=[_breach("some_internal_metric", "daily", 10)])
    _level, _title, message, _fields = format_digest(report)
    assert "CHART-FEEDING" not in message
    assert "Other:" not in message


def test_chart_feeding_and_other_both_respect_the_25_line_total_cap():
    import sentinel.report as report_mod

    real_chart_ids = list(report_mod.CHART_FEEDING_METRIC_IDS)[:8]
    chart = [_breach(mid, "monthly", 100 - i) for i, mid in enumerate(real_chart_ids)]
    other = [_breach(f"internal_{i}", "daily", 50 - i) for i in range(30)]
    report = FreshnessReport(breaches=chart + other)
    total = len(chart) + len(other)

    _level, _title, message, fields = format_digest(report)
    shown_lines = [
        ln for ln in message.split("\n")
        if ln.startswith("`")  # `_breach_line` always starts with a backtick
    ]
    assert len(shown_lines) == 25
    assert f"…and {total - 25} more" in message
    assert fields["Breached"] == str(total)


# --- L1 regression: a full chart-feeding group must never leave a dangling
# --- "Other:" heading with zero lines under it (2026-08-08 Opus review) ----


def test_l1_exactly_25_chart_feeding_breaches_omits_other_heading_entirely():
    """The precise boundary the reviewer proved: chart_breaches alone fills
    the entire 25-line budget while other_breaches is non-empty -- "Other:"
    must not appear at all (0 lines would follow it)."""
    chart_25 = [_breach(f"chart_feeding_synthetic_{i}", "monthly", 200 - i) for i in range(25)]
    other = [_breach("internal_leftover", "daily", 10)]

    import sentinel.report as report_mod

    original = report_mod.CHART_FEEDING_METRIC_IDS
    try:
        report_mod.CHART_FEEDING_METRIC_IDS = frozenset(m.metric_id for m in chart_25)
        report = FreshnessReport(breaches=chart_25 + other)
        _level, _title, message, _fields = format_digest(report)
    finally:
        report_mod.CHART_FEEDING_METRIC_IDS = original

    assert "Other:" not in message
    assert "…and 1 more" in message


# --- M5: accepted-stale ids that are ALSO chart-feeding are permanently ----
# --- invisible without a weekly heartbeat note (2026-08-08 Opus review) ----


def _accepted_stale(mid, as_of=date(2026, 6, 1)):
    return MetricFreshness(
        metric_id=mid, cadence="monthly", latest_as_of=as_of,
        latest_ingested_at=datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc),
        age_days=90, breach=False, tables=("metric_history_monthly",),
    )


def test_m5_heartbeat_names_parked_chart_feeding_accepted_stale_ids():
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[
            _accepted_stale("exports_usd_mn_monthly", date(2026, 6, 1)),
            _accepted_stale("imports_usd_mn_monthly", date(2026, 3, 1)),
            _accepted_stale("tax_gdp_ratio", date(2021, 12, 31)),  # NOT chart-feeding
        ],
    )
    _level, title, message, _fields = format_digest(report)
    assert title.startswith("Freshness sentinel — all")  # heartbeat/all-fresh shape
    assert "Chart-feeding, parked:" in message
    assert "exports_usd_mn_monthly (frozen at 2026-06)" in message
    assert "imports_usd_mn_monthly (frozen at 2026-03)" in message
    # A non-chart-feeding accepted-stale id must NOT appear in this line.
    parked_line = next(ln for ln in message.split("\n") if ln.startswith("Chart-feeding, parked:"))
    assert "tax_gdp_ratio" not in parked_line


def test_m5_no_parked_line_when_no_accepted_stale_chart_feeding_overlap():
    report = FreshnessReport(
        fresh=[_fresh("a")],
        accepted_stale=[_accepted_stale("tax_gdp_ratio", date(2021, 12, 31))],
    )
    _level, _title, message, _fields = format_digest(report)
    assert "Chart-feeding, parked:" not in message


def test_m5_parked_line_only_appears_on_the_heartbeat_no_breach_branch():
    """The line lives in the n_breach==0 branch, which should_send() only
    ever actually posts on the weekly heartbeat day -- a breach digest must
    never carry it (breaches already get their own chart-feeding heading)."""
    report = FreshnessReport(
        breaches=[_breach("some_internal_metric")],
        accepted_stale=[_accepted_stale("exports_usd_mn_monthly")],
    )
    _level, _title, message, _fields = format_digest(report)
    assert "Chart-feeding, parked:" not in message
