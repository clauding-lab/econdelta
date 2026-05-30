from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import requests
import pytest
from utils.supabase_reader import (
    SupabaseReadError, get_metric_history, get_recent_run_ok, get_recent_briefings,
)


def _session(json_body, status=200):
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.text = ""
    sess.get.return_value = resp
    return sess


def test_get_metric_history_builds_ordered_limited_query():
    sess = _session([{"metric_id": "call_money_rate", "as_of": "2026-05-29", "value": 9.34}])
    rows = get_metric_history(
        "call_money_rate", days=90,
        url="https://x.supabase.co", key="sk_test", session=sess,
    )
    assert rows[0]["value"] == 9.34
    url = sess.get.call_args[0][0]
    assert url == ("https://x.supabase.co/rest/v1/metric_history"
                   "?metric_id=eq.call_money_rate&order=as_of.desc&limit=90")
    headers = sess.get.call_args[1]["headers"]
    assert headers["apikey"] == "sk_test"
    assert headers["Authorization"] == "Bearer sk_test"


def test_get_metric_history_raises_on_http_error():
    sess = _session([], status=500)
    with pytest.raises(SupabaseReadError, match="HTTP 500"):
        get_metric_history("x", days=10, url="https://x.supabase.co", key="k", session=sess)


def test_get_recent_run_ok_true_when_recent():
    recent = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sess = _session([{"started_at": recent}])
    assert get_recent_run_ok("aggregate", within_hours=48,
                             url="https://x.supabase.co", key="k", session=sess) is True


def test_get_recent_run_ok_false_when_stale():
    old = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    sess = _session([{"started_at": old}])
    assert get_recent_run_ok("aggregate", within_hours=48,
                             url="https://x.supabase.co", key="k", session=sess) is False


def test_get_recent_run_ok_false_when_no_rows():
    sess = _session([])
    assert get_recent_run_ok("aggregate", within_hours=48,
                             url="https://x.supabase.co", key="k", session=sess) is False


def test_get_recent_briefings_orders_desc():
    sess = _session([{"week_of": "2026-05-25", "title": "t"}])
    rows = get_recent_briefings(limit=8, url="https://x.supabase.co", key="k", session=sess)
    assert rows[0]["week_of"] == "2026-05-25"
    url = sess.get.call_args[0][0]
    assert url == "https://x.supabase.co/rest/v1/briefings?order=week_of.desc&limit=8"
