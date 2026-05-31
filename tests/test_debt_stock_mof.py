"""MoF leg of S5 (debt_domestic_stock_cr + debt_external_stock_cr).

Both metrics reuse the already-registered ``pdf_table_row`` parser against the
SAME MoF Debt Bulletin PDF as debt_gdp_ratio (S4) — only the row-select task
differs (Domestic Debt / External Debt rows, Tk-crore column). The only new
surface to test is that the parser extracts those stock cells from a
Debt-Bulletin-shaped synthetic table using the config's instruction grammar.
The LIVE page/table/row/col indices against the real MoF bulletin layout are
VPS-deferred (BD egress firewalls this Mac); this proves the deterministic path
works given the right indices, and that the FY25 stock figures reconcile against
the Debt/GDP total behind S4's ratio.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table

import parsers.pdf_table_row  # noqa: F401
from fetchers.base import FetchResult
from parsers.registry import get_parser

# FY25 official Debt-Bulletin stock figures (Tk crore): external ~Tk9.49tn,
# domestic ~Tk11.95tn — the same anchors the S4 debt_gdp_ratio fixture uses.
DOMESTIC_CR = 1195000
EXTERNAL_CR = 949000
TOTAL_CR = DOMESTIC_CR + EXTERNAL_CR


@pytest.fixture
def debt_bulletin_artifact(tmp_path: Path) -> FetchResult:
    """A synthetic MoF Debt Bulletin summary table.

    Columns: Item | Tk (crore) | as % of GDP — mirroring the real bulletin's
    summary block where each public-debt component carries both an outstanding
    amount and the %-of-GDP figure.
    """
    pdf_path = tmp_path / "debt_bulletin.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table(
        [
            ["Item", "Tk (crore)", "as % of GDP"],
            ["Domestic Debt", str(DOMESTIC_CR), "21.5"],
            ["External Debt", str(EXTERNAL_CR), "17.1"],
            ["Total Public Debt", str(TOTAL_CR), "38.6"],
        ]
    )
    doc.build([table])
    return FetchResult(
        indicator_id="debt_domestic_stock_cr",
        artifact_path=pdf_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://mof.gov.bd/site/page/debt-bulletin",
        sha256="d" * 64,
        cache_hit=False,
    )


def test_extracts_domestic_stock_crore(debt_bulletin_artifact):
    """Header-label match on the Domestic Debt row, Tk-crore column (col=2)."""
    parser = get_parser("pdf_table_row")
    result = parser.parse(
        debt_bulletin_artifact,
        instruction="page=1 table=1 row=Domestic Debt col=2",
    )
    assert result.value == DOMESTIC_CR


def test_extracts_external_stock_crore(debt_bulletin_artifact):
    """Header-label match on the External Debt row, Tk-crore column (col=2)."""
    parser = get_parser("pdf_table_row")
    result = parser.parse(
        debt_bulletin_artifact,
        instruction="page=1 table=1 row=External Debt col=2",
    )
    assert result.value == EXTERNAL_CR


def test_domestic_and_external_reconcile_with_total(debt_bulletin_artifact):
    """Exit criterion: domestic + external roughly reconciles the total debt
    behind S4's Debt/GDP ratio (within a few % for rounding/coverage gaps)."""
    parser = get_parser("pdf_table_row")
    domestic = parser.parse(
        debt_bulletin_artifact, instruction="page=1 table=1 row=Domestic Debt col=2"
    ).value
    external = parser.parse(
        debt_bulletin_artifact, instruction="page=1 table=1 row=External Debt col=2"
    ).value
    total = parser.parse(
        debt_bulletin_artifact, instruction="page=1 table=1 row=Total Public Debt col=2"
    ).value
    assert abs((domestic + external) - total) / total < 0.02


def test_stocks_sit_inside_config_valid_ranges(debt_bulletin_artifact):
    """Domestic in [500k, 3m] and external in [300k, 3m] Tk crore (config bands)."""
    parser = get_parser("pdf_table_row")
    domestic = parser.parse(
        debt_bulletin_artifact, instruction="page=1 table=1 row=Domestic Debt col=2"
    ).value
    external = parser.parse(
        debt_bulletin_artifact, instruction="page=1 table=1 row=External Debt col=2"
    ).value
    assert 500000.0 <= domestic <= 3000000.0
    assert 300000.0 <= external <= 3000000.0
