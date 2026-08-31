"""Tests for aggregate_latest._write_yield_ladder_monthly_append and its
pure helper (Phase 2 of the 2026-08-08 frozen-charts incident, AGENTS.md
landmine 51).

Covers: the pure derivation/append-only transform (carry-forward across
months, all-or-nothing on a missing/invalid tenor, append-only skip-if-
exists, range check, completed-month computation including the January
year-boundary), and the top-level orchestrator wired against mocked
reader/writer so no real network or Supabase call goes out. Follows
tests/test_macro_monthly_append.py's conventions.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import aggregate_latest as agg  # noqa: E402

TODAY = date(2026, 8, 8)  # -> most recently completed month is July 2026

ALL_8_MONTHLY_IDS = set(agg._YIELD_TENOR_TO_MONTHLY_ID.values())


def _auction_row(tenor: str, auction_date: date, cutoff: float) -> dict:
    return {"auction_date": auction_date.isoformat(), "tenor": tenor, "cutoff": cutoff}


def _full_ladder_rows(auction_date: date, base: float = 10.0) -> list[dict]:
    """One auction row per tenor, all on the same date, for convenience."""
    return [
        _auction_row(tenor, auction_date, base + i * 0.1)
        for i, tenor in enumerate(agg._YIELD_TENOR_TO_MONTHLY_ID)
    ]


# ---------------------------------------------------------------------------
# _yield_ladder_rows_for_month — pure derivation + append-only transform
# ---------------------------------------------------------------------------


class TestYieldLadderRowsForMonth:
    def test_all_8_tenors_present_writes_all_8(self):
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 15))
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        assert len(rows) == 8
        assert {r["metric_id"] for r in rows} == ALL_8_MONTHLY_IDS
        assert {r["as_of"] for r in rows} == {"2026-07-01"}

    def test_carry_forward_across_months_is_used(self):
        """A tenor's most recent auction predates the target month -- this
        IS the expected/normal case (bond tenors auction roughly monthly-
        to-quarterly, not every calendar month) and must still count."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        # 91d auctions fresh in July; 20y's last auction was back in April.
        auction_rows = [
            _auction_row("91d", date(2026, 7, 10), 9.80),
            *[_auction_row(t, date(2026, 4, 5), 11.0) for t in agg._YIELD_TENOR_TO_MONTHLY_ID if t != "91d"],
        ]
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        by_id = {r["metric_id"]: r for r in rows}
        assert by_id["tbill_91d_yield_monthly"]["value"] == pytest.approx(9.80)
        assert by_id["yield_20y_monthly"]["value"] == pytest.approx(11.0)  # carried forward from April

    def test_auction_date_after_month_end_is_excluded(self):
        """A future-dated auction (relative to month_end) must never count
        as "the latest auction for this month" -- carry-forward only looks
        BACKWARD."""
        month_start = date(2026, 6, 1)
        month_end = date(2026, 6, 30)
        auction_rows = _full_ladder_rows(date(2026, 6, 15))
        # 91d ALSO has a later (July) auction that must be ignored for June.
        auction_rows.append(_auction_row("91d", date(2026, 7, 5), 999.0))
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        by_id = {r["metric_id"]: r for r in rows}
        assert by_id["tbill_91d_yield_monthly"]["value"] != pytest.approx(999.0)

    def test_missing_tenor_triggers_all_or_nothing_no_writes(self):
        """The core landmine-51 rule: ONE missing tenor blocks the WHOLE
        month for ALL 8 tenors -- never a partial ladder (the Brief's chart
        takes the union of all 8 tenors' dates and would fabricate a curve
        shape no auction ever actually quoted)."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = [
            _auction_row(t, date(2026, 7, 10), 10.0)
            for t in agg._YIELD_TENOR_TO_MONTHLY_ID if t != "20y"
        ]  # 20y has NO row at all
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows == []
        assert len(reasons) == 1
        assert "all-or-nothing" in reasons[0]
        assert "20y" in reasons[0]
        assert "yield_20y_monthly" in reasons[0]

    def test_multiple_missing_tenors_are_all_named_in_one_reason(self):
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = [_auction_row("91d", date(2026, 7, 10), 9.8)]  # only 1 of 8
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows == []
        assert len(reasons) == 1  # ONE batched reason, not one per tenor
        for tenor in ("182d", "364d", "2y", "5y", "10y", "15y", "20y"):
            assert tenor in reasons[0]

    @pytest.mark.parametrize("bad_cutoff", [-1.0, 0.0, 25.0, 40.0])
    def test_out_of_range_cutoff_is_treated_as_missing_all_or_nothing(self, bad_cutoff):
        """A single bad tenor cutoff must not silently promote the other 7
        good tenors plus a fabricated 8th -- same all-or-nothing rule as a
        genuinely missing row."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        # Corrupt the 91d row's cutoff.
        for row in auction_rows:
            if row["tenor"] == "91d":
                row["cutoff"] = bad_cutoff
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows == []
        assert "outside" in reasons[0]

    def test_boundary_cutoffs_are_accepted(self):
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = [_auction_row(t, date(2026, 7, 10), 24.999) for t in agg._YIELD_TENOR_TO_MONTHLY_ID]
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        assert len(rows) == 8

    def test_append_only_filters_out_existing_tenors(self):
        """Stage 2: even though derivation succeeds for all 8, a tenor that
        already has this month's row must be EXCLUDED from the write --
        never overwritten."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        existing = {("tbill_91d_yield_monthly", month_start), ("yield_20y_monthly", month_start)}
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=existing,
        )
        assert reasons == []
        written_ids = {r["metric_id"] for r in rows}
        assert written_ids == ALL_8_MONTHLY_IDS - {"tbill_91d_yield_monthly", "yield_20y_monthly"}
        assert len(rows) == 6

    def test_all_8_already_existing_returns_nothing_to_write(self):
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        existing = {(mid, month_start) for mid in ALL_8_MONTHLY_IDS}
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=existing,
        )
        assert rows == []
        assert reasons == []  # NOT an all-or-nothing failure -- normal no-op

    def test_source_label_is_bb_auction(self):
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        rows, _reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert all(r["source"] == "bb_auction" for r in rows)

    def test_source_as_of_is_the_real_auction_date_per_tenor(self):
        """2026-08-08 review M2: source_as_of must be the ACTUAL auction_date
        for that tenor, not month_start -- matches the CPI leg's
        true-vintage convention and makes the H1 staleness guard auditable
        directly from the row (a chart-feeding row whose source_as_of
        trails its as_of by nearly 6 months is exactly the H1 scenario
        approaching its limit). as_of stays day-1-of-data-month regardless."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        # Different tenors auction on different real dates within the month.
        auction_rows = [
            _auction_row("91d", date(2026, 7, 10), 9.8),
            _auction_row("182d", date(2026, 7, 12), 9.9),
            *[_auction_row(t, date(2026, 7, 15), 10.0)
              for t in agg._YIELD_TENOR_TO_MONTHLY_ID if t not in ("91d", "182d")],
        ]
        rows, _reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        by_id = {r["metric_id"]: r for r in rows}
        assert all(r["as_of"] == "2026-07-01" for r in rows)  # as_of always day-1
        assert by_id["tbill_91d_yield_monthly"]["source_as_of"] == "2026-07-10"
        assert by_id["tbill_182d_yield_monthly"]["source_as_of"] == "2026-07-12"
        assert by_id["yield_20y_monthly"]["source_as_of"] == "2026-07-15"

    def test_unrecognised_tenor_in_auction_rows_is_ignored(self):
        """A stray/typo'd tenor label in auction_results (e.g. a future
        new tenor bb_auction.py doesn't map here yet) must not crash the
        derivation -- just isn't one of the 8 tracked tenors."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        auction_rows.append(_auction_row("3y", date(2026, 7, 10), 10.5))  # not tracked
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        assert len(rows) == 8

    def test_malformed_cutoff_value_treated_as_missing(self):
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        for row in auction_rows:
            if row["tenor"] == "5y":
                row["cutoff"] = None
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows == []
        assert "5y" in reasons[0]

    # --- H1 (2026-08-08 re-review): bounded carry-forward -------------------
    # Reviewer proof: with auction_results dead since a past date, the
    # appender would happily keep writing new months forever using an
    # ever-more-stale cutoff -- as_of advances every month, the sentinel
    # (which only ever looks at as_of, never at how old the underlying
    # auction is) classes all 8 ids as FRESH, and the chart-feeding alert
    # tier never fires. A frozen ladder becomes an INVISIBLY
    # fabricated-fresh one. Bound: 6 calendar months before month_end.

    def test_h1a_all_tenors_stale_writes_nothing_and_flags_all(self):
        """(a) All 8 tenors' latest auction predates the 6-month floor
        (auction_results has effectively gone dead) -- zero rows, one
        batched all-or-nothing reason naming every tenor."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        # Latest auction for every tenor was back in December 2025 -- 7
        # months before July 2026's month-end, past the 6-month floor.
        auction_rows = _full_ladder_rows(date(2025, 12, 15))
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows == []
        assert len(reasons) == 1
        assert "all-or-nothing" in reasons[0]
        for tenor in agg._YIELD_TENOR_TO_MONTHLY_ID:
            assert tenor in reasons[0]
        assert "2025-12-15" in reasons[0]

    def test_h1b_one_tenor_stale_blocks_the_whole_ladder(self):
        """(b) Only ONE tenor (20y, plausibly the thinnest-traded) has gone
        stale -- the all-or-nothing rule still refuses the WHOLE month, and
        the reason names that specific tenor and its last auction_date."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = [
            *[_auction_row(t, date(2026, 7, 10), 10.0)
              for t in agg._YIELD_TENOR_TO_MONTHLY_ID if t != "20y"],
            _auction_row("20y", date(2025, 12, 20), 11.0),  # 7+ months stale
        ]
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows == []
        assert len(reasons) == 1
        assert "20y" in reasons[0]
        assert "yield_20y_monthly" in reasons[0]
        assert "2025-12-20" in reasons[0]
        assert "6 months" in reasons[0]

    def test_h1c_five_months_old_still_passes(self):
        """(c) 5 months old is still within the 6-month bound -- generous
        for the thinnest tenor, must NOT be treated as absent."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        # 5 months before 2026-07-31 is 2026-02-28 (Feb has 28 days in
        # 2026) -- use a date safely inside that window.
        auction_rows = _full_ladder_rows(date(2026, 3, 1))
        rows, reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        assert len(rows) == 8

    def test_h1_staleness_floor_boundary(self):
        """Pin the exact boundary: 6 calendar months before 2026-07-31 is
        2026-01-31. An auction exactly ON that floor date passes; one day
        older fails."""
        month_end = date(2026, 7, 31)
        floor = agg._yield_ladder_staleness_floor(month_end)
        assert floor == date(2026, 1, 31)

        month_start = date(2026, 7, 1)
        on_floor_rows = _full_ladder_rows(floor)
        rows, reasons = agg._yield_ladder_rows_for_month(
            on_floor_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert reasons == []
        assert len(rows) == 8

        one_day_older_rows = _full_ladder_rows(floor - timedelta(days=1))
        rows2, reasons2 = agg._yield_ladder_rows_for_month(
            one_day_older_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert rows2 == []
        assert reasons2 != []

    def test_h1_staleness_floor_clamps_shorter_target_month(self):
        """monthrange-clamping sanity check: 6 months before March 31 lands
        in September (30 days), so day 31 must clamp to day 30, not raise."""
        floor = agg._yield_ladder_staleness_floor(date(2026, 3, 31))
        assert floor == date(2025, 9, 30)


# ---------------------------------------------------------------------------
# _write_yield_ladder_monthly_append — orchestrator, wired against mocks
# ---------------------------------------------------------------------------


class TestWriteYieldLadderMonthlyAppend:
    def test_completed_month_computation_targets_previous_month(self, monkeypatch):
        """today=2026-08-08 -> the target month is July 2026 (the most
        recently COMPLETED month), never August (still open)."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        captured_as_of_arg = {}

        def fake_get_auction_results_through(as_of, **kwargs):
            captured_as_of_arg["month_end"] = as_of
            return _full_ladder_rows(date(2026, 7, 10))

        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_auction_results_through", fake_get_auction_results_through)
        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 8
        # Both legs share one read, cut off at TODAY (each leg re-filters on
        # its own month_end); the DERIVATION target is proved by the as_of
        # the rows actually carry, which is July's month-start.
        assert captured_as_of_arg["month_end"] == TODAY
        assert all(r["as_of"] == "2026-07-01" for r in captured)

    def test_january_rolls_back_to_prior_december_end_to_end(self, monkeypatch):
        """Year-boundary case: today in January -> target month is prior
        December, not "January minus 1 month = month 0"."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        captured_as_of_arg = {}

        def fake_get_auction_results_through(as_of, **kwargs):
            captured_as_of_arg["month_end"] = as_of
            return _full_ladder_rows(date(2025, 12, 15))

        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_auction_results_through", fake_get_auction_results_through)
        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=date(2026, 1, 15))
        assert n == 8
        assert captured_as_of_arg["month_end"] == date(2026, 1, 15)
        assert all(r["as_of"] == "2025-12-01" for r in captured)

    def test_completed_month_written_and_open_month_quiet_writes_nothing(self, monkeypatch):
        """Once the completed month is fully written for all 8 tenors AND
        the open month has had no auction of its own, a run must write
        NOTHING -- the ordinary mid-month no-op.

        Supersedes the pre-2026-08-31 version of this test, which asserted
        the run skipped the auction_results read entirely. It can't any
        more: the open-month leg has to look at auction_results to know
        whether this month has moved. The invariant that actually matters
        -- no repeated writes -- is what's asserted here instead."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        month_start = agg._previous_month_start(TODAY)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            return [{"metric_id": metric_id, "as_of": month_start.isoformat(), "value": 10.0}]

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        # Auction history exists, but nothing inside August (TODAY's month).
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda *a, **k: _full_ladder_rows(date(2026, 7, 10)),
        )
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda *a, **k: pytest.fail("no rows expected"),
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 0

    def test_open_month_is_written_once_it_has_its_own_auction(self, monkeypatch):
        """The headline behaviour change (2026-08-31): with July already
        written and an August auction on the books, a run on 2026-08-08
        publishes the AUGUST rung too, without waiting for August to end."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        july = agg._previous_month_start(TODAY)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            return [{"metric_id": metric_id, "as_of": july.isoformat(), "value": 10.0}]

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            # August's own 10y auction, plus July history for the other seven.
            lambda *a, **k: [_auction_row("10y", date(2026, 8, 5), 9.2)]
            + _full_ladder_rows(date(2026, 7, 10)),
        )
        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 8
        assert {r["as_of"] for r in captured} == {"2026-08-01"}
        # The tenor that actually auctioned in August carries its real
        # August cutoff; the other seven carry forward from July.
        by_id = {r["metric_id"]: r for r in captured}
        assert by_id["yield_10y_monthly"]["value"] == 9.2
        assert by_id["yield_10y_monthly"]["source_as_of"] == "2026-08-05"
        assert by_id["tbill_91d_yield_monthly"]["source_as_of"] == "2026-07-10"

    def test_open_month_rewrites_only_the_tenors_whose_value_moved(self, monkeypatch):
        """Refresh mode is not a blind overwrite: an open-month tenor whose
        stored value already equals the derived one is left alone, so a
        quiet day doesn't churn ingested_at across all 8 rows."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        july = agg._previous_month_start(TODAY)
        august = date(2026, 8, 1)
        # August already written from an earlier auction; 10y has since moved.
        stored_august = {
            monthly_id: (9.2 if monthly_id == "yield_10y_monthly" else 10.0 + i * 0.1)
            for i, monthly_id in enumerate(agg._YIELD_TENOR_TO_MONTHLY_ID.values())
        }

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            return [
                {"metric_id": metric_id, "as_of": july.isoformat(), "value": 10.0},
                {"metric_id": metric_id, "as_of": august.isoformat(),
                 "value": stored_august[metric_id]},
            ]

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            # A NEWER August 10y print at 8.75 supersedes the stored 9.2.
            lambda *a, **k: [_auction_row("10y", date(2026, 8, 7), 8.75)]
            + _full_ladder_rows(date(2026, 7, 10)),
        )
        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 1
        assert captured[0]["metric_id"] == "yield_10y_monthly"
        assert captured[0]["as_of"] == "2026-08-01"
        assert captured[0]["value"] == 8.75

    def test_completed_month_is_never_overwritten_by_the_open_month_leg(self, monkeypatch):
        """Guard on the one thing refresh mode must never do: reach back
        into a month that is already over. July is stored at a value that
        DISAGREES with what today's auction history would derive, and it
        must survive untouched."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        july = agg._previous_month_start(TODAY)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            return [{"metric_id": metric_id, "as_of": july.isoformat(), "value": 99.0}]

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda *a, **k: [_auction_row("10y", date(2026, 8, 5), 9.2)]
            + _full_ladder_rows(date(2026, 7, 10)),
        )
        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        agg._write_yield_ladder_monthly_append(today=TODAY)
        assert {r["as_of"] for r in captured} == {"2026-08-01"}
        assert not any(r["as_of"] == july.isoformat() for r in captured)

    def test_open_month_deferred_when_it_has_no_auction_of_its_own(self, monkeypatch):
        """The duplicate-line guard: on the 1st of a month, before that
        month has auctioned anything, the open-month rung would be a pure
        carry-forward copy of the completed month. Publish nothing rather
        than draw the same curve twice."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda *a, **k: _full_ladder_rows(date(2026, 7, 10)),
        )
        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=date(2026, 8, 1))
        assert n == 8
        assert {r["as_of"] for r in captured} == {"2026-07-01"}

    def test_all_or_nothing_failure_notifies_and_writes_nothing(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def fake_get_auction_results_through(as_of, **kwargs):
            # Only 7 of 8 tenors have data.
            return [
                {"auction_date": "2026-07-10", "tenor": t, "cutoff": 10.0}
                for t in agg._YIELD_TENOR_TO_MONTHLY_ID if t != "15y"
            ]

        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(reader, "get_auction_results_through", fake_get_auction_results_through)
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda *a, **k: pytest.fail("all-or-nothing: must not write when a tenor is missing"),
        )
        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title, msg)))

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 0
        incomplete_calls = [c for c in notify_calls if "yield ladder incomplete" in c[1]]
        assert incomplete_calls, notify_calls
        assert "15y" in incomplete_calls[0][2]

    def test_existing_rows_read_failure_has_its_own_distinct_message(self, monkeypatch):
        """2026-08-08 review M3: the title must be DISTINCT from the
        auction_results-read-failure title below -- utils.notifier.notify
        dedups on (level, title) for 3600s, so two failures sharing one
        title in the same run would silently suppress the second."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def raise_read(*a, **k):
            raise Exception("boom")  # noqa: TRY002 -- deliberately generic, mirrors M1's JSONDecodeError class

        monkeypatch.setattr(reader, "get_metric_history_monthly", raise_read)
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda *a, **k: pytest.fail("must not read auction_results when the existing-rows check failed"),
        )
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))
        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 0
        assert any(title == agg._YIELD_EXISTING_ROWS_READ_FAILED_TITLE for _level, title in notify_calls)
        assert not any(title == agg._YIELD_AUCTION_READ_FAILED_TITLE for _level, title in notify_calls)

    def test_auction_results_read_failure_has_its_own_distinct_message(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])

        def raise_read(*a, **k):
            raise Exception("boom")  # noqa: TRY002

        monkeypatch.setattr(reader, "get_auction_results_through", raise_read)
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: pytest.fail("no rows expected"))
        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 0
        assert any(title == agg._YIELD_AUCTION_READ_FAILED_TITLE for _level, title in notify_calls)
        assert not any(title == agg._YIELD_EXISTING_ROWS_READ_FAILED_TITLE for _level, title in notify_calls)

    def test_m3_the_two_read_failure_titles_are_never_identical(self):
        """Direct pin of the M3 fix against the PRODUCTION constants
        (2026-08-08 re-review N1) -- the prior version of this test defined
        its own two literal strings inline, which could never fail even if
        the two notify() call sites were accidentally re-merged onto one
        title, since the test wasn't reading from the same source of truth
        the code actually uses. Asserting against agg._YIELD_..._TITLE
        directly means a future edit that collapses the two constants (or
        the two notify() call sites) back onto one title breaks this test."""
        assert agg._YIELD_EXISTING_ROWS_READ_FAILED_TITLE != agg._YIELD_AUCTION_READ_FAILED_TITLE
        # Also guard against both being accidentally emptied/blanked out to
        # "equal nothing" (technically still "not identical" is the wrong
        # bar to clear).
        assert agg._YIELD_EXISTING_ROWS_READ_FAILED_TITLE
        assert agg._YIELD_AUCTION_READ_FAILED_TITLE

    def test_writes_all_8_in_one_upsert_batch(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda *a, **k: _full_ladder_rows(date(2026, 7, 10)),
        )
        calls = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (calls.append(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 8
        assert len(calls) == 1  # ONE batch call, not 8 individual ones
        assert len(calls[0]) == 8


# ---------------------------------------------------------------------------
# main() call-site wiring — isolation between the three legs
# ---------------------------------------------------------------------------


def test_main_calls_yield_ladder_leg_when_supabase_enabled(tmp_path, monkeypatch):
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_writer as sw

    monkeypatch.setattr(sw, "upsert_metric_history", lambda **k: len(k.get("data", {})))
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_history_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(agg, "_write_macro_monthly_append", lambda: 0)

    calls = []
    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", lambda: (calls.append(1), 0)[1])

    exit_code = agg.main()
    assert exit_code == 0
    assert calls == [1]


def test_yield_ladder_failure_does_not_prevent_macro_append_from_completing(tmp_path, monkeypatch):
    """The reviewer WILL probe this (per the spec): a yield-ladder failure
    must never prevent the CPI/remittance legs from reaching their own
    upsert. Since the yield-ladder call is sequenced AFTER the macro-append
    call in main(), this is really just proving the macro-append leg's
    result is unaffected by whatever the yield-ladder leg does next."""
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_writer as sw

    monkeypatch.setattr(sw, "upsert_metric_history", lambda **k: len(k.get("data", {})))
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_history_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_monthly", lambda *a, **k: 0)

    macro_append_calls = []
    monkeypatch.setattr(agg, "_write_macro_monthly_append", lambda: (macro_append_calls.append(1), 3)[1])

    def yield_ladder_raises(*a, **k):
        raise RuntimeError("yield ladder blew up")

    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", yield_ladder_raises)

    notify_calls = []
    monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

    exit_code = agg.main()
    assert exit_code == 0  # must not crash
    assert macro_append_calls == [1]  # the macro-append leg DID run and complete
    assert any("yield ladder" in title.lower() for _level, title in notify_calls)
    # And the yield-ladder failure notify must be distinguishable from the
    # macro-append / D5 reserves-split notify titles.
    assert not any(title == "aggregate — macro monthly append write failed" for _level, title in notify_calls)


def test_macro_append_failure_does_not_prevent_yield_ladder_from_running(tmp_path, monkeypatch):
    """The inverse isolation check: a CPI/remittance-leg failure must not
    block the yield-ladder leg from still running afterward."""
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_writer as sw

    monkeypatch.setattr(sw, "upsert_metric_history", lambda **k: len(k.get("data", {})))
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_history_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_monthly", lambda *a, **k: 0)

    def macro_append_raises(*a, **k):
        raise RuntimeError("macro append blew up")

    monkeypatch.setattr(agg, "_write_macro_monthly_append", macro_append_raises)

    yield_ladder_calls = []
    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", lambda: (yield_ladder_calls.append(1), 0)[1])

    monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

    exit_code = agg.main()
    assert exit_code == 0
    assert yield_ladder_calls == [1]  # yield-ladder leg still ran despite the prior leg's crash


# ---------------------------------------------------------------------------
# _has_auction_in_window — open-month gate helper
# ---------------------------------------------------------------------------


class TestHasAuctionInWindow:
    def test_true_for_a_known_tenor_inside_the_window(self):
        rows = [_auction_row("10y", date(2026, 8, 5), 9.2)]
        assert agg._has_auction_in_window(rows, start=date(2026, 8, 1), end=date(2026, 8, 8))

    def test_window_is_inclusive_at_both_ends(self):
        assert agg._has_auction_in_window(
            [_auction_row("10y", date(2026, 8, 1), 9.2)],
            start=date(2026, 8, 1), end=date(2026, 8, 8),
        )
        assert agg._has_auction_in_window(
            [_auction_row("10y", date(2026, 8, 8), 9.2)],
            start=date(2026, 8, 1), end=date(2026, 8, 8),
        )

    def test_false_when_every_row_predates_the_window(self):
        rows = _full_ladder_rows(date(2026, 7, 10))
        assert not agg._has_auction_in_window(rows, start=date(2026, 8, 1), end=date(2026, 8, 8))

    def test_a_tenor_the_ladder_does_not_plot_cannot_unfreeze_the_month(self):
        """An auction in a tenor with no rung can't move the curve, so it
        must not be what makes the open month publishable."""
        rows = [_auction_row("30y", date(2026, 8, 5), 9.2)]
        assert "30y" not in agg._YIELD_TENOR_TO_MONTHLY_ID
        assert not agg._has_auction_in_window(rows, start=date(2026, 8, 1), end=date(2026, 8, 8))

    def test_unparseable_auction_date_is_ignored_not_raised(self):
        rows = [{"auction_date": "not-a-date", "tenor": "10y", "cutoff": 9.2}]
        assert not agg._has_auction_in_window(rows, start=date(2026, 8, 1), end=date(2026, 8, 8))
