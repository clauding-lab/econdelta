from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_table_row  # noqa: F401
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

_HTML = """
<html><body>
<h1>Marketable T-Bills Outstanding</h1>
<table>
  <tr><th>Tenor</th><th>Outstanding (BDT crore)</th></tr>
  <tr><td>91-day</td><td>50,000</td></tr>
  <tr><td>182-day</td><td>30,000</td></tr>
  <tr><td>364-day</td><td>20,000</td></tr>
  <tr><td><b>Total</b></td><td><b>100,000</b></td></tr>
</table>
</body></html>
"""


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "page.html"
    p.write_text(_HTML)
    return FetchResult(
        indicator_id="treasury_bill_outstanding",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://gsom.bb.org.bd/mtm-bill.php",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_table_total_row(fixture_artifact):
    p = get_parser("html_table_row")
    r = p.parse(fixture_artifact, instruction="row=Total col=2")
    assert r.value == 100_000.0


def test_raises_on_row_not_found(fixture_artifact):
    p = get_parser("html_table_row")
    with pytest.raises(ParseError):
        p.parse(fixture_artifact, instruction="row=Nope col=2")
