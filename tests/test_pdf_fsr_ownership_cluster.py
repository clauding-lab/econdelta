"""Unit tests for parsers.pdf_fsr_ownership_cluster (S10).

The pure helpers (cluster-token parsing, ownership-label matching, row number
extraction, quarter-end recovery, table-walking) are exercised on synthetic
fixtures so they run without a real PDF.

The full ``parse()`` path is exercised against a small PDF generated in-process
with reportlab (available in the venv) — it carries a 4-row ownership table so
``pdfplumber.extract_tables`` returns a real grid. This is the synthetic FSR
fixture the plan asks for; the live FSR fetch/parse is VPS-deferred.
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from fetchers.base import FetchResult
from parsers.base import ParseError


@pytest.fixture(scope="module")
def mod():
    """pdfplumber/reportlab are lazy-imported inside the parser, so importing
    the module here is dependency-free."""
    return importlib.import_module("parsers.pdf_fsr_ownership_cluster")


# --- pure helper: cluster-token parsing -----------------------------------

def test_parse_cluster_token_reads_npl(mod):
    assert mod._parse_cluster_token("cluster=npl page=13") == "npl"


def test_parse_cluster_token_reads_deposits(mod):
    assert mod._parse_cluster_token("read cluster=deposits unit=crore") == "deposits"


def test_parse_cluster_token_missing_raises(mod):
    with pytest.raises(ParseError):
        mod._parse_cluster_token("no cluster token here")


# --- pure helper: ownership-label matching --------------------------------

@pytest.mark.parametrize(
    "label, expected",
    [
        ("State-Owned Commercial Banks", "socb"),
        ("State Owned Banks (SOCB)", "socb"),
        ("Private Commercial Banks", "pcb"),
        ("Foreign Commercial Banks (FCB)", "fcb"),
        ("Specialised Banks", "specialised"),
        ("Specialized Banks", "specialised"),
        ("Development Financial Institutions", "specialised"),
        ("All Banks", None),
        ("Total", None),
    ],
)
def test_which_segment(mod, label, expected):
    assert mod._which_segment(label) == expected


def test_which_segment_handles_multiline_label(mod):
    assert mod._which_segment("State-Owned\nCommercial   Banks") == "socb"


# --- pure helper: row number extraction (right-most = latest) -------------

def test_last_number_picks_rightmost(mod):
    # Periods run left->right with the latest quarter last.
    assert mod._last_number(["18.2", "19.5", "20.8"]) == 20.8


def test_last_number_strips_commas(mod):
    assert mod._last_number(["1,23,456.7"]) == 123456.7


def test_last_number_none_when_no_number(mod):
    assert mod._last_number(["", "n/a", "-"]) is None


# --- pure helper: quarter-end recovery ------------------------------------

def test_extract_as_of_quarter_ending(mod):
    assert mod._extract_as_of("Quarter ending 30 September 2025") == date(2025, 9, 30)


def test_extract_as_of_as_on_no_day_uses_quarter_end(mod):
    # "as on June 2025" -> 30 June.
    assert mod._extract_as_of("Position as on June 2025") == date(2025, 6, 30)


def test_extract_as_of_unrecoverable(mod):
    assert mod._extract_as_of("Financial Stability Report") is None


# --- pure helper: table walking -------------------------------------------

_NPL_TABLE = [
    ["Bank Type", "Jun-24", "Mar-25", "Jun-25"],
    ["State-Owned Commercial Banks", "40.1", "42.0", "44.7"],
    ["Private Commercial Banks", "7.8", "8.5", "9.3"],
    ["Foreign Commercial Banks", "4.1", "4.6", "5.0"],
    ["Specialised Banks", "12.0", "12.5", "13.1"],
    ["All Banks", "11.0", "11.8", "12.5"],
]


def test_extract_from_tables_npl_cluster(mod):
    out = mod._extract_from_tables([_NPL_TABLE])
    assert out == {"socb": 44.7, "pcb": 9.3, "fcb": 5.0, "specialised": 13.1}


def test_extract_from_tables_first_match_wins(mod):
    # A second SOCB row later in the doc must not overwrite the first.
    table2 = [["State-Owned Commercial Banks", "99.9"]]
    out = mod._extract_from_tables([_NPL_TABLE, table2])
    assert out["socb"] == 44.7


# --- full parse() against a reportlab-generated PDF -----------------------

def _build_fsr_pdf(path: Path) -> None:
    """Render a minimal FSR-like PDF with a 4-row deposits-by-ownership table."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    table_data = [
        ["Bank Type", "Jun-24", "Jun-25"],
        ["State-Owned Commercial Banks", "350000", "365000"],
        ["Private Commercial Banks", "1100000", "1180000"],
        ["Foreign Commercial Banks", "55000", "58000"],
        ["Specialised Banks", "42000", "44000"],
    ]
    table = Table(table_data)
    # pdfplumber's table detection is line-based — a borderless table is not
    # found, so draw a grid (an FSR ownership table is always ruled).
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    story = [
        Paragraph("Financial Stability Report — Quarter ending 30 June 2025", styles["Title"]),
        Paragraph("Deposits by bank ownership (BDT crore)", styles["Normal"]),
        table,
    ]
    doc.build(story)


@pytest.fixture
def deposits_pdf(tmp_path: Path, mod) -> FetchResult:
    pytest.importorskip("reportlab")
    pdf_path = tmp_path / "fsr.pdf"
    _build_fsr_pdf(pdf_path)
    return FetchResult(
        indicator_id="deposits_by_ownership",
        artifact_path=pdf_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/fsr.pdf",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_parse_returns_four_key_dict(mod, deposits_pdf):
    parser = mod.PdfFsrOwnershipClusterParser()
    result = parser.parse(deposits_pdf, instruction="cluster=deposits")
    assert result.value == {
        "socb": 365000.0,
        "pcb": 1180000.0,
        "fcb": 58000.0,
        "specialised": 44000.0,
    }
    assert result._parse_strategy == "pdf_fsr_ownership_cluster"


def test_parse_recovers_source_as_of(mod, deposits_pdf):
    parser = mod.PdfFsrOwnershipClusterParser()
    result = parser.parse(deposits_pdf, instruction="cluster=deposits")
    assert result.source_as_of == date(2025, 6, 30)


def test_parse_bad_cluster_token_raises(mod, deposits_pdf):
    parser = mod.PdfFsrOwnershipClusterParser()
    with pytest.raises(ParseError):
        parser.parse(deposits_pdf, instruction="cluster=bogus")
