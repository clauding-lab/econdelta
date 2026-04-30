"""Parser for "Component <ID>" labeled values in BB Monthly Economic Indicators PDFs."""
from __future__ import annotations

import re

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register


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
        return ParseResult(value=float(cleaned), _parse_strategy="pdf_component")
