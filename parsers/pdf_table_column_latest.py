"""Parser for BB MEI / WSEI bulletin tables whose latest period lives in the
last data ROW of a multi-row table, keyed by COLUMN header.

This complements ``pdf_table_latest`` (which keys by row label). Use this
parser when the indicator is a column whose values move month-to-month, and
the most recent month is whatever happens to be the bottom-most row of the
table (skipping Source:/Note: footers and fiscal-year group labels).

Instruction syntax: ``page=<N> col=<column-header-text>``

- ``page`` is 1-indexed (matches ``pdf_table_row``).
- ``col`` is matched case-insensitively after whitespace normalization
  (newlines and multiple spaces collapse to a single space) — pdfplumber
  often returns multi-line headers like ``"Policy rate\\n(repo)"`` that
  must match the human-readable ``"Policy rate (repo)"``.
- The col value runs to end-of-string so it can contain spaces and
  parentheses.

Example: ``page=10 col=Policy rate (repo)`` against the April 2026 BB MEI
bulletin returns ``10.00`` — the April row's value in the Policy rate
column.
"""

from __future__ import annotations

import re

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Sanity bound applied to any extracted value. The per-indicator
# ``parse.valid_range`` in sources-v3.json is the authoritative gate;
# this is a defence-in-depth guard for obviously-broken table extraction.
_SANITY_MIN = -1000.0
_SANITY_MAX = 1000.0

# Month names that mark a data row in the BB bulletin tables. Group-label
# rows ("FY25", "FY26") and footer rows ("Source: ...", "Note: ...")
# never start with these.
_MONTH_NAMES = frozenset(
    {
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    }
)

_PAGE_RE = re.compile(r"\bpage=(\S+)")
_COL_RE = re.compile(r"\bcol=(.+)$")


def _parse_instruction(instruction: str) -> tuple[int, str]:
    page_match = _PAGE_RE.search(instruction)
    if not page_match:
        raise ParseError(f"instruction missing page=<N>: {instruction!r}")
    try:
        page = int(page_match.group(1))
    except ValueError as exc:
        raise ParseError(f"instruction page= must be an integer: {instruction!r}") from exc

    col_match = _COL_RE.search(instruction)
    if not col_match:
        raise ParseError(f"instruction missing col=<header>: {instruction!r}")
    col = col_match.group(1).strip()
    if not col:
        raise ParseError(f"instruction col= is empty: {instruction!r}")
    return page, col


def _normalize_header(cell: str | None) -> str:
    """Lowercase + collapse all whitespace (newlines, multiple spaces) into
    single spaces. Used to compare a target column label against the
    header cells pdfplumber extracted."""
    if cell is None:
        return ""
    # \s catches \n, \t, multiple spaces — collapse to a single space.
    return re.sub(r"\s+", " ", str(cell)).strip().lower()


def _find_column_index(table: list[list], target_col: str) -> int:
    """Scan every row of ``table`` looking for a cell whose normalized
    contents match ``target_col``. Header rows in BB bulletin tables
    often sit on rows 1 or 2 (row 0 may be a units banner); searching
    all rows is robust against minor layout shifts.

    Returns the 0-indexed column where the match was found.
    Raises ParseError if no row contains a matching header cell.
    """
    target_norm = _normalize_header(target_col)
    for row in table:
        if not row:
            continue
        for idx, cell in enumerate(row):
            if _normalize_header(cell) == target_norm:
                return idx
    raise ParseError(f"column header {target_col!r} not found in table")


def _to_float(cell: str | None) -> float | None:
    """Parse a single table cell into a float. Returns None when the cell
    is empty, a placeholder ("--", "---", "----"), or otherwise can't be
    coerced — callers decide what 'no value' means in context."""
    if cell is None:
        return None
    text = str(cell).strip()
    if not text or set(text) <= {"-"}:
        return None
    # Strip thousands separators; keep sign and decimal.
    cleaned = text.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _latest_value_in_column(table: list[list], col_idx: int) -> float:
    """Walk the table bottom-up and return the first value in column
    ``col_idx`` whose row starts with a month name and whose target cell
    parses as a float in the sanity range.

    Raises ParseError if no qualifying row is found, or if the value is
    outside the [-1000, 1000] sanity range (defence-in-depth against
    pdfplumber column-misalignment).
    """
    for row in reversed(table):
        if not row or col_idx >= len(row):
            continue
        first_cell = row[0]
        if first_cell is None:
            continue
        first_norm = _normalize_header(first_cell)
        if first_norm not in _MONTH_NAMES:
            continue
        value = _to_float(row[col_idx])
        if value is None:
            continue
        if not (_SANITY_MIN <= value <= _SANITY_MAX):
            raise ParseError(
                f"value {value} from column {col_idx} fails sanity range "
                f"[{_SANITY_MIN}, {_SANITY_MAX}]"
            )
        return value
    raise ParseError(f"no data row with month label and parseable value found in column {col_idx}")


@register("pdf_table_column_latest")
class PdfTableColumnLatestParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        import pdfplumber  # lazy: keep registry-time import lightweight

        page_num, target_col = _parse_instruction(instruction)
        page_idx = page_num - 1

        with pdfplumber.open(artifact.artifact_path) as pdf:
            if page_idx < 0 or page_idx >= len(pdf.pages):
                raise ParseError(f"page {page_num} out of range; PDF has {len(pdf.pages)} pages")
            page = pdf.pages[page_idx]
            tables = page.extract_tables()

        if not tables:
            raise ParseError(f"no tables found on page {page_num}")

        table = tables[0]
        col_idx = _find_column_index(table, target_col)
        value = _latest_value_in_column(table, col_idx)
        return ParseResult(value=value, _parse_strategy="pdf_table_column_latest")
