"""Tests for the structured row-table writer path (S8).

Covers ``upsert_auction_rows`` and the two thin wrappers
``upsert_auction_results`` / ``upsert_auction_calendar``. Mocks the
requests.Session so no real Supabase call goes out. Verifies:
  - PostgREST endpoint shape with on_conflict=auction_date,tenor for BOTH tables
  - row payload is passed through whole (not flattened to scalars)
  - auth headers + merge-duplicates Prefer
  - date objects normalised to ISO strings
  - missing PK field rejected (ValueError)
  - unknown column rejected (ValueError)
  - empty rows -> 0, no POST
  - batching at the 500-row limit
  - error mapping (network, non-2xx)
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_writer import (
    SupabaseWriteError,
    upsert_auction_calendar,
    upsert_auction_results,
    upsert_auction_rows,
)


def _make_session(status: int = 201, text: str = "") -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    sess.post.return_value = resp
    return sess


# ---------------------------------------------------------------------------
# auction_results — endpoint shape + row passthrough
# ---------------------------------------------------------------------------

def test_results_posts_to_postgrest_with_auction_conflict_clause():
    sess = _make_session()
    rows = [{
        "auction_date": "2026-05-28",
        "tenor": "91d",
        "size": 1500.0,
        "bid": 4200.0,
        "cover": 2.8,
        "wam": 0.25,
        "cutoff": 10.85,
    }]
    n = upsert_auction_results(
        rows,
        url="https://example.supabase.co",
        service_key="sk_test_123",
        session=sess,
    )
    assert n == 1
    args, kwargs = sess.post.call_args
    url = args[0]
    assert url == (
        "https://example.supabase.co/rest/v1/auction_results"
        "?on_conflict=auction_date,tenor"
    )
    assert kwargs["headers"]["apikey"] == "sk_test_123"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_123"
    assert "merge-duplicates" in kwargs["headers"]["Prefer"]
    # The whole row is sent — NOT flattened into scalar metric_history rows.
    assert kwargs["json"] == rows


def test_calendar_posts_to_correct_table_and_conflict_clause():
    sess = _make_session()
    rows = [{"auction_date": "2026-06-02", "tenor": "182d", "notional": 2000.0}]
    n = upsert_auction_calendar(
        rows,
        url="https://example.supabase.co",
        service_key="sk_test_123",
        session=sess,
    )
    assert n == 1
    url = sess.post.call_args[0][0]
    assert url == (
        "https://example.supabase.co/rest/v1/auction_calendar"
        "?on_conflict=auction_date,tenor"
    )
    assert sess.post.call_args[1]["json"] == rows


def test_results_normalises_date_object_to_iso_string():
    """A ``date`` in auction_date is serialised to ISO so PostgREST accepts it."""
    sess = _make_session()
    upsert_auction_results(
        [{"auction_date": date(2026, 5, 28), "tenor": "364d", "cutoff": 11.2}],
        url="https://example.supabase.co",
        service_key="sk",
        session=sess,
    )
    sent = sess.post.call_args[1]["json"]
    assert sent[0]["auction_date"] == "2026-05-28"
    assert sent[0]["tenor"] == "364d"


def test_calendar_row_with_only_pk_and_notional_is_valid():
    """Forward-calendar rows have NO bid/cover/wam/cutoff — just notional."""
    sess = _make_session()
    n = upsert_auction_calendar(
        [{"auction_date": "2026-06-09", "tenor": "5y"}],  # even notional optional
        url="https://example.supabase.co",
        service_key="sk",
        session=sess,
    )
    assert n == 1


def test_heterogeneous_rows_get_a_uniform_key_set():
    """PostgREST bulk-upsert rejects (PGRST102) a batch whose objects differ in keys.

    A bills row (no ``wam``) batched with a bonds row (has ``wam``) must be reconciled
    to a single key set, with the missing field sent as NULL — not omitted, not a
    fabricated value.
    """
    sess = _make_session()
    rows = [
        {"auction_date": "2026-05-24", "tenor": "91d",
         "size": 3500.0, "bid": 6904.22, "cover": 1.97, "cutoff": 10.15},
        {"auction_date": "2026-05-13", "tenor": "5y",
         "size": 3000.0, "bid": 8555.59, "cover": 2.85, "cutoff": 10.78, "wam": 4.96},
    ]
    upsert_auction_results(
        rows, url="https://example.supabase.co", service_key="sk", session=sess,
    )
    posted = sess.post.call_args[1]["json"]
    assert len({frozenset(r) for r in posted}) == 1  # every object shares one key set
    bill = next(r for r in posted if r["tenor"] == "91d")
    assert "wam" in bill and bill["wam"] is None  # NULL, not missing or fabricated


# ---------------------------------------------------------------------------
# Validation — PK required, unknown columns rejected
# ---------------------------------------------------------------------------

def test_missing_tenor_raises_value_error():
    sess = _make_session()
    with pytest.raises(ValueError, match="missing required primary-key field 'tenor'"):
        upsert_auction_results(
            [{"auction_date": "2026-05-28", "cutoff": 10.0}],
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )
    sess.post.assert_not_called()


def test_missing_auction_date_raises_value_error():
    sess = _make_session()
    with pytest.raises(ValueError, match="missing required primary-key field 'auction_date'"):
        upsert_auction_calendar(
            [{"tenor": "91d", "notional": 1000.0}],
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )
    sess.post.assert_not_called()


def test_unknown_column_on_results_raises_value_error():
    """A stray column (typo or leaked field) is rejected, not silently passed
    to PostgREST where it would 400 the whole batch."""
    sess = _make_session()
    with pytest.raises(ValueError, match="unknown column"):
        upsert_auction_results(
            [{"auction_date": "2026-05-28", "tenor": "91d", "yield": 10.0}],  # 'yield' not a column
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )
    sess.post.assert_not_called()


def test_calendar_rejects_result_only_columns():
    """A result field (cover) on a calendar row is an unknown column there —
    enforces the two-shape separation at the writer boundary."""
    sess = _make_session()
    with pytest.raises(ValueError, match="unknown column"):
        upsert_auction_calendar(
            [{"auction_date": "2026-06-02", "tenor": "182d", "cover": 2.5}],
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )
    sess.post.assert_not_called()


# ---------------------------------------------------------------------------
# Empty / batching / error mapping
# ---------------------------------------------------------------------------

def test_empty_rows_returns_zero_and_does_not_post():
    sess = _make_session()
    n = upsert_auction_results(
        [], url="https://example.supabase.co", service_key="sk", session=sess,
    )
    assert n == 0
    sess.post.assert_not_called()


def test_batches_large_payloads_at_500():
    """600 rows -> two POSTs (500 + 100)."""
    rows = [
        {"auction_date": "2026-05-28", "tenor": f"t{i:04d}", "notional": float(i)}
        for i in range(600)
    ]
    sess = _make_session()
    n = upsert_auction_calendar(
        rows, url="https://example.supabase.co", service_key="sk", session=sess,
    )
    assert n == 600
    assert sess.post.call_count == 2


def test_raises_on_non_2xx_response():
    sess = _make_session(status=400, text="duplicate key")
    with pytest.raises(SupabaseWriteError, match="auction_results upsert returned HTTP 400"):
        upsert_auction_results(
            [{"auction_date": "2026-05-28", "tenor": "91d"}],
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )


def test_raises_on_network_error():
    sess = MagicMock(spec=requests.Session)
    sess.post.side_effect = requests.exceptions.ConnectionError("dns lookup failed")
    with pytest.raises(SupabaseWriteError, match="network error during auction_calendar upsert"):
        upsert_auction_calendar(
            [{"auction_date": "2026-06-02", "tenor": "182d"}],
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )


def test_generic_writer_serves_arbitrary_table():
    """upsert_auction_rows is generic — the wrappers just pin table + columns."""
    sess = _make_session()
    n = upsert_auction_rows(
        [{"auction_date": "2026-05-28", "tenor": "91d", "cutoff": 10.0}],
        table="auction_results",
        allowed_columns=frozenset({"auction_date", "tenor", "cutoff"}),
        url="https://example.supabase.co",
        service_key="sk",
        session=sess,
    )
    assert n == 1
    assert "auction_results?on_conflict=auction_date,tenor" in sess.post.call_args[0][0]
