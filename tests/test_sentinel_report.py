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
