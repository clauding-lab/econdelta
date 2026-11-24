"""Extracts call-money tenors (1D/7D/14D/90D) as a dict from bb.org.bd's call
money market page, WITH the page's own value date.

Real page shape (verified against the live page archived 2026-10-19 and
2026-11-23 on the ExonVPS — tests/fixtures/bb_call_money.html is the latter):

    <tbody>
      <tr><td colspan="7" class="page_header">23 November, 2026</td></tr>
      <tr><td>Overnight</td><td>1 Day(/s)</td><td>4886.99</td>
          <td>11.00</td><td>8.95</td><td>9.16</td><td>73</td></tr>
      <tr><td>Short Notice</td><td>7 Day(/s)</td>...<td>9.31</td>...</tr>
      ...
    </tbody>

Columns: Product | Maturity | Amount (crore) | Highest | Lowest | Average |
Deals. The tenor value is the **Average** (weighted) rate — the published
"call money rate".

History (why this rewrite exists): the original parser looked for rows whose
first cell was literally ``1D``/``7D``/... — a shape the live page never had.
It raised ParseError every single run, so the LLM fallback supplied the
numbers with no ``source_as_of``. Once PR #137 (2026-10-20) made undated
observations ``quality="unavailable"``, every call-money reading after
19 Oct was silently dropped from metric_history — The Brief printed
"9.45% · 19 Oct" for five weeks while the page said 9.16% on 23 Nov.

Date: ``source_as_of`` is the NEWEST ``page_header`` date on the page and only
rows under that header are read, so a page showing several days can never
mix one day's overnight with another day's 7-day. ``recover_source_as_of``
gives the LLM fallback the same date (AGENTS.md landmine 26).
"""
from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_money_market_ref_rate import parse_header_date
from parsers.registry import register

# Maturity in days → the tenor key the aggregate fan-out mints
# (call_money_rate_1d / _7d / _14d / _90d). Other maturities (3, 6, 9 day…)
# are published too but have never been part of this indicator.
_TENOR_BY_DAYS = {1: "1D", 7: "7D", 14: "14D", 90: "90D"}
# The LLM fallback (parsers/hybrid.py) validates its dict keys against this set.
_TENORS = frozenset(_TENOR_BY_DAYS.values())

_MATURITY_RE = re.compile(r"^\s*(\d+)\s*Day", re.IGNORECASE)

# Column positions in a data row (see module docstring).
_COL_MATURITY = 1
_COL_AVERAGE = 5
_MIN_CELLS = 6

# In-parser sanity envelope (open interval), mirroring the config
# valid_range. hybrid.parse_one does NOT validate dict values, so a shifted
# column (e.g. the Amount, 4886.99) would otherwise publish as a rate.
_MIN_RATE = 0.0
_MAX_RATE = 25.0


def _newest_block(soup: BeautifulSoup) -> tuple[date, list[list[str]]] | None:
    """(date, data rows) for the newest page_header date block, or None."""
    blocks: list[tuple[date, list[list[str]]]] = []
    current: list[list[str]] | None = None
    for tr in soup.find_all("tr"):
        header = tr.find(class_="page_header")
        if header is not None:
            d = parse_header_date(header.get_text(strip=True))
            if d is not None:
                current = []
                blocks.append((d, current))
            else:
                current = None
            continue
        if current is None:
            continue
        cells = [c.get_text(strip=True) for c in tr.find_all("td")]
        if cells:
            current.append(cells)
    if not blocks:
        return None
    return max(blocks, key=lambda b: b[0])


@register("html_call_money")
class HtmlCallMoneyParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        soup = BeautifulSoup(artifact.artifact_path.read_text(encoding="utf-8"), "html.parser")
        block = _newest_block(soup)
        if block is None:
            raise ParseError("no dated page_header block on the call money page")
        as_of, rows = block

        out: dict[str, float] = {}
        for cells in rows:
            if len(cells) < _MIN_CELLS:
                continue
            m = _MATURITY_RE.match(cells[_COL_MATURITY])
            if not m:
                continue
            tenor = _TENOR_BY_DAYS.get(int(m.group(1)))
            if tenor is None or tenor in out:
                continue
            try:
                rate = float(cells[_COL_AVERAGE].replace(",", ""))
            except ValueError:
                continue
            if not (_MIN_RATE < rate < _MAX_RATE):
                raise ParseError(
                    f"{tenor} average {rate} outside ({_MIN_RATE}, {_MAX_RATE}) — "
                    "column layout likely changed"
                )
            out[tenor] = rate

        if "1D" not in out:
            # The overnight rate is the headline; without it the indicator is
            # meaningless. Let the hybrid fall back rather than publish a
            # partial dict as if it were complete.
            raise ParseError(f"no overnight (1 Day) row under {as_of}; got {sorted(out)}")
        return ParseResult(value=out, _parse_strategy="html_call_money", source_as_of=as_of)

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Value date for the LLM-fallback path (landmine 26). Never raises."""
        try:
            soup = BeautifulSoup(
                artifact.artifact_path.read_text(encoding="utf-8"), "html.parser"
            )
            block = _newest_block(soup)
            return block[0] if block else None
        except Exception:  # noqa: BLE001 — recovery is best-effort
            return None
