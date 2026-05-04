"""Extracts the four call-money tenors (1D/7D/14D/90D) as a dict from
bb.org.bd's call money market page."""
from __future__ import annotations

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

_TENORS = {"1D", "7D", "14D", "90D"}


@register("html_call_money")
class HtmlCallMoneyParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        soup = BeautifulSoup(artifact.artifact_path.read_text(), "html.parser")
        out: dict[str, float] = {}
        for tr in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) >= 2 and cells[0] in _TENORS:
                try:
                    out[cells[0]] = float(cells[1])
                except ValueError:
                    continue
        if len(out) < 4:
            raise ParseError(f"expected 4 tenors, got {sorted(out)}")
        return ParseResult(value=out, _parse_strategy="html_call_money")
