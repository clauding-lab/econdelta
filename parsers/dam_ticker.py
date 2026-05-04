"""Parser for the Department of Agricultural Marketing daily-price ticker.

The DAM portal at http://market.dam.gov.bd/market_daily_price_report renders
a top-of-page ticker like:

    আমন চাল - মোটা :&nbsp;৪৮.০০ - ৫০.০০ ▲০.০০%
    চিনি (দেশী) :&nbsp;১৩২.০০ - ১৩৫.০০ ▲০.০০%

Each item is `<bengali-label> : <low> - <high> <arrow><pct>%` with Bengali
digits (০-৯). The brief consumes a single midpoint price per item, so this
parser converts Bengali digits → Western digits, finds the row whose label
matches the instruction, and returns the (low+high)/2 midpoint.

Instruction format: a literal Bengali label as it appears on the page,
e.g. ``চিনি (দেশী)``. Any whitespace and parens are matched literally
(no regex metacharacters from the user — we re.escape).

source_as_of: the parser extracts the "Date of report: DD-MM-YYYY" header
that the DAM portal renders above the ticker. This is the true publication
date of the report, not the EconDelta run date.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Bengali digit ০-৯ ↔ Western 0-9. Built once at import.
_BN_TO_EN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# DAM report-date header: "Date of report: 04-05-2026" or "Date of report: 04/05/2026"
# Captures DD (group 1), MM (group 2), YYYY (group 3).
_DATE_REPORT_RE = re.compile(
    r"date\s+of\s+report\s*:?\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Strip HTML tags, decode &nbsp;, convert Bengali digits → Western,
    apply Unicode NFC normalization.

    NFC matters because the DAM portal's source HTML uses composed Bengali
    codepoints (e.g. য় = U+09DF, single-codepoint 'ya with nukta') while
    a config string typed in an editor may produce the decomposed form
    (য + ◌় = U+09AF + U+09BC). Without NFC, ``re.search`` misses real
    matches.
    """
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = plain.replace("&nbsp;", " ")
    plain = plain.translate(_BN_TO_EN_DIGITS)
    plain = re.sub(r"\s+", " ", plain)
    return unicodedata.normalize("NFC", plain)


def _extract_report_date(plain: str) -> date | None:
    """Return the DAM report date from the normalized page text, or None."""
    m = _DATE_REPORT_RE.search(plain)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


@register("dam_ticker")
class DamTickerParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        raw = artifact.artifact_path.read_text(encoding="utf-8", errors="replace")
        plain = _normalize(raw)
        instruction_nfc = unicodedata.normalize("NFC", instruction)
        # Pattern: <label> : <low> - <high>
        # Numbers can be int or decimal; Bengali digits already translated to ASCII.
        pattern = (
            re.escape(instruction_nfc)
            + r"\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)"
        )
        m = re.search(pattern, plain)
        if not m:
            raise ParseError(
                f"DAM ticker label {instruction!r} not found in normalized HTML"
            )
        low = float(m.group(1))
        high = float(m.group(2))
        midpoint = round((low + high) / 2.0, 2)
        source_as_of = _extract_report_date(plain)
        return ParseResult(value=midpoint, _parse_strategy="dam_ticker", source_as_of=source_as_of)
