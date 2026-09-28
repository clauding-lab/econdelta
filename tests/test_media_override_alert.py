"""R2 fix 10b (owner decision k): a press-override step problem still reaches the operator.

Fix 10 gave the approved-press override step its own receipt and contained its
failures, so one bad media_review row can no longer crash main() or stop the
snapshot. As a side effect the old Discord notify for this step disappeared.
Owner decision (k) keeps an operator alert here: EconDelta's existing notify()
path at the same "error" level, at most ONE grouped alert per run, naming each
affected metric and the step that failed, with exception types only.

R2 fix H2: a press value is now read back by exact (metric_id, as_of) key, so a healthy
override is confirmed (receipt ok, no alert), and a readback that disproves or cannot
read a sent value is itself a step problem that alerts.

All rows below are synthetic test data. No database, model or Discord call is made:
notify() is replaced by the same list-appending fake the fix-10 tests use, and
metric_history is the in-memory FakeMetricHistory of the fix-10 contract tests.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import pytest

import aggregate_latest as agg
from tests import test_write_status_contract as contract
from utils import supabase_reader as reader
from utils import supabase_writer as sw

# The fix-10 fixture: the real main() with a confirmed daily write, every state file
# in tmp_path, and notify() replaced by a fake that records (level, title, message).
run = contract.run
metric_history_table = contract.metric_history_table

TITLE = "aggregate — press override step failed"
# Synthetic: BB's own series is still on the older quarter, so the press row is re-asserted.
DATA = {"gross_npl_ratio": 35.73}
AS_OF = {"gross_npl_ratio": date(2025, 9, 30)}


def _row(**changes) -> dict:
    return {**contract._npl_override(), **changes}


def _refuse(message: str):
    def refuse(*a, **kw):
        raise sw.SupabaseWriteError(message)
    return refuse


@pytest.fixture
def step(monkeypatch, metric_history_table):
    """The override step on its own, with fake database calls (a synthetic metric_history
    that the press value lands in and is read back from) and a recording notify fake."""
    sent: list[tuple] = []
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [])
    monkeypatch.setattr(sw, "set_media_review_status", lambda *a, **kw: None)
    return sent


def _override_alerts(sent: list[tuple]) -> list[tuple]:
    return [alert for alert in sent if alert[1] == TITLE]


def _only_alert_lines(sent: list[tuple]) -> list[str]:
    alerts = _override_alerts(sent)
    assert len(alerts) == 1, alerts  # exactly one grouped alert per run
    level, _, message = alerts[0]
    assert level == "error"  # the level the pre-fix-10 alert for this step used
    return [line[2:] for line in message.splitlines() if line.startswith("- ")]


@pytest.mark.parametrize("rows, patches, expected", [
    ([_row()], {"upsert_metric_history": _refuse("metric_history upsert HTTP 503")},
     ["gross_npl_ratio: press value write failed (SupabaseWriteError)"]),
    ([_row()], {"set_media_review_status": _refuse("media_review status patch HTTP 503")},
     ["gross_npl_ratio: press value sent; media_review status update to applied failed (SupabaseWriteError)"]),
    ([_row(press_as_of="not-a-date")], {},
     ["gross_npl_ratio: media_review row not applied (ValueError)"]),
    (None, {}, ["press override step stopped early (TypeError)"]),
], ids=["value write failed", "value sent but status update failed",
        "malformed media_review row", "unexpected exception in the step"])
def test_each_override_step_problem_sends_one_alert_naming_the_metric_and_the_failed_step(
        step, monkeypatch, rows, patches, expected):
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: rows)
    for name, fake in patches.items():
        monkeypatch.setattr(sw, name, fake)
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert receipt["status"] == "failed"  # the receipt still records the problem
    assert _only_alert_lines(step) == expected


def test_a_superseded_rows_failed_status_update_says_no_press_value_was_sent(step, monkeypatch):
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_row()])
    monkeypatch.setattr(sw, "set_media_review_status", _refuse("media_review status patch HTTP 503"))
    # Synthetic: BB's own figure has reached the press period, so the row is superseded.
    agg._media_override_receipt({"gross_npl_ratio": 32.26}, {"gross_npl_ratio": date(2026, 3, 31)})
    assert _only_alert_lines(step) == [
        "gross_npl_ratio: superseded by BB, no press value sent; "
        "media_review status update to superseded failed (SupabaseWriteError)"]


@pytest.mark.parametrize("rows", [[], [_row()]], ids=["no active override", "superseded, status updated"])
def test_an_override_step_with_nothing_wrong_sends_no_alert(step, monkeypatch, rows):
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: rows)
    # Synthetic: BB's own figure is on the press period, so nothing is sent and nothing is unconfirmed.
    receipt = agg._media_override_receipt({"gross_npl_ratio": 32.26}, {"gross_npl_ratio": date(2026, 3, 31)})
    assert receipt["status"] == "skipped"
    assert _override_alerts(step) == []


@pytest.mark.parametrize("row", [_row(), _row(status="applied")],
                         ids=["approved, status update succeeds", "already applied, re-asserted"])
def test_a_healthy_press_override_is_confirmed_by_readback_and_sends_no_alert(step, monkeypatch, row):
    # R2 fix H2: the press value is read back by exact key, so a healthy send is confirmed
    # (receipt ok), never "unconfirmed", and it never pages the operator (decisions j/k).
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [row])
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert (receipt["status"], receipt["failures"], receipt["confirmed_rows"]) == ("ok", [], 2)
    assert _override_alerts(step) == []


def test_a_healthy_override_on_the_primary_and_the_retry_run_sends_no_alert(run, monkeypatch):
    latest, sent = run
    row = _row()
    writes: list[dict] = []

    def set_status(rid, status, **kw):
        row["status"] = status  # the PATCH succeeded: the next run reads an applied row

    upsert = sw.upsert_metric_history  # the synthetic table's writer
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [dict(row)])
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: writes.append(kw) or upsert(**kw))
    monkeypatch.setattr(sw, "set_media_review_status", set_status)
    for _ in ("primary timer run", "retry timer run"):
        assert agg.main() == 0
        assert json.loads(latest.read_text())["write_status"]["media_overrides"]["status"] == "ok"
    assert len([w for w in writes if str(w.get("source", "")).startswith("media-approved")]) == 2
    assert _override_alerts(sent) == []


def test_a_confirmed_value_is_not_named_when_another_override_problem_triggers_the_alert(step, monkeypatch):
    upsert = sw.upsert_metric_history  # the synthetic table's writer

    def writer(**kw):
        if "other_metric" in kw.get("data", {}):
            raise sw.SupabaseWriteError("metric_history upsert HTTP 503")
        return upsert(**kw)

    rows = [_row(id=10, metric_id="other_metric", parsed_value=1.0, press_value=2.0), _row()]
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: rows)
    monkeypatch.setattr(sw, "upsert_metric_history", writer)
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert _only_alert_lines(step) == ["other_metric: press value write failed (SupabaseWriteError)"]
    assert (receipt["status"], receipt["confirmed_rows"]) == ("failed", 2)  # NPL's rows still counted


def test_several_override_problems_in_one_run_give_one_alert_naming_all_of_them(run, monkeypatch):
    latest, sent = run
    written: list[dict] = []
    upsert = sw.upsert_metric_history  # the synthetic table's writer

    def writer(**kw):
        if "other_metric" in kw.get("data", {}):
            raise sw.SupabaseWriteError("metric_history upsert HTTP 503")
        written.append(kw)
        return upsert(**kw)

    rows = [_row(id=10, metric_id="other_metric", parsed_value=1.0, press_value=2.0),
            _row(id=11, metric_id="third_metric", press_as_of="not-a-date"),
            _row()]
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: rows)
    monkeypatch.setattr(sw, "upsert_metric_history", writer)
    monkeypatch.setattr(sw, "set_media_review_status", _refuse("media_review status patch HTTP 503"))
    assert agg.main() == 0
    assert _only_alert_lines(sent) == [
        "other_metric: press value write failed (SupabaseWriteError)",
        "third_metric: media_review row not applied (ValueError)",
        "gross_npl_ratio: press value sent; media_review status update to applied failed (SupabaseWriteError)"]
    status = json.loads(latest.read_text())["write_status"]
    assert status["daily"]["status"] == "ok"  # still never charged to the confirmed daily write
    assert status["media_overrides"]["status"] == "failed"
    assert len(status["media_overrides"]["failures"]) == 3  # the NPL value itself was read back fine


def test_a_clean_run_sends_no_override_alert(run):
    latest, sent = run
    assert agg.main() == 0
    assert json.loads(latest.read_text())["write_status"]["media_overrides"]["status"] == "skipped"
    assert _override_alerts(sent) == []


def test_the_override_alert_carries_exception_types_never_raw_error_text(step, monkeypatch):
    secret = "https://example.invalid/rest/v1/media_review?apikey=SYNTHETIC-KEY"
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_row()])
    monkeypatch.setattr(sw, "set_media_review_status", _refuse(f"HTTP 503 from {secret}"))
    agg._media_override_receipt(dict(DATA), dict(AS_OF))
    (_, _, message), = _override_alerts(step)
    assert "SYNTHETIC-KEY" not in message and "example.invalid" not in message
    assert "32.26" not in message and "thedailystar" not in message  # no row payload either


@pytest.mark.parametrize("transport", ["raises", "returns False"])
def test_a_failing_alert_transport_changes_nothing_else_and_is_logged(step, monkeypatch, caplog, transport):
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_row()])
    monkeypatch.setattr(sw, "set_media_review_status", _refuse("media_review status patch HTTP 503"))
    expected = agg._media_override_receipt(dict(DATA), dict(AS_OF))  # notify fake works here

    def broken(*a, **kw):
        if transport == "raises":
            raise RuntimeError("synthetic webhook transport failure")
        return False

    monkeypatch.setattr(agg, "notify", broken)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=agg.logger.name):
        receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert {k: v for k, v in receipt.items() if k != "attempted_at"} == {
        k: v for k, v in expected.items() if k != "attempted_at"}
    assert "media_review status update to applied failed" in caplog.text  # the failure log line is kept
    assert "press override alert not sent" in caplog.text


def test_a_failing_alert_transport_does_not_stop_the_snapshot(run, monkeypatch):
    latest, sent = run
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_row(press_as_of="not-a-date")])
    attempts: list[tuple] = []

    def broken_for_this_alert(*a, **kw):
        if a[1] != TITLE:
            return sent.append(a)  # other alert paths keep the fix-10 fake
        attempts.append(a)
        raise RuntimeError("synthetic webhook transport failure")

    monkeypatch.setattr(agg, "notify", broken_for_this_alert)
    assert agg.main() == 0
    assert len(attempts) == 1  # the alert really was attempted, and its failure was contained
    assert json.loads(latest.read_text())["write_status"]["media_overrides"]["status"] == "failed"
