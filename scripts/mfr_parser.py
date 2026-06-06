"""Deterministic parser for the MOF Monthly Fiscal Report (MFR) PDFs.

Extracts the *single-month* government bank borrowing (Table 6, row
``2.1 Borrowing from Banking System (Net)``) and *single-month* NBR tax
revenue (Table 4, row ``a. NBR``) from a downloaded MFR PDF.

Why this is its own module (mirrors econdelta/parsers/pdf_table_*.py style):
the MFR column layout is NOT fixed-position. ``pdfplumber.extract_tables()``
mangles these grids (numbers collapse into a single cell), so we parse the
clean ``extract_text()`` lines instead. Crucially, the number of columns
changes between the first month of a fiscal year (July) and later months:

  Later months (Aug+):  Budget FY25 | Revised FY25 | Act FY25 (month) |
                        Act FY25 (upto month) | Act FY25 (full yr) |
                        Budget FY26 | Act FY26 (month) | Act FY26 (upto month)

  First month (July):   Budget FY25 | Revised FY25 | Act FY25 (July) |
                        Act FY25 (full yr) | Budget FY26 | Act FY26 (July) |
                        <repeat> | <repeat>

So a fixed column index would silently read the wrong number. The robust
anchor used here: locate the *current-FY annual budget* value (stable all fiscal
year), then the single-month figure is the next number and the FYTD figure
is the one after that. This is layout-independent and self-validating.

The FYTD-diff self-check (this-month FYTD minus prior-month FYTD ~= the
published single-month figure) is computed by the caller across the full
month series; see backfill_fiscal.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pdfplumber

# Numeric token: optional leading minus, digits with optional thousands commas,
# optional decimal. Matches "-7,521", "104,000", "3.54", "0".
_NUM = re.compile(r"-?\d[\d,]*\.?\d*")

# Row labels — matched after whitespace-insensitive normalization so PDF
# spacing quirks ("2.1Borrowing", "a. NBR") don't break the anchor. We use the
# stable fragment "Borrowing from Banking" (NOT the full "...System (Net)")
# because in some issues (August 2025) the "System (Net)" suffix wraps onto a
# later line and the row's numbers sit on a standalone line in between.
_BANK_BORROW_LABEL = "Borrowing from Banking"
_NBR_LABEL = "a. NBR"

# Table page anchors. We anchor on the *data row label* rather than the table
# title, because the table titles ("Table 4: Revenue...") also appear in the
# CONTENTS / List-of-Tables pages near the front of the PDF. Anchoring on the
# data row guarantees we land on the real table page, not the TOC. We use the
# shortened "Borrowing from Banking" fragment so wrapped-label issues (August
# 2025, where "System (Net)" spills onto a separate line) still detect the page.
_TABLE6_ANCHOR = "Borrowing from Banking"
_TABLE4_ANCHOR = "a. NBR"

# Report month/year anchor on page 1, e.g. "October 2025" or "July 2025".
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_REPORT_MONTH_RE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})\b",
    re.IGNORECASE,
)


class MfrParseError(Exception):
    """Raised when a required table/row/value cannot be located in the MFR."""


@dataclass(frozen=True)
class FiscalRow:
    """A single extracted single-month + FYTD pair, in BDT crore."""

    metric: str          # "govt_bank_borrow" | "nbr_revenue"
    single_month: float  # this report-month's stand-alone figure
    fytd: float          # fiscal-year-to-date figure as of this report month
    fy_budget: float     # the annual current-FY budget anchor used (provenance)


def _to_float(token: str) -> float:
    return float(token.replace(",", ""))


# A "numeric continuation" line is one made up only of numbers + whitespace.
# In some MFR issues (e.g. August 2025) the long "2.1 Borrowing from Banking
# System (Net)" label wraps across 2-3 lines and its numbers land on a
# standalone numeric line wedged between the wrapped label fragments.
_NUMERIC_LINE_RE = re.compile(r"^[\s\d,.\-]+$")


def _tokens_of(line: str) -> list[str]:
    return [t for t in _NUM.findall(line) if re.search(r"\d", t)]


def _numbers_after_label(text: str, label: str, *, lookahead: int = 3) -> list[str]:
    """Return numeric tokens for the row anchored by ``label``.

    Handles three observed layouts:
      1. Label + numbers on one line ("2.1Borrowing... 137,500 99,000 ...").
      2. Label on one line, a partial match where the label key is a prefix.
      3. Wrapped label whose numbers sit on a standalone numeric line within
         the next ``lookahead`` lines (August-2025 style).

    Matching is whitespace-insensitive on the label so "2.1Borrowing" and
    "2.1 Borrowing from Banking System (Net)" (even when wrapped) both anchor.
    """
    label_norm = re.sub(r"\s+", "", label.lower())
    lines = text.split("\n")
    for li, line in enumerate(lines):
        line_norm = re.sub(r"\s+", "", line.lower())
        if label_norm not in line_norm:
            continue
        # Case 1/2: numbers on the same line, after the label text.
        collapsed = ""
        for i, ch in enumerate(line):
            if not ch.isspace():
                collapsed += ch.lower()
            if collapsed.endswith(label_norm):
                rest = line[i + 1:]
                same_line = _tokens_of(rest)
                if same_line:
                    return same_line
                break
        # Case 3: wrapped label — scan the next few lines for the first
        # standalone numeric line (the row's values).
        for nxt in lines[li + 1: li + 1 + lookahead]:
            if _NUMERIC_LINE_RE.match(nxt) and _tokens_of(nxt):
                return _tokens_of(nxt)
        # If we matched a label fragment only (e.g. "2.1 Borrowing from Banking"
        # with the System(Net) part on a later line), keep scanning subsequent
        # lines too in case this fragment preceded the numeric line.
    raise MfrParseError(f"label {label!r} not found (or no numeric row) in table text")


def _extract_month_fytd(numbers: list[float], fy_budget: float) -> tuple[float, float]:
    """Given the numeric row tokens and the known FY annual-budget anchor,
    return (single_month, fytd).

    The single-month figure is the value immediately AFTER the FY budget
    anchor; the FYTD figure is the one after that. For the first month of a
    fiscal year (July), the report repeats the month figure in the FYTD slot,
    which is correct (month == FYTD for month 1).

    Raises MfrParseError if the anchor isn't found or has no trailing value.
    """
    # Find the LAST occurrence of the budget anchor (the current-FY block is the
    # trailing group; FY25 budget is identical only by coincidence and lives
    # earlier — using the last match targets the current-FY block).
    anchor_idx = None
    for i, v in enumerate(numbers):
        if abs(v - fy_budget) < 0.5:
            anchor_idx = i
    if anchor_idx is None:
        raise MfrParseError(
            f"FY budget anchor {fy_budget} not found in row numbers {numbers}"
        )
    if anchor_idx + 2 >= len(numbers):
        raise MfrParseError(
            f"FY budget anchor at index {anchor_idx} has no month+fytd "
            f"following it in {numbers}"
        )
    single_month = numbers[anchor_idx + 1]
    fytd = numbers[anchor_idx + 2]
    return single_month, fytd


def _find_page_text(pdf: pdfplumber.PDF, anchor: str) -> str:
    for page in pdf.pages:
        txt = page.extract_text() or ""
        if anchor in txt:
            return txt
    raise MfrParseError(f"page containing {anchor!r} not found")


def parse_report_month(pdf_path: str) -> tuple[int, int]:
    """Return (year, month) of the report from page 1's title line.

    e.g. "Monthly Report on Fiscal Position / October 2025" -> (2025, 10).
    """
    with pdfplumber.open(pdf_path) as pdf:
        txt = pdf.pages[0].extract_text() or ""
    m = _REPORT_MONTH_RE.search(txt)
    if not m:
        raise MfrParseError(f"could not parse report month from page 1 of {pdf_path}")
    return int(m.group(2)), _MONTHS[m.group(1).lower()]


def parse_bank_borrowing(pdf_path: str, *, fy_budget_crore: float) -> FiscalRow:
    """Extract single-month + FYTD govt bank borrowing (Table 6) in BDT crore.

    Args:
        pdf_path: local path to the MFR PDF.
        fy_budget_crore: the current fiscal year's annual *budget* for the
            banking-borrowing row (the stable anchor, e.g. 104000 for FY26).
    """
    with pdfplumber.open(pdf_path) as pdf:
        txt = _find_page_text(pdf, _TABLE6_ANCHOR)
    tokens = _numbers_after_label(txt, _BANK_BORROW_LABEL)
    numbers = [_to_float(t) for t in tokens]
    single, fytd = _extract_month_fytd(numbers, fy_budget_crore)
    return FiscalRow("govt_bank_borrow", single, fytd, fy_budget_crore)


def parse_nbr_revenue(pdf_path: str, *, fy_budget_crore: float) -> FiscalRow:
    """Extract single-month + FYTD NBR tax revenue (Table 4) in BDT crore.

    Args:
        pdf_path: local path to the MFR PDF.
        fy_budget_crore: the current fiscal year's annual *budget* for NBR
            (the stable anchor, e.g. 499001 for FY26).
    """
    with pdfplumber.open(pdf_path) as pdf:
        txt = _find_page_text(pdf, _TABLE4_ANCHOR)
    tokens = _numbers_after_label(txt, _NBR_LABEL)
    numbers = [_to_float(t) for t in tokens]
    single, fytd = _extract_month_fytd(numbers, fy_budget_crore)
    return FiscalRow("nbr_revenue", single, fytd, fy_budget_crore)
