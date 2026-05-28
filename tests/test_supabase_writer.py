"""Tests for utils.supabase_writer.upsert_metric_history.

Mocks the requests.Session so no real Supabase call goes out. Verifies:
  - filtering of non-numeric values
  - bool excluded (it's int subclass in Python)
  - PostgREST endpoint shape with on_conflict
  - auth headers
  - batching behaviour
  - error mapping (network, non-2xx)
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_writer import (
    SupabaseWriteError,
    _rows_from_data,
    upsert_metric_history,
)


def _make_session(status: int = 201, text: str = "") -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    sess.post.return_value = resp
    return sess


def test_rows_from_data_filters_non_numeric():
    data = {
        "policy_rate": 10.0,
        "macro_cpi_food": 8.29,
        "live_status": "ok",            # str — skip
        "config_block": {"foo": "bar"},  # dict — skip
        "is_trading_day": True,           # bool — skip even though int subclass
        "trading_volume": 0,              # int 0 — keep (real reading)
        "negative_npl_change": -2.4,      # negative float — keep
        "broken": None,                   # None — skip
    }
    rows = _rows_from_data(data, date(2026, 5, 2), "EconDelta")
    metric_ids = {r["metric_id"] for r in rows}
    assert metric_ids == {"policy_rate", "macro_cpi_food", "trading_volume", "negative_npl_change"}


def test_upsert_posts_to_postgrest_with_on_conflict_clause():
    sess = _make_session()
    n = upsert_metric_history(
        data={"npl": 35.73},
        as_of=date(2026, 5, 2),
        url="https://example.supabase.co",
        service_key="sk_test_123",
        session=sess,
    )
    assert n == 1
    args, kwargs = sess.post.call_args
    url = args[0]
    assert url == "https://example.supabase.co/rest/v1/metric_history?on_conflict=metric_id,as_of"
    assert kwargs["headers"]["apikey"] == "sk_test_123"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test_123"
    assert "merge-duplicates" in kwargs["headers"]["Prefer"]
    payload = kwargs["json"]
    assert payload == [{
        "metric_id": "npl",
        "as_of": "2026-05-02",
        "value": 35.73,
        "source": "EconDelta",
    }]


def test_upsert_returns_zero_when_no_numeric_values():
    sess = _make_session()
    n = upsert_metric_history(
        data={"all_strings": "nope", "config": {"k": "v"}},
        as_of=date(2026, 5, 2),
        url="https://example.supabase.co",
        service_key="sk_test_123",
        session=sess,
    )
    assert n == 0
    sess.post.assert_not_called()


def test_upsert_raises_on_non_2xx_response():
    sess = _make_session(status=401, text="invalid_jwt")
    with pytest.raises(SupabaseWriteError, match="HTTP 401"):
        upsert_metric_history(
            data={"x": 1.0},
            as_of=date(2026, 5, 2),
            url="https://example.supabase.co",
            service_key="bad_key",
            session=sess,
        )


def test_upsert_raises_on_network_error():
    sess = MagicMock(spec=requests.Session)
    sess.post.side_effect = requests.exceptions.ConnectionError("dns lookup failed")
    with pytest.raises(SupabaseWriteError, match="network error"):
        upsert_metric_history(
            data={"x": 1.0},
            as_of=date(2026, 5, 2),
            url="https://example.supabase.co",
            service_key="sk",
            session=sess,
        )


def test_upsert_raises_when_credentials_missing(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SupabaseWriteError, match="SUPABASE_URL"):
        upsert_metric_history(data={"x": 1.0}, as_of=date(2026, 5, 2))


def test_upsert_picks_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://from-env.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sk_env_456")
    sess = _make_session()
    upsert_metric_history(
        data={"x": 1.0}, as_of=date(2026, 5, 2), session=sess,
    )
    url = sess.post.call_args[0][0]
    # Trailing slash is stripped, scheme preserved.
    assert url.startswith("https://from-env.supabase.co/rest/v1/metric_history")
    assert sess.post.call_args[1]["headers"]["apikey"] == "sk_env_456"


def test_upsert_batches_large_payloads():
    """500 row batch limit — sending 1200 rows should trigger 3 POSTs."""
    data = {f"metric_{i:04d}": float(i) for i in range(1200)}
    sess = _make_session()
    n = upsert_metric_history(
        data=data, as_of=date(2026, 5, 2),
        url="https://example.supabase.co", service_key="sk", session=sess,
    )
    assert n == 1200
    assert sess.post.call_count == 3


# ---------------------------------------------------------------------------
# Observability — silent drops now warn (regression guard for PR #31 class).
# ---------------------------------------------------------------------------

def test_rows_from_data_scalar_does_not_warn(caplog):
    """Scalar values build a row and produce no warning — keep the happy path quiet."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": 1.0}, date(2026, 5, 2), "EconDelta")
    assert len(rows) == 1
    assert rows[0]["metric_id"] == "foo"
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_rows_from_data_dict_triggers_warning(caplog):
    """A dict value is dropped (no row built) and logs a warning naming the metric_id and type."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": {"a": 1.0}}, date(2026, 5, 2), "EconDelta")
    assert rows == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "metric_id=foo" in msg
    assert "type=dict" in msg


def test_rows_from_data_bool_stays_silent(caplog):
    """Booleans are filtered without a warning — pre-existing behavior, by design."""
    with caplog.at_level("WARNING", logger="supabase_writer"):
        rows = _rows_from_data({"foo": True}, date(2026, 5, 2), "EconDelta")
    assert rows == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
