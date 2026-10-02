"""R2 fix H2: an approved press override is confirmed by an exact readback.

Before H2 every run with an active approved override (production: NPL 32.78 @ 2026-06-30
via The Daily Star, media_review id 9) recorded `write_status.media_overrides` as failed /
"write unconfirmed", because override rows had no readback. The Brief then read
"degraded" every such day and could never say "ready".

Now the press rows the override step sent are read back by their exact
(metric_id, as_of) keys, all in ONE request. A row whose value, date and source equal
what was written confirms it (receipt ok, no alert). A different value or source, a
missing row, or a reader error is a real step failure: it is recorded with its own
reason category and goes into the grouped operator alert (owner decision k; controller
ruling 28 Sep: into `problems`, never the unconfirmed context list). No new alert
category and no hold.

All rows are synthetic test data (the NPL row mirrors the production row's shape and
figures). No database, model or Discord call is made.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

import aggregate_latest as agg
from tests import test_write_status_contract as contract
from utils import supabase_reader as reader
from utils import supabase_writer as sw
from utils import write_receipts as wr
from utils.schema import WriteStatus

run = contract.run
metric_history_table = contract.metric_history_table

TITLE = agg.MEDIA_OVERRIDE_ALERT_TITLE
PRESS_DAY = "2026-06-30"
# Synthetic replica of media_review id 9: BB's own series is still on the older FSR quarter.
DATA = {"gross_npl_ratio": 35.73}
AS_OF = {"gross_npl_ratio": date(2025, 9, 30)}
NPL_KEYS = [("banking_npl_pct", PRESS_DAY), ("gross_npl_ratio", PRESS_DAY)]

# The exact media_overrides receipt a confirmed NPL override produces, as serialised by
# WriteStatus (attempted_at excluded). The Brief's tests/test_readiness_write_status_contract.py
# (CONFIRMED_OVERRIDE) carries this same literal and must read it as "ready".
CONFIRMED_NPL_RECEIPT = {"status": "ok", "reason": "2 row(s) written and confirmed by exact readback",
                         "confirmed_rows": 2, "failures": []}
# ... and when the readback finds the banking_npl_pct row missing (the Brief's
# tests/test_producer_consumer_contract.py MISSING_NPL_OVERRIDE carries this literal).
MISSING_NPL_RECEIPT = {"status": "failed", "confirmed_rows": 1,
                       "reason": "readback failed (readback missing): banking_npl_pct 2026-06-30; "
                                 "1 row(s) written and confirmed by exact readback",
                       "failures": [{"operation": "readback", "category": "readback missing",
                                     "detail": "banking_npl_pct 2026-06-30"}]}


def _npl_row(**changes) -> dict:
    return {**contract._npl_override(PRESS_DAY), "press_value": 32.78, **changes}


def _other_row() -> dict:
    """A second synthetic approved override on a synthetic metric with no brief alias."""
    return {**contract._npl_override("2026-08-31"), "id": 12, "metric_id": "other_metric",
            "parsed_value": 9.1, "press_value": 9.45}


@pytest.fixture
def step(monkeypatch, metric_history_table):
    """The override step alone; returns the alerts notify() was asked to send."""
    sent: list[tuple] = []
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_row()])
    monkeypatch.setattr(sw, "set_media_review_status", lambda *a, **kw: None)
    return sent


def _alert_lines(sent: list[tuple]) -> list[str]:
    alerts = [a for a in sent if a[1] == TITLE]
    assert len(alerts) == 1, alerts  # ONE grouped alert per run
    assert alerts[0][0] == "error"
    return [line[2:] for line in alerts[0][2].splitlines() if line.startswith("- ")]


def _without_time(receipt: dict) -> dict:
    return {k: v for k, v in receipt.items() if k != "attempted_at"}


def _tamper_after_write(monkeypatch, table, key: tuple[str, str], **stored) -> None:
    """The write lands, then the stored row differs from what was sent (synthetic)."""
    upsert = table.upsert

    def writer(**kw):
        count = upsert(**kw)
        if key in table.rows:
            table.rows[key] = {**table.rows[key], **stored}
        return count

    monkeypatch.setattr(sw, "upsert_metric_history", writer)


# --- a confirmed override reads ok -------------------------------------------------------


def test_a_press_value_read_back_exactly_as_written_confirms_the_override(step, metric_history_table):
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert _without_time(receipt) == CONFIRMED_NPL_RECEIPT
    assert metric_history_table.rows[("gross_npl_ratio", PRESS_DAY)]["source"] == "media-approved:thedailystar"
    assert [a for a in step if a[1] == TITLE] == []  # a confirmed override never pages anyone


def test_the_confirmed_receipt_is_the_exact_contract_shape_the_brief_reads_as_ready(step):
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    stage = {"status": "ok", "attempted_at": "2026-09-26T21:00:00Z", "reason": "rows confirmed"}
    serialised = WriteStatus.model_validate(
        {"daily": stage, "monthly": stage, "media_overrides": receipt}).model_dump(mode="json")
    assert _without_time(serialised["media_overrides"]) == CONFIRMED_NPL_RECEIPT


def test_every_override_key_is_read_back_in_one_request(step, monkeypatch, metric_history_table):
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_row(), _other_row()])
    receipt = agg._media_override_receipt({**DATA, "other_metric": 9.1},
                                          {**AS_OF, "other_metric": date(2026, 7, 31)})
    assert len(metric_history_table.reads) == 1  # batched: no per-row loop of requests
    assert sorted(metric_history_table.reads[0]) == sorted([*NPL_KEYS, ("other_metric", "2026-08-31")])
    assert (receipt["status"], receipt["confirmed_rows"]) == ("ok", 3)


def test_an_approved_override_confirmed_in_the_real_run_reads_ok_in_latest_json(run, monkeypatch):
    latest, sent = run
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_row()])
    assert agg.main() == 0
    status = json.loads(latest.read_text())["write_status"]
    assert status["daily"]["status"] == "ok"
    assert _without_time(status["media_overrides"]) == CONFIRMED_NPL_RECEIPT
    assert [a for a in sent if a[1] == TITLE] == []


# --- a readback that disproves or cannot read the value is a step failure that alerts -----


@pytest.mark.parametrize("stored", [{"value": 32.77}, {"source": "EconDelta"}],
                         ids=["different value", "a later normal upsert re-labelled the row"])
def test_a_readback_that_differs_from_the_sent_press_value_fails_and_alerts(
        step, monkeypatch, metric_history_table, stored):
    _tamper_after_write(monkeypatch, metric_history_table, ("banking_npl_pct", PRESS_DAY), **stored)
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert receipt["status"] == "failed"
    assert receipt["failures"] == [{"operation": "readback", "category": wr.READBACK_MISMATCH,
                                    "detail": f"banking_npl_pct {PRESS_DAY}"}]
    assert receipt["confirmed_rows"] == 1  # the gross_npl_ratio row itself did read back
    assert _alert_lines(step) == [f"gross_npl_ratio: press value sent; readback mismatch (banking_npl_pct {PRESS_DAY})"]


def test_a_row_the_reader_returns_for_another_day_does_not_confirm_the_press_day(
        step, monkeypatch, metric_history_table):
    _tamper_after_write(monkeypatch, metric_history_table, ("banking_npl_pct", PRESS_DAY), as_of="2026-03-31")
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert _without_time(receipt) == MISSING_NPL_RECEIPT
    assert _alert_lines(step) == [f"gross_npl_ratio: press value sent; readback missing (banking_npl_pct {PRESS_DAY})"]


def test_a_press_row_missing_on_readback_fails_and_alerts(step, monkeypatch):
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: 2)  # a 2xx that stored nothing
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert receipt["status"] == "failed"
    assert receipt["failures"] == [
        {"operation": "readback", "category": wr.READBACK_MISSING, "detail": f"banking_npl_pct {PRESS_DAY}"},
        {"operation": "readback", "category": wr.READBACK_MISSING, "detail": f"gross_npl_ratio {PRESS_DAY}"}]
    assert receipt["confirmed_rows"] == 0
    assert _alert_lines(step) == [f"gross_npl_ratio: press value sent; readback missing "
                                  f"(banking_npl_pct {PRESS_DAY}, gross_npl_ratio {PRESS_DAY})"]


def test_a_readback_that_cannot_be_read_fails_and_alerts_with_the_exception_type_only(step, monkeypatch):
    secret = "https://example.invalid/rest/v1/metric_history?apikey=SYNTHETIC-KEY"

    def unreadable(*a, **kw):
        raise reader.SupabaseReadError(f"GET returned HTTP 503 from {secret}")

    monkeypatch.setattr(reader, "get_metric_history_at", unreadable)
    receipt = agg._media_override_receipt(dict(DATA), dict(AS_OF))
    assert receipt["status"] == "failed"
    assert receipt["failures"] == [{"operation": "readback", "category": wr.READBACK_UNAVAILABLE,
                                    "detail": "banking_npl_pct, gross_npl_ratio (SupabaseReadError)"}]
    assert receipt["confirmed_rows"] == 0
    lines = _alert_lines(step)
    assert lines == ["gross_npl_ratio: press value sent; readback failed (SupabaseReadError)"]
    assert "SYNTHETIC-KEY" not in json.dumps([receipt, step]) and "example.invalid" not in json.dumps(step)


def test_only_the_disproved_override_is_named_when_another_is_confirmed(step, monkeypatch, metric_history_table):
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_other_row(), _npl_row()])
    _tamper_after_write(monkeypatch, metric_history_table, ("other_metric", "2026-08-31"), value=9.44)
    receipt = agg._media_override_receipt({**DATA, "other_metric": 9.1},
                                          {**AS_OF, "other_metric": date(2026, 7, 31)})
    assert (receipt["status"], receipt["confirmed_rows"]) == ("failed", 2)
    assert _alert_lines(step) == ["other_metric: press value sent; readback mismatch (other_metric 2026-08-31)"]


def test_a_readback_problem_is_a_failure_in_the_real_run_and_the_brief_contract_still_validates(run, monkeypatch):
    latest, sent = run
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [_npl_row()])
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: 2)  # nothing lands anywhere
    assert agg.main() == 0
    overrides = json.loads(latest.read_text())["write_status"]["media_overrides"]
    assert overrides["status"] == "failed"
    assert {f["category"] for f in overrides["failures"]} == {wr.READBACK_MISSING}
    assert len([a for a in sent if a[1] == TITLE]) == 1


# --- the exact-key daily reader (utils.supabase_reader) -----------------------------------


class _Resp:
    def __init__(self, status_code: int, rows: list[dict]) -> None:
        self.status_code, self._rows, self.text = status_code, rows, json.dumps(rows)

    def json(self) -> list[dict]:
        return self._rows


class _Session:
    """Records every GET; answers with the synthetic rows it was given."""

    def __init__(self, status_code: int = 200, rows: list[dict] | None = None) -> None:
        self.status_code, self.rows, self.urls = status_code, rows or [], []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return _Resp(self.status_code, self.rows)


CREDS = {"url": "https://synthetic.invalid", "key": "synthetic-key"}


def test_the_daily_reader_asks_once_for_every_key_and_returns_only_the_exact_pairs():
    stored = [{"metric_id": "gross_npl_ratio", "as_of": PRESS_DAY, "value": 32.78, "source": "s"},
              {"metric_id": "banking_npl_pct", "as_of": PRESS_DAY, "value": 32.78, "source": "s"},
              # the in.() x in.() filter can also return this pair, which nobody asked for
              {"metric_id": "gross_npl_ratio", "as_of": "2026-08-31", "value": 35.73, "source": "s"},
              {"metric_id": "other_metric", "as_of": "2026-08-31", "value": 9.45, "source": "s"}]
    session = _Session(rows=stored)
    rows = reader.get_metric_history_at([*NPL_KEYS, ("other_metric", "2026-08-31")], session=session, **CREDS)
    assert len(session.urls) == 1
    url = session.urls[0]
    assert url.startswith("https://synthetic.invalid/rest/v1/metric_history?")
    assert "metric_id=in.(banking_npl_pct,gross_npl_ratio,other_metric)" in url
    assert "as_of=in.(2026-06-30,2026-08-31)" in url
    assert sorted((r["metric_id"], r["as_of"]) for r in rows) == sorted(
        [*NPL_KEYS, ("other_metric", "2026-08-31")])


def test_the_daily_reader_sends_nothing_when_there_is_nothing_to_read():
    session = _Session()
    assert reader.get_metric_history_at([], session=session, **CREDS) == []
    assert session.urls == []


@pytest.mark.parametrize("keys", [[("gross_npl_ratio", "30-06-2026")], [("gross_npl_ratio,x", PRESS_DAY)],
                                  [("gross_npl_ratio)", PRESS_DAY)]],
                         ids=["date not ISO", "comma in id", "parenthesis in id"])
def test_the_daily_reader_refuses_a_key_it_cannot_ask_for_exactly_before_any_request(keys):
    session = _Session()
    with pytest.raises(ValueError):
        reader.get_metric_history_at(keys, session=session, **CREDS)
    assert session.urls == []


def test_the_daily_reader_raises_on_a_failed_request():
    with pytest.raises(reader.SupabaseReadError):
        reader.get_metric_history_at(NPL_KEYS, session=_Session(status_code=503), **CREDS)
