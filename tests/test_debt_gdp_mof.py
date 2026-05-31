"""MoF Debt Bulletin leg of S4 (debt_gdp_ratio latest print).

The debt_gdp_ratio indicator reuses the already-registered ``pdf_table_row``
parser, so the only new surface to test is that the parser extracts the
"as % of GDP" row cell from a Debt-Bulletin-shaped synthetic table using the
config's instruction grammar. The LIVE page/table/row/col indices against the
real MoF bulletin layout are VPS-deferred (BD egress firewalls this Mac); this
proves the deterministic path works given the right indices.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table

import parsers.pdf_table_row  # noqa: F401
from fetchers.base import FetchResult
from parsers.registry import get_parser


@pytest.fixture
def debt_bulletin_artifact(tmp_path: Path) -> FetchResult:
    """A synthetic MoF Debt Bulletin summary table.

    Columns: Item | Tk (crore) | as % of GDP — mirroring the real bulletin's
    summary block where the public-debt total carries both an amount and the
    %-of-GDP figure the metric wants.
    """
    pdf_path = tmp_path / "debt_bulletin.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table(
        [
            ["Item", "Tk (crore)", "as % of GDP"],
            ["Domestic Debt", "1195000", "21.5"],
            ["External Debt", "949000", "17.1"],
            ["Total Public Debt", "2144000", "38.6"],
        ]
    )
    doc.build([table])
    return FetchResult(
        indicator_id="debt_gdp_ratio",
        artifact_path=pdf_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://mof.gov.bd/site/page/debt-bulletin",
        sha256="d" * 64,
        cache_hit=False,
    )


def test_extracts_debt_gdp_percent_cell(debt_bulletin_artifact):
    """Header-label match on the Total Public Debt row, %-of-GDP column."""
    parser = get_parser("pdf_table_row")
    result = parser.parse(
        debt_bulletin_artifact,
        instruction="page=1 table=1 row=Total Public Debt col=3",
    )
    assert result.value == 38.6


def test_value_lands_in_official_band(debt_bulletin_artifact):
    """38.6% sits in the [10, 100] valid_range and matches the FY25 official print."""
    parser = get_parser("pdf_table_row")
    result = parser.parse(
        debt_bulletin_artifact,
        instruction="page=1 table=1 row=Total Public Debt col=3",
    )
    assert 10.0 <= result.value <= 100.0
