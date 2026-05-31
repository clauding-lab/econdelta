"""Tests for the IMF DataMapper debt/GDP scraper (scrapers/imf_debt_gdp.py).

All tests are fully local / NO egress: parse tests run against a captured BGD
fixture, fetch/upsert tests mock the network and the Supabase writer.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.imf_debt_gdp import (
    METRIC_ID,
    FetchError,
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
    series = parse_imf_series(imf_payload)
    # Anchor years verified against the live API response on 2026-05-31.
    assert series[2003] == 37.0
    assert series[2024] == 41.0
    assert series[2025] == 42.0


def test_latest_recent_year_is_in_official_debt_band(imf_payload):
    """Exit criterion: latest print is ~38-42% (IMF general-govt is a touch high)."""
    series = parse_imf_series(imf_payload)
    latest = max(y for y in series if y <= 2025)
    assert 36.0 <= series[latest] <= 44.0


def test_all_returned_years_are_four_digit_ints(imf_payload):
    series = parse_imf_series(imf_payload)
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
    series = parse_imf_series(payload)
    assert series == {2024: 41.0, 2026: 41.8}


def test_skips_non_year_keys():
    payload = {
        "values": {"GGXWDG_NGDP": {"BGD": {"2024": 41.0, "notayear": 5.0, "20": 6.0}}}
    }
    assert parse_imf_series(payload) == {2024: 41.0}


def test_missing_indicator_raises():
    with pytest.raises(FetchError, match="missing indicator"):
        parse_imf_series({"values": {"OTHER": {"BGD": {"2024": 41.0}}}})


def test_missing_country_raises(imf_payload):
    with pytest.raises(FetchError, match="missing/empty series"):
        parse_imf_series(imf_payload, country="ZZZ")


def test_no_values_object_raises():
    with pytest.raises(FetchError, match="no 'values'"):
        parse_imf_series({"api": {"version": "1"}})


# --------------------------------------------------------------------------- #
# upsert_history — one upsert call per year, correct as_of stamping
# --------------------------------------------------------------------------- #


def test_upsert_history_writes_one_row_per_year_stamped_year_end():
    series = {2023: 39.7, 2024: 41.0}
    with patch("scrapers.imf_debt_gdp.upsert_metric_history", return_value=1) as mock_up:
        total = upsert_history(series)

    assert total == 2
    assert mock_up.call_count == 2
    # Years upserted in ascending order, each stamped <year>-12-31.
    calls = mock_up.call_args_list
    first = calls[0].kwargs
    assert first["data"] == {METRIC_ID: 39.7}
    assert first["as_of"] == date(2023, 12, 31)
    assert first["source_as_of_map"] == {METRIC_ID: date(2023, 12, 31)}
    assert first["source"] == "IMF DataMapper"
    second = calls[1].kwargs
    assert second["data"] == {METRIC_ID: 41.0}
    assert second["as_of"] == date(2024, 12, 31)


def test_end_to_end_fixture_to_upsert(imf_payload):
    """Parse the real fixture, then confirm every year upserts under debt_gdp_ratio."""
    series = parse_imf_series(imf_payload)
    with patch("scrapers.imf_debt_gdp.upsert_metric_history", return_value=1) as mock_up:
        total = upsert_history(series)
    assert total == len(series)
    # Every call targets the shared debt_gdp_ratio id (so MoF + IMF share one series).
    assert all(c.kwargs["data"].keys() == {METRIC_ID} for c in mock_up.call_args_list)


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
