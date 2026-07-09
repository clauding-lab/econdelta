"""E2.2 — the post-write landed-count invariant.

A 2xx / "wrote N rows" log is NOT proof of persistence (landmine 22). These
tests exercise ``verify_landed_count`` (the read-back guard) and confirm
``upsert_metric_history`` threads an explicit ``ingested_at`` so the read-back
can count exactly this run's rows.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import requests

from utils.supabase_writer import upsert_metric_history, verify_landed_count

_CREDS = {"url": "https://proj.supabase.co", "service_key": "sk_test"}


def _get_resp(total: int, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Range": f"0-0/{total}"}
    resp.json.return_value = [{"metric_id": "x"}] if total else []
    return resp


def test_verify_returns_true_and_stays_silent_on_match(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    called = []
    monkeypatch.setattr("utils.notifier.notify", lambda *a, **k: called.append(a))
    sess = MagicMock(spec=requests.Session)
    sess.get.return_value = _get_resp(3)

    out = verify_landed_count(
        3, since=datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc),
        source_label="aggregate", session=sess, **_CREDS,
    )
    assert out is True
    assert called == []


def test_verify_alerts_on_mismatch(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    called = []
    monkeypatch.setattr("utils.notifier.notify", lambda *a, **k: called.append(a))
    sess = MagicMock(spec=requests.Session)
    sess.get.return_value = _get_resp(0)  # nothing landed (the misroute class)

    out = verify_landed_count(
        3, since=datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc),
        source_label="world_bank_pink_sheet", session=sess, **_CREDS,
    )
    assert out is False
    assert len(called) == 1
    level, title, _msg = called[0]
    assert level == "error"
    assert "landed-count mismatch" in title


def test_verify_scopes_query_by_metric_ids(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr("utils.notifier.notify", lambda *a, **k: None)
    sess = MagicMock(spec=requests.Session)
    sess.get.return_value = _get_resp(2)

    verify_landed_count(
        2, since=datetime(2026, 7, 9, tzinfo=timezone.utc),
        metric_ids=["lng_price_usd_mmbtu", "wheat_price_usd_mt"],
        session=sess, **_CREDS,
    )
    params = sess.get.call_args.kwargs["params"]
    assert params["metric_id"].startswith("in.(")
    assert "lng_price_usd_mmbtu" in params["metric_id"]
    assert params["ingested_at"].startswith("gte.2026-07-09")


def test_verify_returns_none_on_read_exception(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    called = []
    monkeypatch.setattr("utils.notifier.notify", lambda *a, **k: called.append(a))
    sess = MagicMock(spec=requests.Session)
    sess.get.side_effect = requests.exceptions.ConnectionError("boom")

    out = verify_landed_count(3, since=datetime(2026, 7, 9, tzinfo=timezone.utc),
                              session=sess, **_CREDS)
    assert out is None          # couldn't verify — never crash the writer
    assert called == []         # a transient read failure must not alert


def test_verify_returns_none_on_http_error(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr("utils.notifier.notify", lambda *a, **k: None)
    sess = MagicMock(spec=requests.Session)
    sess.get.return_value = _get_resp(0, status=500)

    out = verify_landed_count(3, since=datetime(2026, 7, 9, tzinfo=timezone.utc),
                              session=sess, **_CREDS)
    assert out is None


def test_verify_is_a_noop_when_supabase_skipped(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    sess = MagicMock(spec=requests.Session)
    out = verify_landed_count(3, since=datetime(2026, 7, 9, tzinfo=timezone.utc),
                              session=sess, **_CREDS)
    assert out is None
    sess.get.assert_not_called()


def test_upsert_threads_explicit_ingested_at_to_every_row():
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = 201
    sess.post.return_value = resp
    stamp = datetime(2026, 7, 9, 7, 0, 12, tzinfo=timezone.utc)

    upsert_metric_history(
        data={"money_multiplier": 5.37, "policy_rate_repo": 10.0},
        as_of=date(2026, 7, 9), ingested_at=stamp, session=sess, **_CREDS,
    )
    posted = sess.post.call_args.kwargs["json"]
    assert posted, "expected rows posted"
    assert all(r["ingested_at"] == stamp.isoformat() for r in posted)
