"""Tests for utils.supabase_reader.

Mocks requests.Session so no real Supabase call goes out.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from utils.supabase_reader import (
    SupabaseReadError,
    get_metric_history_monthly,
)


def _make_session(status: int = 200, payload: object = None) -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else []
    sess.get.return_value = resp
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
