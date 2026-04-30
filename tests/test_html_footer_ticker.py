from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.html_footer_ticker  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

_HTML = """
<html><body>
<div class="ticker">USD/BDT 122.50 EUR/BDT 132.10 Policy Rate 10.00% SLF 11.50% SDF 8.50%</div>
</body></html>
"""


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "page.html"
    p.write_text(_HTML)
    return FetchResult(
        indicator_id="policy_rate_slf_sdf",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_policy_rate(fixture_artifact):
    p = get_parser("html_footer_ticker")
    r = p.parse(fixture_artifact, instruction="Policy Rate")
    assert r.value == 10.00
    assert r._parse_strategy == "html_footer_ticker"


def test_raises_on_label_not_found(fixture_artifact):
    p = get_parser("html_footer_ticker")
    with pytest.raises(ParseError, match="not found"):
        p.parse(fixture_artifact, instruction="Nonexistent Indicator")
