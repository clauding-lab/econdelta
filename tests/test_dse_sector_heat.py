"""Unit tests for the DSE sector-heat parser.

The parser reads DSE's Recent Market Information page (one fetch covering
all listed scrips), extracts each scrip's daily % change, then aggregates
by sector via simple-average using the static taxonomy in
config/dse_sector_constituents.json.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

import parsers.dse_sector_heat  # noqa: F401 — registers
from fetchers.base import FetchResult
from parsers.base import ParseError
from parsers.registry import get_parser


# Realistic fragment mirroring DSE's actual page layout: one anchor block
# per scrip with code + price + up/down image + change + pct.
def _scrip_block(code: str, price: float, pct: float) -> str:
    direction = "tkup.gif" if pct >= 0 else "tkdown.gif"
    chg_abs = round(price * pct / 100.0, 2)
    return (
        f'<a href="displayCompany.php?name={code}" class=\'abhead\' target=\'_top\'>'
        f'{code}&nbsp;{price:.2f}&nbsp;'
        f'<img src=\'assets/imgs/{direction}\' border=\'0\'>\t<br>'
        f'{chg_abs:.2f}&nbsp;&nbsp;&nbsp;&nbsp;{pct:.2f}%'
        f'</a>'
    )


_HTML = "<html><body>" + "".join([
    # Banks (all decline) — avg ≈ -1.4%
    _scrip_block("BRACBANK",   73.00, -1.50),
    _scrip_block("DUTCHBANGL", 49.00, -1.30),
    _scrip_block("EBL",        38.00, -1.40),
    # NBFI (mixed but skewed negative) — avg ≈ -1.1%
    _scrip_block("IDLC",  35.00, -1.20),
    _scrip_block("IPDC",  21.00, -1.00),
    # Pharma (positive) — avg ≈ +0.4%
    _scrip_block("SQURPHARMA", 211.80,  0.50),
    _scrip_block("BEXIMCO",    138.00,  0.30),
    # IT (small positive) — avg ≈ +0.1%
    _scrip_block("IBN", 18.00,  0.10),
    # An UNKNOWN scrip not in the taxonomy — must be ignored
    _scrip_block("UNKNOWNSCRIP", 100.00,  5.00),
]) + "</body></html>"


@pytest.fixture
def fixture_artifact(tmp_path: Path) -> FetchResult:
    p = tmp_path / "dse-rmi.html"
    p.write_text(_HTML, encoding="utf-8")
    return FetchResult(
        indicator_id="dse_sector_heat",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.dsebd.org/recent_market_information.php",
        sha256="x" * 64,
        cache_hit=False,
    )


def test_extracts_per_scrip_pct_changes(fixture_artifact):
    """Lower-level extraction: scrip → pct dict before sector aggregation."""
    from parsers.dse_sector_heat import _parse_scrip_pcts
    plain = fixture_artifact.artifact_path.read_text(encoding="utf-8")
    pcts = _parse_scrip_pcts(plain)
    assert pcts["BRACBANK"] == -1.50
    assert pcts["GP"] == 0.0 if "GP" in pcts else True  # GP not in fixture
    assert pcts["SQURPHARMA"] == 0.50
    assert pcts["UNKNOWNSCRIP"] == 5.00


def test_aggregates_to_sector_dict(fixture_artifact):
    parser = get_parser("dse_sector_heat")
    result = parser.parse(fixture_artifact, instruction="")
    assert isinstance(result.value, dict)
    # 8 sectors mapped, but only those with at least one constituent
    # present in the page get a numeric value.
    # Banks: avg(-1.50, -1.30, -1.40) = -1.40
    assert "Banks" in result.value
    assert round(result.value["Banks"], 2) == -1.40
    # NBFI: avg(-1.20, -1.00) = -1.10
    assert round(result.value["NBFI"], 2) == -1.10
    # Pharma: avg(0.50, 0.30) = 0.40
    assert round(result.value["Pharma"], 2) == 0.40


def test_skips_sectors_with_no_constituents_in_page(fixture_artifact):
    """Sectors whose constituents don't appear in the fetched HTML are
    omitted from the output rather than emitting null/NaN."""
    parser = get_parser("dse_sector_heat")
    result = parser.parse(fixture_artifact, instruction="")
    # Telecom constituents (GP/ROBI/BSCCL) absent from fixture
    assert "Telecom" not in result.value
    # Food/Textile/Fuel also absent
    assert "Food" not in result.value


def test_unknown_scrips_dont_pollute_aggregation(fixture_artifact):
    """A scrip on the page but not in the taxonomy must not show up in
    any sector's average."""
    parser = get_parser("dse_sector_heat")
    result = parser.parse(fixture_artifact, instruction="")
    # UNKNOWNSCRIP at +5% should not pull any sector positive
    assert all(pct < 1.0 for pct in result.value.values())


def test_raises_when_no_scrips_parsed(tmp_path):
    """Empty page → ParseError so hybrid orchestrator falls back to LLM."""
    p = tmp_path / "empty.html"
    p.write_text("<html><body>nothing here</body></html>", encoding="utf-8")
    artifact = FetchResult(
        indicator_id="dse_sector_heat",
        artifact_path=p, artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="x", sha256="x" * 64, cache_hit=False,
    )
    parser = get_parser("dse_sector_heat")
    with pytest.raises(ParseError):
        parser.parse(artifact, instruction="")
