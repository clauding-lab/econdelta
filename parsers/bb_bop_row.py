"""Row/column extractor for Bangladesh Bank's Balance of Payments (BoP) table.

Written for `current_account_balance` after a two-month production bug
(cab-memo-2026-08-05.md): the generic `html_table_row` parser selected BB's
BoP column by a hardcoded 1-based index (`col=2`), but BB's column order is
NOT chronological — the monthly page runs prior-FY, current-FY-revised,
current-FY-provisional left to right — so `col=2` read last year's number.
`html_table_row` also has no unit-conversion step, so even the right cell
would have been off by 1000x (BB states the table in USD million; this
metric's `value_type` is USD billion).

This parser fixes both defects structurally rather than by picking a
different hardcoded index (which would just relocate the same landmine):

1. **Column selection is by HEADER TEXT, never position.** Each header cell
   is scored as (fiscal_year_start, month_span) from patterns like
   "2025-26PJuly-May"; the column with the highest fiscal year — and, among
   columns sharing that year, the longest month window — is selected. A
   "% Changes" delta column carries no fiscal-year/month pattern and is
   naturally excluded. This generalises across BB's monthly page (5 columns)
   and yearly page (4 columns) without change.
2. **Unit scale is detected from the page's own "In million US$" /
   "In billion US$" label**, not assumed. Converts to the billions the
   `value_type=amount_usd_bn` config expects.

Instruction syntax: ``row=<label>``. The row label is matched EXACTLY after
normalisation (case-folded, whitespace collapsed) — not a substring — so a
row-name collision (e.g. "Current Account Balance" vs some future "Current
Account Balance (Provisional)" variant) fails loudly instead of silently
matching the wrong row. Mirrors the exact-match design of
`html_labeled_value.py` (2026-08-03) rather than `html_table_row.py`'s older
substring style — see AGENTS.md landmine 39 for why "fail loud on a rename"
beats "quietly match a neighbour".
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_table_row import _to_number
from parsers.registry import register

_MONTHS_FULL = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
# Map both full names and 3-letter abbreviations to a 1-12 month number.
# BB mixes the two on the same header row ("July" full, "Apr" abbreviated —
# see "2025-26RJuly-Apr"), so both forms must be recognised.
_MONTH_ORDER: dict[str, int] = {}
for _i, _name in enumerate(_MONTHS_FULL):
    _MONTH_ORDER[_name] = _i + 1
    _MONTH_ORDER[_name[:3]] = _i + 1

# Longest alternatives first so "july" isn't cut short by an earlier partial
# match inside the regex engine's left-to-right alternation.
_MONTH_ALT = "|".join(sorted(_MONTH_ORDER, key=len, reverse=True))
_FY_RE = re.compile(r"(\d{4})-(\d{2})")
_MONTH_RANGE_RE = re.compile(rf"({_MONTH_ALT})-({_MONTH_ALT})", re.IGNORECASE)
_UNIT_RE = re.compile(r"in\s+(million|billion)\s+US\$", re.IGNORECASE)
_UNIT_DIVISOR = {"million": 1000.0, "billion": 1.0}  # divisor to reach USD billion


def _parse_instruction(instruction: str) -> str:
    head = instruction.split(" -- ", 1)[0].strip()
    if not head.lower().startswith("row="):
        raise ParseError(f"instruction must be 'row=<label>[ -- ...]', got {instruction!r}")
    label = head[len("row="):].strip()
    if not label:
        raise ParseError(f"empty row label in instruction {instruction!r}")
    return label


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(":").strip().casefold()


def _score_column_header(text: str) -> tuple[int, int] | None:
    """Score a BoP column header as (fiscal_year_start, month_span_months).

    Returns None for headers with no fiscal-year + month-window pattern
    (e.g. the label column "Items", or a "% Changes" delta column) — those
    are never eligible for selection.
    """
    fy_match = _FY_RE.search(text)
    month_match = _MONTH_RANGE_RE.search(text)
    if not fy_match or not month_match:
        return None
    fy_start = int(fy_match.group(1))
    start_month = _MONTH_ORDER[month_match.group(1).lower()]
    end_month = _MONTH_ORDER[month_match.group(2).lower()]
    # BD fiscal year runs July-June; span counts months from start to end
    # inclusive, wrapping over the year boundary (e.g. July-May = 11).
    span = (end_month - start_month) % 12 + 1
    return (fy_start, span)


def _select_column(header_cells: list[str]) -> int:
    """Index (into the FULL row, including the label cell) of the most
    recent fiscal-year, longest-window column. Never picks by position."""
    scored = {
        idx: score
        for idx, text in enumerate(header_cells)
        if (score := _score_column_header(text)) is not None
    }
    if not scored:
        raise ParseError(f"no fiscal-year-labelled column found in header {header_cells!r}")
    return max(scored, key=lambda idx: scored[idx])


def _detect_unit_divisor(page_text: str) -> float:
    """Divisor to convert the table's stated unit to USD billion.

    Reads the unit from the page (e.g. "In million US$") instead of
    assuming millions — if BB ever states the table in billions, this
    converts correctly instead of silently dividing by 1000 a second time.
    """
    m = _UNIT_RE.search(page_text)
    if not m:
        raise ParseError("could not find an 'In million/billion US$' unit label on the page")
    return _UNIT_DIVISOR[m.group(1).lower()]


@register("bb_bop_row")
class BbBopRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        row_label = _parse_instruction(instruction)
        raw_html = artifact.artifact_path.read_text()
        soup = BeautifulSoup(raw_html, "html.parser")

        rows: list[list[str]] = []
        for tr in soup.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            rows.append([c.get_text(strip=True) for c in cells])

        header_cells: list[str] | None = None
        for texts in rows:
            if any(_score_column_header(t) is not None for t in texts):
                header_cells = texts
                break
        if header_cells is None:
            raise ParseError("no fiscal-year header row found in artifact")
        col_idx = _select_column(header_cells)

        want = _norm(row_label)
        seen: list[str] = []
        data_cells: list[str] | None = None
        for texts in rows:
            first = texts[0]
            seen.append(first)
            if _norm(first) == want:
                data_cells = texts
                break
        if data_cells is None:
            raise ParseError(f"row {row_label!r} not found; saw {seen}")
        if col_idx >= len(data_cells):
            raise ParseError(
                f"row {row_label!r} has only {len(data_cells)} cells, "
                f"need header-selected column index {col_idx}"
            )

        raw_value = _to_number(data_cells[col_idx])
        divisor = _detect_unit_divisor(raw_html)
        return ParseResult(value=raw_value / divisor, _parse_strategy="bb_bop_row")
