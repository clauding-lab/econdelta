"""R2 fix 10: `write_status` is a typed, validated persistence contract.

The snapshot may omit it (a pre-receipt producer), but a receipt that is present
must say exactly one of ok / failed / skipped, carry a timezone-aware attempt time,
and never let a leg's failure hide under a healthier parent status. The producer
validates before writing, and an approved-press override step can neither rewrite
the main daily write's receipt nor stop the snapshot from being written.
No database, model or Discord call is made.
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

import aggregate_latest as agg
from tests.test_aggregator import _build_data_tree
from utils import supabase_reader as reader
from utils import supabase_writer as sw
from utils import write_receipts as wr
from utils.schema import LatestBundle

AT = "2026-09-26T21:00:00+00:00"
DAILY_OK = {"status": "ok", "attempted_at": AT, "reason": "rows confirmed"}
MONTHLY_SKIP = {"status": "skipped", "attempted_at": AT, "reason": "official lag"}


def _bundle(**extra) -> dict:
    return {"updated_at": AT, "sources_status": {}, "data": {}, **extra}


# --- the contract itself ------------------------------------------------------


@pytest.mark.parametrize("payload", [_bundle(), _bundle(write_status=None)], ids=["absent", "null"])
def test_a_pre_receipt_snapshot_stays_valid_and_gains_no_receipt(payload):
    assert LatestBundle.model_validate(payload).write_status is None


def _real_monthly_receipt() -> dict:
    """The exact shape the fix-8 receipt machinery produces: nested legs, one failed."""
    with wr.monthly_attempt("macro", {"cpi_yoy_monthly": "cpi", "remit_monthly_mn": "remittance"}) as r:
        r.declare("cpi", "remittance")
        r.leg("cpi").confirmed_rows = 2
        r.fail_leg("remittance", "read", wr.SOURCE_FETCH_FAILED, "RuntimeError")
    with wr.monthly_attempt("yield") as y:
        y.skip_leg(None, wr.NO_NEW_OFFICIAL_PERIOD, "no auction since 2026-09-01")
    return wr.combine_monthly({"chart_appenders": wr.combine_monthly(
        {"macro": r.result(), "yield": y.result()})})


def test_every_receipt_shape_the_producer_emits_validates_and_round_trips():
    status = {"daily": DAILY_OK, "monthly": _real_monthly_receipt()}
    parsed = LatestBundle.model_validate(_bundle(write_status=status))
    dumped = parsed.model_dump(mode="json")["write_status"]
    assert dumped["monthly"]["legs"]["chart_appenders"]["legs"]["macro"]["legs"]["cpi"]["confirmed_rows"] == 2
    assert dumped["monthly"]["status"] == "failed"
    assert "failures" not in dumped["daily"]  # an absent optional field is not serialised as null
    assert LatestBundle.model_validate(json.loads(json.dumps(parsed.model_dump(mode="json")))) == parsed


def _lag_skip_status(**skip_extra) -> dict:
    with wr.monthly_attempt("macro", {"remittance_usd_mn_monthly": "remittance"}) as r:
        r.declare("remittance", "cpi")
        r.skip_leg("remittance", wr.NO_NEW_OFFICIAL_PERIOD, "BB lists nothing newer", **skip_extra)
        r.skip_leg("cpi", wr.NO_NEWER_DATABASE_VINTAGE, "already recorded")
    return {"daily": DAILY_OK, "monthly": r.result()}


def test_publication_lag_keeps_its_newest_recorded_month_through_validation_and_serialisation():
    """R2 fix 4: the Brief ages the newest recorded month against its monthly window; the snapshot
    must carry it as an ISO date, and a skip without one must not gain a null."""
    parsed = LatestBundle.model_validate(_bundle(write_status=_lag_skip_status(newest_recorded=date(2026, 8, 1))))
    legs = json.loads(parsed.model_dump_json())["write_status"]["monthly"]["legs"]

    assert legs["remittance"]["skips"] == [{"category": "no new official period",
                                            "detail": "BB lists nothing newer", "newest_recorded": "2026-08-01"}]
    assert legs["cpi"]["skips"] == [{"category": "no newer database vintage", "detail": "already recorded"}]


def test_publication_lag_keeps_its_accepted_lag_window_through_validation_and_serialisation():
    """R2 fix 4 round 1: the window the Brief ages the lag against travels as a whole number of
    days; a skip without one gains no null."""
    status = _lag_skip_status(newest_recorded=date(2026, 8, 1), lag_window_days=165)
    parsed = LatestBundle.model_validate(_bundle(write_status=status))
    legs = json.loads(parsed.model_dump_json())["write_status"]["monthly"]["legs"]

    assert legs["remittance"]["skips"] == [{"category": "no new official period", "detail": "BB lists nothing newer",
                                            "newest_recorded": "2026-08-01", "lag_window_days": 165}]
    assert "lag_window_days" not in legs["cpi"]["skips"][0]


@pytest.mark.parametrize("window", [0, -45, True, "45", 45.5], ids=["zero", "negative", "bool", "text", "fraction"])
def test_a_lag_window_that_is_not_a_positive_whole_number_of_days_is_rejected(window):
    status = _lag_skip_status(newest_recorded=date(2026, 8, 1), lag_window_days=45)
    status["monthly"]["legs"]["remittance"]["skips"][0]["lag_window_days"] = window
    with pytest.raises(ValidationError):
        LatestBundle.model_validate(_bundle(write_status=status))


def test_a_newest_recorded_month_that_is_not_a_date_is_rejected():
    status = _lag_skip_status(newest_recorded=date(2026, 8, 1))
    status["monthly"]["legs"]["remittance"]["skips"][0]["newest_recorded"] = "August"
    with pytest.raises(ValidationError):
        LatestBundle.model_validate(_bundle(write_status=status))


WRITE_FAILURE = {"operation": "write", "category": "database write failed", "detail": "x"}
# The exact shape _media_override_receipt emits: failures but no confirmed_rows.
OVERRIDE_FAILED = {"status": "failed", "attempted_at": AT, "reason": "write failed", "failures": [WRITE_FAILURE]}


def _leaf(**kw) -> dict:
    return {"status": "ok", "attempted_at": AT, "reason": "r", "confirmed_rows": 1,
            "failures": [], "skips": [], **kw}


MALFORMED = {
    "status spelled OK": {"daily": {**DAILY_OK, "status": "OK"}, "monthly": MONTHLY_SKIP},
    "status not a word": {"daily": {**DAILY_OK, "status": "done"}, "monthly": MONTHLY_SKIP},
    "naive attempt time": {"daily": {**DAILY_OK, "attempted_at": "2026-09-26T21:00:00"}, "monthly": MONTHLY_SKIP},
    "attempt time missing": {"daily": {"status": "ok", "reason": "x"}, "monthly": MONTHLY_SKIP},
    "stage missing": {"daily": DAILY_OK},
    "stage null": {"daily": DAILY_OK, "monthly": None},
    "unknown stage": {"daily": DAILY_OK, "monthly": MONTHLY_SKIP, "weekly": MONTHLY_SKIP},
    "unknown receipt field": {"daily": {**DAILY_OK, "extra": 1}, "monthly": MONTHLY_SKIP},
    "reason not text": {"daily": {**DAILY_OK, "reason": 5}, "monthly": MONTHLY_SKIP},
    "negative rows": {"daily": DAILY_OK, "monthly": _leaf(confirmed_rows=-1)},
    "boolean rows": {"daily": DAILY_OK, "monthly": _leaf(confirmed_rows=True)},
    "text rows": {"daily": DAILY_OK, "monthly": _leaf(confirmed_rows="1")},
    "fractional rows": {"daily": DAILY_OK, "monthly": _leaf(confirmed_rows=1.0)},
    "failed leg under ok parent": {"daily": DAILY_OK, "monthly": {
        "status": "ok", "attempted_at": AT, "reason": "r", "confirmed_rows": 1,
        "legs": {"cpi": _leaf(), "remittance": _leaf(status="failed", confirmed_rows=0, failures=[
            {"operation": "read", "category": "source fetch failed", "detail": "RuntimeError"}])}}},
    "leaf failure reported ok": {"daily": DAILY_OK, "monthly": _leaf(failures=[
        {"operation": "write", "category": "database write failed", "detail": "x"}])},
    "ok with zero confirmed rows": {"daily": DAILY_OK, "monthly": _leaf(confirmed_rows=0)},
    "parent rows disagree with legs": {"daily": DAILY_OK, "monthly": {
        "status": "ok", "attempted_at": AT, "reason": "r", "confirmed_rows": 9, "legs": {"cpi": _leaf()}}},
    "failure entry untyped": {"daily": DAILY_OK, "monthly": _leaf(status="failed", failures=["boom"])},
    # Failure dominates even without a row count: the daily and media_overrides receipts carry none.
    "daily failure reported ok": {"daily": {**DAILY_OK, "failures": [WRITE_FAILURE]}, "monthly": MONTHLY_SKIP},
    "rowless monthly failure reported ok": {"daily": DAILY_OK, "monthly": {
        "status": "ok", "attempted_at": AT, "reason": "r", "failures": [WRITE_FAILURE]}},
    "override failure reported ok": {"daily": DAILY_OK, "monthly": MONTHLY_SKIP,
                                     "media_overrides": {**OVERRIDE_FAILED, "status": "ok"}},
    "override failure reported skipped": {"daily": DAILY_OK, "monthly": MONTHLY_SKIP,
                                          "media_overrides": {**OVERRIDE_FAILED, "status": "skipped"}},
}


@pytest.mark.parametrize("write_status", list(MALFORMED.values()), ids=list(MALFORMED))
def test_a_present_receipt_that_breaks_the_contract_is_rejected(write_status):
    with pytest.raises(ValidationError):
        LatestBundle.model_validate(_bundle(write_status=write_status))


def test_a_rowless_failed_receipt_is_valid_so_its_mislabelled_twins_fail_only_on_status():
    status = {"daily": {**DAILY_OK, "status": "failed", "failures": [WRITE_FAILURE]},
              "monthly": MONTHLY_SKIP, "media_overrides": OVERRIDE_FAILED}
    assert LatestBundle.model_validate(_bundle(write_status=status)).write_status.media_overrides.status == "failed"


# --- the producer: main() validates, and the override step is its own receipt ---


class FakeMetricHistory:
    """Synthetic in-memory metric_history: rows land by (metric_id, as_of), last write wins,
    and read back only by exact key (R2 fix H2). No database is involved."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.reads: list[list[tuple[str, str]]] = []

    def upsert(self, *, data, as_of, source="EconDelta", source_as_of_map=None, **kw) -> int:
        dates = source_as_of_map or {}
        landed = 0
        for mid, value in data.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                day = dates.get(mid, as_of).isoformat()
                self.rows[(mid, day)] = {"metric_id": mid, "as_of": day, "value": value, "source": source}
                landed += 1
        return landed

    def read_at(self, keys, **kw) -> list[dict]:
        wanted = [tuple(k) for k in keys]
        self.reads.append(wanted)
        return [dict(self.rows[k]) for k in wanted if k in self.rows]


@pytest.fixture
def metric_history_table(monkeypatch) -> FakeMetricHistory:
    """The daily writer and the exact-key readback share one synthetic table."""
    table = FakeMetricHistory()
    monkeypatch.setattr(sw, "upsert_metric_history", table.upsert)
    monkeypatch.setattr(reader, "get_metric_history_at", table.read_at, raising=False)
    return table


@pytest.fixture
def run(tmp_path, monkeypatch, metric_history_table):
    """The real main() with a confirmed daily write; returns (latest path, sent alerts)."""
    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest = data_dir / "latest.json"
    latest.write_text('{"previous": true}')
    for name, value in (("DATA_DIR", data_dir), ("LATEST_PATH", latest),
                        ("ARCHIVE_DIR", data_dir / "archive"), ("CONFIG_PATH", cfg_path),
                        ("STALENESS_STATE_PATH", data_dir / "staleness_state.json"),
                        ("WATCHLIST_STALENESS_STATE_PATH", data_dir / "watchlist_staleness_state.json"),
                        ("STALE_FALLBACK_ALERT_STATE_PATH", data_dir / "stale_fallback_alert_state.json")):
        monkeypatch.setattr(agg, name, value)  # every state file main() writes stays in tmp_path
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    sent: list[tuple] = []
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **kw: 0)
    monkeypatch.setattr(sw, "verify_landed_count", lambda *a, **kw: True)
    monkeypatch.setattr(agg, "_write_reserves_monthly_split", lambda *a: 0)
    monkeypatch.setattr(agg, "_run_chart_feeding_monthly_appenders", lambda: dict(MONTHLY_SKIP))
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [])
    monkeypatch.setattr(sw, "set_media_review_status", lambda *a, **kw: None)
    return latest, sent


def _npl_override(press_as_of: str = "2026-03-31") -> dict:
    return {"id": 9, "metric_id": "gross_npl_ratio", "parsed_value": 35.73, "parsed_as_of": "2025-09-30",
            "press_value": 32.26, "press_as_of": press_as_of, "kind": "fresher_period",
            "source_outlet": "thedailystar", "status": "approved"}


@pytest.mark.parametrize("bad", [{**MONTHLY_SKIP, "status": "done"},
                                 {**MONTHLY_SKIP, "attempted_at": "2026-09-26T21:00:00"}],
                         ids=["unknown status", "naive attempt time"])
def test_the_producer_never_writes_a_receipt_that_breaks_the_contract(run, monkeypatch, bad):
    latest, _ = run
    monkeypatch.setattr(agg, "_run_chart_feeding_monthly_appenders", lambda: dict(bad))
    with pytest.raises(ValidationError):
        agg.main()
    assert json.loads(latest.read_text()) == {"previous": True}  # the accepted snapshot is untouched
    assert not latest.with_suffix(".json.tmp").exists()


def test_an_override_status_update_failure_is_not_charged_to_the_confirmed_daily_write(run, monkeypatch):
    latest, sent = run
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_override()])

    def refuse(*a, **kw):
        raise sw.SupabaseWriteError("media_review status patch HTTP 503")

    monkeypatch.setattr(sw, "set_media_review_status", refuse)
    assert agg.main() == 0
    status = json.loads(latest.read_text())["write_status"]
    assert status["daily"]["status"] == "ok"
    assert status["daily"]["reason"] == "rows confirmed"
    assert status["media_overrides"]["status"] == "failed"
    assert "gross_npl_ratio" in status["media_overrides"]["reason"]
    assert not any("Supabase write failed" in str(alert[1]) for alert in sent)  # no false upsert alarm


def test_a_press_value_sent_before_its_status_update_failed_is_recorded_as_sent_not_as_a_failed_write(
        run, monkeypatch):
    """The metric_history write succeeded; only the media_review status PATCH failed.
    The receipt must say the value went out (confirmed by exact readback, R2 fix H2) and
    name the PATCH, never claim the press figure failed to reach metric_history."""
    latest, _ = run
    written: list[dict] = []
    upsert = sw.upsert_metric_history  # the synthetic table's writer
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_override()])
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: written.append(kw) or upsert(**kw))

    def refuse(*a, **kw):
        raise sw.SupabaseWriteError("media_review status patch HTTP 503")

    monkeypatch.setattr(sw, "set_media_review_status", refuse)
    assert agg.main() == 0
    overrides = json.loads(latest.read_text())["write_status"]["media_overrides"]
    assert any(kw.get("as_of") == date(2026, 3, 31) for kw in written)  # the press value really was sent
    assert overrides["status"] == "failed"
    assert overrides["failures"] == [
        {"operation": "write", "category": wr.DATABASE_WRITE_FAILED,
         "detail": "gross_npl_ratio media_review status update (SupabaseWriteError)"}]
    assert overrides["confirmed_rows"] == 2  # gross_npl_ratio and its banking_npl_pct alias, read back


def test_a_superseded_rows_status_update_failure_names_the_update_and_sends_nothing():
    written: list[dict] = []

    def refuse(*a, **kw):
        raise sw.SupabaseWriteError("media_review status patch HTTP 503")

    # BB's own figure is now on the press period, so the override is superseded, not re-sent.
    outcome = agg._apply_media_overrides(
        {"gross_npl_ratio": 32.26}, {"gross_npl_ratio": date(2026, 3, 31)},
        writer=lambda **kw: written.append(kw) or 1, reader=lambda: [_npl_override()], set_status=refuse)
    assert written == []
    assert outcome == {"written": [], "failures": [
        {"operation": "write", "category": wr.DATABASE_WRITE_FAILED,
         "detail": "gross_npl_ratio media_review status update (SupabaseWriteError)"}]}


def _refused_once(written: list[dict]):
    """A metric_history writer whose first call gets HTTP 503; later calls land."""
    attempts: list[dict] = []

    def writer(**kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise sw.SupabaseWriteError("metric_history upsert HTTP 503")
        written.append(kw)
        return 1
    return writer


@pytest.mark.parametrize("first_row, make_writer, first_failure", [
    ({**_npl_override(), "id": 10, "metric_id": "other_metric", "parsed_value": 1.0, "press_value": 2.0},
     _refused_once,
     {"operation": "write", "category": wr.DATABASE_WRITE_FAILED, "detail": "other_metric (SupabaseWriteError)"}),
    ({**_npl_override("not-a-date"), "id": 10, "metric_id": "other_metric"},
     lambda written: lambda **kw: written.append(kw) or 1,
     {"operation": "unknown", "category": wr.UNHANDLED_EXCEPTION, "detail": "other_metric (ValueError)"}),
], ids=["first row's write refused", "first row malformed"])
def test_one_failed_override_row_still_lets_the_next_approved_press_value_through(
        first_row, make_writer, first_failure):
    """Docstring promise of _apply_media_overrides: a failure on one row is recorded
    and the next row still runs, so an owner-approved press figure is not silently dropped."""
    written: list[dict] = []
    patched: list[tuple] = []
    outcome = agg._apply_media_overrides(
        {"gross_npl_ratio": 35.73, "other_metric": 1.0},
        {"gross_npl_ratio": date(2025, 9, 30), "other_metric": date(2025, 9, 30)},
        writer=make_writer(written), reader=lambda: [first_row, _npl_override()],
        set_status=lambda *a, **kw: patched.append((*a, kw)))
    assert [(kw["data"]["gross_npl_ratio"], kw["as_of"]) for kw in written] == [(32.26, date(2026, 3, 31))]
    assert patched == [(9, "applied", {"applied": True})]  # only the row that really went out is marked applied
    assert outcome == {"written": ["gross_npl_ratio"], "failures": [first_failure]}


def test_a_malformed_override_row_cannot_stop_the_snapshot_being_written(run, monkeypatch):
    latest, _ = run
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_override("not-a-date")])
    assert agg.main() == 0
    status = json.loads(latest.read_text())["write_status"]
    assert status["daily"]["status"] == "ok"
    assert status["media_overrides"]["status"] == "failed"


def test_an_override_write_without_readback_is_never_reported_ok(run, monkeypatch):
    latest, _ = run
    written: list[dict] = []
    upsert = sw.upsert_metric_history  # the synthetic table's writer
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_override()])
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: written.append(kw) or upsert(**kw))

    def unreadable(*a, **kw):
        raise reader.SupabaseReadError("metric_history readback HTTP 503")

    monkeypatch.setattr(reader, "get_metric_history_at", unreadable, raising=False)
    assert agg.main() == 0
    overrides = json.loads(latest.read_text())["write_status"]["media_overrides"]
    assert any(kw.get("as_of") == date(2026, 3, 31) for kw in written)  # the override really was sent
    assert overrides["status"] == "failed"
    assert [(f["operation"], f["category"]) for f in overrides["failures"]] == [
        ("readback", wr.READBACK_UNAVAILABLE)]


def test_no_active_override_is_a_skip_not_a_success(run):
    latest, _ = run
    assert agg.main() == 0
    overrides = json.loads(latest.read_text())["write_status"]["media_overrides"]
    assert overrides["status"] == "skipped"


def test_database_writes_disabled_skips_every_stage_including_overrides(run, monkeypatch):
    latest, _ = run
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    assert agg.main() == 0
    status = json.loads(latest.read_text())["write_status"]
    assert {stage: receipt["status"] for stage, receipt in status.items()} == {
        "daily": "skipped", "monthly": "skipped", "media_overrides": "skipped"}


def test_a_held_day_receipt_that_breaks_the_contract_is_logged_not_written(tmp_path, monkeypatch, caplog):
    latest = tmp_path / "latest.json"
    sidecar = latest.with_suffix(".attempt.json")
    sidecar.write_text('{"older": true}')
    monkeypatch.setattr(agg, "LATEST_PATH", latest)
    agg._write_held_attempt_receipt({"daily": {"status": "skipped", "attempted_at": AT},
                                     "monthly": {**MONTHLY_SKIP, "status": "done"}})
    assert json.loads(sidecar.read_text()) == {"older": True}  # the older receipt is left as it was
    assert not sidecar.with_suffix(".json.tmp").exists()
    assert "not written (ValidationError" in caplog.text
