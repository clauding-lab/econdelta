from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table

import parsers.pdf_table_row  # noqa: F401
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "table.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table([["Tenor", "Outstanding"], ["91-day", "50000"], ["Total", "100000"]])
    doc.build([table])
    return FetchResult(
        indicator_id="x", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_extracts_total_row(pdf_artifact):
    p = get_parser("pdf_table_row")
    r = p.parse(pdf_artifact, instruction="page=1 table=1 row=Total col=2")
    assert r.value == 100_000.0


# ---------------------------------------------------------------------------
# Config-conversion batch 1 (2026-08-05): the 4 deficit-financing metrics,
# against the real captured fixture (tests/_pdfs/bb_mei_2026_june.pdf, page
# 16 / printed page 13, "C. Government deficit financing" grid table).
#
# Grammar note: `row=` is a SINGLE whitespace-free token (parsers/pdf_table_row
# ._parse_instruction tokenizes on plain .split(), so "row=July-May of FY 26"
# would silently truncate to just "July-May" — which is a substring of BOTH
# the FY25 and FY26 rows, and the FIRST match (FY25, stale) would win). "26"
# is the shortest token that appears in row[0] of the "July-May of FY 26" row
# and NO OTHER row[0] in this table (verified against the full page text —
# see PR body). This needs updating to "27" at the FY26->FY27 rollover
# (~2026-07); until then, a stale token fails CLOSED (ParseError -> safe LLM
# fallback), never silently reads the wrong row, because "26" simply stops
# matching anything once the row's own label changes.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
MEI_FIXTURE = REPO_ROOT / "tests" / "_pdfs" / "bb_mei_2026_june.pdf"


@pytest.fixture
def mei_artifact() -> FetchResult:
    return FetchResult(
        indicator_id="x", artifact_path=MEI_FIXTURE, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd//pub/monthly/selectedecooind/2026_june.pdf",
        sha256="30f593863230aaa744d61652f8c8a11f198a06541bfcbf5b4fb7a81a82354b8f",
        cache_hit=False,
    )


@pytest.mark.parametrize(
    "col,expected",
    [
        (2, 94158.90),   # bank_borrowing_for_deficit_financing
        (3, -567.67),    # non_bank_borrowing_for_deficit_financing (legitimately negative)
        (4, 93591.23),   # domestic_borrowing_for_budget_deficit
        (5, 21944.28),   # foreign_borrowing_for_budget_deficit
    ],
)
def test_deficit_financing_columns_against_real_fixture(mei_artifact, col, expected):
    p = get_parser("pdf_table_row")
    r = p.parse(mei_artifact, instruction=f"page=16 table=1 row=26 col={col}")
    assert r.value == pytest.approx(expected)
    assert r.source_as_of == date(2026, 6, 30)


def test_row_token_stops_matching_once_fy_label_changes(mei_artifact):
    """Fails CLOSED, not silently-wrong: a row= token that no longer matches
    any row (simulating the FY26->FY27 rollover) raises ParseError rather
    than reading the wrong (stale) row."""
    p = get_parser("pdf_table_row")
    with pytest.raises(ParseError):
        p.parse(mei_artifact, instruction="page=16 table=1 row=99 col=2")
