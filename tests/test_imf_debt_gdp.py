"""Tests for the IMF DataMapper debt/GDP scraper (scrapers/imf_debt_gdp.py).

All tests are fully local / NO egress: parse tests run against a captured BGD
fixture, fetch/upsert tests mock the network and the Supabase writer.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.imf_debt_gdp import (
    IMF_METRIC_ID,
    MOF_METRIC_ID,
    FetchError,
    archive_fetched_payload,
    fetch_imf_payload,
    parse_imf_series,
    upsert_history,
)

FIXTURE = Path(__file__).parent / "fixtures" / "imf_ggxwdg_ngdp_bgd.json"


@pytest.fixture
def imf_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# parse_imf_series — pure, no egress
# --------------------------------------------------------------------------- #


def test_parses_bgd_series_with_known_anchor_years(imf_payload):
    """The captured BGD slice carries the real IMF general-govt debt/GDP series."""
    series = parse_imf_series(imf_payload, today=date(2026, 9, 25))
    # Anchor years verified against the live API response on 2026-05-31.
    assert series[2003] == 37.0
    assert series[2024] == 41.0
    assert series[2025] == 42.0
    assert series[2026] == 41.8
    assert 2031 not in series


def test_latest_recent_year_is_in_official_debt_band(imf_payload):
    """Exit criterion: latest print is ~38-42% (IMF general-govt is a touch high)."""
    series = parse_imf_series(imf_payload, today=date(2026, 9, 25))
    latest = max(y for y in series if y <= 2025)
    assert 36.0 <= series[latest] <= 44.0


def test_all_returned_years_are_four_digit_ints(imf_payload):
    series = parse_imf_series(imf_payload, today=date(2026, 9, 25))
    assert series  # non-empty
    assert all(isinstance(y, int) and 1900 <= y <= 2100 for y in series)
    assert all(isinstance(v, float) for v in series.values())


def test_drops_out_of_range_values():
    """A forecast outlier above valid_range is dropped, valid years kept."""
    payload = {
        "values": {
            "GGXWDG_NGDP": {
                "BGD": {"2024": 41.0, "2025": 999.9, "2026": 41.8}
            }
        }
    }
    series = parse_imf_series(payload, today=date(2026, 9, 25))
    assert series == {2024: 41.0, 2026: 41.8}


def test_skips_non_year_keys():
    payload = {
        "values": {"GGXWDG_NGDP": {"BGD": {"2024": 41.0, "notayear": 5.0, "20": 6.0}}}
    }
    assert parse_imf_series(payload, today=date(2026, 9, 25)) == {2024: 41.0}


def test_missing_indicator_raises():
    with pytest.raises(FetchError, match="missing indicator"):
        parse_imf_series({"values": {"OTHER": {"BGD": {"2024": 41.0}}}}, today=date(2026, 9, 25))


def test_missing_country_raises(imf_payload):
    with pytest.raises(FetchError, match="missing/empty series"):
        parse_imf_series(imf_payload, country="ZZZ", today=date(2026, 9, 25))


def test_no_values_object_raises():
    with pytest.raises(FetchError, match="no 'values'"):
        parse_imf_series({"api": {"version": "1"}}, today=date(2026, 9, 25))


def test_does_not_publish_incomplete_bangladesh_fiscal_year(imf_payload):
    series = parse_imf_series(imf_payload, today=date(2026, 6, 29))
    assert 2026 not in series
    assert 2025 in series


# --------------------------------------------------------------------------- #
# upsert_history — one upsert call per year, correct as_of stamping
# --------------------------------------------------------------------------- #


def test_upsert_history_writes_only_completed_fiscal_years_at_june_end():
    series = {2023: 39.7, 2024: 41.0}
    with patch("scrapers.imf_debt_gdp.upsert_metric_history", return_value=1) as mock_up:
        total = upsert_history(series, today=date(2026, 9, 25))

    assert total == 2
    assert mock_up.call_count == 2
    # Years upserted in ascending order, each stamped at Bangladesh FY end.
    calls = mock_up.call_args_list
    first = calls[0].kwargs
    assert first["data"] == {IMF_METRIC_ID: 39.7}
    assert first["as_of"] == date(2023, 6, 30)
    assert first["source_as_of_map"] == {IMF_METRIC_ID: date(2023, 6, 30)}
    assert "IMF" in first["source"] and "estimate" in first["source"].lower()
    second = calls[1].kwargs
    assert second["data"] == {IMF_METRIC_ID: 41.0}
    assert second["as_of"] == date(2024, 6, 30)


def test_end_to_end_fixture_to_upsert(imf_payload):
    """Parse the real fixture, then confirm every year upserts under debt_gdp_ratio."""
    series = parse_imf_series(imf_payload, today=date(2026, 9, 25))
    with patch("scrapers.imf_debt_gdp.upsert_metric_history", return_value=1) as mock_up:
        total = upsert_history(series, today=date(2026, 9, 25))
    assert total == len(series)
    # IMF history is separate; debt_gdp_ratio remains reserved for MoF.
    assert all(c.kwargs["data"].keys() == {IMF_METRIC_ID} for c in mock_up.call_args_list)
    assert all(MOF_METRIC_ID not in c.kwargs["data"] for c in mock_up.call_args_list)
    assert all(c.kwargs["as_of"].month == 6 and c.kwargs["as_of"].day == 30
               for c in mock_up.call_args_list)
    assert all(c.kwargs["as_of"] <= date(2026, 9, 25) for c in mock_up.call_args_list)


def test_upsert_defensively_withholds_uncompleted_fiscal_year():
    series = {2026: 41.8, 2031: 48.8}
    with patch("scrapers.imf_debt_gdp.upsert_metric_history", return_value=1) as mock_up:
        total = upsert_history(series, today=date(2026, 9, 25))
    assert total == 1
    assert mock_up.call_count == 1
    assert mock_up.call_args.kwargs["data"] == {IMF_METRIC_ID: 41.8}
    assert mock_up.call_args.kwargs["as_of"] == date(2026, 6, 30)


# --------------------------------------------------------------------------- #
# fetch_imf_payload — mocked network
# --------------------------------------------------------------------------- #


def test_fetch_returns_json_on_200(imf_payload):
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = imf_payload
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    assert fetch_imf_payload(session=mock_sess) == imf_payload


def test_fetch_raises_on_non_200():
    mock_resp = MagicMock(status_code=503)
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with pytest.raises(FetchError, match="HTTP 503"):
        fetch_imf_payload(session=mock_sess)


def test_fetch_raises_on_non_json():
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.side_effect = ValueError("not json")
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with pytest.raises(FetchError, match="not JSON"):
        fetch_imf_payload(session=mock_sess)


def test_fetch_forces_ipv4_during_call_and_restores(imf_payload):
    """IMF DataMapper's IPv6 is blackholed from the ExonVPS box — the fetch must
    resolve IPv4-only, then restore the process-global so the later upsert (same
    one-shot process) is unaffected."""
    import urllib3.util.connection as u3conn

    seen: dict[str, object] = {}

    def capture_get(*_a, **_k):
        seen["during"] = u3conn.HAS_IPV6
        resp = MagicMock(status_code=200)
        resp.json.return_value = imf_payload
        return resp

    sess = MagicMock()
    sess.get.side_effect = capture_get
    original = u3conn.HAS_IPV6
    u3conn.HAS_IPV6 = True
    try:
        fetch_imf_payload(session=sess)
        assert seen["during"] is False  # IPv4 forced during the IMF fetch
        assert u3conn.HAS_IPV6 is True  # restored after — no bleed into the upsert
    finally:
        u3conn.HAS_IPV6 = original


def test_upsert_does_not_override_supabase_url():
    """Regression: upsert_history must NOT pass IMF_URL as upsert_metric_history's ``url=``
    (the Supabase base-URL override) — that POSTed every yearly write to www.imf.org instead
    of Supabase (slow → the service's 2-min systemd timeout, nothing persisted)."""
    with patch("scrapers.imf_debt_gdp.upsert_metric_history", return_value=1) as mock_up:
        upsert_history({2024: 41.0}, today=date(2026, 9, 25))
    _args, kwargs = mock_up.call_args
    assert kwargs.get("url") is None, "must not override SUPABASE_URL with IMF_URL"


def test_archive_preserves_full_payload_and_retrieval_timestamp(imf_payload, tmp_path):
    retrieved_at = datetime(2026, 9, 25, 12, 34, 56, tzinfo=timezone.utc)
    archived = archive_fetched_payload(
        imf_payload, retrieved_at=retrieved_at, archive_dir=tmp_path,
    )
    saved = json.loads(archived.read_text(encoding="utf-8"))
    assert saved["payload"] == imf_payload
    assert saved["retrieved_at"] == retrieved_at.isoformat()
    assert saved["indicator"] == "GGXWDG_NGDP"
    assert saved["payload"]["values"]["GGXWDG_NGDP"]["BGD"]["2031"] == 48.8


def test_main_archives_before_parsing_and_history_write(imf_payload, monkeypatch, tmp_path):
    import scrapers.imf_debt_gdp as scraper

    events: list[str] = []
    monkeypatch.setattr(scraper, "ARCHIVE_DIR", tmp_path)
    monkeypatch.setattr(scraper, "fetch_imf_payload", lambda: (events.append("fetch"), imf_payload)[1])
    real_archive = archive_fetched_payload

    def archive(payload, *, retrieved_at, archive_dir=None):
        events.append("archive")
        return real_archive(payload, retrieved_at=retrieved_at, archive_dir=archive_dir or tmp_path)

    monkeypatch.setattr(scraper, "archive_fetched_payload", archive)
    monkeypatch.setattr(scraper, "parse_imf_series", lambda payload, *, today: (events.append("parse"), {2025: 42.0})[1])
    monkeypatch.setattr(scraper, "upsert_history", lambda series, *, today: (events.append("write"), 1)[1])

    assert scraper.main() == 0
    assert events == ["fetch", "archive", "parse", "write"]
