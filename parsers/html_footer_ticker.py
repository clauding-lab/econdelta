"""Parser for the bb.org.bd footer ticker (policy rate, SLF, SDF, USD/BDT).

The instruction names a label (e.g. "Policy Rate"). We find it in the rendered
HTML text and grab the numeric token immediately after.
"""
from __future__ import annotations

import re

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register


@register("html_footer_ticker")
class HtmlFooterTickerParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        text = artifact.artifact_path.read_text()
        plain = re.sub(r"<[^>]+>", " ", text)
        pattern = re.escape(instruction) + r"\s*([0-9]+(?:\.[0-9]+)?)\s*%?"
        m = re.search(pattern, plain, re.IGNORECASE)
        if not m:
            raise ParseError(f"label {instruction!r} not found in HTML")
        return ParseResult(value=float(m.group(1)), _parse_strategy="html_footer_ticker")
