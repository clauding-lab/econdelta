"""Tests for the fiscal revenue/GDP scrapers (scrapers/fiscal_gdp_ratios.py).

All tests are fully local / NO egress: parse tests run against captured BGD
fixtures, fetch/upsert tests mock the network and the Supabase writer. The
fixtures are the real API payloads:
    tests/fixtures/imf_rev_bgd.json   — IMF DataMapper "rev" nested-dict shape
    tests/fixtures/wb_tax_gdp_bgd.json — World Bank [meta, rows] array shape
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.fiscal_gdp_ratios import (
    REV_METRIC_ID,
    TAX_METRIC_ID,
    FetchError,
    fetch_imf_payload,
    fetch_wb_payload,
    parse_imf_rev_series,
    parse_wb_tax_series,
    upsert_rev_history,
    upsert_tax_history,
)

FIXTURES = Path(__file__).parent / "fixtures"
IMF_FIXTURE = FIXTURES / "imf_rev_bgd.json"
WB_FIXTURE = FIXTURES / "wb_tax_gdp_bgd.json"


@pytest.fixture
def imf_payload() -> dict:
    return json.loads(IMF_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def wb_payload() -> list:
    return json.loads(WB_FIXTURE.read_text(encoding="utf-8"))


# =========================================================================== #
# IMF DataMapper — rev_gdp_ratio
# =========================================================================== #

# --------------------------------------------------------------------------- #
# parse_imf_rev_series — pure, no egress
# --------------------------------------------------------------------------- #


def test_imf_parses_bgd_series_with_known_anchor_years(imf_payload):
    """The captured BGD slice carries the real IMF government-revenue/GDP series."""
    series = parse_imf_rev_series(imf_payload)
    # Anchor values taken verbatim from the captured live payload.
    assert series[2024] == 8.3381745972143
    assert round(series[2024], 2) == 8.34  # latest known good, % of GDP
    assert series[1990] == 8.9847034497859
    assert series[2021] == 9.3618797183766


def test_imf_latest_year_is_in_official_revenue_band(imf_payload):
    """Bangladesh government revenue sits ~7-10% of GDP — a real, meaningful band."""
    series = parse_imf_rev_series(imf_payload)
    latest = max(series)
    assert latest == 2024
    assert 6.0 <= series[latest] <= 12.0


def test_imf_all_returned_years_are_four_digit_ints(imf_payload):
    series = parse_imf_rev_series(imf_payload)
    assert series  # non-empty
    assert all(isinstance(y, int) and 1900 <= y <= 2100 for y in series)
    assert all(isinstance(v, float) for v in series.values())


def test_imf_drops_out_of_range_values():
    """A value above valid_range (0, 40) is dropped, valid years kept."""
    payload = {"values": {"rev": {"BGD": {"2023": 8.24, "2024": 999.9, "2022": 8.51}}}}
    series = parse_imf_rev_series(payload)
    assert series == {2023: 8.24, 2022: 8.51}


def test_imf_skips_non_year_keys():
    payload = {"values": {"rev": {"BGD": {"2024": 8.34, "notayear": 5.0, "20": 6.0}}}}
    assert parse_imf_rev_series(payload) == {2024: 8.34}


def test_imf_missing_indicator_raises():
    with pytest.raises(FetchError, match="missing indicator"):
        parse_imf_rev_series({"values": {"OTHER": {"BGD": {"2024": 8.34}}}})


def test_imf_missing_country_raises(imf_payload):
    with pytest.raises(FetchError, match="missing/empty series"):
        parse_imf_rev_series(imf_payload, country="ZZZ")


def test_imf_no_values_object_raises():
    with pytest.raises(FetchError, match="no 'values'"):
        parse_imf_rev_series({"api": {"version": "1"}})


# --------------------------------------------------------------------------- #
# upsert_rev_history — one upsert per year, correct as_of + source
# --------------------------------------------------------------------------- #


def test_rev_upsert_writes_one_row_per_year_stamped_year_end():
    series = {2023: 8.24, 2024: 8.34}
    with patch(
        "scrapers.fiscal_gdp_ratios.upsert_metric_history", return_value=1
    ) as mock_up:
        total = upsert_rev_history(series)

    assert total == 2
    assert mock_up.call_count == 2
    calls = mock_up.call_args_list
    first = calls[0].kwargs
    assert first["data"] == {REV_METRIC_ID: 8.24}
    assert first["as_of"] == date(2023, 12, 31)
    assert first["source_as_of_map"] == {REV_METRIC_ID: date(2023, 12, 31)}
    assert first["source"] == "IMF DataMapper"
    second = calls[1].kwargs
    assert second["data"] == {REV_METRIC_ID: 8.34}
    assert second["as_of"] == date(2024, 12, 31)
    assert second["source_as_of_map"] == {REV_METRIC_ID: date(2024, 12, 31)}


def test_rev_end_to_end_fixture_to_upsert(imf_payload):
    """Parse the real fixture, then confirm every year upserts under rev_gdp_ratio."""
    series = parse_imf_rev_series(imf_payload)
    with patch(
        "scrapers.fiscal_gdp_ratios.upsert_metric_history", return_value=1
    ) as mock_up:
        total = upsert_rev_history(series)
    assert total == len(series)
    assert all(c.kwargs["data"].keys() == {REV_METRIC_ID} for c in mock_up.call_args_list)


def test_rev_upsert_does_not_override_supabase_url():
    """Regression (landmine 22): upsert must NOT pass IMF_URL as ``url=`` (the
    Supabase base-URL override) — that would POST every write to www.imf.org."""
    with patch(
        "scrapers.fiscal_gdp_ratios.upsert_metric_history", return_value=1
    ) as mock_up:
        upsert_rev_history({2024: 8.34})
    _args, kwargs = mock_up.call_args
    assert kwargs.get("url") is None, "must not override SUPABASE_URL with IMF_URL"


# --------------------------------------------------------------------------- #
# fetch_imf_payload — mocked network
# --------------------------------------------------------------------------- #


def test_imf_fetch_returns_json_on_200(imf_payload):
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = imf_payload
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    assert fetch_imf_payload(session=mock_sess) == imf_payload


def test_imf_fetch_raises_on_non_200():
    mock_resp = MagicMock(status_code=503)
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with pytest.raises(FetchError, match="HTTP 503"):
        fetch_imf_payload(session=mock_sess)


def test_imf_fetch_raises_on_non_json():
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.side_effect = ValueError("not json")
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with pytest.raises(FetchError, match="not JSON"):
        fetch_imf_payload(session=mock_sess)


def test_imf_fetch_forces_ipv4_during_call_and_restores(imf_payload):
    """www.imf.org's IPv6 is blackholed from the ExonVPS box — the fetch must
    resolve IPv4-only, then restore the process-global (the later upsert in the
    same one-shot process must be unaffected)."""
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


# =========================================================================== #
# World Bank API — tax_gdp_ratio
# =========================================================================== #

# --------------------------------------------------------------------------- #
# parse_wb_tax_series — pure, no egress
# --------------------------------------------------------------------------- #


def test_wb_parses_bgd_series_with_known_anchor_years(wb_payload):
    """The captured BGD slice carries the real World Bank tax-revenue/GDP series."""
    series = parse_wb_tax_series(wb_payload)
    # Latest non-null observation is 2021 = 7.64% — the true (intentionally stale) vintage.
    assert series[2021] == 7.64236301017901
    assert round(series[2021], 2) == 7.64
    assert series[2020] == 7.00153674405437
    assert series[2015] == 8.49834882820801


def test_wb_latest_year_is_2021_and_series_stops_there(wb_payload):
    """The WB series intentionally stops at 2021 (post-2021 rows are null → skipped)."""
    series = parse_wb_tax_series(wb_payload)
    assert max(series) == 2021
    assert 2022 not in series and 2025 not in series


def test_wb_skips_null_values(wb_payload):
    """Null-valued rows (2022-2025 and pre-2001) are skipped, not zero-filled."""
    series = parse_wb_tax_series(wb_payload)
    # 2000 and earlier are null in the fixture.
    assert 2000 not in series
    assert 1999 not in series
    # Every returned value is a real float in the tax band.
    assert all(isinstance(v, float) and 0.0 < v <= 30.0 for v in series.values())


def test_wb_all_returned_years_are_four_digit_ints(wb_payload):
    series = parse_wb_tax_series(wb_payload)
    assert series  # non-empty
    assert all(isinstance(y, int) and 1900 <= y <= 2100 for y in series)


def test_wb_drops_out_of_range_values():
    """A value above valid_range (0, 30) is dropped, valid years kept."""
    payload = [
        {"page": 1},
        [
            _wb_row("2021", 7.64),
            _wb_row("2020", 99.9),  # out of range → dropped
            _wb_row("2019", 7.63),
        ],
    ]
    series = parse_wb_tax_series(payload)
    assert series == {2021: 7.64, 2019: 7.63}


def test_wb_skips_non_year_date_keys():
    payload = [
        {"page": 1},
        [
            _wb_row("2021", 7.64),
            _wb_row("notayear", 5.0),
            _wb_row("21", 6.0),
        ],
    ]
    assert parse_wb_tax_series(payload) == {2021: 7.64}


def test_wb_missing_indicator_raises():
    """No row matches the requested indicator → raise (never return an empty series)."""
    payload = [{"page": 1}, [_wb_row("2021", 7.64, indicator="OTHER.INDICATOR")]]
    with pytest.raises(FetchError, match="no rows for country"):
        parse_wb_tax_series(payload)


def test_wb_missing_country_raises(wb_payload):
    with pytest.raises(FetchError, match="no rows for country"):
        parse_wb_tax_series(wb_payload, country="ZZZ")


def test_wb_wrong_envelope_shape_raises():
    """A World Bank error response is a 1-element array — reject the shape."""
    with pytest.raises(FetchError, match="not the \\[meta, rows\\] array shape"):
        parse_wb_tax_series([{"message": [{"id": "120", "value": "bad request"}]}])


def test_wb_null_rows_element_raises():
    """WB returns ``[meta, null]`` for an unknown country/indicator — the rows
    element is None, not a list. Reject the shape, never return an empty series."""
    with pytest.raises(FetchError, match="rows element is not a list"):
        parse_wb_tax_series([{"page": 1, "total": 0}, None])


def test_wb_parses_lowercase_countryiso3code():
    """Defensive: a casing quirk in countryiso3code must not take the tax leg
    offline — the parser normalises before matching."""
    payload = [{"page": 1}, [_wb_row("2021", 7.64, iso3="bgd")]]
    assert parse_wb_tax_series(payload) == {2021: 7.64}


def _wb_row(
    date_str: str,
    value: float | None,
    *,
    indicator: str = "GC.TAX.TOTL.GD.ZS",
    iso3: str = "BGD",
) -> dict:
    """Build one World Bank observation row mirroring the real API shape."""
    return {
        "indicator": {"id": indicator, "value": "Tax revenue (% of GDP)"},
        "country": {"id": "BD", "value": "Bangladesh"},
        "countryiso3code": iso3,
        "date": date_str,
        "value": value,
        "unit": "",
        "obs_status": "",
        "decimal": 1,
    }


# --------------------------------------------------------------------------- #
# upsert_tax_history — one upsert per year, correct as_of + source
# --------------------------------------------------------------------------- #


def test_tax_upsert_writes_one_row_per_year_stamped_year_end():
    series = {2020: 7.00, 2021: 7.64}
    with patch(
        "scrapers.fiscal_gdp_ratios.upsert_metric_history", return_value=1
    ) as mock_up:
        total = upsert_tax_history(series)

    assert total == 2
    assert mock_up.call_count == 2
    calls = mock_up.call_args_list
    first = calls[0].kwargs
    assert first["data"] == {TAX_METRIC_ID: 7.00}
    assert first["as_of"] == date(2020, 12, 31)
    assert first["source_as_of_map"] == {TAX_METRIC_ID: date(2020, 12, 31)}
    assert first["source"] == "World Bank"
    second = calls[1].kwargs
    assert second["data"] == {TAX_METRIC_ID: 7.64}
    assert second["as_of"] == date(2021, 12, 31)
    assert second["source_as_of_map"] == {TAX_METRIC_ID: date(2021, 12, 31)}


def test_tax_end_to_end_fixture_to_upsert(wb_payload):
    """Parse the real fixture, then confirm every year upserts under tax_gdp_ratio."""
    series = parse_wb_tax_series(wb_payload)
    with patch(
        "scrapers.fiscal_gdp_ratios.upsert_metric_history", return_value=1
    ) as mock_up:
        total = upsert_tax_history(series)
    assert total == len(series)
    assert all(c.kwargs["data"].keys() == {TAX_METRIC_ID} for c in mock_up.call_args_list)


def test_tax_upsert_does_not_override_supabase_url():
    """Regression (landmine 22): upsert must NOT pass WB_URL as ``url=``."""
    with patch(
        "scrapers.fiscal_gdp_ratios.upsert_metric_history", return_value=1
    ) as mock_up:
        upsert_tax_history({2021: 7.64})
    _args, kwargs = mock_up.call_args
    assert kwargs.get("url") is None, "must not override SUPABASE_URL with WB_URL"


# --------------------------------------------------------------------------- #
# fetch_wb_payload — mocked network
# --------------------------------------------------------------------------- #


def test_wb_fetch_returns_json_on_200(wb_payload):
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = wb_payload
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    assert fetch_wb_payload(session=mock_sess) == wb_payload


def test_wb_fetch_raises_on_non_200():
    mock_resp = MagicMock(status_code=503)
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with pytest.raises(FetchError, match="HTTP 503"):
        fetch_wb_payload(session=mock_sess)


def test_wb_fetch_raises_on_non_json():
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.side_effect = ValueError("not json")
    mock_sess = MagicMock()
    mock_sess.get.return_value = mock_resp
    with pytest.raises(FetchError, match="not JSON"):
        fetch_wb_payload(session=mock_sess)


def test_wb_fetch_forces_ipv4_during_call_and_restores(wb_payload):
    """api.worldbank.org's IPv6 is blackholed from the ExonVPS box — the fetch
    must resolve IPv4-only during the call, then restore the process-global."""
    import urllib3.util.connection as u3conn

    seen: dict[str, object] = {}

    def capture_get(*_a, **_k):
        seen["during"] = u3conn.HAS_IPV6
        resp = MagicMock(status_code=200)
        resp.json.return_value = wb_payload
        return resp

    sess = MagicMock()
    sess.get.side_effect = capture_get
    original = u3conn.HAS_IPV6
    u3conn.HAS_IPV6 = True
    try:
        fetch_wb_payload(session=sess)
        assert seen["during"] is False  # IPv4 forced during the World Bank fetch
        assert u3conn.HAS_IPV6 is True  # restored after
    finally:
        u3conn.HAS_IPV6 = original
