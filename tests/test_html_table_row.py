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


# _to_number cell-value regression cases (audit E23 defects A/B): a bare
# ValueError escaping _to_number skips both the LLM fallback and the
# extract_failed sentinel in hybrid.py's parse ladder, so every non-numeric
# residue must surface as ParseError, and accounting-style parenthesized
# negatives must be interpreted as negative rather than stripped to positive.
@pytest.mark.parametrize(
    "text",
    [
        "-",
        "2025-26",
        "5.00-5.50",
        "n/a",
    ],
)
def test_to_number_raises_parse_error_on_non_numeric_residue(text):
    from parsers.html_table_row import _to_number

    with pytest.raises(ParseError):
        _to_number(text)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("(1,234.56)", -1234.56),
        ("(5.2)", -5.2),
        ("100,000", 100_000.0),
    ],
)
def test_to_number_handles_parens_and_plain_numeric(text, expected):
    from parsers.html_table_row import _to_number

    assert _to_number(text) == expected
