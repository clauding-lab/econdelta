"""Parser for BB WSEI/MEI tabular rows where the latest period is the last
"absolute" numeric value on the row, followed by smaller pct-change columns.

Instruction syntax: ``row="<label>" [min=<float>]``

The row label is matched case-insensitively as a substring on each line of
the extracted PDF text. ``min`` (default ``0``) filters out small numbers
(e.g. percentage-change columns) so the parser returns the last value whose
``abs(value) >= min``.

Example for the WSEI Item 11 row:

    a) Reserve Money (RM) (BDT in crore) 374602.90 413179.00 424618.80 13.35 2.77 -9.44 -0.11

With ``row="a) Reserve Money" min=1000`` the parser keeps only the three
absolute values (≥ 1000) and returns ``424618.80`` — the latest period.
"""
from __future__ import annotations

import re

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Match a signed number with optional thousands separators and decimal.
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_ROW_RE = re.compile(r'row="([^"]+)"')
_MIN_RE = re.compile(r"min=(-?\d+(?:\.\d+)?)")


def _parse_instruction(instruction: str) -> tuple[str, float]:
    m = _ROW_RE.search(instruction)
    if not m:
        raise ParseError(f'instruction must include row="<label>": {instruction!r}')
    row_label = m.group(1)
    min_match = _MIN_RE.search(instruction)
    min_value = float(min_match.group(1)) if min_match else 0.0
    return row_label, min_value


def _find_latest_in_text(text: str, row_label: str, min_value: float) -> float | None:
    """Return the last number on a line containing ``row_label`` whose
    ``abs(value) >= min_value``. ``None`` if no matching line/number found.
    """
    needle = row_label.lower()
    for line in text.splitlines():
        if needle not in line.lower():
            continue
        last: float | None = None
        for token in _NUMBER_RE.findall(line):
            try:
                v = float(token.replace(",", ""))
            except ValueError:
                continue
            if abs(v) >= min_value:
                last = v
        if last is not None:
            return last
    return None


@register("pdf_table_latest")
class PdfTableLatestParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        import pdfplumber  # lazy: keep registry-time import lightweight

        row_label, min_value = _parse_instruction(instruction)
        with pdfplumber.open(artifact.artifact_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        value = _find_latest_in_text(full_text, row_label, min_value)
        if value is None:
            raise ParseError(
                f"no row matching {row_label!r} with a number "
                f"of magnitude >= {min_value} found"
            )
        return ParseResult(value=value, _parse_strategy="pdf_table_latest")
