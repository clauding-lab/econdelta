"""Unit tests for the DAM portal daily-price ticker parser.

Covers Bengali-digit translation, NFC normalization, and midpoint
extraction. The fixture mirrors the real DAM HTML structure: items
listed as `<bengali-label> :&nbsp;<low> - <high>` with Bengali digits.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.dam_ticker  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser

# Realistic ticker fragment with mixed entity references and Bengali digits.
_HTML = (
    "<div class='ticker'>"
    "চিনি (দেশী) :&nbsp;১৩২.০০ - ১৩৫.০০ ▲০.০০% "
    "খামারের মুরগী :&nbsp;১৬২.০০ - ১৬৭.০০ ▲০.০০% "
    "সয়াবিন তেল :&nbsp;১৬৩.০০ - ১৬৫.০০ ▲০.০০% "
    "আমন চাল - মোটা :&nbsp;৪৮.০০ - ৫০.০০ ▲০.০০%"
    "</div>"
)


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "dam.html"
    p.write_text(_HTML, encoding="utf-8")
    return FetchResult(
        indicator_id="dam_food_test",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="http://market.dam.gov.bd/market_daily_price_report",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_sugar_midpoint(fixture_artifact):
    parser = get_parser("dam_ticker")
    result = parser.parse(fixture_artifact, instruction="চিনি (দেশী)")
    # Mid of 132.00 - 135.00 = 133.5
    assert result.value == 133.5
    assert result._parse_strategy == "dam_ticker"


def test_extracts_chicken_midpoint(fixture_artifact):
    parser = get_parser("dam_ticker")
    result = parser.parse(fixture_artifact, instruction="খামারের মুরগী")
    # Mid of 162.00 - 167.00 = 164.5
    assert result.value == 164.5


def test_handles_decomposed_unicode_in_instruction(fixture_artifact):
    """Real HTML uses single-codepoint য় (U+09DF). The instruction string
    might be typed as decomposed য + ◌় (U+09AF + U+09BC). NFC normalization
    on both sides keeps the match working."""
    parser = get_parser("dam_ticker")
    # সয়াবিন with য় as decomposed pair (U+09AF + U+09BC)
    decomposed = "সয়াবিন তেল"
    result = parser.parse(fixture_artifact, instruction=decomposed)
    assert result.value == 164.0


def test_raises_on_unknown_label(fixture_artifact):
    parser = get_parser("dam_ticker")
    with pytest.raises(ParseError, match="not found"):
        parser.parse(fixture_artifact, instruction="পেঁয়াজ - দেশী")  # not in fixture
