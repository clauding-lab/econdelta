"""Tests for PR-C's three new/derived monthly-chart legs and daily
derivation:

1. The imports monthly leg (build-brief item 1) -- parses BB's own MEI PDF
   "Custom based import (c&f)" table, the mandatory splice check, row
   selection, and the fetch wrapper wiring into _write_macro_monthly_append.
2. The M2 growth monthly leg (build-brief item 4) -- a single-id sibling of
   the CPI trio's derivation pattern.
3. The daily yield-curve derivation from auction_results (build-brief item
   3, AGENTS.md landmine 49's two-yield-column trap fix).

The imports PDF-parsing tests run against tests/_pdfs/bb_mei_2026_june.pdf,
the SAME real, committed capture already used by test_pdf_table_row.py /
test_pdf_table_latest.py / test_config_conversion_batch1.py -- not a
hand-built fixture (AGENT_LEARNINGS.md's explicit lesson: a synthetic table
can invert the real producer's row/column semantics without anyone
noticing).
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
from fetchers.base import FetchError  # noqa: E402

MEI_FIXTURE = REPO_ROOT / "tests" / "_pdfs" / "bb_mei_2026_june.pdf"
TODAY = date(2026, 8, 22)


# ---------------------------------------------------------------------------
# parse_imports_c_and_f_table / _find_imports_table / _parse_imports_rows
# ---------------------------------------------------------------------------


class TestParseImportsCAndFTable:
    def test_parses_every_provisional_month_from_the_real_fixture(self):
        rows, _revised = agg.parse_imports_c_and_f_table(MEI_FIXTURE)
        by_date = dict(rows)
        # Values verified live 2026-08-22 against the real June-2026 MEI PDF
        # (the source scout's numbers, independently reconfirmed here).
        assert by_date[date(2026, 3, 1)] == pytest.approx(5826.22)
        assert by_date[date(2026, 4, 1)] == pytest.approx(7066.10)
        assert by_date[date(2026, 5, 1)] == pytest.approx(6108.22)
        # July 2025 (the first month of FY26) is also present.
        assert by_date[date(2025, 7, 1)] == pytest.approx(6270.46)

    def test_only_the_provisional_fy26_block_is_returned_not_the_fy25_comparator(self):
        """The 'p_rows' half must NEVER contain the table's 'R' column (FY25
        revised, the SAME months one year earlier) -- reading it into P
        would double-count a month this leg already captured a year prior
        under its own 'P' reading."""
        rows, _revised = agg.parse_imports_c_and_f_table(MEI_FIXTURE)
        values = {v for _as_of, v in rows}
        # FY25's comparator value for March (5896.66) must not appear.
        assert 5896.66 not in values

    def test_revised_half_carries_the_fy25_comparator_column(self):
        """Round 2, HIGH-1: the SECOND return value is the table's revised
        (R) column, keyed by its OWN true month -- anchor-use only, never
        written. March 2026's comparator (FY25, i.e. March 2025) is
        5896.66 in the real fixture."""
        _rows, revised = agg.parse_imports_c_and_f_table(MEI_FIXTURE)
        assert revised[date(2025, 3, 1)] == pytest.approx(5896.66)
        assert revised[date(2025, 4, 1)] == pytest.approx(5820.45)
        assert revised[date(2025, 5, 1)] == pytest.approx(5787.32)

    def test_annual_summary_rows_are_not_month_rows(self):
        rows, _revised = agg.parse_imports_c_and_f_table(MEI_FIXTURE)
        values = {v for _as_of, v in rows}
        # The 'July-May' cumulative total (67727.60) must never be mistaken
        # for a single month's reading.
        assert 67727.60 not in values

    def test_no_matching_header_raises(self, tmp_path):
        import pdfplumber
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Table

        pdf_path = tmp_path / "empty.pdf"
        doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
        doc.build([Table([["Unrelated", "Table"], ["a", "1"]])])
        with pytest.raises(ValueError, match="no table"):
            agg.parse_imports_c_and_f_table(pdf_path)
        del pdfplumber  # imported only to fail fast if the dep is missing


class TestParseImportsPAndRRows:
    """Direct coverage of the new P+R extraction (Opus review round 2,
    HIGH-1) -- _parse_imports_rows/parse_imports_c_and_f_table are thin
    wrappers around this."""

    def test_r_column_resolves_to_its_own_true_month_one_year_back(self):
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY26P", "FY25R"],
            ["July", "100.0", "90.0"],
            ["January", "200.0", "190.0"],
        ]
        p_rows, r_by_month = agg._parse_imports_p_and_r_rows(table)
        assert dict(p_rows) == {date(2025, 7, 1): 100.0, date(2026, 1, 1): 200.0}
        assert r_by_month == {date(2024, 7, 1): 90.0, date(2025, 1, 1): 190.0}

    def test_post_roll_shape_p_has_only_the_new_fy_month_r_has_the_prior_fy(self):
        """The REAL post-roll shape (Opus review round 2 finding): BB does
        NOT duplicate the monthly block -- it RE-LABELS the SAME block's
        two columns in place. Only ONE 'Month' sub-header, now reading
        'FY27P'/'FY26R' instead of 'FY26P'/'FY25R'."""
        table = [
            ["(USD in million)", None, None, None, None],
            ["", "Custom based import (c&f)", None, "Import LCs opening", "Import LCs settlement"],
            ["Month", "FY26R", "FY25R", "FY26R1", "FY26R1"],
            ["July-June", "73553.70", "68354.21", "74000.00", "72000.00"],
            ["", "(+7.61)", "(+2.44)", "(+0.18)", "(+4.18)"],
            ["Month", "FY27P", "FY26R", "FY27P2", "FY27P2"],
            ["July", "6501.00", "6270.46", "6600.00", "6400.00"],
        ]
        p_rows, r_by_month = agg._parse_imports_p_and_r_rows(table)
        assert dict(p_rows) == {date(2026, 7, 1): 6501.00}
        assert r_by_month == {date(2025, 7, 1): 6270.46}

    def test_empty_r_when_no_r_column_present(self):
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY26P"],
            ["July", "100.0"],
        ]
        p_rows, r_by_month = agg._parse_imports_p_and_r_rows(table)
        assert dict(p_rows) == {date(2025, 7, 1): 100.0}
        assert r_by_month == {}

    def test_no_raise_on_empty_p_unlike_the_require_wrapper(self):
        """_parse_imports_p_and_r_rows itself never raises -- only
        _require_imports_p_rows (used by the back-compat wrappers) does."""
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY25R", "FY24R"],
            ["July", "999.0", "888.0"],
        ]
        p_rows, r_by_month = agg._parse_imports_p_and_r_rows(table)
        assert p_rows == []
        assert r_by_month == {date(2024, 7, 1): 999.0}


class TestFindImportsTable:
    def test_raises_on_zero_matches(self):
        with pytest.raises(ValueError, match="no table"):
            agg._find_imports_table_from_tables([[["unrelated"]]])

    def test_raises_on_ambiguous_matches(self):
        matching = [["custom based import (c&f)", None], ["July", "1.0"]]
        with pytest.raises(ValueError, match="ambiguous"):
            agg._find_imports_table_from_tables([matching, matching])


class TestParseImportsRows:
    def test_year_rolls_correctly_across_the_fiscal_boundary(self):
        """July-December belong to (FY end - 1); January-June belong to
        (FY end) -- BD's FY runs July-June."""
        table = [
            ["(USD in million)"],
            ["", "Custom based import (c&f)", None],
            ["Month", "FY26P", "FY25R"],
            ["July", "100.0", "90.0"],
            ["January", "200.0", "190.0"],
        ]
        rows = agg._parse_imports_rows(table)
        assert (date(2025, 7, 1), 100.0) in rows
        assert (date(2026, 1, 1), 200.0) in rows

    def test_revised_only_block_is_skipped_entirely(self):
        """A 'Month' sub-header where the group column reads 'R' (not 'P')
        opens no usable block at all."""
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY25R", "FY24R"],
            ["July", "999.0", "888.0"],
        ]
        with pytest.raises(ValueError, match="ZERO"):
            agg._parse_imports_rows(table)

    def test_non_numeric_cell_is_skipped_not_crashed(self):
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY26P", "FY25R"],
            ["July", "n/a", "90.0"],
            ["August", "100.0", "90.0"],
        ]
        rows = agg._parse_imports_rows(table)
        assert rows == [(date(2025, 8, 1), 100.0)]

    def test_zero_rows_raises(self):
        table = [["(USD in million)"], ["", "Custom based import (c&f)", None]]
        with pytest.raises(ValueError, match="ZERO"):
            agg._parse_imports_rows(table)

    def test_l2_footnote_digit_after_p_is_tolerated(self):
        """A 'FY26P2' header (a footnote-marked P column, confirmed live on
        this same MEI PDF's Import LCs columns, though never observed yet
        on 'Custom based import (c&f)' itself) must still resolve as a P
        column for FY26, not silently fail to match at all."""
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY26P2", "FY25R"],
            ["July", "100.0", "90.0"],
        ]
        rows = agg._parse_imports_rows(table)
        assert rows == [(date(2025, 7, 1), 100.0)]

    def test_l1_prefers_the_max_fy_end_year_when_both_columns_are_p(self):
        """An unusual transition-edition shape: BOTH of the group's two
        columns read 'P' (different FY years) instead of the normal P-vs-R
        pairing. The max FY-end-year must win, regardless of which column
        comes first in iteration order."""
        table = [
            ["", "Custom based import (c&f)", None],
            ["Month", "FY26P", "FY27P"],  # older P listed FIRST -- must not win
            ["July", "100.0", "999.0"],
        ]
        rows = agg._parse_imports_rows(table)
        # FY27P wins (max), so "July" resolves under FY27 (July 2026), using
        # the FY27P column's OWN value (999.0), not FY26P's (100.0).
        assert rows == [(date(2026, 7, 1), 999.0)]


# ---------------------------------------------------------------------------
# _imports_splice_check -- the MANDATORY pre-write guard
# ---------------------------------------------------------------------------


class TestImportsSpliceCheck:
    def test_passes_within_tolerance(self):
        problem = agg._imports_splice_check(
            {date(2026, 3, 1): 5826.22}, {date(2026, 3, 1): 5826.2},
        )
        assert problem is None

    def test_fails_beyond_2pct(self):
        problem = agg._imports_splice_check(
            {date(2026, 3, 1): 6500.0}, {date(2026, 3, 1): 5826.2},
        )
        assert problem is not None
        assert "FAILED" in problem

    def test_missing_pdf_value_fails_closed(self):
        problem = agg._imports_splice_check({}, {date(2026, 3, 1): 5826.2})
        assert problem is not None
        assert "unavailable" in problem

    def test_missing_db_value_fails_closed(self):
        problem = agg._imports_splice_check({date(2026, 3, 1): 5826.22}, {})
        assert problem is not None
        assert "unavailable" in problem

    def test_just_under_2pct_boundary_passes(self):
        db = 5826.2
        pdf = db * 1.019
        problem = agg._imports_splice_check({date(2026, 3, 1): pdf}, {date(2026, 3, 1): db})
        assert problem is None

    def test_just_over_2pct_boundary_fails(self):
        db = 5826.2
        pdf = db * 1.021
        problem = agg._imports_splice_check({date(2026, 3, 1): pdf}, {date(2026, 3, 1): db})
        assert problem is not None

    # --- Opus review round 1, H1: dynamic anchor (max of the intersection) --

    def test_anchor_is_the_max_of_the_overlap_not_the_first_key(self):
        """Multiple months overlap -- the anchor must be the LATEST shared
        month, not an arbitrary/first one. Deliberately makes the older
        shared month fail tolerance so a wrong (non-max) anchor choice
        would be caught by this test."""
        pdf_rows = {
            date(2026, 3, 1): 9999.0,  # would FAIL tolerance if wrongly chosen
            date(2026, 4, 1): 7066.10,
            date(2026, 5, 1): 6108.22,
        }
        db_rows = {
            date(2026, 3, 1): 5826.2,
            date(2026, 4, 1): 7066.10,
            date(2026, 5, 1): 6108.22,
        }
        problem = agg._imports_splice_check(pdf_rows, db_rows)
        assert problem is None  # anchored on May (the max), which matches exactly

    def test_no_overlap_at_all_fails_closed(self):
        """The narrow post-FY-roll window this fix cannot fully eliminate:
        if the PDF's provisional column and the DB share literally no
        month, there is nothing to verify continuity against."""
        problem = agg._imports_splice_check(
            {date(2026, 7, 1): 6270.46}, {date(2026, 5, 1): 6108.22},
        )
        assert problem is not None
        assert "unavailable" in problem

    def test_survives_a_hardcoded_march_anchor_that_no_longer_exists(self):
        """Regression pin (would only pass under a P-only or P-then-R
        anchor -- proves the OLD hardcoded-March design is what's being
        replaced, not a claim that P-only alone survives every roll shape;
        see TestImportsSpliceCheckAcrossFYRoll for the REAL roll shape,
        where P alone does NOT have an overlap and the R fallback is what
        actually saves the day)."""
        pdf_rows = {
            date(2026, 6, 1): 9440.0,
            date(2026, 7, 1): 6270.46,
        }
        db_rows = {
            date(2026, 5, 1): 6108.22,
            date(2026, 6, 1): 9440.0,
        }
        problem = agg._imports_splice_check(pdf_rows, db_rows)
        assert problem is None  # anchored on June (still in P here) -- March is irrelevant now

    # --- Opus review round 2, HIGH-1: revised (R) column fallback anchor ---

    def test_r_fallback_used_when_p_has_no_overlap(self):
        pdf_rows = {date(2026, 7, 1): 6501.00}  # P: only the new FY27 month
        pdf_revised_rows = {date(2025, 7, 1): 6270.46}  # R: FY26's July, already in DB
        db_rows = {date(2025, 7, 1): 6270.46, date(2026, 6, 1): 9440.0}
        problem = agg._imports_splice_check(pdf_rows, db_rows, pdf_revised_rows)
        assert problem is None

    def test_r_fallback_enforces_the_same_2pct_band(self):
        pdf_rows = {date(2026, 7, 1): 6501.00}
        pdf_revised_rows = {date(2025, 7, 1): 9999.0}  # drifted > 2% vs db
        db_rows = {date(2025, 7, 1): 6270.46}
        problem = agg._imports_splice_check(pdf_rows, db_rows, pdf_revised_rows)
        assert problem is not None
        assert "FAILED" in problem
        assert "revised (R)" in problem

    def test_p_preferred_over_r_when_both_have_overlap(self):
        """P is tried FIRST -- R is a fallback only, never preferred even
        when both happen to have a usable overlap."""
        pdf_rows = {date(2026, 6, 1): 9440.0}  # P has an overlap
        pdf_revised_rows = {date(2026, 6, 1): 1.0}  # would FAIL if wrongly chosen
        db_rows = {date(2026, 6, 1): 9440.0}
        problem = agg._imports_splice_check(pdf_rows, db_rows, pdf_revised_rows)
        assert problem is None

    def test_no_overlap_in_either_column_fails_closed(self):
        problem = agg._imports_splice_check(
            {date(2026, 7, 1): 6501.00}, {date(2020, 1, 1): 1.0}, {date(2025, 7, 1): 6270.46},
        )
        assert problem is not None
        assert "unavailable" in problem

    def test_missing_revised_rows_argument_defaults_to_no_fallback(self):
        """Backward compatible: omitting the 3rd argument entirely behaves
        exactly like round 1 -- P-only, fail closed if P has no overlap."""
        problem = agg._imports_splice_check(
            {date(2026, 7, 1): 6501.00}, {date(2026, 6, 1): 9440.0},
        )
        assert problem is not None
        assert "unavailable" in problem


class TestImportsSpliceCheckAcrossFYRoll:
    """Opus review round 2, HIGH-1: the round-1 version of this class
    certified a FICTIONAL shape (two separate 'Month' blocks, an old
    FY26P-tail block followed by a new FY27P block, as if BB duplicated
    the monthly block at rollover). A reviewer simulation against the
    REAL fixture re-labelled forward one fiscal year proved BB does NOT
    duplicate the block -- it RE-LABELS the SAME block's two columns in
    place. There is only ever ONE monthly 'Month' sub-header, and the
    instant the first FY27 month publishes, EVERY FY26 month (including
    whatever the DB most recently has) moves to 'R' in that SAME update.
    Under a P-only anchor this is a permanent deadlock (P ∩ db is empty
    forever, not just for one run) -- this class now pins the REAL shape
    and proves the R-fallback (added to _imports_splice_check above) is
    what actually resolves it."""

    # The REAL post-roll table shape: ONE annual block (now FY26R/FY25R)
    # + ONE monthly block, re-labelled FY27P/FY26R, carrying only July so
    # far (BB's first post-roll issue).
    ROLLED_TABLE = [
        ["(USD in million)", None, None, None, None],
        ["", "Custom based import (c&f)", None, "Import LCs opening", "Import LCs settlement"],
        ["Month", "FY26R", "FY25R", "FY26R1", "FY26R1"],
        ["July-June", "73553.70", "68354.21", "74000.00", "72000.00"],
        ["", "(+7.61)", "(+2.44)", "(+0.18)", "(+4.18)"],
        ["Month", "FY27P", "FY26R", "FY27P2", "FY27P2"],
        ["July", "6501.00", "6270.46", "6600.00", "6400.00"],
    ]

    # The DB state on the day of the roll: appended through June 2026 (the
    # last FY26 month, captured from the PREVIOUS issue) plus older
    # pre-freeze seeded history -- July 2026 is NOT yet present (that's
    # exactly the row this leg is trying to write this run).
    DB_AT_ROLL = {
        date(2025, 7, 1): 6270.46,
        date(2026, 3, 1): 5826.2,
        date(2026, 4, 1): 7066.10,
        date(2026, 5, 1): 6108.22,
        date(2026, 6, 1): 9440.0,
    }

    def test_p_only_has_zero_overlap_with_the_db_this_is_the_deadlock(self):
        """Confirms the failure mode this fix addresses: with ONLY the P
        column considered, the DB and the PDF share NOTHING -- not a
        one-run gap, a permanent one (every FY26 month the DB has just
        moved to R in this SAME update; no future run's P column can ever
        contain it again)."""
        p_rows, _revised = agg._parse_imports_p_and_r_rows(self.ROLLED_TABLE)
        assert set(dict(p_rows)) & set(self.DB_AT_ROLL) == set()

    def test_anchor_found_via_revised_column_and_july_is_written(self):
        p_rows, revised = agg._parse_imports_p_and_r_rows(self.ROLLED_TABLE)
        assert dict(p_rows) == {date(2026, 7, 1): 6501.00}
        assert revised[date(2025, 7, 1)] == pytest.approx(6270.46)

        # The mandatory splice check must ANCHOR VIA R (July 2025, which the
        # DB already has from a year ago) and PASS (6270.46 vs 6270.46 --
        # exact match).
        problem = agg._imports_splice_check(dict(p_rows), self.DB_AT_ROLL, revised)
        assert problem is None

        # With the check passing, July 2026 (the one new P-column month) is
        # the row that gets WRITTEN.
        rows, reasons = agg._select_new_imports_rows(
            p_rows, existing_as_of=set(self.DB_AT_ROLL), today=date(2026, 10, 15),
        )
        assert [r["as_of"] for r in rows] == ["2026-07-01"]
        assert rows[0]["value"] == pytest.approx(6501.00)
        assert reasons == []

    def test_band_enforced_when_revised_column_value_has_drifted(self):
        """The R-fallback anchor is NOT a rubber stamp -- if the PDF's own
        revised-column reading for July 2025 disagrees with the DB's by
        more than 2%, the whole leg still refuses, exactly like the P-path
        already does."""
        drifted_table = [row[:] for row in self.ROLLED_TABLE]
        # Replace July's R-column reading (index 2) with a value that
        # diverges >2% from the DB's 6270.46 for that same month.
        july_row_idx = next(i for i, r in enumerate(drifted_table) if r and r[0] == "July")
        drifted_table[july_row_idx] = ["July", "6501.00", "9999.00", "6600.00", "6400.00"]

        p_rows, revised = agg._parse_imports_p_and_r_rows(drifted_table)
        assert revised[date(2025, 7, 1)] == pytest.approx(9999.00)

        problem = agg._imports_splice_check(dict(p_rows), self.DB_AT_ROLL, revised)
        assert problem is not None
        assert "FAILED" in problem
        assert "revised (R)" in problem

        # And nothing gets written this run -- _write_macro_monthly_append's
        # caller-level contract (proven at the sub-path level elsewhere):
        # a non-None splice_problem means the whole leg is skipped.

    def test_self_heals_as_more_fy27_months_publish(self):
        """Once a SECOND FY27 month is available, the P-column itself
        starts to reconnect with what the leg is now writing under its own
        progression -- proving this isn't a one-off, R-only crutch."""
        two_month_table = [row[:] for row in self.ROLLED_TABLE]
        two_month_table.append(["August", "5200.00", "5222.73", "5300.00", "5100.00"])
        db_after_july_written = dict(self.DB_AT_ROLL)
        db_after_july_written[date(2026, 7, 1)] = 6501.00  # last run's write

        p_rows, revised = agg._parse_imports_p_and_r_rows(two_month_table)
        # August run: P now overlaps the DB directly on July -- no R
        # fallback needed any more.
        assert set(dict(p_rows)) & set(db_after_july_written) == {date(2026, 7, 1)}
        problem = agg._imports_splice_check(dict(p_rows), db_after_july_written, revised)
        assert problem is None


# ---------------------------------------------------------------------------
# _select_new_imports_rows
# ---------------------------------------------------------------------------


class TestSelectNewImportsRows:
    def test_only_months_at_or_after_the_freeze_point_are_selected(self):
        parsed = [(date(2026, 3, 1), 5826.22), (date(2026, 4, 1), 7066.10)]
        rows, reasons = agg._select_new_imports_rows(
            parsed, existing_as_of=set(), today=TODAY,
        )
        assert [r["as_of"] for r in rows] == ["2026-04-01"]
        assert reasons == []

    def test_skip_if_already_exists_append_only(self):
        parsed = [(date(2026, 4, 1), 7066.10), (date(2026, 5, 1), 6108.22)]
        rows, _reasons = agg._select_new_imports_rows(
            parsed, existing_as_of={date(2026, 4, 1)}, today=TODAY,
        )
        assert [r["as_of"] for r in rows] == ["2026-05-01"]

    def test_future_month_is_rejected(self):
        parsed = [(date(2026, 12, 1), 7000.0)]
        rows, reasons = agg._select_new_imports_rows(
            parsed, existing_as_of=set(), today=TODAY,
        )
        assert rows == []
        assert any("future" in r for r in reasons)

    def test_out_of_range_value_is_rejected(self):
        parsed = [(date(2026, 4, 1), 99999.0)]
        rows, reasons = agg._select_new_imports_rows(
            parsed, existing_as_of=set(), today=TODAY,
        )
        assert rows == []
        assert any("outside" in r for r in reasons)

    def test_row_shape(self):
        parsed = [(date(2026, 4, 1), 7066.10)]
        rows, _reasons = agg._select_new_imports_rows(
            parsed, existing_as_of=set(), today=TODAY,
        )
        assert rows[0] == {
            "metric_id": "imports_usd_mn_monthly", "as_of": "2026-04-01",
            "value": 7066.10, "source": "bb_mei_imports_cf", "source_as_of": "2026-04-01",
        }


# ---------------------------------------------------------------------------
# Integration: the imports sub-path inside _write_macro_monthly_append
# ---------------------------------------------------------------------------


class TestImportsSubPath:
    def _cpi_daily_row(self, value, as_of):
        return [{"metric_id": "x", "value": value, "as_of": as_of.isoformat(),
                  "source": "econdelta", "ingested_at": f"{as_of.isoformat()}T00:00:00+00:00"}]

    def _silence_cpi_and_remittance(self, monkeypatch):
        import utils.supabase_reader as reader

        monkeypatch.setattr(reader, "get_metric_history", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))

    def test_writes_new_month_when_splice_check_passes(self, monkeypatch, tmp_path):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        self._silence_cpi_and_remittance(monkeypatch)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._IMPORTS_MONTHLY_ID:
                return [{"metric_id": metric_id, "as_of": "2026-03-01", "value": 5826.2}]
            return []

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: MEI_FIXTURE)

        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append(today=TODAY)
        by_as_of = {r["as_of"]: r["value"] for r in captured if r["metric_id"] == agg._IMPORTS_MONTHLY_ID}
        assert by_as_of["2026-04-01"] == pytest.approx(7066.10)
        assert by_as_of["2026-05-01"] == pytest.approx(6108.22)
        assert n == len(captured)

    def test_splice_check_failure_writes_nothing_and_notifies(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        self._silence_cpi_and_remittance(monkeypatch)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._IMPORTS_MONTHLY_ID:
                # Seeded DB value deliberately far from the fixture's real
                # March reading (5826.22) -- must refuse the WHOLE leg.
                return [{"metric_id": metric_id, "as_of": "2026-03-01", "value": 1234.0}]
            return []

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: MEI_FIXTURE)
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: pytest.fail("splice check failed -- must not write") if any(
                r["metric_id"] == agg._IMPORTS_MONTHLY_ID for r in rows
            ) else 0,
        )

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        agg._write_macro_monthly_append(today=TODAY)
        assert any("splice check failed" in title for _level, title in notify_calls)

    def test_fetch_failure_is_isolated_and_notifies(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        self._silence_cpi_and_remittance(monkeypatch)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", lambda: (_ for _ in ()).throw(FetchError("boom")))
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: 0)

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0
        assert any("imports fetch/parse failed" in title for _level, title in notify_calls)

    def test_existing_rows_read_failure_is_isolated(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer
        from utils.supabase_reader import SupabaseReadError

        self._silence_cpi_and_remittance(monkeypatch)

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._IMPORTS_MONTHLY_ID:
                raise SupabaseReadError("boom")
            return []

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            agg, "_fetch_imports_mei_pdf",
            lambda: pytest.fail("must not fetch when the existing-rows read failed"),
        )
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: 0)

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0
        assert any("imports read failed" in title for _level, title in notify_calls)

    # --- M5 (Opus review round 1): mirror remittance's M6 skip-fetch gate --

    def test_m5_fetch_skipped_when_previous_month_already_present(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        self._silence_cpi_and_remittance(monkeypatch)
        prev_month = agg._previous_month_start(TODAY)  # 2026-07-01

        def get_metric_history_monthly_dispatch(metric_id, **kwargs):
            if metric_id == agg._IMPORTS_MONTHLY_ID:
                return [{"metric_id": metric_id, "as_of": prev_month.isoformat(), "value": 6270.46}]
            return []

        monkeypatch.setattr(reader, "get_metric_history_monthly", get_metric_history_monthly_dispatch)
        monkeypatch.setattr(
            agg, "_fetch_imports_mei_pdf",
            lambda: pytest.fail("M5: fetch must be skipped when the previous month already exists"),
        )
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: 0)
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0

    def test_m5_fetch_attempted_when_previous_month_missing(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        self._silence_cpi_and_remittance(monkeypatch)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])

        fetch_calls = {"n": 0}

        def counted_fetch():
            fetch_calls["n"] += 1
            raise FetchError("unreachable")

        monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", counted_fetch)
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: 0)
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        agg._write_macro_monthly_append(today=TODAY)
        assert fetch_calls["n"] == 1


# ---------------------------------------------------------------------------
# M2 growth monthly leg
# ---------------------------------------------------------------------------


class TestM2MonthlyAppendRows:
    def test_writes_when_month_end_and_closed(self):
        rows, reasons = agg._m2_monthly_append_rows(
            m2_row=(11.11, date(2026, 6, 30)), existing_pairs=set(), today=TODAY,
        )
        assert reasons == []
        assert rows == [{
            "metric_id": "m2_growth_yoy_monthly", "as_of": "2026-06-01", "value": 11.11,
            "source": "econdelta_daily_m2", "source_as_of": "2026-06-30",
        }]

    def test_no_daily_row_is_skipped(self):
        rows, reasons = agg._m2_monthly_append_rows(m2_row=None, existing_pairs=set(), today=TODAY)
        assert rows == []
        assert any("no daily" in r for r in reasons)

    def test_non_month_end_as_of_is_skipped(self):
        rows, reasons = agg._m2_monthly_append_rows(
            m2_row=(11.11, date(2026, 6, 15)), existing_pairs=set(), today=TODAY,
        )
        assert rows == []
        assert any("not a month-end" in r for r in reasons)

    def test_current_open_month_is_rejected(self):
        rows, reasons = agg._m2_monthly_append_rows(
            m2_row=(11.11, date(2026, 8, 31)), existing_pairs=set(), today=date(2026, 8, 31),
        )
        assert rows == []
        assert any("CURRENT" in r for r in reasons)

    @pytest.mark.parametrize("bad_value", [-10.01, 40.01])
    def test_range_check(self, bad_value):
        rows, reasons = agg._m2_monthly_append_rows(
            m2_row=(bad_value, date(2026, 6, 30)), existing_pairs=set(), today=TODAY,
        )
        assert rows == []
        assert any("outside" in r for r in reasons)

    def test_append_only_skips_existing(self):
        rows, reasons = agg._m2_monthly_append_rows(
            m2_row=(11.11, date(2026, 6, 30)),
            existing_pairs={("m2_growth_yoy_monthly", date(2026, 6, 1))},
            today=TODAY,
        )
        assert rows == []
        assert reasons == []


class TestM2SubPath:
    def test_m2_writes_alongside_cpi(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer

        def fake_get_metric_history(metric_id, *, days, **kwargs):
            if metric_id == agg._M2_DAILY_ID:
                return [{"metric_id": metric_id, "value": 11.11, "as_of": "2026-06-30",
                          "source": "econdelta", "ingested_at": "2026-06-30T00:00:00+00:00"}]
            return []

        monkeypatch.setattr(reader, "get_metric_history", fake_get_metric_history)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))

        captured = []
        monkeypatch.setattr(
            writer, "upsert_metric_history_monthly",
            lambda rows, **k: (captured.extend(rows), len(rows))[1],
        )
        monkeypatch.setattr(agg, "notify", lambda *a, **k: True)

        agg._write_macro_monthly_append(today=TODAY)
        ids = {r["metric_id"] for r in captured}
        assert "m2_growth_yoy_monthly" in ids

    def test_m2_read_failure_is_isolated(self, monkeypatch):
        import utils.supabase_reader as reader
        import utils.supabase_writer as writer
        from utils.supabase_reader import SupabaseReadError

        def fake_get_metric_history(metric_id, *, days, **kwargs):
            if metric_id == agg._M2_DAILY_ID:
                raise SupabaseReadError("boom")
            return []

        monkeypatch.setattr(reader, "get_metric_history", fake_get_metric_history)
        monkeypatch.setattr(reader, "get_metric_history_monthly", lambda *a, **k: [])
        monkeypatch.setattr(agg, "_fetch_remittance_html", lambda: (_ for _ in ()).throw(FetchError("x")))
        monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda *a, **k: 0)

        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        n = agg._write_macro_monthly_append(today=TODAY)
        assert n == 0
        assert any("M2 read failed" in title for _level, title in notify_calls)


# ---------------------------------------------------------------------------
# Daily yield-curve derivation from auction_results (landmine 49 fix)
# ---------------------------------------------------------------------------


class TestDailyYieldsFromAuctionRows:
    def test_5y_returns_cutoff_never_standard(self):
        """The exact regression this leg exists to fix: tbond_5y_yield must
        return the CUT-OFF yield (9.3496), never the Standard/Devolvement
        column's 9.15 the old html_table_row/LLM path shipped."""
        rows = [
            {"tenor": "5y", "auction_date": "2026-08-12", "cutoff": 9.3496},
        ]
        values, source_as_of = agg._daily_yields_from_auction_rows(rows)
        assert values["tbond_5y_yield"] == pytest.approx(9.3496)
        assert values["tbond_5y_yield"] != pytest.approx(9.15)
        assert source_as_of["tbond_5y_yield"] == date(2026, 8, 12)

    def test_10y_post_cut_repricing(self):
        rows = [{"tenor": "10y", "auction_date": "2026-08-19", "cutoff": 9.234}]
        values, _ = agg._daily_yields_from_auction_rows(rows)
        assert values["tbond_10y_yield"] == pytest.approx(9.234)

    def test_takes_the_first_newest_first_row_per_tenor(self):
        rows = [
            {"tenor": "91d", "auction_date": "2026-08-17", "cutoff": 9.0285},
            {"tenor": "91d", "auction_date": "2026-07-01", "cutoff": 8.5},  # older, must be ignored
        ]
        values, source_as_of = agg._daily_yields_from_auction_rows(rows)
        assert values["bill_bond_rates"] == pytest.approx(9.0285)
        assert source_as_of["bill_bond_rates"] == date(2026, 8, 17)

    def test_unmapped_tenor_is_ignored(self):
        rows = [{"tenor": "20y", "auction_date": "2026-07-29", "cutoff": 10.40}]
        values, _ = agg._daily_yields_from_auction_rows(rows)
        assert values == {}

    def test_out_of_range_cutoff_is_dropped(self):
        rows = [{"tenor": "182d", "auction_date": "2026-08-17", "cutoff": 999.0}]
        values, _ = agg._daily_yields_from_auction_rows(rows)
        assert "tbill_182d_yield" not in values

    def test_missing_or_malformed_fields_are_skipped_not_crashed(self):
        rows = [
            {"tenor": "364d", "auction_date": None, "cutoff": 9.17},
            {"tenor": "5y", "auction_date": "2026-08-12", "cutoff": "not-a-number"},
        ]
        values, _ = agg._daily_yields_from_auction_rows(rows)
        assert values == {}

    def test_all_five_ids_independent_partial_result(self):
        """Only SOME tenors having data is fine -- unlike the monthly
        ladder, this is NOT all-or-nothing (module docstring)."""
        rows = [{"tenor": "91d", "auction_date": "2026-08-17", "cutoff": 9.0285}]
        values, _ = agg._daily_yields_from_auction_rows(rows)
        assert set(values) == {"bill_bond_rates"}


class TestDeriveDailyYieldsFromAuctions:
    def test_read_failure_returns_empty_and_notifies(self, monkeypatch):
        import utils.supabase_reader as reader
        from utils.supabase_reader import SupabaseReadError

        def raise_read(*a, **k):
            raise SupabaseReadError("boom")

        monkeypatch.setattr(reader, "get_auction_results_through", raise_read)
        notify_calls = []
        monkeypatch.setattr(agg, "notify", lambda level, title, msg, **k: notify_calls.append((level, title)))

        values, source_as_of = agg._derive_daily_yields_from_auctions(today=TODAY)
        assert values == {}
        assert source_as_of == {}
        assert any("auction_results read failed" in title for _level, title in notify_calls)

    def test_success_path_returns_values_and_dates(self, monkeypatch):
        import utils.supabase_reader as reader

        monkeypatch.setattr(
            reader, "get_auction_results_through",
            lambda as_of, **k: [{"tenor": "5y", "auction_date": "2026-08-12", "cutoff": 9.3496}],
        )
        values, source_as_of = agg._derive_daily_yields_from_auctions(today=TODAY)
        assert values == {"tbond_5y_yield": pytest.approx(9.3496)}
        assert source_as_of == {"tbond_5y_yield": date(2026, 8, 12)}


def test_main_merges_derived_yields_into_data_and_source_as_of_map(tmp_path, monkeypatch):
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")

    import utils.supabase_reader as reader
    import utils.supabase_writer as sw

    monkeypatch.setattr(
        reader, "get_auction_results_through",
        lambda as_of, **k: [{"tenor": "5y", "auction_date": "2026-08-12", "cutoff": 9.3496}],
    )

    captured_data = {}

    def fake_upsert_metric_history(*, data, as_of, source_as_of_map=None, **k):
        captured_data.update(data)
        captured_data["__source_as_of_map__"] = dict(source_as_of_map or {})
        return len(data)

    monkeypatch.setattr(sw, "upsert_metric_history", fake_upsert_metric_history)
    monkeypatch.setattr(sw, "verify_landed_count", lambda *a, **k: None)
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_history_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_monthly", lambda *a, **k: 0)
    monkeypatch.setattr(agg, "_write_macro_monthly_append", lambda: 0)
    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", lambda: 0)

    exit_code = agg.main()
    assert exit_code == 0
    assert captured_data.get("tbond_5y_yield") == pytest.approx(9.3496)
    assert captured_data["__source_as_of_map__"].get("tbond_5y_yield") == date(2026, 8, 12)
    # H3 (Opus review round 1): the brief-facing alias (tbond_bond_5y ->
    # tbond_5y_yield) must carry the SAME real auction date, not fall
    # through to upsert_metric_history's as_of=today forgery fallback.
    assert captured_data.get("tbond_bond_5y") == pytest.approx(9.3496)
    assert captured_data["__source_as_of_map__"].get("tbond_bond_5y") == date(2026, 8, 12)


# ---------------------------------------------------------------------------
# Build-brief item 2 verification: _cpi_monthly_append_rows (pre-existing,
# UNCHANGED by this PR) produces the correct July 2026 rows once
# general_inflation/point_to_point_inflation are fed from the new HTML
# source's real month-end vintage. food_inflation/non_food_inflation are
# NOT repointed in this PR (still MEI-PDF-sourced) -- this test documents
# that the appender's EXISTING guards (month-end vintage check, closed-
# month check, equality guard) all pass cleanly once the daily rows carry
# a genuine July source_as_of, regardless of which parser produced them.
# ---------------------------------------------------------------------------


def test_cpi_monthly_append_rows_produces_july_2026_given_the_new_daily_values():
    rows, reasons = agg._cpi_monthly_append_rows(
        general_row=(8.66, date(2026, 7, 31)),   # general_inflation, now from econdata/inflation
        food_row=(7.16, date(2026, 7, 31)),       # food_inflation, still MEI-PDF-sourced once it lands
        nonfood_row=(9.28, date(2026, 7, 31)),    # non_food_inflation, same
        p2p_row=(8.32, date(2026, 7, 31)),        # point_to_point_inflation, now from econdata/inflation
        existing_pairs=set(),
        today=date(2026, 8, 22),
    )
    assert reasons == []
    by_id = {r["metric_id"]: r for r in rows}
    assert by_id["cpi_12m_avg_monthly"]["value"] == pytest.approx(8.66)
    assert by_id["cpi_12m_avg_monthly"]["as_of"] == "2026-07-01"
    assert by_id["cpi_p2p_food_monthly"]["value"] == pytest.approx(7.16)
    assert by_id["cpi_p2p_nonfood_monthly"]["value"] == pytest.approx(9.28)


def test_main_skips_yield_derivation_when_supabase_disabled(tmp_path, monkeypatch):
    """tests/conftest.py's default -- the whole test suite must never make
    a real auction_results read unless a test explicitly opts in."""
    from tests.test_aggregator import _build_data_tree

    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")

    def must_not_be_called(*a, **k):
        pytest.fail("auction_results must not be read when Supabase is disabled")

    monkeypatch.setattr(agg, "_derive_daily_yields_from_auctions", must_not_be_called)

    exit_code = agg.main()
    assert exit_code == 0
