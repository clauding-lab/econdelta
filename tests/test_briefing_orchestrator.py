from datetime import date
from unittest.mock import patch

import briefing.__main__ as orch
from briefing.freshness import FreshnessResult
from claude_max.max_client import MaxCallResult


def _history(metric_id, latest_value, latest_as_of="2026-05-29"):
    return [{"metric_id": metric_id, "as_of": latest_as_of, "value": latest_value},
            {"metric_id": metric_id, "as_of": "2026-05-28", "value": latest_value}]


def _patches(core_stale=False, parsed=None):
    """Common monkeypatch set for the orchestrator's collaborators."""
    fresh = FreshnessResult(core_stale=core_stale, stale_series=[],
                            data_as_of=date(2026, 5, 29), reasons=["x"] if core_stale else [])
    result = MaxCallResult(raw_text="{}", parsed=parsed, usage={}, total_cost_usd=0.0)
    return fresh, result


def test_core_stale_returns_2_and_does_not_write():
    fresh, _ = _patches(core_stale=True)
    with patch.object(orch, "_collect_history", return_value={"call_money_rate": _history("call_money_rate", 9.0)}), \
         patch.object(orch, "assess_freshness", return_value=fresh), \
         patch.object(orch, "get_recent_run_ok", return_value=False), \
         patch.object(orch, "get_recent_briefings", return_value=[]), \
         patch.object(orch, "notify") as mock_notify, \
         patch.object(orch, "upsert_briefing") as mock_write, \
         patch.object(orch, "run_max") as mock_run:
        rc = orch.main()
    assert rc == 2
    mock_write.assert_not_called()
    mock_run.assert_not_called()
    mock_notify.assert_called_once()


def test_invalid_json_returns_1_and_does_not_write():
    fresh, result = _patches(core_stale=False, parsed=None)  # parsed=None -> validation fails
    with patch.object(orch, "_collect_history", return_value={"call_money_rate": _history("call_money_rate", 9.34)}), \
         patch.object(orch, "assess_freshness", return_value=fresh), \
         patch.object(orch, "get_recent_run_ok", return_value=True), \
         patch.object(orch, "get_recent_briefings", return_value=[]), \
         patch.object(orch, "notify") as mock_notify, \
         patch.object(orch, "upsert_briefing") as mock_write, \
         patch.object(orch, "run_max", return_value=result):
        rc = orch.main()
    assert rc == 1
    mock_write.assert_not_called()
    mock_notify.assert_called_once()


def test_happy_path_writes_and_returns_0():
    parsed = {"title": "T", "body": "B",
              "featured_anomalies": [{"candidate_id": "call_money_rate:change", "why": "w"}],
              "updated_threads": []}
    fresh, result = _patches(core_stale=False, parsed=parsed)
    # series with a change >= threshold so candidate "call_money_rate:change" exists
    hist = {"call_money_rate": [
        {"metric_id": "call_money_rate", "as_of": "2026-05-29", "value": 9.34},
        {"metric_id": "call_money_rate", "as_of": "2026-05-28", "value": 7.10}]}
    with patch.object(orch, "_collect_history", return_value=hist), \
         patch.object(orch, "assess_freshness", return_value=fresh), \
         patch.object(orch, "get_recent_run_ok", return_value=True), \
         patch.object(orch, "get_recent_briefings", return_value=[]), \
         patch.object(orch, "_thresholds", return_value={"call_money_rate": 2.0}), \
         patch.object(orch, "_cadence", return_value={"call_money_rate": "daily"}), \
         patch.object(orch, "_labels", return_value={"call_money_rate": "Call Money Rate"}), \
         patch.object(orch, "notify"), \
         patch.object(orch, "upsert_briefing") as mock_write, \
         patch.object(orch, "run_max", return_value=result):
        rc = orch.main()
    assert rc == 0
    mock_write.assert_called_once()
    row = mock_write.call_args[0][0]
    assert row["week_of"]  # set
    assert row["featured_anomalies"][0]["value"] == 9.34  # Python's number, merged in
    assert row["model"]  # provenance recorded
