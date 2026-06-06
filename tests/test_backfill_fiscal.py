"""Tests for the one-time fiscal backfill (govt bank borrowing + NBR revenue,
ADP completion %).

Two layers:
  * Pure-transform tests (no I/O): month_end, fiscal_year_start, row builders,
    the FYTD-diff self-check, discovery filtering.
  * Parser tests against REAL cached MFR PDFs (Jul/Aug/Sep/Oct 2025). These
    fixtures live in this scratch dir; they were downloaded from the MOF Oracle
    CDN during the build and are the empirical ground truth. Tests skip
    gracefully if a fixture PDF is absent.

Run:
  PYTHONPATH=/Users/adnanrashid/Projects/clauding-lab/econdelta \
  /Users/adnanrashid/Projects/clauding-lab/econdelta/.venv/bin/python -m pytest \
  /tmp/backfill-build/F7_fiscal/test_backfill_fiscal.py -v
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import scripts.backfill_fiscal as bf
import scripts.mfr_parser as mfr

HERE = Path(__file__).resolve().parent

# Real fixture PDFs and their expected single-month + FYTD values (BDT crore),
# verified by hand against the printed tables during the build.
FIXTURES = {
    "c513580c-f220-49aa-9305-605585b84180.pdf": {  # October 2025
        "ym": (2025, 10),
        "borrow_single": 5720.0, "borrow_fytd": 7570.0,
        "nbr_single": 28027.0, "nbr_fytd": 117420.0,
    },
    "6989e04d-56ff-44f2-be6c-42efbfbaf803.pdf": {  # August 2025 (wrapped label)
        "ym": (2025, 8),
        "borrow_single": -6289.0, "borrow_fytd": -3427.0,
        "nbr_single": 26643.0, "nbr_fytd": 53525.0,
    },
    "45d7e589-b9d2-42f4-b256-ba5f6e461414.pdf": {  # July 2025 (FY first month)
        "ym": (2025, 7),
        "borrow_single": 2862.0,
        "nbr_single": 26882.0,
    },
}


def _fixture_path(name: str) -> Path:
    # PDFs are cached under _pdfs/ by a prior dry-run, or sit alongside tests.
    for cand in (HERE / "_pdfs" / name, HERE / name):
        if cand.exists():
            return cand
    pytest.skip(f"fixture PDF {name} not present")


# ---------------------------------------------------------------------------
# Pure transforms
# ---------------------------------------------------------------------------


class TestMonthEnd:
    def test_returns_last_day_of_31_day_month(self):
        assert bf.month_end(2025, 10) == date(2025, 10, 31)

    def test_returns_last_day_of_30_day_month(self):
        assert bf.month_end(2025, 9) == date(2025, 9, 30)

    def test_handles_february_non_leap(self):
        assert bf.month_end(2025, 2) == date(2025, 2, 28)

    def test_handles_december_year_boundary(self):
        assert bf.month_end(2025, 12) == date(2025, 12, 31)


class TestFiscalYearStart:
    def test_fy25_starts_july_2024(self):
        # 'FY25' ends June 2025 -> starts 1 July 2024.
        assert bf.fiscal_year_start(2025) == date(2024, 7, 1)

    def test_fy21_starts_july_2020(self):
        assert bf.fiscal_year_start(2021) == date(2020, 7, 1)


class TestFiscalYearOf:
    def test_july_is_first_month_of_next_fy(self):
        assert bf.fiscal_year_of(2025, 7) == 2026

    def test_december_is_same_fy_as_following_june(self):
        assert bf.fiscal_year_of(2024, 12) == 2025

    def test_june_is_last_month_of_its_fy(self):
        assert bf.fiscal_year_of(2025, 6) == 2025

    def test_october_2025_is_fy26(self):
        assert bf.fiscal_year_of(2025, 10) == 2026


class TestFyAnchorTables:
    def test_fy26_anchors_match_known_values(self):
        assert bf.FY_BORROW_BUDGET[2026] == 104000.0
        assert bf.FY_NBR_BUDGET[2026] == 499001.0

    def test_fy25_and_fy24_anchors_present(self):
        assert bf.FY_BORROW_BUDGET[2025] == 137500.0
        assert bf.FY_NBR_BUDGET[2025] == 480000.0
        assert bf.FY_BORROW_BUDGET[2024] == 132395.0
        assert bf.FY_NBR_BUDGET[2024] == 430000.0


class TestBuildMonthlyRow:
    def test_row_uses_month_end_as_of(self):
        row = bf.build_monthly_row(bf.METRIC_NBR, 2025, 10, 28027.0)
        assert row["metric_id"] == bf.METRIC_NBR
        assert row["as_of"] == "2025-10-31"
        assert row["source_as_of"] == "2025-10-31"
        assert row["value"] == 28027.0
        assert row["source"] == bf.DEFAULT_SOURCE


class TestSelfCheckFytd:
    def _mk(self, y, m, b_single, b_fytd, n_single=0.0, n_fytd=0.0):
        return bf.ParsedMfr(y, m, "url", b_single, b_fytd, n_single, n_fytd)

    def test_consecutive_months_within_tolerance_no_warning(self):
        # Aug single == Aug_fytd - Jul_fytd exactly.
        series = {
            (2025, 7): self._mk(2025, 7, 2862.0, 2862.0),
            (2025, 8): self._mk(2025, 8, -6289.0, -3427.0),
        }
        assert bf.self_check_fytd(series, "borrow") == []

    def test_divergent_month_emits_warning(self):
        # Aug single claims 100 but FYTD diff is 2000 -> way over 5%.
        series = {
            (2025, 7): self._mk(2025, 7, 500.0, 500.0),
            (2025, 8): self._mk(2025, 8, 100.0, 2500.0),
        }
        warnings = bf.self_check_fytd(series, "borrow")
        assert len(warnings) == 1
        assert "2025-08" in warnings[0]

    def test_july_is_skipped(self):
        # July is FY first month; no prior month to diff against.
        series = {(2025, 7): self._mk(2025, 7, 2862.0, 9999.0)}
        assert bf.self_check_fytd(series, "borrow") == []

    def test_gap_month_is_skipped(self):
        # Oct present, Sep missing -> cannot cross-check Oct.
        series = {
            (2025, 8): self._mk(2025, 8, -6289.0, -3427.0),
            (2025, 10): self._mk(2025, 10, 5720.0, 7570.0),
        }
        assert bf.self_check_fytd(series, "borrow") == []


class TestDiscoveryFiltering:
    def test_keeps_only_oracle_office_mof_pdfs(self):
        fake = {
            "links": [
                "https://mof.gov.bd/pages/static-pages/abc",  # not a PDF
                f"https://{bf.ORACLE_CDN_HOST}/n/x/b/V2Ministry/o/office-mof/2026/3/a.pdf",
                f"https://{bf.ORACLE_CDN_HOST}/n/x/b/V2Ministry/o/office-imed/2024/12/b.pdf",  # imed, not mof
                "https://example.com/report.pdf",  # wrong host
                f"https://{bf.ORACLE_CDN_HOST}/n/x/b/V2Ministry/o/office-mof/2026/0/c.pdf",
            ]
        }
        out = bf.discover_mfr_pdf_links(scrape_fn=lambda _u: fake)
        assert out == [
            f"https://{bf.ORACLE_CDN_HOST}/n/x/b/V2Ministry/o/office-mof/2026/3/a.pdf",
            f"https://{bf.ORACLE_CDN_HOST}/n/x/b/V2Ministry/o/office-mof/2026/0/c.pdf",
        ]

    def test_dedupes_repeated_links(self):
        dup = f"https://{bf.ORACLE_CDN_HOST}/n/x/b/V2Ministry/o/office-mof/2026/3/a.pdf"
        out = bf.discover_mfr_pdf_links(scrape_fn=lambda _u: {"links": [dup, dup]})
        assert out == [dup]


# ---------------------------------------------------------------------------
# Real-PDF parser tests (empirical ground truth)
# ---------------------------------------------------------------------------


class TestMfrParserAgainstRealPdfs:
    @pytest.mark.parametrize("name", list(FIXTURES))
    def test_report_month_detected(self, name):
        path = _fixture_path(name)
        assert mfr.parse_report_month(str(path)) == FIXTURES[name]["ym"]

    @pytest.mark.parametrize("name", list(FIXTURES))
    def test_bank_borrowing_single_month(self, name):
        path = _fixture_path(name)
        row = mfr.parse_bank_borrowing(str(path), fy_budget_crore=bf.FY_BORROW_BUDGET[2026])
        assert row.single_month == FIXTURES[name]["borrow_single"]

    @pytest.mark.parametrize("name", list(FIXTURES))
    def test_nbr_revenue_single_month(self, name):
        path = _fixture_path(name)
        row = mfr.parse_nbr_revenue(str(path), fy_budget_crore=bf.FY_NBR_BUDGET[2026])
        assert row.single_month == FIXTURES[name]["nbr_single"]

    def test_oct_fytd_values(self):
        # FYTD only meaningful for non-July months; check October directly.
        name = "c513580c-f220-49aa-9305-605585b84180.pdf"
        path = _fixture_path(name)
        b = mfr.parse_bank_borrowing(str(path), fy_budget_crore=bf.FY_BORROW_BUDGET[2026])
        n = mfr.parse_nbr_revenue(str(path), fy_budget_crore=bf.FY_NBR_BUDGET[2026])
        assert b.fytd == FIXTURES[name]["borrow_fytd"]
        assert n.fytd == FIXTURES[name]["nbr_fytd"]


class TestEndToEndDryRunConsistency:
    """The whole point of the FYTD self-check: when we parse a real run of
    consecutive months, every non-July single-month must reconcile with the
    FYTD difference. This is the business rule (single-month is a TRUE MoM
    figure, not an FYTD diff)."""

    def test_consecutive_months_reconcile(self):
        parsed = {}
        for name in FIXTURES:
            path = _fixture_path(name)
            pm = bf.parse_one_mfr(str(path), "url://" + name)
            parsed[(pm.year, pm.month)] = pm
        # With Jul/Aug/Sep/Oct present, both series must produce zero warnings.
        assert bf.self_check_fytd(parsed, "borrow") == []
        assert bf.self_check_fytd(parsed, "nbr") == []
