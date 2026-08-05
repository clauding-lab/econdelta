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
   "2025-26PJuly-May"; the column with the highest fiscal year wins, and
   among columns sharing that year — e.g. BB's "R" (Revised, a shorter
   month-window) vs "P" (Provisional, the full window-to-date) suffix on
   the same fiscal year — the LONGEST month window wins. The revision
   letter itself is never inspected; the tiebreak is purely
   "how many months does this column cover", which is what "R vs P"
   actually cashes out to on the real page. A "% Changes" delta column
   carries no fiscal-year/month pattern and is naturally excluded.
2. **Unit scale is detected from the SELECTED table's own "In million US$" /
   "In billion US$" label**, not assumed and not searched page-wide (a stray
   unit label elsewhere in the document must not silently rescale the
   value). Converts to the billions the `value_type=amount_usd_bn` config
   expects.
3. **Header row and data row are resolved WITHIN THE SAME `<table>`
   element.** Early versions of this parser walked a flat, document-wide
   list of `<tr>` — on a page with more than one `<table>` (e.g. a decoy or
   an unrelated table above the real one), that let the header come from
   one table while the data came from another, silently producing an
   in-range but wrong value (proved in review: a decoy table's header
   pushed column selection to the WRONG index, reading the real table's
   Jul-Apr column instead of Jul-May — a value that still passes
   `validate_value`). The parser now walks `soup.find_all("table")` and,
   for each table, requires BOTH the row match and a scoreable header row
   inside that same element before accepting a result.

Column selection derives entirely from header text — no fixed column count
is assumed — so it does not by itself break on a table shaped differently
from the monthly BoP page. That said, the yearly BoP page
(`/econdata/bop_yearly/1`) has NOT been fetched or tested against this
parser: if its column headers state a fiscal year without a month-window
(plausible for a page whose figures are already annual totals), this parser
raises `ParseError` rather than guessing — a safe failure (falls through to
hold-last-good), but the yearly page is not confirmed to work through this
parser as shipped.

Instruction syntax: ``row=<label>``. The row label is matched EXACTLY after
normalisation (case-folded, whitespace collapsed) — not a substring — so a
row-name collision (e.g. "Current Account Balance" vs "A. Current Account
Balance") fails loudly instead of silently matching the wrong row. Mirrors
the exact-match design of `html_labeled_value.py` (2026-08-03) rather than
`html_table_row.py`'s older substring style — see AGENTS.md landmine 39 for
why "fail loud on a rename" beats "quietly match a neighbour".
"""
from __future__ import annotations

import calendar
import re
from datetime import date

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


def _detect_unit_divisor(scoped_text: str) -> float:
    """Divisor to convert the table's stated unit to USD billion.

    Reads the unit from the SCOPED text passed in (the selected table's own
    markup, not the whole page — a stray "In billion US$" label elsewhere
    on the page must not silently rescale this value) instead of assuming
    millions.
    """
    m = _UNIT_RE.search(scoped_text)
    if not m:
        raise ParseError("could not find an 'In million/billion US$' unit label in this table")
    return _UNIT_DIVISOR[m.group(1).lower()]


def _column_end_date(header_text: str) -> date | None:
    """The real-world last day of the period a BoP column header covers,
    e.g. "2025-26PJuly-May" -> 2026-05-31.

    Used as `source_as_of` so a metric read mid-month is dated by its real
    reporting period rather than today's run date — the "stale value reads
    as fresh" failure this codebase has fixed elsewhere (AGENTS.md landmine
    26). Returns None when the header carries no fiscal-year/month-window
    pattern (mirrors `_score_column_header`).
    """
    fy_match = _FY_RE.search(header_text)
    month_match = _MONTH_RANGE_RE.search(header_text)
    if not fy_match or not month_match:
        return None
    fy_start = int(fy_match.group(1))
    start_month = _MONTH_ORDER[month_match.group(1).lower()]
    end_month = _MONTH_ORDER[month_match.group(2).lower()]
    # BD fiscal year starts July. The end month falls in the SAME calendar
    # year as fy_start only when it is >= the start month (e.g. a
    # hypothetical "July-December" column); the common case ("July-May")
    # wraps into the following calendar year.
    end_year = fy_start if end_month >= start_month else fy_start + 1
    last_day = calendar.monthrange(end_year, end_month)[1]
    return date(end_year, end_month, last_day)


@register("bb_bop_row")
class BbBopRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        row_label = _parse_instruction(instruction)
        raw_html = artifact.artifact_path.read_text()
        soup = BeautifulSoup(raw_html, "html.parser")

        want = _norm(row_label)
        seen: list[str] = []
        for table in soup.find_all("table"):
            rows: list[list[str]] = []
            for tr in table.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if not cells:
                    continue
                rows.append([c.get_text(strip=True) for c in cells])

            data_cells: list[str] | None = None
            for texts in rows:
                seen.append(texts[0])
                if _norm(texts[0]) == want:
                    data_cells = texts
                    break
            if data_cells is None:
                continue  # row not in THIS table — try the next one

            header_cells: list[str] | None = None
            for texts in rows:
                if any(_score_column_header(t) is not None for t in texts):
                    header_cells = texts
                    break
            if header_cells is None:
                raise ParseError(
                    f"row {row_label!r} found in a table with no fiscal-year "
                    f"header row of its own"
                )
            col_idx = _select_column(header_cells)
            if col_idx >= len(data_cells):
                raise ParseError(
                    f"row {row_label!r} has only {len(data_cells)} cells, "
                    f"need header-selected column index {col_idx}"
                )

            raw_value = _to_number(data_cells[col_idx])
            divisor = _detect_unit_divisor(str(table))
            source_as_of = _column_end_date(header_cells[col_idx])
            return ParseResult(
                value=raw_value / divisor,
                _parse_strategy="bb_bop_row",
                source_as_of=source_as_of,
            )

        raise ParseError(f"row {row_label!r} not found in any table; saw {seen}")
