"""S6 — ways_means_usage_cr (Ways & Means Advances usage; BB overdraft to govt).

Reuses the already-registered ``pdf_table_row`` parser against the BB Major
Economic Indicators (MEI) government-finance table (alternate: MoF Debt
Bulletin) — only the row-select task differs (the 'Ways and Means Advances'
row, Tk-crore column). No new parser surface; the only thing to prove locally is
that the parser extracts the WMA usage cell from a MEI-shaped synthetic table
using the config's instruction grammar, and that the figure sits inside the
config's usage band.

USAGE-ONLY by design: there is NO published monthly limit/ceiling cell, so this
metric carries no 'vs limit' denominator. The fixture deliberately contains only
a usage row — there is no limit row to extract or fabricate.

The LIVE page/table/row/col indices against the real MEI edition (page numbers
shift edition-to-edition) are VPS-deferred — BD egress firewalls this Mac. This
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

# CEIC-sourced reference prints (Tk crore): Nov-2025 = 120,000; Oct-2025 = 90,924.
WMA_USAGE_CR = 120000


@pytest.fixture
def mei_govt_finance_artifact(tmp_path: Path) -> FetchResult:
    """A synthetic BB MEI government-finance table.

    Columns: Item | Tk (crore) — mirroring the MEI govt-finance block where the
    Ways and Means Advances usage outstanding sits alongside other govt
    financing lines. There is intentionally NO limit/ceiling column or row.
    """
    pdf_path = tmp_path / "mei_govt_finance.pdf"
    doc = SimpleDocTemplate(str(pdf_path), pagesize=letter)
    table = Table(
        [
            ["Item", "Tk (crore)"],
            ["Bank Borrowing", "210000"],
            ["Ways and Means Advances", str(WMA_USAGE_CR)],
            ["Non-bank Borrowing", "95000"],
        ]
    )
    doc.build([table])
    return FetchResult(
        indicator_id="ways_means_usage_cr",
        artifact_path=pdf_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/publication/publictn/3/11",
        sha256="w" * 64,
        cache_hit=False,
    )


def test_extracts_ways_means_usage_crore(mei_govt_finance_artifact):
    """Header-label match on the Ways and Means row, Tk-crore column (col=2)."""
    parser = get_parser("pdf_table_row")
    result = parser.parse(
        mei_govt_finance_artifact,
        instruction="page=1 table=1 row=Ways and Means col=2",
    )
    assert result.value == WMA_USAGE_CR


def test_usage_sits_inside_config_valid_range(mei_govt_finance_artifact):
    """WMA usage in [0, 500000] Tk crore (config band)."""
    parser = get_parser("pdf_table_row")
    usage = parser.parse(
        mei_govt_finance_artifact, instruction="page=1 table=1 row=Ways and Means col=2"
    ).value
    assert 0.0 <= usage <= 500000.0


def test_partial_row_label_matches_ways_means(mei_govt_finance_artifact):
    """The parser does substring row-label matching (landmine E), so a shorter
    'Ways and Means' anchor still resolves the full 'Ways and Means Advances'
    row — guarding against edition-to-edition wording drift on the live PDF."""
    parser = get_parser("pdf_table_row")
    result = parser.parse(
        mei_govt_finance_artifact,
        instruction="page=1 table=1 row=ways and means col=2",
    )
    assert result.value == WMA_USAGE_CR
