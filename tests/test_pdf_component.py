from datetime import datetime, timezone
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

import parsers.pdf_component  # noqa: F401
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> FetchResult:
    pdf_path = tmp_path / "test.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(100, 800, "Component 11a Broad Money: 1900000")
    c.drawString(100, 780, "Component 12c Private Sector Credit: 1500000")
    c.showPage()
    c.save()
    return FetchResult(
        indicator_id="broad_money", artifact_path=pdf_path, artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc), source_url="x", sha256="x"*64, cache_hit=False,
    )


def test_extracts_component_value(pdf_artifact):
    p = get_parser("pdf_component")
    r = p.parse(pdf_artifact, instruction="Component 11a")
    assert r.value == 1_900_000.0


def test_raises_when_component_missing(pdf_artifact):
    p = get_parser("pdf_component")
    with pytest.raises(ParseError):
        p.parse(pdf_artifact, instruction="Component 99z")
