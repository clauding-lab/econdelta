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
from datetime import date
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

    def test_source_as_of_matches_day_1_as_of(self):
        """Unlike the CPI trio (which recovers a true intra-month source
        vintage), the yield ladder's source_as_of is the same day-1 value
        as as_of -- the derivation IS month-level by construction (latest
        auction on or before month-end), not a specific auction date."""
        month_start = date(2026, 7, 1)
        month_end = date(2026, 7, 31)
        auction_rows = _full_ladder_rows(date(2026, 7, 10))
        rows, _reasons = agg._yield_ladder_rows_for_month(
            auction_rows, month_start=month_start, month_end=month_end, existing_pairs=set(),
        )
        assert all(r["source_as_of"] == r["as_of"] == "2026-07-01" for r in rows)

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
        assert captured_as_of_arg["month_end"] == date(2026, 7, 31)
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
        assert captured_as_of_arg["month_end"] == date(2025, 12, 31)
        assert all(r["as_of"] == "2025-12-01" for r in captured)

    def test_append_only_skip_avoids_reading_auction_results_entirely(self, monkeypatch):
        """Optimization + isolation: once a month is fully written for all
        8 tenors, subsequent runs in the SAME calendar month must not even
        read auction_results (matches Phase 1's M6 fetch-skip pattern, here
        applied to a DB read instead of a browser launch)."""
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        month_start = agg._previous_month_start(TODAY)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            return [{"metric_id": metric_id, "as_of": month_start.isoformat()}]

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda *a, **k: pytest.fail("must not read auction_results when all 8 tenors already exist"),
        )
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda *a, **k: pytest.fail("no rows expected"),
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_yield_ladder_monthly_append(today=TODAY)
        assert n == 0

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

    def test_existing_rows_read_failure_has_its_own_message_and_skips_auction_read(self, monkeypatch):
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
        assert any("yield ladder read failed" in title for _level, title in notify_calls)

    def test_auction_results_read_failure_has_its_own_message(self, monkeypatch):
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
        assert any("yield ladder read failed" in title for _level, title in notify_calls)

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
