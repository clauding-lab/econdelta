"""R2 fix H4: the yield ladder, EPB exports and reserves writers say WHY they wrote nothing.

Until H4 each of these three writers recorded a zero-row run as "no rows written (reason not
itemised by this writer)". Unknown is never healthy, so The Brief read every quiet day as
degraded. Each zero-row outcome now names its category (utils/write_receipts.py):

- "no new official period": the source lists nothing newer than what is recorded, stated with
  the newest recorded month and the series' accepted lag window (R2 fix 4), so the Brief can
  tell normal publication lag from a stuck listing;
- "withheld by validation": a guard refused the write, quoting the guard;
- "source older than recorded months": the source went backwards behind a recorded month;
- "not attempted": the writer had nothing it could write from.

What gets written is unchanged; only the receipts say more. These tests drive the REAL writers
and the real exact-key readback against an in-memory metric_history_monthly (the fix-8
FakeMonthlyTable); only the database and EPB edges are fake. All values are SYNTHETIC except
the EPB July/August 2026 goods totals (shared contract monthly_contract.epb_evidence).
"""
from __future__ import annotations

import functools
import json
from datetime import date
from pathlib import Path

import pytest

import aggregate_latest as agg
import utils.epb_monthly as epb
import utils.supabase_reader as reader
import utils.supabase_writer as writer
import utils.write_receipts as wr
from tests.test_monthly_leg_receipts import FakeMonthlyTable
from utils.schema import ForexReserves, WriteReceipt
from utils.write_receipts import monthly_attempt

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "tests/fixtures/contracts/brief-observations-v1.json")
                      .read_text())["monthly_contract"]
QUIET = CONTRACT["quiet_day_legs"]
TODAY = date.fromisoformat(QUIET["run_date_utc"])  # UTC date of the 25 Sep 02:55 BDT run
TENORS = list(agg._YIELD_TENOR_TO_MONTHLY_ID)
NOT_ITEMISED = "reason not itemised"


# --- the database edge -------------------------------------------------------------------


def _auction(tenor: str, day: str, cutoff: float) -> dict:
    return {"auction_date": day, "tenor": tenor, "cutoff": cutoff}


def _ladder_listing() -> list[dict]:
    """SYNTHETIC auction_results: every tenor auctioned late August, and again 21 September."""
    return ([_auction(t, "2026-08-25", 9.00 + i * 0.30) for i, t in enumerate(TENORS)]
            + [_auction(t, "2026-09-21", 9.05 + i * 0.30) for i, t in enumerate(TENORS)])


def _rungs_matching(listing: list[dict]) -> list[dict]:
    """The Aug and Sep rungs a caught-up ladder already holds: each tenor's newest auction
    in that month, dated by its real auction day (landmines 51/54)."""
    rows = []
    for month in ("2026-08", "2026-09"):
        for tenor, mid in agg._YIELD_TENOR_TO_MONTHLY_ID.items():
            newest = max((a for a in listing if a["tenor"] == tenor and a["auction_date"][:7] == month),
                         key=lambda a: a["auction_date"])
            rows.append({"metric_id": mid, "as_of": f"{month}-01", "value": newest["cutoff"],
                         "source": agg._YIELD_LADDER_SOURCE, "source_as_of": newest["auction_date"]})
    return rows


@pytest.fixture
def edges(monkeypatch):
    """A fake monthly table behind the real reader/writer; the network is never reached."""
    tables: dict[str, FakeMonthlyTable] = {}

    def use(rows: list[dict]) -> FakeMonthlyTable:
        tables["t"] = FakeMonthlyTable(rows)
        return tables["t"]

    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr(reader, "_get", lambda path, **kw: tables["t"].get(path, **kw))
    monkeypatch.setattr(writer, "_upsert_monthly_table",
                        lambda table, rows, conflict, **kw: tables["t"].upsert(table, rows, conflict))
    monkeypatch.setattr(writer, "upsert_metric_definitions_monthly", lambda defs, **kw: len(defs))
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: None)
    return use


def _run(name: str, write) -> tuple[int, dict]:
    """One writer through production's own containment (_run_monthly_writer)."""
    counts: list[int] = []
    with monthly_attempt(name) as receipt:
        agg._run_monthly_writer(name, lambda: counts.append(write()) or counts[-1], receipt)
    return counts[0] if counts else 0, receipt.result()


def _published(leg: dict) -> dict:
    """The leg as latest.json carries it, with the attempt time normalised to the contract's."""
    return WriteReceipt.model_validate({**leg, "attempted_at": QUIET["attempted_at"]}).model_dump(mode="json")


def _categories(leg: dict) -> list[str]:
    return [s["category"] for s in leg["skips"]]


# --- yield ladder --------------------------------------------------------------------------


def _yield(edges, monkeypatch, listing: list[dict], stored: list[dict], today: date = TODAY):
    table = edges(stored)
    monkeypatch.setattr(reader, "get_auction_results_through", lambda as_of, **kw: sorted(
        (a for a in listing if a["auction_date"] <= as_of.isoformat()),
        key=lambda a: a["auction_date"], reverse=True))
    before = [dict(r) for r in table.rows]
    count, leg = _run("yield", functools.partial(agg._write_yield_ladder_monthly_append, today=today))
    return count, leg, before, table


def test_a_caught_up_yield_ladder_is_official_lag_with_its_newest_month_and_window(edges, monkeypatch):
    listing = _ladder_listing()
    count, leg, before, table = _yield(edges, monkeypatch, listing, _rungs_matching(listing))

    assert (count, table.rows) == (0, before)  # nothing written, exactly as before H4
    assert _published(leg) == QUIET["legs"]["yield"]
    assert leg["skips"][0]["lag_window_days"] == agg._accepted_lag_days("tbill_91d_yield_monthly")
    assert NOT_ITEMISED not in leg["reason"]


def test_before_the_open_months_first_auction_the_completed_month_is_the_newest_recorded(edges, monkeypatch):
    listing = _ladder_listing()
    count, leg, _, _ = _yield(edges, monkeypatch, listing, _rungs_matching(listing), today=date(2026, 10, 2))

    assert count == 0
    assert _categories(leg) == [wr.NO_NEW_OFFICIAL_PERIOD]
    assert leg["skips"][0]["newest_recorded"] == "2026-09-01"


def _cutoff_20y(cutoff: float) -> list[dict]:
    return [{**a, "cutoff": cutoff} if a["tenor"] == "20y" else a for a in _ladder_listing()]


INCOMPLETE_LADDERS = {
    # 20y never listed and nothing recorded yet: the ladder cannot be completed
    "tenor never auctioned": (lambda: [a for a in _ladder_listing() if a["tenor"] != "20y"], lambda: [],
                              "20y (yield_20y_monthly): no auction_results row"),
    # 20y listed at a corrupt cutoff on the very days its recorded rungs are dated by
    "cutoff out of range": (lambda: _cutoff_20y(30.0), lambda: _rungs_matching(_cutoff_20y(30.0)),
                            "20y (yield_20y_monthly): latest cutoff 30.0 outside (0.0, 25.0)"),
}


@pytest.mark.parametrize("listing,stored,problem", list(INCOMPLETE_LADDERS.values()), ids=list(INCOMPLETE_LADDERS))
def test_an_incomplete_ladder_is_withheld_by_validation_quoting_the_guard(edges, monkeypatch, listing,
                                                                          stored, problem):
    count, leg, before, table = _yield(edges, monkeypatch, listing(), stored())

    assert (count, table.rows) == (0, before)
    assert _categories(leg) == [wr.WITHHELD_BY_VALIDATION, wr.WITHHELD_BY_VALIDATION]
    for skip, month in zip(leg["skips"], ("2026-08-01", "2026-09-01")):
        assert skip["detail"].startswith(f"yield ladder incomplete for {month} -- writing NOTHING")
        assert "all-or-nothing, landmine 51" in skip["detail"]
        assert problem in skip["detail"]
    assert leg["status"] == "skipped" and NOT_ITEMISED not in leg["reason"]


def _lost(tenor: str, day: str):
    return lambda a: (a["tenor"], a["auction_date"]) != (tenor, day)


LOST_AUCTIONS = {
    # auction_results lost the 91d auction the recorded September rung is dated by
    "open month's recorded auction gone": (_lost("91d", "2026-09-21"),
                                           "tbill_91d_yield_monthly for 2026-09 recorded through 2026-09-21"),
    # ...or the one the completed August rung is dated by: an older August auction and the
    # later September one are still listed, so only a per-month comparison can see it
    "completed month's recorded auction gone": (_lost("91d", "2026-08-25"),
                                                "tbill_91d_yield_monthly for 2026-08 recorded through 2026-08-25 "
                                                "(newest listed: 2026-08-11)"),
    # ...or the whole open month is gone, so its leg is not even derived this run
    "whole recorded month gone": (lambda a: a["auction_date"][:7] != "2026-09",
                                  "yield_20y_monthly for 2026-09 recorded through 2026-09-21"),
}


@pytest.mark.parametrize("keep,named", list(LOST_AUCTIONS.values()), ids=list(LOST_AUCTIONS))
def test_auction_results_behind_a_recorded_rung_is_source_older_never_lag(edges, monkeypatch, keep, named):
    full = [*_ladder_listing(), _auction("91d", "2026-08-11", 8.95)]  # an earlier August bill auction
    count, leg, before, table = _yield(edges, monkeypatch, [a for a in full if keep(a)], _rungs_matching(full))

    assert (count, table.rows) == (0, before)  # the recorded rung stands, as before H4
    assert _categories(leg) == [wr.SOURCE_OLDER_THAN_RECORDS]
    assert named in leg["skips"][0]["detail"]


def test_a_yield_run_that_moves_a_rung_is_ok_with_confirmed_rows_and_no_lag(edges, monkeypatch):
    listing = _ladder_listing()
    fresher = [*listing, _auction("91d", "2026-09-23", 9.10)]
    count, leg, _, table = _yield(edges, monkeypatch, fresher, _rungs_matching(listing))

    assert (count, leg["status"], leg["confirmed_rows"], leg["skips"]) == (1, "ok", 1, [])
    assert table.value("tbill_91d_yield_monthly", "2026-09-01") == 9.10


def test_a_completed_month_moved_forward_onto_its_final_auction_is_ok_and_not_a_guard_refusal(edges, monkeypatch):
    """Landmine 54: the first run of a month carries the month just ended onto its last auction.
    That is the forward-only guard letting a write through, not refusing one."""
    listing = _ladder_listing()
    final = [*listing, _auction("91d", "2026-08-31", 8.97)]
    count, leg, _, table = _yield(edges, monkeypatch, final, _rungs_matching(listing))

    assert (count, leg["status"], leg["confirmed_rows"], leg["skips"]) == (1, "ok", 1, [])
    assert table.value("tbill_91d_yield_monthly", "2026-08-01") == 8.97


FORWARD_GUARD = "a closed month's rung moves only onto a strictly later auction"


def _recut(listing: list[dict], tenor: str, day: str, cutoff: float) -> list[dict]:
    return [{**a, "cutoff": cutoff} if (a["tenor"], a["auction_date"]) == (tenor, day) else a for a in listing]


def _undated(rows: list[dict], mid: str, as_of: str) -> list[dict]:
    return [{**r, "source_as_of": None} if (r["metric_id"], r["as_of"]) == (mid, as_of) else r for r in rows]


HELD_BY_FORWARD_GUARD = {
    # auction_results now lists a different cutoff for the very auction the recorded August rung is
    # dated by (a same-date re-extraction, landmine 49): the guard will not move a closed month
    # onto the same auction date, so the recorded rung and the source disagree
    "same-date cutoff correction": (
        lambda listing: _recut(listing, "91d", "2026-08-25", 9.40), lambda rows: rows,
        "tbill_91d_yield_monthly for 2026-08 stored 9.0 dated 2026-08-25, "
        "auction_results lists 9.4 dated 2026-08-25"),
    # the recorded August rung carries no auction date, so the guard cannot tell older from newer
    "recorded rung without an auction date": (
        lambda listing: listing, lambda rows: _undated(rows, "tbill_91d_yield_monthly", "2026-08-01"),
        "tbill_91d_yield_monthly for 2026-08 stored 9.0 with no auction date, "
        "auction_results lists 9.0 dated 2026-08-25"),
}


@pytest.mark.parametrize("listing,stored,named", list(HELD_BY_FORWARD_GUARD.values()), ids=list(HELD_BY_FORWARD_GUARD))
def test_a_completed_month_rung_the_forward_only_guard_keeps_is_withheld_never_lag(edges, monkeypatch, listing,
                                                                                   stored, named):
    """The guard's refusal is a validation refusal, not publication lag: the source lists an auction
    for the rung that the ladder does not carry."""
    base = _ladder_listing()
    count, leg, before, table = _yield(edges, monkeypatch, listing(base), stored(_rungs_matching(base)))

    assert (count, table.rows) == (0, before)  # the guard still keeps the recorded rung, as before H4
    assert _categories(leg) == [wr.WITHHELD_BY_VALIDATION]
    assert FORWARD_GUARD in leg["skips"][0]["detail"] and named in leg["skips"][0]["detail"]
    assert leg["status"] == "skipped" and NOT_ITEMISED not in leg["reason"]


def _aug_20y_corrupt_sep_open() -> tuple[list[dict], list[dict], date]:
    """August's newest 20y cutoff is corrupt; September derives cleanly and is not yet recorded."""
    listing = _ladder_listing()
    stored = [r for r in _rungs_matching(listing) if r["as_of"] == "2026-08-01"]
    return _recut(listing, "20y", "2026-08-25", 30.0), stored, date(2026, 9, 24)


def _with_new_182d(listing: list[dict]) -> list[dict]:
    return [*listing, _auction("182d", "2026-09-23", 9.40)]  # moves the open month's 182d rung


def _aug_91d_lost_sep_moves() -> tuple[list[dict], list[dict], date]:
    full = [*_ladder_listing(), _auction("91d", "2026-08-11", 8.95)]
    return _with_new_182d([a for a in full if _lost("91d", "2026-08-25")(a)]), _rungs_matching(full), TODAY


def _aug_91d_recut_sep_moves() -> tuple[list[dict], list[dict], date]:
    listing = _ladder_listing()
    return _with_new_182d(_recut(listing, "91d", "2026-08-25", 9.40)), _rungs_matching(listing), TODAY


REFUSED_BESIDE_A_WRITE = {
    "completed month incomplete": (_aug_20y_corrupt_sep_open, 8, wr.WITHHELD_BY_VALIDATION,
                                   "yield ladder incomplete for 2026-08-01"),
    "completed month's auction gone": (_aug_91d_lost_sep_moves, 1, wr.SOURCE_OLDER_THAN_RECORDS,
                                       "tbill_91d_yield_monthly for 2026-08 recorded through 2026-08-25"),
    "completed month held by the forward-only guard": (_aug_91d_recut_sep_moves, 1, wr.WITHHELD_BY_VALIDATION,
                                                       FORWARD_GUARD),
}


@pytest.mark.parametrize("scenario,written,category,named", list(REFUSED_BESIDE_A_WRITE.values()),
                         ids=list(REFUSED_BESIDE_A_WRITE))
def test_a_refused_or_regressed_month_is_itemised_even_when_the_other_month_writes_rows(
        edges, monkeypatch, scenario, written, category, named):
    """Controller ruling (2): a guard refusal or a source gone backwards is reported even when a
    sibling write is confirmed. The open month's rows land and read back; August stays as it was,
    and the leg still says why August was not moved (the Brief reads this day as degraded)."""
    listing, stored, today = scenario()
    count, leg, before, table = _yield(edges, monkeypatch, listing, stored, today=today)

    assert (count, leg["status"], leg["confirmed_rows"]) == (written, "ok", written)
    assert [r for r in table.rows if r["as_of"] == "2026-08-01"] == [r for r in before if r["as_of"] == "2026-08-01"]
    assert _categories(leg) == [category]
    assert named in leg["skips"][0]["detail"]


# --- EPB exports ---------------------------------------------------------------------------

EPB = {date.fromisoformat(r["as_of"]): r["value"] for r in CONTRACT["epb_evidence"]["rows"]}  # Jul, Aug


def _epb_row(day: date, value: float) -> dict:
    return {"metric_id": epb.METRIC_ID, "as_of": day.isoformat(), "value": value, "source": epb.SOURCE,
            "source_as_of": agg._month_end(day).isoformat()}


def _exports(edges, monkeypatch, tmp_path, listed: dict[date, float], stored: dict[date, float]):
    table = edges([_epb_row(d, v) for d, v in stored.items()])
    monkeypatch.setattr(epb, "fetch_exports", lambda: (sorted(listed.items()), "https://epb.example/w.xlsx"))
    before = [dict(r) for r in table.rows]
    count, leg = _run("exports", functools.partial(epb.write_exports_monthly, TODAY, evidence_dir=tmp_path))
    return count, leg, before, table


def test_caught_up_epb_exports_are_official_lag_with_their_newest_month_and_window(edges, monkeypatch, tmp_path):
    count, leg, before, table = _exports(edges, monkeypatch, tmp_path, EPB, EPB)

    assert (count, table.rows) == (0, before)
    assert _published(leg) == QUIET["legs"]["exports"]
    assert leg["skips"][0]["lag_window_days"] == agg._accepted_lag_days(epb.METRIC_ID)


JULY = {d: v for d, v in EPB.items() if d.month == 7}
OPEN_SEPTEMBER = {date(2026, 9, 1): 4000.0}  # SYNTHETIC: a column for the month still under way


@pytest.mark.parametrize("listed", [JULY, {**JULY, **OPEN_SEPTEMBER}],
                         ids=["closed months only", "plus an open-month column"])
def test_an_epb_listing_behind_a_recorded_month_is_source_older_never_lag(edges, monkeypatch, tmp_path, listed):
    """An unfinished month's column is never written and never counts as a newer listed month,
    so it cannot hide a listing that went backwards behind a recorded month."""
    count, leg, before, table = _exports(edges, monkeypatch, tmp_path, listed, EPB)

    assert (count, table.rows) == (0, before)
    assert leg["skips"] == [{"category": wr.SOURCE_OLDER_THAN_RECORDS,
                             "detail": "EPB goods summary (closed months) lists nothing at or after the newest recorded month "
                                       "2026-08-01 (newest listed: 2026-07-01)"}]


def test_a_new_epb_month_is_ok_with_confirmed_rows_and_no_lag(edges, monkeypatch, tmp_path):
    count, leg, _, _ = _exports(edges, monkeypatch, tmp_path, EPB, JULY)

    assert (count, leg["status"], leg["confirmed_rows"], leg["skips"]) == (1, "ok", 1, [])


# --- reserves split ------------------------------------------------------------------------


def _reserves(edges, gross: float, bpm6: float | None, *, present: bool = True):
    table = edges([])
    reading = ForexReserves(gross_reserves_usd_bn=gross, bpm6_reserves_usd_bn=bpm6,
                            reserves_date=date(2026, 9, 22), source_url=agg.RESERVES_MONTHLY_SOURCE_URL)
    count, leg = _run("reserves", lambda: agg._write_reserves_monthly_split(reading if present else None))
    return count, leg, table


def test_a_fresh_reserves_reading_is_ok_with_both_rows_confirmed(edges):
    count, leg, _ = _reserves(edges, 26.50, 21.40)

    assert count == 2
    assert _published(leg) == QUIET["legs"]["reserves"]


GUARDS = {
    "column swap": (21.40, 26.50, "bpm6 (26.5000) >= gross (21.4000) for 2026-09-22 -- refusing BOTH "
                                  "monthly writes (column-identification failure)"),
    "ratio outside band": (26.50, 13.25, "bpm6/gross ratio 0.5000 for 2026-09-22 is outside [0.70, 0.95] -- "
                                         "refusing BOTH monthly writes (magnitude/unit corruption, not a "
                                         "column swap)"),
}


@pytest.mark.parametrize("gross,bpm6,guard", list(GUARDS.values()), ids=list(GUARDS))
def test_a_refused_reserves_reading_is_withheld_by_validation_quoting_the_guard(edges, gross, bpm6, guard):
    count, leg, table = _reserves(edges, gross, bpm6)

    assert (count, table.rows) == (0, [])
    assert leg["skips"] == [{"category": wr.WITHHELD_BY_VALIDATION, "detail": guard}]
    assert leg["status"] == "skipped" and NOT_ITEMISED not in leg["reason"]


def test_a_forex_snapshot_without_a_reserves_reading_is_not_attempted(edges):
    count, leg, table = _reserves(edges, 26.50, 21.40, present=False)

    assert (count, table.rows) == (0, [])
    assert leg["skips"] == [{"category": wr.NOT_ATTEMPTED,
                             "detail": "the bb_forex snapshot carries no reserves reading"}]
