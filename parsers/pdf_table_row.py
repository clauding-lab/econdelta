"""Extract a value from a specific (page, table_index, row_label) triple in a PDF."""
from __future__ import annotations

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_table_row import _to_number
from parsers.registry import register


def _parse_instruction(instruction: str) -> dict:
    out = {}
    for token in instruction.split():
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    for k in ("page", "table", "row", "col"):
        if k not in out:
            raise ParseError(f"instruction missing {k}: {instruction!r}")
    return out


@register("pdf_table_row")
class PdfTableRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        ins = _parse_instruction(instruction)
        page_idx = int(ins["page"]) - 1
        tbl_idx = int(ins["table"]) - 1
        row_label = ins["row"]
        col = int(ins["col"]) - 1
        _TABLE_SETTINGS = [
            {},
            {"vertical_strategy": "text", "horizontal_strategy": "text"},
        ]
        with pdfplumber.open(artifact.artifact_path) as pdf:
            if page_idx >= len(pdf.pages):
                raise ParseError(f"page {ins['page']} > {len(pdf.pages)} pages")
            page = pdf.pages[page_idx]
            tables = []
            for settings in _TABLE_SETTINGS:
                tables = page.extract_tables(settings) if settings else page.extract_tables()
                if tables:
                    break
        if tbl_idx >= len(tables):
            raise ParseError(f"table {ins['table']} > {len(tables)} on page {ins['page']}")
        for row in tables[tbl_idx]:
            if row and row[0] and row_label.lower() in str(row[0]).lower():
                if col >= len(row):
                    raise ParseError(f"row has {len(row)} cols, need {ins['col']}")
                cell = row[col]
                if cell is None:
                    raise ParseError(f"cell at col {col} is empty")
                return ParseResult(value=_to_number(str(cell)), _parse_strategy="pdf_table_row")
        raise ParseError(f"row {row_label!r} not found in page {ins['page']} table {ins['table']}")
