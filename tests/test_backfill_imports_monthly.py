"""Tests for scripts/backfill_imports_monthly.py -- the cross-check + row
build logic, and the CLI's --dry-run path (mocked fetch, no network, no
Supabase credentials needed). NEVER exercises the real --write path.

Unlike scripts/backfill_cpi_july_2026.py (pure hardcoded values), this
script RE-READS its two months' values from a live PDF fetch every run --
the tests here mock _fetch_imports_mei_pdf to point at the real committed
fixture (tests/_pdfs/bb_mei_2026_june.pdf) so the parse path is exercised
for real without a network call.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.backfill_imports_monthly import (
    DEFINITION_UPDATE,
    _cross_check,
    build_history_rows,
    run,
)

MEI_FIXTURE = Path(__file__).parent / "_pdfs" / "bb_mei_2026_june.pdf"


class TestCrossCheck:
    def test_passes_when_values_match(self):
        verified = _cross_check({date(2026, 4, 1): 7066.10, date(2026, 5, 1): 6108.22})
        assert verified == {date(2026, 4, 1): 7066.10, date(2026, 5, 1): 6108.22}

    def test_missing_month_is_skipped_gracefully_not_raised(self, caplog):
        """Opus review round 1, H1 (blocker): a missing month must NOT
        brick the script -- it's the expected shape once BB's fiscal year
        rolls and April/May move out of the PDF's provisional column
        (aggregate_latest._imports_splice_check's docstring has the full
        mechanism). Only a genuinely DRIFTED present value should raise."""
        verified = _cross_check({date(2026, 5, 1): 6108.22})  # April missing
        assert verified == {date(2026, 5, 1): 6108.22}
        assert any("no longer in the PDF" in r.message for r in caplog.records)

    def test_both_months_missing_returns_empty_not_raised(self):
        assert _cross_check({}) == {}

    def test_raises_when_value_drifted(self):
        with pytest.raises(AssertionError, match="cross-check failed"):
            _cross_check({date(2026, 4, 1): 7500.0, date(2026, 5, 1): 6108.22})

    def test_a_present_drifted_month_raises_even_if_the_other_is_missing(self):
        """A month that's actually there but WRONG is still a real problem
        worth failing loud on, independent of whichever other month has
        simply rolled out of the provisional column."""
        with pytest.raises(AssertionError, match="cross-check failed"):
            _cross_check({date(2026, 4, 1): 7500.0})  # May missing, April drifted


class TestBuildHistoryRows:
    def test_builds_exactly_april_and_may(self):
        rows = build_history_rows({date(2026, 4, 1): 7066.10, date(2026, 5, 1): 6108.22})
        by_as_of = {r["as_of"]: r for r in rows}
        assert set(by_as_of) == {"2026-04-01", "2026-05-01"}
        assert by_as_of["2026-04-01"]["value"] == pytest.approx(7066.10)
        assert by_as_of["2026-04-01"]["metric_id"] == "imports_usd_mn_monthly"
        assert by_as_of["2026-04-01"]["source"] == "bb_mei_imports_cf"

    def test_ignores_extra_months_in_the_parsed_dict(self):
        """Only April/May are backfilled by this script -- June onward is
        the live leg's job (aggregate_latest._write_macro_monthly_append)."""
        parsed = {
            date(2026, 3, 1): 5826.22, date(2026, 4, 1): 7066.10,
            date(2026, 5, 1): 6108.22, date(2026, 6, 1): 9999.0,
        }
        rows = build_history_rows(parsed)
        assert {r["as_of"] for r in rows} == {"2026-04-01", "2026-05-01"}

    def test_raises_on_cross_check_failure(self):
        with pytest.raises(AssertionError):
            build_history_rows({date(2026, 4, 1): 1.0, date(2026, 5, 1): 6108.22})

    def test_one_month_rolled_off_returns_the_other_alone_not_bricked(self):
        """The 'not brick' requirement, end-to-end: April has rolled out of
        the PDF's provisional column, May is still there and verifies --
        the script must still produce May's row, not crash."""
        rows = build_history_rows({date(2026, 5, 1): 6108.22})
        assert [r["as_of"] for r in rows] == ["2026-05-01"]

    def test_both_months_rolled_off_returns_empty_list_not_bricked(self):
        assert build_history_rows({date(2026, 7, 1): 6270.46}) == []


class TestDefinitionUpdate:
    def test_repoints_away_from_dead_site(self):
        assert "thenazmussakib" not in DEFINITION_UPDATE["source_url"]
        assert DEFINITION_UPDATE["source_url"] == (
            "https://www.bb.org.bd/en/index.php/publication/publictn/3/11"
        )

    def test_full_row_not_partial(self):
        required = {"metric_id", "display_name", "unit", "source_url", "domain"}
        assert required <= set(DEFINITION_UPDATE)


class TestDryRunCliAgainstRealFixture:
    def test_dry_run_parses_the_real_pdf_and_prints_the_verified_values(self, capsys):
        with patch(
            "scripts.backfill_imports_monthly._fetch_imports_mei_pdf",
            return_value=MEI_FIXTURE,
        ):
            exit_code = run(["--dry-run"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "2026-04-01" in out
        assert "7066.1" in out
        assert "2026-05-01" in out
        assert "6108.22" in out

    def test_dry_run_is_the_default(self, capsys):
        with patch(
            "scripts.backfill_imports_monthly._fetch_imports_mei_pdf",
            return_value=MEI_FIXTURE,
        ):
            exit_code = run([])
        assert exit_code == 0
        assert "DRY RUN" in capsys.readouterr().out

    def test_explicit_dry_run_flag_and_default_agree(self, capsys):
        """L3: --dry-run is purely documentation -- passing it explicitly
        or omitting it must behave identically (both are "not --write")."""
        with patch(
            "scripts.backfill_imports_monthly._fetch_imports_mei_pdf",
            return_value=MEI_FIXTURE,
        ):
            explicit = run(["--dry-run"])
            implicit = run([])
        assert explicit == implicit == 0

    def test_no_matching_table_raises_a_structural_error(self, tmp_path):
        """Different failure class from a rolled-off target month: no
        table on the page AT ALL matching the 'Custom based import (c&f)'
        header means the source structure itself changed -- that's a real
        problem worth surfacing loudly, unlike a month simply not being in
        the provisional column anymore."""
        import pdfplumber  # noqa: F401 -- fail fast if the dep is missing
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Table

        pdf_path = tmp_path / "no_match.pdf"
        doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
        doc.build([Table([["Unrelated", "Table"], ["a", "1"]])])

        with patch(
            "scripts.backfill_imports_monthly._fetch_imports_mei_pdf",
            return_value=pdf_path,
        ), pytest.raises(ValueError, match="no table"):
            run(["--dry-run"])

    def test_post_roll_shape_does_not_brick_the_script(self, capsys):
        """H1 end-to-end: a PDF whose table HAS the right header but no
        longer carries April/May in its provisional column (both rolled to
        'R', a real July-only reading in their place) must complete
        successfully with 0 rows, not raise. parse_imports_c_and_f_table
        is mocked directly (rather than built via a synthetic reportlab
        PDF, which needs explicit grid lines for pdfplumber's line-based
        table detection to find anything at all) -- the fetch is still
        mocked to the real fixture path just so _fetch_imports_mei_pdf
        itself is never exercised for real."""
        with patch(
            "scripts.backfill_imports_monthly._fetch_imports_mei_pdf",
            return_value=MEI_FIXTURE,
        ), patch(
            "scripts.backfill_imports_monthly.parse_imports_c_and_f_table",
            return_value=[(date(2026, 7, 1), 6270.46)],
        ):
            exit_code = run(["--dry-run"])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "0 total" in out
        assert "0 total" in out
