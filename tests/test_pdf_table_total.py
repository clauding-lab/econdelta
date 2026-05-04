from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table

import parsers.pdf_table_total  # noqa: F401
from fetchers.base import FetchResult
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


def test_extracts_last_total(pdf_artifact):
    p = get_parser("pdf_table_total")
    r = p.parse(pdf_artifact, instruction="page=1")
    assert r.value == 100_000.0
