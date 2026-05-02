"""Parser for DSE Recent Market Information — sector-level heat aggregation.

The DSE page at https://www.dsebd.org/recent_market_information.php renders
one anchor block per listed scrip (~389 scrips), each shaped like:

    <a href="displayCompany.php?name=BRACBANK" class='abhead' target='_top'>
        BRACBANK&nbsp;73.20&nbsp;<img src='assets/imgs/tkup.gif' border='0'>
        <br>
        0.20&nbsp;&nbsp;&nbsp;&nbsp;0.27%
    </a>

This parser:
  1. Extracts each (scrip_code, pct_change) pair from the page.
  2. Loads `config/dse_sector_constituents.json` for the 8-sector taxonomy.
  3. For each sector, takes a simple average of constituent % changes
     present in today's page.
  4. Returns ``{"Banks": -1.4, "NBFI": -1.1, ...}`` as a single dict value
     (Phase 3.1 V5 fidelity output shape).

Sectors with zero constituents present in the fetched page are omitted
rather than emitting NaN, so consumers can use a missing key as the
"no fresh data" signal.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "config" / "dse_sector_constituents.json"

# Each scrip block has a unique anchor: name=CODE ... percentage% near </a>
# Capture the trading code from the href and the pct from the trailing text.
_SCRIP_BLOCK_RE = re.compile(
    r'displayCompany\.php\?name=([A-Z0-9]+)'              # scrip code
    r'(?:.|\n)*?'                                          # any chars across the block
    r'(-?\d+\.\d+)\s*%',                                   # last numeric%  inside this block
    re.DOTALL,
)


def _parse_scrip_pcts(html: str) -> dict[str, float]:
    """Pull (scrip → pct) tuples from the DSE Recent Market Information HTML.

    Greedy scan with a non-greedy inner match: each `displayCompany.php?name=`
    starts a fresh window, the regex's lazy `(?:.|\n)*?` then snaps to the
    NEXT pct% — which is the one belonging to that scrip's anchor block.
    """
    pcts: dict[str, float] = {}
    for m in _SCRIP_BLOCK_RE.finditer(html):
        code = m.group(1)
        try:
            pct = float(m.group(2))
        except ValueError:
            continue
        # First occurrence wins — DSE's main grid lists each scrip once at top.
        if code not in pcts:
            pcts[code] = pct
    return pcts


def _load_taxonomy() -> dict[str, list[str]]:
    """Return ``{sector: [scrip_code, ...]}`` from the canonical taxonomy file."""
    if not _TAXONOMY_PATH.exists():
        raise ParseError(f"sector taxonomy missing at {_TAXONOMY_PATH}")
    raw = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    sectors = raw.get("sectors", {})
    return {
        sector: list(block.get("constituents", []))
        for sector, block in sectors.items()
        if isinstance(block, dict)
    }


def _aggregate_by_sector(
    pcts: dict[str, float],
    taxonomy: dict[str, list[str]],
) -> dict[str, float]:
    """Simple-average the constituent % changes per sector.

    Sectors with NO constituents present in `pcts` are dropped (the brief
    treats a missing key as 'sector data unavailable').
    """
    out: dict[str, float] = {}
    for sector, constituents in taxonomy.items():
        present = [pcts[c] for c in constituents if c in pcts]
        if not present:
            continue
        out[sector] = round(sum(present) / len(present), 2)
    return out


@register("dse_sector_heat")
class DseSectorHeatParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        html = artifact.artifact_path.read_text(encoding="utf-8", errors="replace")
        pcts = _parse_scrip_pcts(html)
        if not pcts:
            raise ParseError("DSE sector heat: no scrip blocks parsed from page")

        taxonomy = _load_taxonomy()
        sector_heat = _aggregate_by_sector(pcts, taxonomy)
        if not sector_heat:
            raise ParseError(
                "DSE sector heat: no taxonomy constituents matched any scrip on the page"
            )

        return ParseResult(value=sector_heat, _parse_strategy="dse_sector_heat")
