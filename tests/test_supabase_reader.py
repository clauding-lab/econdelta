"""Tests for utils.supabase_reader.

Mocks requests.Session so no real Supabase call goes out.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_reader import (
    SupabaseReadError,
    get_auction_results_through,
    get_metric_history_monthly,
)


def _make_session(status: int = 200, payload: object = None) -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else []
    sess.get.return_value = resp
    return sess


def _make_multi_response_session(pages: list[list[dict]], status: int = 200) -> MagicMock:
    """A session whose sess.get() returns each of ``pages`` in order, one
    per call -- for testing offset-paging across multiple responses."""
    sess = MagicMock(spec=requests.Session)
    responses = []
    for page in pages:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = page
        responses.append(resp)
    sess.get.side_effect = responses
    return sess


def test_get_metric_history_monthly_hits_the_monthly_table():
    sess = _make_session(payload=[{"metric_id": "cpi_12m_avg_monthly", "as_of": "2026-06-01"}])
    rows = get_metric_history_monthly(
        "cpi_12m_avg_monthly", url="https://example.supabase.co",
        key="sk_test", session=sess,
    )
    assert rows == [{"metric_id": "cpi_12m_avg_monthly", "as_of": "2026-06-01"}]
    args, kwargs = sess.get.call_args
    url = args[0]
    assert url == (
        "https://example.supabase.co/rest/v1/metric_history_monthly"
        "?metric_id=eq.cpi_12m_avg_monthly&order=as_of.desc&limit=36"
    )
    assert kwargs["headers"]["apikey"] == "sk_test"


def test_get_metric_history_monthly_respects_custom_limit():
    sess = _make_session()
    get_metric_history_monthly(
        "remittance_usd_mn_monthly", limit=5,
        url="https://example.supabase.co", key="sk_test", session=sess,
    )
    args, _kwargs = sess.get.call_args
    assert "limit=5" in args[0]


def test_get_metric_history_monthly_raises_on_non_2xx():
    sess = _make_session(status=500)
    with pytest.raises(SupabaseReadError):
        get_metric_history_monthly(
            "x", url="https://example.supabase.co", key="sk_test", session=sess,
        )


def test_get_metric_history_monthly_raises_on_missing_credentials(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SupabaseReadError):
        get_metric_history_monthly("x")


# ---------------------------------------------------------------------------
# get_auction_results_through (Phase 2, AGENTS.md landmine 51; 2026-08-08
# re-review finding M4 -- this reader previously had ZERO direct tests,
# everything exercised it only via monkeypatching in
# tests/test_yield_ladder_append.py). Mirrors the get_metric_history_monthly
# block above.
# ---------------------------------------------------------------------------


def test_get_auction_results_through_hits_the_auction_results_table():
    sess = _make_session(payload=[{"auction_date": "2026-07-10", "tenor": "91d", "cutoff": 9.8}])
    rows = get_auction_results_through(
        date(2026, 7, 31), url="https://example.supabase.co", key="sk_test", session=sess,
    )
    assert rows == [{"auction_date": "2026-07-10", "tenor": "91d", "cutoff": 9.8}]
    args, kwargs = sess.get.call_args
    url = args[0]
    assert url == (
        "https://example.supabase.co/rest/v1/auction_results"
        "?select=auction_date,tenor,cutoff&auction_date=lte.2026-07-31"
        "&order=auction_date.desc,tenor.asc&limit=1000&offset=0"
    )
    assert kwargs["headers"]["apikey"] == "sk_test"


def test_get_auction_results_through_orders_by_auction_date_desc_then_tenor_asc():
    """2026-08-08 review M1: auction_date alone is not a unique sort key
    (up to 8 tenors share a date; PK is (auction_date, tenor)) -- OFFSET
    paging over a non-deterministic ORDER BY can silently drop/duplicate
    rows at page boundaries once the table exceeds page_size. The
    tenor.asc tiebreaker must be present in the actual query string, not
    just in a comment."""
    sess = _make_session()
    get_auction_results_through(
        date(2026, 7, 31), url="https://example.supabase.co", key="sk_test", session=sess,
    )
    args, _kwargs = sess.get.call_args
    assert "order=auction_date.desc,tenor.asc" in args[0]


def test_get_auction_results_through_pages_with_a_custom_page_size():
    page1 = [
        {"auction_date": "2026-07-10", "tenor": "91d", "cutoff": 9.8},
        {"auction_date": "2026-07-10", "tenor": "182d", "cutoff": 9.9},
    ]
    page2 = [
        {"auction_date": "2026-06-15", "tenor": "364d", "cutoff": 10.0},
    ]
    sess = _make_multi_response_session([page1, page2])
    rows = get_auction_results_through(
        date(2026, 7, 31), page_size=2,
        url="https://example.supabase.co", key="sk_test", session=sess,
    )
    assert rows == page1 + page2
    assert sess.get.call_count == 2
    first_url = sess.get.call_args_list[0][0][0]
    second_url = sess.get.call_args_list[1][0][0]
    assert "limit=2&offset=0" in first_url
    assert "limit=2&offset=2" in second_url


def test_get_auction_results_through_stops_paging_on_a_short_final_page():
    """A page shorter than page_size signals the end -- must not issue a
    3rd, unnecessary request."""
    page1 = [
        {"auction_date": "2026-07-10", "tenor": "91d", "cutoff": 9.8},
        {"auction_date": "2026-07-10", "tenor": "182d", "cutoff": 9.9},
    ]
    page2 = [
        {"auction_date": "2026-06-15", "tenor": "364d", "cutoff": 10.0},
    ]  # 1 row < page_size=2 -> last page
    sess = _make_multi_response_session([page1, page2])
    get_auction_results_through(
        date(2026, 7, 31), page_size=2,
        url="https://example.supabase.co", key="sk_test", session=sess,
    )
    assert sess.get.call_count == 2


def test_get_auction_results_through_raises_on_non_2xx():
    sess = _make_session(status=500)
    with pytest.raises(SupabaseReadError):
        get_auction_results_through(
            date(2026, 7, 31), url="https://example.supabase.co", key="sk_test", session=sess,
        )


def test_get_auction_results_through_raises_on_missing_credentials(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SupabaseReadError):
        get_auction_results_through(date(2026, 7, 31))
