from datetime import date
from unittest.mock import MagicMock
import requests
import pytest
from utils.supabase_writer import SupabaseWriteError, upsert_briefing


def _session(status=201):
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock(); resp.status_code = status; resp.text = ""
    sess.post.return_value = resp
    return sess


def _row():
    return {
        "week_of": "2026-05-25", "title": "t", "body": "b",
        "featured_anomalies": [], "open_threads": [],
        "data_as_of": "2026-05-24", "stale_series": [],
        "model": "opus[1m]", "effort": "xhigh", "total_cost_usd": 0.0,
    }


def test_upsert_briefing_posts_with_week_of_on_conflict():
    sess = _session()
    upsert_briefing(_row(), url="https://x.supabase.co", service_key="sk_test", session=sess)
    args, kwargs = sess.post.call_args
    assert args[0] == "https://x.supabase.co/rest/v1/briefings?on_conflict=week_of"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_test"
    assert "merge-duplicates" in kwargs["headers"]["Prefer"]
    assert kwargs["json"]["week_of"] == "2026-05-25"


def test_upsert_briefing_raises_on_http_error():
    sess = _session(status=400)
    with pytest.raises(SupabaseWriteError, match="HTTP 400"):
        upsert_briefing(_row(), url="https://x.supabase.co", service_key="sk_test", session=sess)
