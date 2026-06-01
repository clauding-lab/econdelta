"""Tests for the IMF EFF-outstanding scraper (scrapers/imf_eff.py).

All tests are fully local / NO egress: parse tests run against a captured
Bangladesh Financial-Position HTML fixture; fetch/upsert tests mock the network
and the Supabase writer. The live page IS reachable from any host (no BD egress
wall — verified 2026-05-31, EFF = 1,373.26 SDR mn as of 2026-04-30), but the
unit tests stay offline so they pass deterministically in CI-less runs.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.imf_eff import (
    METRIC_ID,
    FetchError,
    _build_url,
    fetch_imf_position_html,
    parse_eff_outstanding,
    upsert_eff,
)

FIXTURE = Path(__file__).parent / "fixtures" / "imf_position_bgd.html"


@pytest.fixture
def position_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# parse_eff_outstanding — pure, no egress
# --------------------------------------------------------------------------- #


def test_extracts_eff_outstanding_sdr_million(position_html):
    """Header-LABEL match on the 'Extended Arrangements' row, SDR-million column."""
    value, as_of = parse_eff_outstanding(position_html)
    # Verified against the live IMF Financial Position page on 2026-05-31.
    assert value == 1373.26


def test_parses_reporting_date_from_title(position_html):
    """as_of comes from the page title's 'as of <Month DD, YYYY>'."""
    _value, as_of = parse_eff_outstanding(position_html)
    assert as_of == date(2026, 4, 30)


def test_picks_eff_not_a_sibling_facility(position_html):
    """The page lists RSF/RCF/ECF on adjacent lines — must grab EFF's number, not theirs."""
    value, _as_of = parse_eff_outstanding(position_html)
    assert value not in (666.68, 159.99, 686.64)  # RSF / RCF / ECF


def test_grabs_sdr_column_not_quota_percent(position_html):
    """The FIRST numeric after the label is the SDR-mn cell, not the %-quota (128.75)."""
    value, _as_of = parse_eff_outstanding(position_html)
    assert value != 128.75


def test_missing_facility_row_raises():
    html = "<title>Financial Position in the Fund for Foo as of May 31, 2026</title>"
    with pytest.raises(FetchError, match="not found"):
        parse_eff_outstanding(html)


def test_out_of_range_value_raises():
    html = (
        "<title>Financial Position in the Fund for X as of May 31, 2026</title>"
        "Extended Arrangements 99999.99 100.0"
    )
    with pytest.raises(FetchError, match="out of range"):
        parse_eff_outstanding(html)


def test_missing_title_yields_none_as_of():
    html = "Extended Arrangements 1,373.26 128.75"
    value, as_of = parse_eff_outstanding(html)
    assert value == 1373.26
    assert as_of is None


# --------------------------------------------------------------------------- #
# fetch_imf_position_html — mocked network
# --------------------------------------------------------------------------- #


def test_fetch_returns_text_on_200():
    sess = MagicMock()
    sess.get.return_value = MagicMock(status_code=200, text="<html>ok</html>")
    assert fetch_imf_position_html(session=sess) == "<html>ok</html>"


def test_fetch_raises_on_non_200():
    sess = MagicMock()
    sess.get.return_value = MagicMock(status_code=403)
    with pytest.raises(FetchError, match="HTTP 403"):
        fetch_imf_position_html(session=sess)


def test_build_url_carries_member_and_datekey():
    url = _build_url(55, on=date(2026, 4, 30))
    assert "memberKey1=55" in url
    assert "date1key=2026-04-30" in url


# --------------------------------------------------------------------------- #
# upsert_eff — mocked writer
# --------------------------------------------------------------------------- #


def test_upsert_writes_one_row_under_metric_id():
    with patch("scrapers.imf_eff.upsert_metric_history", return_value=1) as mock_up:
        written = upsert_eff(1373.26, date(2026, 4, 30))
    assert written == 1
    _args, kwargs = mock_up.call_args
    assert kwargs["data"] == {METRIC_ID: 1373.26}
    assert kwargs["as_of"] == date(2026, 4, 30)
    assert kwargs["source_as_of_map"] == {METRIC_ID: date(2026, 4, 30)}


def test_fetch_forces_ipv4_during_call_and_restores():
    """IMF's IPv6 is blackholed from the ExonVPS box, so the fetch must resolve
    IPv4-only — then restore the process-global so the later Supabase upsert (in
    the same one-shot process) is unaffected."""
    import urllib3.util.connection as u3conn

    seen: dict[str, object] = {}

    def capture_get(*_a, **_k):
        seen["during"] = u3conn.HAS_IPV6
        return MagicMock(status_code=200, text="<html>ok</html>")

    sess = MagicMock()
    sess.get.side_effect = capture_get
    original = u3conn.HAS_IPV6
    u3conn.HAS_IPV6 = True
    try:
        fetch_imf_position_html(session=sess)
        assert seen["during"] is False  # IPv4 forced during the IMF fetch
        assert u3conn.HAS_IPV6 is True  # restored after — no bleed into the upsert
    finally:
        u3conn.HAS_IPV6 = original


def test_upsert_does_not_override_supabase_url():
    """Regression: upsert_eff must NOT pass the IMF source URL as upsert_metric_history's
    ``url=`` — that kwarg is the Supabase base-URL OVERRIDE. The bug POSTed the write to
    www.imf.org instead of Supabase (a 301 → 2xx that logged 'upserted 1 row' but never
    persisted — metric_history stayed empty)."""
    with patch("scrapers.imf_eff.upsert_metric_history", return_value=1) as mock_up:
        upsert_eff(1373.26, date(2026, 4, 30))
    _args, kwargs = mock_up.call_args
    assert kwargs.get("url") is None, "must not override SUPABASE_URL with the IMF source URL"
