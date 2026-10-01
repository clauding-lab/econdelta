"""Unit tests for the DSE DS30 day-end-close backfill.

Two layers:
  1. Synthetic-JSON tests that pin the parsing rules (closep not other price
     columns, window filtering, code filter, range guard, pagination).
  2. Real-source fixture tests against captured www.dse.com.bd/api/live JSON
     (2026-10-01) so the parser is verified against the actual response shape.
     ``dse_api_day_end_bracbank.json`` deliberately covers the SAME window as
     the retired 2026-05-30 dsebd.org HTML capture, so the known data point
     (2026-05-24 close = 67.3) carries over and proves old/new parity.

No network and no Supabase writes here — fixtures are static files.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import scripts.backfill_dse_dayend as bd
from scripts.backfill_dse_dayend import (
    BackfillError,
    CloseRow,
    fetch_scrip_closes,
    group_rows_by_date,
    parse_day_end_archive,
    parse_ds30_codes,
    rows_to_supabase_payload,
)
from utils.http_client import HttpClient
from utils.supabase_writer import SupabaseWriteError

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- #
# Synthetic-JSON parsing rules
# --------------------------------------------------------------------------- #


def _row(d: str, code: str = "BRACBANK", **over) -> dict:
    base = {
        "serial": 1, "date": d, "tradingCode": code, "ltp": 67.3, "high": 68.1,
        "low": 66.8, "openp": 67, "closep": 67.3, "ycp": 66.6, "trade": 3307,
        "value": 186.274, "volume": 2761602,
    }
    base.update(over)
    return base


def _page(rows: list[dict], total: int | None = None, page: int = 1) -> dict:
    return {"rows": rows, "total": len(rows) if total is None else total,
            "page": page, "pageSize": 500}


_MIN_ARCHIVE = _page([
    _row("2026-05-24"),
    # CLOSEP=66.6 while LTP=66.5, OPENP=64.1, YCP=64 -> must keep 66.6
    _row("2026-05-23", ltp=66.5, openp=64.1, closep=66.6, ycp=64),
])


def test_parse_day_end_archive_keeps_closep_not_other_price_columns():
    rows = parse_day_end_archive(_MIN_ARCHIVE, expected_code="BRACBANK")
    by_date = {r.as_of: r.closep for r in rows}
    assert by_date[date(2026, 5, 23)] == 66.6
    assert by_date[date(2026, 5, 24)] == 67.3


def test_parse_day_end_archive_accepts_raw_json_text():
    rows = parse_day_end_archive(json.dumps(_MIN_ARCHIVE), expected_code="BRACBANK")
    assert len(rows) == 2


def test_parse_day_end_archive_sorts_ascending_by_date():
    """The endpoint returns newest-first; CloseRows come back ascending."""
    rows = parse_day_end_archive(_MIN_ARCHIVE, expected_code="BRACBANK")
    assert [r.as_of for r in rows] == [date(2026, 5, 23), date(2026, 5, 24)]


def test_parse_day_end_archive_filters_unexpected_code():
    payload = _page([_row("2026-05-24", code="GP"), _row("2026-05-23")])
    rows = parse_day_end_archive(payload, expected_code="BRACBANK")
    assert [r.code for r in rows] == ["BRACBANK"]


def test_parse_day_end_archive_drops_rows_outside_window():
    """Live quirk (2026-10-01): asking for 2024-09-01..2024-09-05 -- before the
    archive's ~2-year horizon -- returns the 2024-10-02 row. A row outside the
    caller's window must never be written under its date range."""
    payload = _page([_row("2024-10-02", closep=341.1, code="GP")])
    rows = parse_day_end_archive(
        payload, expected_code="GP", start=date(2024, 9, 1), end=date(2024, 9, 5)
    )
    assert rows == []


def test_parse_day_end_archive_skips_bad_and_out_of_range_rows():
    payload = _page([
        _row("2026-05-24", closep=0),        # zero close (untraded/bad) -> skipped
        _row("2026-05-23", closep=None),     # missing close -> skipped
        _row("not-a-date"),                  # bad date -> skipped
        _row("2026-05-22", closep=1234.5),   # valid high-priced close kept
    ])
    rows = parse_day_end_archive(payload, expected_code="BRACBANK")
    assert [(r.as_of, r.closep) for r in rows] == [(date(2026, 5, 22), 1234.5)]


@pytest.mark.parametrize("body", ["<html>Gone</html>", "[]", json.dumps({"error": "x"})])
def test_parse_day_end_archive_raises_on_wrong_shape(body):
    with pytest.raises(BackfillError):
        parse_day_end_archive(body)


def test_parse_day_end_archive_empty_rows_is_empty_not_error():
    """Unknown/untraded code -> {"rows": [], "total": 0}: nothing, not a crash."""
    assert parse_day_end_archive(_page([]), expected_code="NOPE") == []


class _FakeClient:
    """Serves canned archive pages keyed by the `page` query param."""

    def __init__(self, pages: dict[int, dict]):
        self.pages = pages
        self.calls: list[dict] = []

    def fetch_html(self, url, params=None, **kw):
        self.calls.append({"url": url, **(params or {})})
        return json.dumps(self.pages[params["page"]])


def test_fetch_scrip_closes_follows_pagination_until_total():
    p1 = _page([_row("2026-05-24"), _row("2026-05-23")], total=3, page=1)
    p2 = _page([_row("2026-05-22")], total=3, page=2)
    client = _FakeClient({1: p1, 2: p2})
    rows = fetch_scrip_closes(client, "BRACBANK", date(2026, 5, 1), date(2026, 5, 31))
    assert [r.as_of.day for r in rows] == [22, 23, 24]
    assert [c["page"] for c in client.calls] == [1, 2]
    assert client.calls[0] == {
        "url": bd.ARCHIVE_URL, "from": "2026-05-01", "to": "2026-05-31",
        "inst": "BRACBANK", "page": 1,
    }


def test_fetch_scrip_closes_single_page_makes_one_request():
    client = _FakeClient({1: _MIN_ARCHIVE})
    fetch_scrip_closes(client, "BRACBANK", date(2026, 5, 1), date(2026, 5, 31))
    assert len(client.calls) == 1


def test_fetch_scrip_closes_bounded_if_total_never_reached(monkeypatch):
    monkeypatch.setattr(bd, "_MAX_ARCHIVE_PAGES", 3)
    page = _page([_row("2026-05-24")], total=10_000)
    client = _FakeClient({1: page, 2: page, 3: page})
    with pytest.raises(BackfillError, match="still paging"):
        fetch_scrip_closes(client, "BRACBANK", date(2026, 5, 1), date(2026, 5, 31))


def test_parse_ds30_codes_dedupes_and_rejects_empty():
    assert parse_ds30_codes({"rows": [{"code": "GP"}, {"code": "gp"}, {"code": "BSC"}]}) == [
        "GP", "BSC",
    ]
    with pytest.raises(BackfillError):
        parse_ds30_codes({"rows": []})
    with pytest.raises(BackfillError):
        parse_ds30_codes("<html>Gone</html>")


def test_metric_id_uses_dse_close_prefix():
    row = CloseRow(code="BRACBANK", as_of=date(2026, 5, 24), closep=67.3)
    assert row.metric_id == "dse_close_BRACBANK"


def test_rows_to_supabase_payload_builds_data_and_as_of_map():
    rows = [
        CloseRow("BRACBANK", date(2026, 5, 24), 67.3),
        CloseRow("GP", date(2026, 5, 24), 330.1),
    ]
    data, as_of_map = rows_to_supabase_payload(rows)
    assert data == {"dse_close_BRACBANK": 67.3, "dse_close_GP": 330.1}
    assert as_of_map == {
        "dse_close_BRACBANK": date(2026, 5, 24),
        "dse_close_GP": date(2026, 5, 24),
    }


def test_group_rows_by_date_partitions_for_per_day_upsert():
    rows = [
        CloseRow("BRACBANK", date(2026, 5, 24), 67.3),
        CloseRow("GP", date(2026, 5, 24), 330.1),
        CloseRow("BRACBANK", date(2026, 5, 23), 66.6),
    ]
    grouped = group_rows_by_date(rows)
    assert set(grouped) == {date(2026, 5, 23), date(2026, 5, 24)}
    assert len(grouped[date(2026, 5, 24)]) == 2
    assert len(grouped[date(2026, 5, 23)]) == 1


# --------------------------------------------------------------------------- #
# Real-source fixture tests (captured live JSON, 2026-10-01)
# --------------------------------------------------------------------------- #


def test_parse_ds30_codes_returns_exactly_30_from_live_response():
    codes = parse_ds30_codes((FIXTURES / "dse_api_ds30_constituents.json").read_text())
    assert len(codes) == 30
    assert "BRACBANK" in codes
    assert "GP" in codes
    assert all(c.isalnum() and c.isupper() for c in codes)


def test_parse_bracbank_archive_fixture_matches_old_site_capture():
    """Same window as the retired dsebd.org capture of 2026-05-30."""
    text = (FIXTURES / "dse_api_day_end_bracbank.json").read_text()
    rows = parse_day_end_archive(text, expected_code="BRACBANK")
    assert len(rows) == 39
    assert all(r.code == "BRACBANK" for r in rows)
    assert all(0 < r.closep < 1000 for r in rows)
    by_date = {r.as_of: r.closep for r in rows}
    # Known data point from the old-site capture: 2026-05-24 close = 67.3
    assert by_date[date(2026, 5, 24)] == 67.3


def test_parse_gp_archive_fixture_recent_window():
    text = (FIXTURES / "dse_api_day_end_gp.json").read_text()
    rows = parse_day_end_archive(text, expected_code="GP")
    assert len(rows) == 10
    assert all(r.metric_id == "dse_close_GP" for r in rows)
    assert rows[-1].as_of == date(2026, 10, 1)
    assert rows[-1].closep == 241.1
    # Spans the old->new site switchover (24-28 Sep) with no gap on trading days.
    assert date(2026, 9, 24) in {r.as_of for r in rows}
    assert date(2026, 9, 27) in {r.as_of for r in rows}


# --------------------------------------------------------------------------- #
# E1.6 — failure alerting on the daily production path (notify_on_failure)
#
# dse_dayend was the ONLY scraper without a notifier import, which is how the
# DSE feed froze unnoticed for 24 days. run_backfill(notify_on_failure=True)
# (set by scrapers.dse_dayend) must fire a Discord error alert on total fetch
# failure, a below-floor partial, or a Supabase write error — while manual
# backfills (the default, notify_on_failure=False) stay quiet.
# --------------------------------------------------------------------------- #


def _close_row(code: str, closep: float = 100.0) -> CloseRow:
    return CloseRow(code=code, as_of=date(2026, 7, 1), closep=closep)


@pytest.fixture(autouse=False)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bd.time, "sleep", lambda *a, **k: None)


def test_all_fetch_fail_alerts_error_and_returns_1(monkeypatch, _no_sleep):
    """Every ticker fetch failing must BOTH return exit 1 AND fire an error alert
    (the missing signal behind the 24-day silent DSE freeze — E1.6)."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bd, "notify", lambda level, title, msg, *a, **k: calls.append((level, title)))

    def _boom(client, code, start, end):
        raise HttpClient.FetchError(bd.ARCHIVE_URL, None, "TLS chain broken")

    monkeypatch.setattr(bd, "fetch_scrip_closes", _boom)

    rc = bd.run_backfill(
        start=date(2026, 6, 11), end=date(2026, 7, 9),
        dry_run=False, sample_only=False,
        codes_override=["BRACBANK", "GP"], notify_on_failure=True,
    )
    assert rc == 1
    assert any(level == "error" for level, _ in calls), "expected an error notify on total fetch failure"


def test_all_fetch_fail_stays_quiet_when_notify_disabled(monkeypatch, _no_sleep):
    """Manual backfills / dry-runs (notify_on_failure=False default) must NOT
    alert — only the daily production path opts in."""
    calls: list = []
    monkeypatch.setattr(bd, "notify", lambda *a, **k: calls.append(a))

    def _boom(client, code, start, end):
        raise HttpClient.FetchError("x", None, "y")

    monkeypatch.setattr(bd, "fetch_scrip_closes", _boom)

    rc = bd.run_backfill(
        start=date(2026, 6, 11), end=date(2026, 7, 9),
        dry_run=False, sample_only=False, codes_override=["BRACBANK"],
    )
    assert rc == 1
    assert calls == []  # a manual run must not page anyone


def test_supabase_write_error_alerts_and_reraises(monkeypatch, _no_sleep):
    """A write failure mid-upsert must alert AND propagate (systemd records a
    fail), never be swallowed."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bd, "notify", lambda level, title, msg, *a, **k: calls.append((level, title)))
    monkeypatch.setattr(bd, "fetch_scrip_closes", lambda client, code, s, e: [_close_row(code)])

    import utils.supabase_writer as sw

    def _raise(**kwargs):
        raise SupabaseWriteError("PostgREST 500")

    monkeypatch.setattr(sw, "upsert_metric_history", _raise)

    with pytest.raises(SupabaseWriteError):
        bd.run_backfill(
            start=date(2026, 7, 1), end=date(2026, 7, 1),
            dry_run=False, sample_only=False,
            codes_override=["BRACBANK"], notify_on_failure=True,
        )
    assert any(level == "error" for level, _ in calls), "expected an error notify before re-raise"


def test_below_floor_full_run_alerts_but_still_writes(monkeypatch, _no_sleep):
    """A full DS30 run that lands fewer than the ticker floor must alert but still
    write the partial set (return 0) — partial data beats no data."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bd, "notify", lambda level, title, msg, *a, **k: calls.append((level, title)))

    codes = [f"T{i:02d}" for i in range(30)]
    monkeypatch.setattr(bd, "fetch_ds30_codes", lambda client: codes)

    ok = set(codes[:10])  # only 10/30 succeed -> below the 25 floor

    def _fetch(client, code, s, e):
        if code in ok:
            return [_close_row(code)]
        raise HttpClient.FetchError("x", None, "miss")

    monkeypatch.setattr(bd, "fetch_scrip_closes", _fetch)

    import utils.supabase_writer as sw
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: len(kw["data"]))

    rc = bd.run_backfill(
        start=date(2026, 7, 1), end=date(2026, 7, 1),
        dry_run=False, sample_only=False, notify_on_failure=True,
    )
    assert rc == 0  # partial set still written
    assert any(level == "error" and "floor" in title for level, title in calls)


def test_ds30_list_fetch_failure_alerts_and_propagates(monkeypatch, _no_sleep):
    """The DS30 constituent-list fetch runs BEFORE the per-scrip loop — a failure
    there (TLS break, block, page reshape) must fire the error alert too, not
    escape the alerting entirely (review HIGH on PR #75)."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(bd, "notify", lambda level, title, msg, *a, **k: calls.append((level, title)))

    def _boom(client):
        raise HttpClient.FetchError(bd.DS30_URL, None, "TLS chain broken")

    monkeypatch.setattr(bd, "fetch_ds30_codes", _boom)

    with pytest.raises(HttpClient.FetchError):
        bd.run_backfill(
            start=date(2026, 6, 11), end=date(2026, 7, 9),
            dry_run=False, sample_only=False, notify_on_failure=True,
        )
    assert any(level == "error" and "DS30 list" in title for level, title in calls), (
        "expected an error notify when the DS30 list fetch fails"
    )


def test_ds30_list_fetch_failure_stays_quiet_when_notify_disabled(monkeypatch, _no_sleep):
    """Manual backfills (default) must not page on a list-fetch failure either —
    the exception still propagates for the CLI user to see."""
    calls: list = []
    monkeypatch.setattr(bd, "notify", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(bd, "fetch_ds30_codes",
                        lambda client: (_ for _ in ()).throw(bd.BackfillError("no table")))

    with pytest.raises(BackfillError):
        bd.run_backfill(
            start=date(2026, 6, 11), end=date(2026, 7, 9),
            dry_run=False, sample_only=False,
        )
    assert calls == []
