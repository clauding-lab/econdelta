"""Monthly persistence receipts name the exact leg, operation and reason.

R2 fix 8. The macro appender writes four independent chart series in one run
(CPI trio, remittance, imports, M2). Its receipt used to say only
"monthly source/read failure: <ExceptionType>" under one "macro" heading, and
every zero-row outcome read "no new accepted month" -- so a guard refusal, a
dead source page and a legitimately unpublished month all looked alike.

These tests drive the REAL appender, writer hook and readback against an
in-memory ``metric_history_monthly`` that behaves like PostgREST (newest-first
ordering, ``limit``, ``as_of=in.(...)``/``eq.``/``gte.`` filters), so they do
not depend on which reader function the receipt code chooses.
"""
from __future__ import annotations

import functools
from datetime import date, datetime

import pytest

import aggregate_latest as agg
import utils.supabase_reader as reader
import utils.supabase_writer as writer
from utils.write_receipts import monthly_attempt

TODAY = date(2026, 9, 15)
_REAL_YIELD_APPENDER = agg._write_yield_ladder_monthly_append  # before any fixture stubs it
_DAILY = {  # latest daily readings; CPI/M2 vintages are closed month-ends
    "general_inflation": (8.29, "2026-08-31"),
    "food_inflation": (7.60, "2026-08-31"),
    "non_food_inflation": (9.10, "2026-08-31"),
    "point_to_point_inflation": (8.50, "2026-08-31"),
    "m2_growth_yoy_pct": (8.10, "2026-07-31"),
}
_STATUSES = {"ok", "failed", "skipped"}


class FakeMonthlyTable:
    """A tiny PostgREST stand-in for metric_history_monthly."""

    def __init__(self, rows: list[dict]):
        self.rows = [dict(r) for r in rows]
        self.drift: dict[str, float] = {}  # metric_id -> value a concurrent writer stores

    def get(self, path: str, **_kw) -> list[dict]:
        table, _, query = path.partition("?")
        assert table == "metric_history_monthly", path
        params = dict(part.split("=", 1) for part in query.split("&"))
        mid = params["metric_id"].removeprefix("eq.")
        found = [dict(r) for r in self.rows if r["metric_id"] == mid]
        if "as_of" in params:
            op, _, arg = params["as_of"].partition(".")
            if op == "in":
                wanted = set(arg.strip("()").split(","))
                found = [r for r in found if r["as_of"] in wanted]
            elif op == "eq":
                found = [r for r in found if r["as_of"] == arg]
            elif op == "gte":
                found = [r for r in found if r["as_of"] >= arg]
            else:
                raise AssertionError(f"unsupported filter {params['as_of']}")
        if params.get("order") == "as_of.desc":
            found.sort(key=lambda r: r["as_of"], reverse=True)
        if "limit" in params:
            found = found[: int(params["limit"])]
        return found

    def upsert(self, table: str, rows: list[dict], on_conflict: str, **_kw) -> int:
        assert table == "metric_history_monthly"
        for row in rows:
            stored = dict(row)
            if row["metric_id"] in self.drift:
                stored["value"] = self.drift[row["metric_id"]]
            self.rows = [r for r in self.rows
                         if (r["metric_id"], r["as_of"]) != (row["metric_id"], row["as_of"])]
            self.rows.append(stored)
        return len(rows)

    def value(self, mid: str, as_of: str) -> float | None:
        return next((r["value"] for r in self.rows
                     if (r["metric_id"], r["as_of"]) == (mid, as_of)), None)


def _row(mid: str, as_of: str, value: float, source: str = "seed") -> dict:
    return {"metric_id": mid, "as_of": as_of, "value": value, "source": source,
            "source_as_of": as_of}


def _months(start: date, count: int) -> list[str]:
    out, y, m = [], start.year, start.month
    for _ in range(count):
        out.append(date(y, m, 1).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@pytest.fixture
def world(monkeypatch):
    """Real macro appender + real writer hook; only the network edges are fake."""
    table = FakeMonthlyTable([
        _row("m2_growth_yoy_monthly", "2026-07-01", 8.10),        # M2 vintage already recorded
        _row("imports_usd_mn_monthly", "2026-08-01", 6100.0),     # previous month recorded
    ])
    sent: list[tuple] = []
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr(reader, "_get", table.get)
    monkeypatch.setattr(reader, "get_metric_history", lambda mid, *, days, **kw: (
        [{"metric_id": mid, "value": _DAILY[mid][0], "as_of": _DAILY[mid][1]}] if mid in _DAILY else []))
    monkeypatch.setattr(writer, "_upsert_monthly_table", table.upsert)
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: sent.append(a))
    monkeypatch.setattr(agg, "_write_macro_monthly_append",
                        functools.partial(agg._write_macro_monthly_append, today=TODAY))
    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", lambda: 0)
    monkeypatch.setattr("utils.epb_monthly.write_exports_monthly", lambda: 0)
    monkeypatch.setattr(agg, "_fetch_remittance_html",
                        lambda: (_ for _ in ()).throw(RuntimeError("challenge page")))
    return table, sent


def _macro(result: dict) -> dict:
    return result["legs"]["macro"]


def _assert_brief_readable(receipt: dict) -> None:
    """Every level keeps the fields the Brief reader judges (status/attempted_at/reason)."""
    assert receipt["status"] in _STATUSES
    assert datetime.fromisoformat(receipt["attempted_at"]).utcoffset() is not None
    assert isinstance(receipt["reason"], str) and receipt["reason"]
    for child in receipt.get("legs", {}).values():
        _assert_brief_readable(child)


def test_remittance_source_failure_is_its_own_leg_and_does_not_erase_confirmed_cpi(world):
    table, _ = world
    result = agg._run_chart_feeding_monthly_appenders()
    macro = _macro(result)

    assert set(macro["legs"]) == {"cpi", "remittance", "imports", "m2"}
    remit = macro["legs"]["remittance"]
    assert remit["status"] == "failed"
    assert remit["failures"] == [{"operation": "read", "category": "source fetch failed",
                                  "detail": "BB wage-remittance page (RuntimeError)"}]
    cpi = macro["legs"]["cpi"]
    assert (cpi["status"], cpi["confirmed_rows"]) == ("ok", 3)
    assert table.value("cpi_12m_avg_monthly", "2026-08-01") == 8.29
    # Failure dominates, but the confirmed CPI rows are still counted at every level.
    assert (macro["status"], macro["confirmed_rows"]) == ("failed", 3)
    assert (result["status"], result["confirmed_rows"]) == ("failed", 3)
    assert "remittance: read failed (source fetch failed)" in macro["reason"]
    _assert_brief_readable(result)


def test_already_recorded_latest_period_is_no_new_official_period_not_a_guard(world):
    result = agg._run_chart_feeding_monthly_appenders()
    legs = _macro(result)["legs"]

    for leg, category in (("imports", "no new official period"),   # read off BB's own listing
                          ("m2", "no newer database vintage")):     # derived from our daily table
        assert legs[leg]["status"] == "skipped"
        assert [s["category"] for s in legs[leg]["skips"]] == [category]
    assert "2026-08-01" in legs["imports"]["skips"][0]["detail"]
    assert "2026-07" in legs["m2"]["skips"][0]["detail"]


def _daily_scrapers_stuck_at_july(table: FakeMonthlyTable, monkeypatch) -> None:
    """Every daily CPI/M2 vintage stopped at 31 Jul and July is already recorded; it is now
    20 Nov. Our table cannot tell BB's publication lag from a scraper dead for months."""
    stuck = {mid: (value, "2026-07-31") for mid, (value, _) in _DAILY.items()}
    table.rows = [_row(mid, "2026-07-01", stuck[daily][0])
                  for daily, mid in agg._CPI_DAILY_TO_MONTHLY.items()]
    table.rows += [_row("m2_growth_yoy_monthly", "2026-07-01", 8.10),
                   _row("remittance_usd_mn_monthly", "2026-10-01", 2400.0),
                   _row("imports_usd_mn_monthly", "2026-10-01", 6300.0)]
    monkeypatch.setattr(reader, "get_metric_history", lambda mid, *, days, **kw: (
        [{"metric_id": mid, "value": stuck[mid][0], "as_of": stuck[mid][1]}] if mid in stuck else []))
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", _raise(RuntimeError("must not be fetched")))
    monkeypatch.setattr(agg, "_write_macro_monthly_append", functools.partial(
        agg._write_macro_monthly_append.func, today=date(2026, 11, 20)))


@pytest.mark.parametrize("leg, metric_ids", [
    ("cpi", ("cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly")),
    ("m2", ("m2_growth_yoy_monthly",)),
])
def test_derived_leg_with_nothing_newer_in_our_database_never_claims_bb_has_not_published(
        world, monkeypatch, leg, metric_ids):
    """CPI and M2 are derived from our own daily table, never from BB's listing. "Nothing newer
    there" proves only that the database holds no newer vintage -- a scraper dead since July reads
    the same -- so it must not be labelled "no new official period", which fix 4 treats as
    healthy BB publication lag."""
    table, _ = world
    _daily_scrapers_stuck_at_july(table, monkeypatch)

    result = _leg(leg)

    assert (result["status"], result["failures"], result["confirmed_rows"]) == ("skipped", [], 0)
    assert [s["category"] for s in result["skips"]] == ["no newer database vintage"] * len(metric_ids)
    for metric_id, skip in zip(sorted(metric_ids), sorted(result["skips"], key=lambda s: s["detail"])):
        assert skip["detail"] == (f"{metric_id} 2026-07-01 already recorded "
                                  "(latest source vintage 2026-07-31)")


def test_guard_refusal_is_withheld_not_no_new_period(world, monkeypatch):
    daily = {**_DAILY, "m2_growth_yoy_pct": (55.0, "2026-08-31")}  # out of M2's range
    monkeypatch.setattr(reader, "get_metric_history", lambda mid, *, days, **kw: (
        [{"metric_id": mid, "value": daily[mid][0], "as_of": daily[mid][1]}] if mid in daily else []))
    m2 = _macro(agg._run_chart_feeding_monthly_appenders())["legs"]["m2"]

    assert m2["status"] == "skipped"
    assert [s["category"] for s in m2["skips"]] == ["withheld by validation"]
    assert "outside" in m2["skips"][0]["detail"]


def test_database_write_failure_is_charged_to_each_leg_in_the_batch(world, monkeypatch):
    table, sent = world

    def refuse(*a, **kw):
        raise writer.SupabaseWriteError("HTTP 503")

    monkeypatch.setattr(writer, "_upsert_monthly_table", refuse)
    macro = _macro(agg._run_chart_feeding_monthly_appenders())

    assert set(macro["legs"]) == {"cpi", "remittance", "imports", "m2"}
    assert macro["legs"]["cpi"]["failures"] == [{
        "operation": "write", "category": "database write failed",
        "detail": "3 row(s) for cpi_12m_avg_monthly, cpi_p2p_food_monthly, "
                  "cpi_p2p_nonfood_monthly (SupabaseWriteError)"}]
    assert macro["legs"]["imports"]["status"] == "skipped"
    assert macro["confirmed_rows"] == 0
    assert any("macro monthly append" in args[1] for args in sent)


def test_concurrent_overwrite_is_a_readback_mismatch_on_that_leg_only(world, monkeypatch):
    table, _ = world
    table.rows = [r for r in table.rows if r["metric_id"] != "m2_growth_yoy_monthly"]
    table.drift["cpi_p2p_food_monthly"] = 7.0
    legs = _macro(agg._run_chart_feeding_monthly_appenders())["legs"]

    assert legs["cpi"]["status"] == "failed"
    assert legs["cpi"]["confirmed_rows"] == 2
    assert [(f["operation"], f["category"]) for f in legs["cpi"]["failures"]] == [
        ("readback", "readback mismatch")]
    assert "cpi_p2p_food_monthly 2026-08-01" in legs["cpi"]["failures"][0]["detail"]
    assert (legs["m2"]["status"], legs["m2"]["confirmed_rows"]) == ("ok", 1)


def test_exact_readback_confirms_a_row_older_than_the_newest_36(monkeypatch):
    rows = [_row("cpi_12m_avg_monthly", m, 9.0) for m in _months(date(2023, 2, 1), 40)]
    table = FakeMonthlyTable(rows)
    monkeypatch.setattr(reader, "_get", table.get)
    monkeypatch.setattr(writer, "_upsert_monthly_table", table.upsert)
    old = _row("cpi_12m_avg_monthly", "2023-01-01", 8.57, source="bbs_official")

    with monthly_attempt() as receipt:
        writer.upsert_metric_history_monthly([old])

    assert receipt.result()["status"] == "ok"
    assert receipt.result()["confirmed_rows"] == 1


def test_remittance_append_only_holds_beyond_the_newest_36_rows(world, monkeypatch):
    table, _ = world
    table.rows = [_row("remittance_usd_mn_monthly", m, 2000.0, source="bb_backfill")
                  for m in _months(date(2026, 7, 1), 37)]  # 2026-07 .. 2029-07
    table.rows.append(_row("imports_usd_mn_monthly", "2029-08-01", 7000.0))
    parsed = [(date.fromisoformat(m), 2100.0) for m in _months(date(2026, 7, 1), 38)]
    monkeypatch.setattr(agg, "_write_macro_monthly_append",
                        functools.partial(agg._write_macro_monthly_append.func, today=date(2029, 9, 10)))
    monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: "<html/>")
    monkeypatch.setattr(agg, "parse_remittance_table", lambda html: parsed)
    monkeypatch.setattr(reader, "get_metric_history", lambda *a, **kw: [])

    result = agg._run_chart_feeding_monthly_appenders()

    assert table.value("remittance_usd_mn_monthly", "2026-07-01") == 2000.0  # backfill kept
    assert table.value("remittance_usd_mn_monthly", "2029-08-01") == 2100.0
    remit = _macro(result)["legs"]["remittance"]
    assert (remit["status"], remit["confirmed_rows"]) == ("ok", 1)


def test_disabled_database_writes_say_not_attempted(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    result = agg._run_chart_feeding_monthly_appenders()

    assert result["status"] == "skipped"
    for name in ("macro", "yield", "exports"):
        assert "not attempted: database writes disabled" in result["legs"][name]["reason"]


def test_yield_ladder_read_failure_names_the_table_and_operation(world, monkeypatch):
    def unreachable(*a, **kw):
        raise reader.SupabaseReadError("HTTP 500")

    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", functools.partial(
        _REAL_YIELD_APPENDER, today=TODAY))
    monkeypatch.setattr(reader, "get_auction_results_through", unreachable)
    ladder = agg._run_chart_feeding_monthly_appenders()["legs"]["yield"]

    assert ladder["status"] == "failed"
    assert ladder["failures"] == [{"operation": "read", "category": "database read failed",
                                   "detail": f"auction_results through {TODAY} (SupabaseReadError)"}]


def test_exact_readback_reader_queries_only_the_written_keys():
    from unittest.mock import MagicMock

    import requests

    sess = MagicMock(spec=requests.Session)
    sess.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=[]))
    reader.get_metric_history_monthly_at("cpi_12m_avg_monthly", ["2026-08-01", "2023-01-01"],
                                         url="https://example.supabase.co", key="k", session=sess)

    assert sess.get.call_args[0][0] == (
        "https://example.supabase.co/rest/v1/metric_history_monthly"
        "?metric_id=eq.cpi_12m_avg_monthly&as_of=in.(2023-01-01,2026-08-01)")
    with pytest.raises(ValueError):
        reader.get_metric_history_monthly_at("x", ["2026-08-01),or=(1"], url="u", key="k",
                                             session=sess)


def _reserves_leg_after_main(tmp_path, monkeypatch, split) -> dict:
    """Run the real main() with ``split`` as the reserves writer; return the snapshot's reserves leg."""
    import json

    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest = data_dir / "latest.json"
    for name, value in (("DATA_DIR", data_dir), ("LATEST_PATH", latest),
                        ("ARCHIVE_DIR", data_dir / "archive"), ("CONFIG_PATH", cfg_path)):
        monkeypatch.setattr(agg, name, value)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(writer, "upsert_metric_definitions_seed", lambda *a, **kw: 0)
    monkeypatch.setattr(writer, "upsert_metric_history", lambda **kw: 1)
    monkeypatch.setattr(writer, "verify_landed_count", lambda *a, **kw: True)
    monkeypatch.setattr(agg, "_apply_media_overrides", lambda *a: None)
    monkeypatch.setattr(agg, "_run_chart_feeding_monthly_appenders", lambda: {
        "status": "skipped", "attempted_at": "2026-09-25T00:00:00+00:00", "reason": "official lag"})
    monkeypatch.setattr(agg, "_write_reserves_monthly_split", split)

    assert agg.main() == 0
    return json.loads(latest.read_text())["write_status"]["monthly"]["legs"]["reserves"]


def _refuse_write(*a, **kw):
    raise writer.SupabaseWriteError("HTTP 503")


def test_reserves_write_failure_reaches_the_snapshot_as_a_reserves_write_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "_upsert_monthly_table", _refuse_write)
    reserves = _reserves_leg_after_main(tmp_path, monkeypatch, lambda reserves: writer.upsert_metric_history_monthly(
        [_row("reserves_gross_usd_bn_monthly", "2026-09-01", 31.2)]))

    assert reserves["status"] == "failed"
    assert reserves["failures"] == [{
        "operation": "write", "category": "database write failed",
        "detail": "1 row(s) for reserves_gross_usd_bn_monthly (SupabaseWriteError)"}]


# --- Review round 1: each labelling rule has a test that fails when the rule breaks. ---

_CPI_IDS = ("cpi_12m_avg_monthly", "cpi_p2p_food_monthly", "cpi_p2p_nonfood_monthly")


def _raise(exc: Exception):
    def boom(*a, **kw):
        raise exc
    return boom


def _leg(name: str) -> dict:
    return _macro(agg._run_chart_feeding_monthly_appenders())["legs"][name]


def _unreadable(table: FakeMonthlyTable, *metric_ids: str):
    """A metric_history_monthly GET that fails for ``metric_ids`` and serves every other id."""
    def get(path: str, **kw) -> list[dict]:
        if any(f"metric_id=eq.{mid}&" in f"{path}&" for mid in metric_ids):
            raise reader.SupabaseReadError("HTTP 500")
        return table.get(path, **kw)
    return get


def _daily_unreadable(*metric_ids: str):
    def get_metric_history(mid, *, days, **kw):
        if mid in metric_ids:
            raise reader.SupabaseReadError("HTTP 500")
        return [{"metric_id": mid, "value": _DAILY[mid][0], "as_of": _DAILY[mid][1]}] if mid in _DAILY else []
    return get_metric_history


def _read_failure(detail: str) -> dict:
    return {"operation": "read", "category": "database read failed", "detail": detail}


_DERIVE_FAILURE = {"operation": "derive", "category": "derivation failed",
                   "detail": "candidate derivation (ValueError)"}


@pytest.mark.parametrize("leg, fault, expected", [
    ("cpi", "daily", _read_failure("daily CPI observations (metric_history) (SupabaseReadError)")),
    ("cpi", "monthly", _read_failure("existing CPI trio rows (metric_history_monthly) (SupabaseReadError)")),
    ("cpi", "derive", _DERIVE_FAILURE),
    ("m2", "daily", _read_failure("daily m2_growth_yoy_pct observation (metric_history) (SupabaseReadError)")),
    ("m2", "monthly", _read_failure(
        "existing m2_growth_yoy_monthly rows (metric_history_monthly) (SupabaseReadError)")),
    ("m2", "derive", _DERIVE_FAILURE),
])
def test_cpi_and_m2_failures_name_the_step_and_table_that_failed(world, monkeypatch, leg, fault, expected):
    table, _ = world
    if fault == "daily":
        daily_id = "general_inflation" if leg == "cpi" else "m2_growth_yoy_pct"
        monkeypatch.setattr(reader, "get_metric_history", _daily_unreadable(daily_id))
    elif fault == "monthly":
        monkeypatch.setattr(reader, "_get", _unreadable(
            table, *(_CPI_IDS if leg == "cpi" else ("m2_growth_yoy_monthly",))))
    else:
        deriver = "_cpi_monthly_append_rows" if leg == "cpi" else "_m2_monthly_append_rows"
        monkeypatch.setattr(agg, deriver, _raise(ValueError("vintage arithmetic broke")))

    result = _leg(leg)

    assert result["status"] == "failed"
    assert result["failures"] == [expected]


@pytest.mark.parametrize("leg, metric_id", [("remittance", "remittance_usd_mn_monthly"),
                                            ("imports", "imports_usd_mn_monthly")])
def test_existing_rows_read_failure_is_a_database_read_failure_before_any_source_fetch(
        world, monkeypatch, leg, metric_id):
    table, _ = world
    monkeypatch.setattr(reader, "_get", _unreadable(table, metric_id))
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", _raise(RuntimeError("must not be fetched")))

    result = _leg(leg)  # the fixture's remittance fetch also raises if it is ever reached

    assert result["status"] == "failed"
    assert result["failures"] == [_read_failure(
        f"existing {metric_id} rows (metric_history_monthly) (SupabaseReadError)")]


@pytest.mark.parametrize("leg, step, category, detail", [
    ("remittance", "fetch", "source fetch failed", "BB wage-remittance page (RuntimeError)"),
    ("remittance", "parse", "source parse failed", "BB wage-remittance page (ValueError)"),
    ("imports", "fetch", "source fetch failed", "BB MEI PDF (RuntimeError)"),
    ("imports", "parse", "source parse failed", "BB MEI PDF (ValueError)"),
])
def test_source_fetch_and_parse_failures_are_told_apart(world, monkeypatch, leg, step, category, detail):
    table, _ = world
    fetch, parse = {"remittance": ("_fetch_remittance_html", "parse_remittance_table"),
                    "imports": ("_fetch_imports_mei_pdf", "parse_imports_c_and_f_table")}[leg]
    if leg == "imports":  # previous month not recorded yet, so the PDF is fetched
        table.rows = [r for r in table.rows if r["metric_id"] != "imports_usd_mn_monthly"]
    if step == "fetch":
        monkeypatch.setattr(agg, fetch, _raise(RuntimeError("challenge page")))
    else:
        monkeypatch.setattr(agg, fetch, lambda: "fetched")
        monkeypatch.setattr(agg, parse, _raise(ValueError("header changed")))

    result = _leg(leg)

    assert result["status"] == "failed"
    assert result["failures"] == [{"operation": "read", "category": category, "detail": detail}]


def test_parsed_source_listing_only_recorded_months_is_no_new_official_period(world, monkeypatch):
    table, _ = world
    table.rows = [_row("remittance_usd_mn_monthly", "2026-07-01", 2400.0),
                  _row("imports_usd_mn_monthly", "2026-06-01", 6000.0),
                  _row("imports_usd_mn_monthly", "2026-07-01", 6100.0),
                  _row("m2_growth_yoy_monthly", "2026-07-01", 8.10)]
    monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: "<html/>")
    monkeypatch.setattr(agg, "parse_remittance_table", lambda html: [
        (date(2026, 6, 1), 2300.0), (date(2026, 7, 1), 2400.0)])
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: "mei.pdf")
    monkeypatch.setattr(agg, "parse_imports_c_and_f_table", lambda path: (
        [(date(2026, 6, 1), 6000.0), (date(2026, 7, 1), 6100.0)], {}))

    legs = _macro(agg._run_chart_feeding_monthly_appenders())["legs"]

    assert legs["remittance"]["skips"] == [{
        "category": "no new official period",
        "detail": "BB wage-remittance page lists no month after those recorded (2026-08-01 not listed)",
        "newest_recorded": "2026-07-01", "lag_window_days": 45}]
    assert legs["imports"]["skips"] == [{
        "category": "no new official period",
        "detail": "BB MEI PDF lists no month after those recorded (2026-08-01 not listed)",
        "newest_recorded": "2026-07-01", "lag_window_days": 165}]
    for name in ("remittance", "imports"):
        assert (legs[name]["status"], legs[name]["failures"]) == ("skipped", [])


def test_imports_splice_refusal_is_withheld_by_validation_and_still_alerts(world, monkeypatch):
    table, sent = world
    table.rows = [_row("imports_usd_mn_monthly", "2026-07-01", 6100.0),
                  _row("m2_growth_yoy_monthly", "2026-07-01", 8.10)]
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: "mei.pdf")
    monkeypatch.setattr(agg, "parse_imports_c_and_f_table", lambda path: (
        [(date(2026, 7, 1), 5000.0), (date(2026, 8, 1), 5200.0)], {}))  # July 18% off the DB

    imports = _leg("imports")

    assert imports["status"] == "skipped"
    assert [s["category"] for s in imports["skips"]] == ["withheld by validation"]
    assert "splice check FAILED" in imports["skips"][0]["detail"]
    assert table.value("imports_usd_mn_monthly", "2026-08-01") is None
    assert ("error", "aggregate — macro monthly append: imports splice check failed") in [a[:2] for a in sent]


def _remittance_page_in_wrong_unit(table: FakeMonthlyTable, monkeypatch) -> None:
    table.rows.append(_row("remittance_usd_mn_monthly", "2026-07-01", 2400.0))
    monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: "<html/>")
    # BB's newest month arrives in BDT crore instead of USD mn: 25000 is outside the accepted range.
    monkeypatch.setattr(agg, "parse_remittance_table", lambda html: [
        (date(2026, 7, 1), 2400.0), (date(2026, 8, 1), 25000.0)])


def _mei_pdf_with_future_month(table: FakeMonthlyTable, monkeypatch) -> None:
    table.rows = [_row("imports_usd_mn_monthly", "2026-07-01", 6100.0),
                  _row("m2_growth_yoy_monthly", "2026-07-01", 8.10)]
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: "mei.pdf")
    # A corrupted fiscal-year label resolves the new month to August 2027, after today.
    monkeypatch.setattr(agg, "parse_imports_c_and_f_table", lambda path: (
        [(date(2026, 7, 1), 6100.0), (date(2027, 8, 1), 6200.0)], {}))


@pytest.mark.parametrize("leg, arrange, metric_id, refused_month, detail_fragment", [
    ("remittance", _remittance_page_in_wrong_unit, "remittance_usd_mn_monthly", "2026-08-01",
     "value 25000.0 outside"),
    ("imports", _mei_pdf_with_future_month, "imports_usd_mn_monthly", "2027-08-01",
     "is in the future"),
])
def test_source_value_refused_by_a_guard_is_withheld_not_publication_lag(
        world, monkeypatch, leg, arrange, metric_id, refused_month, detail_fragment):
    """A refused official value is a guard outcome. Calling it "no new official period" would let
    healthy-lag handling (fix 4) pass a bad BB value off as normal publication delay."""
    table, _ = world
    arrange(table, monkeypatch)

    result = _leg(leg)

    assert (result["status"], result["failures"]) == ("skipped", [])
    assert [s["category"] for s in result["skips"]] == ["withheld by validation"]
    assert refused_month in result["skips"][0]["detail"]
    assert detail_fragment in result["skips"][0]["detail"]
    assert table.value(metric_id, refused_month) is None


def test_remittance_previous_month_recorded_is_no_new_period_without_fetching(world):
    table, _ = world
    table.rows.append(_row("remittance_usd_mn_monthly", "2026-08-01", 2400.0))

    remit = _leg("remittance")  # the fixture's remittance fetch raises if it is ever called

    assert (remit["status"], remit["failures"]) == ("skipped", [])
    assert remit["skips"] == [{
        "category": "no new official period",
        "detail": "previous month 2026-08-01 already recorded; source not re-fetched",
        "newest_recorded": "2026-08-01", "lag_window_days": 45}]


def _remittance_page_lost_its_newest_year(table: FakeMonthlyTable, monkeypatch) -> None:
    table.rows = [_row("remittance_usd_mn_monthly", m, 2400.0) for m in _months(date(2026, 7, 1), 4)]
    table.rows += [_row("imports_usd_mn_monthly", "2026-11-01", 6000.0),
                   _row("m2_growth_yoy_monthly", "2026-07-01", 8.10)]
    monkeypatch.setattr(agg, "_write_macro_monthly_append", functools.partial(
        agg._write_macro_monthly_append.func, today=date(2026, 12, 20)))
    monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: "<html/>")
    # A layout change dropped the FY2026-27 block; only FY2025-26 (Jul 2025 - Jun 2026) still parses.
    monkeypatch.setattr(agg, "parse_remittance_table", lambda html: [
        (date.fromisoformat(m), 2300.0) for m in _months(date(2025, 7, 1), 12)])


def _mei_pdf_older_than_recorded(table: FakeMonthlyTable, monkeypatch) -> None:
    table.rows = [_row("imports_usd_mn_monthly", m, v)
                  for m, v in (("2026-06-01", 6000.0), ("2026-07-01", 6100.0), ("2026-08-01", 6200.0))]
    table.rows.append(_row("m2_growth_yoy_monthly", "2026-07-01", 8.10))
    monkeypatch.setattr(agg, "_write_macro_monthly_append", functools.partial(
        agg._write_macro_monthly_append.func, today=date(2026, 10, 15)))  # September not yet recorded
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: "mei.pdf")
    monkeypatch.setattr(agg, "parse_imports_c_and_f_table", lambda path: (
        [(date(2026, 6, 1), 6000.0), (date(2026, 7, 1), 6100.0)], {}))


@pytest.mark.parametrize("leg, arrange, detail", [
    ("remittance", _remittance_page_lost_its_newest_year,
     "BB wage-remittance page lists nothing at or after the newest recorded month 2026-10-01 "
     "(newest listed: 2026-06-01)"),
    ("imports", _mei_pdf_older_than_recorded,
     "BB MEI PDF lists nothing at or after the newest recorded month 2026-08-01 (newest listed: 2026-07-01)"),
])
def test_source_older_than_recorded_history_is_not_called_publication_lag(world, monkeypatch, leg, arrange,
                                                                          detail):
    table, _ = world
    arrange(table, monkeypatch)

    result = _leg(leg)

    assert result["status"] == "skipped"  # unchanged status; only the category is corrected
    assert result["skips"] == [{"category": "source older than recorded months", "detail": detail}]


def test_rows_reported_without_exact_readback_are_unconfirmed_not_ok(world, monkeypatch):
    monkeypatch.setattr("utils.epb_monthly.write_exports_monthly", lambda: 2)

    exports = agg._run_chart_feeding_monthly_appenders()["legs"]["exports"]

    assert exports["status"] == "failed"
    assert exports["failures"] == [{"operation": "readback", "category": "write unconfirmed",
                                    "detail": "writer reported 2 row(s) but no exact readback ran"}]


def test_reserves_definitions_write_failure_is_still_a_failed_reserves_leg(tmp_path, monkeypatch):
    # The split upserts metric definitions BEFORE its rows, so no row-level hook sees this failure.
    reserves = _reserves_leg_after_main(tmp_path, monkeypatch,
                                        _raise(writer.SupabaseWriteError("definitions HTTP 503")))

    assert reserves["status"] == "failed"
    assert reserves["failures"] == [{"operation": "unknown", "category": "unhandled exception",
                                     "detail": "SupabaseWriteError"}]


@pytest.mark.parametrize("leg, metric_id", [("remittance", "remittance_usd_mn_monthly"),
                                             ("imports", "imports_usd_mn_monthly")])
def test_publication_lag_names_the_newest_month_already_recorded_so_its_age_can_be_judged(
        world, leg, metric_id):
    """R2 fix 4: "no new official period" is healthy lag only while the newest recorded month is
    inside the Brief's monthly window, so the skip carries that month as data, not prose."""
    table, _ = world
    table.rows += [_row(metric_id, "2026-06-01", 6000.0),   # an older month first ...
                   _row(metric_id, "2026-08-01", 6100.0)]   # ... then the previous month
    table.rows = list({(r["metric_id"], r["as_of"]): r for r in table.rows}.values())

    result = _leg(leg)  # previous month (August) already recorded: the source is not re-fetched

    assert [(s["category"], s["newest_recorded"]) for s in result["skips"]] == [
        ("no new official period", "2026-08-01")]


def test_only_publication_lag_carries_a_newest_recorded_month(world):
    """Guard refusals and database vintages say nothing about BB's publication calendar."""
    legs = _macro(agg._run_chart_feeding_monthly_appenders())["legs"]

    assert all("newest_recorded" not in s and "lag_window_days" not in s for s in legs["m2"]["skips"])
    assert legs["m2"]["skips"][0]["category"] == "no newer database vintage"


@pytest.mark.parametrize("leg, metric_id, accepted_days", [
    ("remittance", "remittance_usd_mn_monthly", 45),   # sentinel "monthly" (the _monthly suffix)
    ("imports", "imports_usd_mn_monthly", 165),        # sentinel "quarterly": BB's ~2-month MEI lag
])
def test_publication_lag_states_the_accepted_lag_window_for_its_own_series(
        world, leg, metric_id, accepted_days):
    """R2 fix 4 round 1: the Brief reads lag as healthy only inside the window EconDelta itself
    accepts for that series (ruling E1: the sentinel vintage policy). Imports' normal ~2-month
    lag needs its 165-day window; a single monthly window would call every such lag stale."""
    from sentinel.cadence import GRACE_DAYS_BY_CADENCE, load_cadence_map, resolve_cadence
    table, _ = world
    table.rows = [r for r in table.rows if r["metric_id"] != metric_id] + [_row(metric_id, "2026-08-01", 6100.0)]

    result = _leg(leg)  # previous month (August) already recorded: the source is not re-fetched

    policy = GRACE_DAYS_BY_CADENCE[resolve_cadence(metric_id, load_cadence_map(), from_monthly_table=True)]
    assert [(s["category"], s["lag_window_days"]) for s in result["skips"]] == [
        ("no new official period", accepted_days)]
    assert policy == accepted_days

