"""Extract the bottom-right number from the last table on a page."""
from __future__ import annotations

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_table_row import _to_number
from parsers.registry import register

_TABLE_SETTINGS = [
    {},
    {"vertical_strategy": "text", "horizontal_strategy": "text"},
]


@register("pdf_table_total")
class PdfTableTotalParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        page_idx = 0
        for tok in instruction.split():
            if tok.startswith("page="):
                page_idx = int(tok.split("=", 1)[1]) - 1
        with pdfplumber.open(artifact.artifact_path) as pdf:
            if page_idx >= len(pdf.pages):
                raise ParseError(f"page {page_idx + 1} out of range")
            page = pdf.pages[page_idx]
            tables = []
            for settings in _TABLE_SETTINGS:
                tables = page.extract_tables(settings) if settings else page.extract_tables()
                if tables:
                    break
        if not tables:
            raise ParseError(f"no tables on page {page_idx + 1}")
        last_table = tables[-1]
        # Filter out empty rows (text strategy may insert them)
        non_empty_rows = [r for r in last_table if any(c for c in r if c and str(c).strip())]
        if not non_empty_rows:
            raise ParseError("no non-empty rows in last table")
        last_row = non_empty_rows[-1]
        for cell in reversed(last_row):
            if cell is None:
                continue
            try:
                return ParseResult(value=_to_number(str(cell)), _parse_strategy="pdf_table_total")
            except ParseError:
                continue
        raise ParseError("no numeric cell in last row of last table")
