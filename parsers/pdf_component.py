"""Parser for "Component <ID>" labeled values in BB Monthly Economic Indicators PDFs.

Extended to extract ``source_as_of`` from BB FSAR cover-page text of the form
"Quarter ending DD Month YYYY" (e.g. "Quarter ending 30 September 2025").
"""
from __future__ import annotations

import re
from datetime import date

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Matches "Quarter ending 30 September 2025" on the FSAR cover page.
# Group 1: day (1-31), Group 2: month name, Group 3: 4-digit year.
_QUARTER_END_RE = re.compile(
    r"quarter\s+ending\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_quarter_end(text: str) -> date | None:
    """Return the quarter-end date from FSAR cover text, or None if not found."""
    m = _QUARTER_END_RE.search(text)
    if not m:
        return None
    day = int(m.group(1))
    month = _MONTH_NAMES.get(m.group(2).lower())
    year = int(m.group(3))
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


@register("pdf_component")
class PdfComponentParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        with pdfplumber.open(artifact.artifact_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        pattern = re.escape(instruction) + r"[^\d\-]*([\-]?[0-9][0-9,\.]*)"
        m = re.search(pattern, full_text, re.IGNORECASE)
        if not m:
            raise ParseError(f"component {instruction!r} not found in PDF")
        cleaned = m.group(1).replace(",", "")
        source_as_of = _extract_quarter_end(full_text)
        return ParseResult(
            value=float(cleaned),
            _parse_strategy="pdf_component",
            source_as_of=source_as_of,
        )
