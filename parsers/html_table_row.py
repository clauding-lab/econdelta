"""Parser for HTML pages with a single relevant table.

Instruction syntax: "row=<label> col=<1-based index>". Finds the row whose
first cell text contains <label> and extracts the number at <col>.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register


def _parse_instruction(instruction: str) -> tuple[str, int]:
    parts = dict(p.split("=", 1) for p in instruction.split() if "=" in p)
    if "row" not in parts or "col" not in parts:
        raise ParseError(f"instruction must be 'row=<label> col=<int>', got {instruction!r}")
    return parts["row"], int(parts["col"])


def _to_number(text: str) -> float:
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned:
        raise ParseError(f"no number in cell text {text!r}")
    return float(cleaned)


@register("html_table_row")
class HtmlTableRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        row_label, col = _parse_instruction(instruction)
        soup = BeautifulSoup(artifact.artifact_path.read_text(), "html.parser")
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            first = cells[0].get_text(strip=True)
            if row_label.lower() in first.lower():
                if len(cells) < col:
                    raise ParseError(f"row {row_label!r} has only {len(cells)} cells, need col {col}")
                return ParseResult(
                    value=_to_number(cells[col - 1].get_text(strip=True)),
                    _parse_strategy="html_table_row",
                )
        raise ParseError(f"row matching {row_label!r} not found")
