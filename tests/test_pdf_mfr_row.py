"""Tests for the MoF MFR row parser (daily-pipeline registry).

Covers the Non-NBR-tax revenue use case (S3): row-label anchoring + the
FY-budget anchor that disambiguates the month vs FYTD columns on the MFR's
variable-position grid, plus the null/ParseError fall-through paths the hybrid
orchestrator relies on.

We render a synthetic MFR Table-4 (Revenue) page as PRE-FORMATTED text so
``pdfplumber.extract_text()`` reproduces the line layout the real MFR has — the
parser reads text lines, NOT ``extract_tables()`` (which mangles these grids,
the documented reason ``scripts/mfr_parser.py`` exists).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Preformatted, SimpleDocTemplate

import parsers.pdf_mfr_row  # noqa: F401  (registers the parser)
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

# Synthetic Table-4 (Revenue) layout. Each revenue row, after the row label,
# carries: Budget FY25 | Revised FY25 | Act FY25(month) | Act FY25(FYTD) |
# Act FY25(full yr) | Budget FY26 | Act FY26(month) | Act FY26(FYTD).
# The FY26 annual budget is the stable anchor; the month figure follows it and
# the FYTD figure follows the month.
#
#   row "b. Non-NBR Tax": ... Budget FY26 = 18,000 | month = 1,250 | FYTD = 6,480
#   row "a. NBR":         ... Budget FY26 = 499,001 | month = 28,027 | FYTD = 117,420
_MFR_TABLE4_TEXT = """Table 4: Revenue (Taka in Crore)
Budget Revised Actual Actual Actual Budget Actual Actual
FY25 FY25 FY25(m) FY25(ytd) FY25(yr) FY26 FY26(m) FY26(ytd)
a. NBR 480000 470000 26000 110000 460000 499001 28027 117420
b. Non-NBR Tax 16500 16000 1100 5800 16200 18000 1250 6480
Non-Tax Revenue 45000 44000 3000 14000 43000 47000 3200 15100
"""


@pytest.fixture
def mfr_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "mfr.pdf"
    doc = SimpleDocTemplate(str(pdf_path))
    style = getSampleStyleSheet()["Code"]
    doc.build([Preformatted(_MFR_TABLE4_TEXT, style)])
    return FetchResult(
        indicator_id="non_nbr_tax_revenue", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x" * 64, cache_hit=False,
    )


class TestNonNbrTaxFytd:
    def test_returns_fytd_figure_anchored_on_fy26_budget(self, mfr_artifact):
        # The business rule: the donut wants the FYTD Non-NBR-tax figure, found
        # two cells after the FY26 annual-budget anchor (18000 -> 1250 -> 6480).
        p = get_parser("pdf_mfr_row")
        r = p.parse(mfr_artifact, instruction="row=Non-NBR col=fytd anchor=18000")
        assert r.value == 6480.0
        assert r._parse_strategy == "pdf_mfr_row"

    def test_returns_single_month_figure_when_col_month(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        r = p.parse(mfr_artifact, instruction="row=Non-NBR col=month anchor=18000")
        assert r.value == 1250.0

    def test_defaults_to_fytd_when_col_omitted(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        r = p.parse(mfr_artifact, instruction="row=Non-NBR anchor=18000")
        assert r.value == 6480.0

    def test_does_not_confuse_non_nbr_with_nbr_or_non_tax(self, mfr_artifact):
        # Verifies the row anchor selects the right leg, not the adjacent NBR
        # (117420) or Non-Tax (15100) rows. This is the semantic-trap guard.
        p = get_parser("pdf_mfr_row")
        nbr = p.parse(mfr_artifact, instruction="row=a. NBR col=fytd anchor=499001")
        assert nbr.value == 117420.0
        non_nbr = p.parse(mfr_artifact, instruction="row=Non-NBR col=fytd anchor=18000")
        assert non_nbr.value == 6480.0
        assert non_nbr.value != nbr.value


class TestFallThroughToLlm:
    """Every failure must raise ParseError (never None) so hybrid falls to LLM."""

    def test_missing_row_token_raises(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        with pytest.raises(ParseError):
            p.parse(mfr_artifact, instruction="col=fytd anchor=18000")

    def test_missing_anchor_raises_so_llm_handles_it(self, mfr_artifact):
        # The shipped config leaves anchor=TODO_VPS_FILL... until the VPS run
        # confirms the real FY26 Non-NBR budget; until then the deterministic
        # parser must ParseError and let the LLM fallback do the extraction.
        p = get_parser("pdf_mfr_row")
        with pytest.raises(ParseError):
            p.parse(mfr_artifact, instruction="row=Non-NBR col=fytd")

    def test_non_numeric_anchor_placeholder_raises(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        with pytest.raises(ParseError):
            p.parse(
                mfr_artifact,
                instruction="row=Non-NBR col=fytd anchor=TODO_VPS_FILL_FY26_NON_NBR_BUDGET_CRORE",
            )

    def test_anchor_not_in_row_raises(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        with pytest.raises(ParseError):
            p.parse(mfr_artifact, instruction="row=Non-NBR col=fytd anchor=99999")

    def test_row_absent_from_pdf_raises(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        with pytest.raises(ParseError):
            p.parse(mfr_artifact, instruction="row=Customs Duty col=fytd anchor=18000")

    def test_bad_col_value_raises(self, mfr_artifact):
        p = get_parser("pdf_mfr_row")
        with pytest.raises(ParseError):
            p.parse(mfr_artifact, instruction="row=Non-NBR col=quarter anchor=18000")
