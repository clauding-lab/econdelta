"""Extract a value from a specific (page, table_index, row_label) triple in a PDF.

Also recovers ``source_as_of`` (the report's reporting period-end) so slow-cadence
figures from this parser can't display undated and stale — the bug that let a
stale NPL outrank a fresher press figure (PR #64/#65). This parser is shared by
25 indicators across 4-5 report families; two of them carry a recoverable as-of
date INSIDE the PDF and are dated here (every other source returns None — a safe
no-op, since their cadence/global-as_of handling is unchanged):

  - BB "Major Economic Indicators: Monthly Update" — every page header reads
    "Monthly Update (April 2026)"; the cover adds "Volume 04/2026 April 2026".
    The figure is as-of the END of that report month.
  - MoF "Quarterly Debt Bulletin" — cover carries no date; the body states
    "As of 31 December 2025" and "... up to Dec FY26" beside the debt table.

The report is identified by a stable CONTENT marker (the report's own title), not
by URL host: ``latest_pdf_link`` discovery rewrites ``source_url`` to the resolved
PDF link, which for MoF is a third-party object store (not ``mof.gov.bd``). The
marker is the full title ("major economic indicators" / "quarterly debt bulletin")
so a sibling MoF fiscal report that merely *mentions* "debt bulletin" in passing
is not mis-gated into the debt-date branch.

These are narrative+tabular government reports that also print comparison/prior
dates ("as of 30 June 2024", "up to Jun FY25"). To avoid locking onto a STALE
comparison date — the exact NPL-class failure this recovery exists to prevent —
recovery selects the LATEST date among ALL idiom matches, never the first. Any
unrecognised report / missing idiom yields None — caught by the slow-cadence
guard in aggregate_latest, the safe failure (no wrong date is ever fabricated).
Date recovery is best-effort and isolated so it can never break value extraction.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date

import pdfplumber

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.html_table_row import _to_number
from parsers.registry import register

logger = logging.getLogger(__name__)

# Month name OR 3-letter abbreviation → month number (keyed on first 3 letters,
# which are unique across all 12 months: jan feb mar apr may jun jul aug sep …).
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# BB Major Economic Indicators monthly-update idioms. Group 1: month, Group 2: year.
_BB_MONTHLY_RE = re.compile(r"Monthly Update\s*\(\s*([A-Za-z]+)\s+(\d{4})\s*\)", re.IGNORECASE)
_BB_VOLUME_RE = re.compile(r"Volume\s+\d{1,2}/\d{4}\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)

# MoF Debt Bulletin idioms. Preferred: an explicit "As of DD Month YYYY" (gives
# the exact day). Fallback: "up to <Mon> FY<NN>" beside the debt-stock table.
_MOF_AS_OF_RE = re.compile(r"\bas\s+of\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)
_MOF_UP_TO_FY_RE = re.compile(r"\bup\s+to\s+([A-Za-z]{3,9})\s+FY\s*'?(\d{2})\b", re.IGNORECASE)


def _month_end(year: int, month: int) -> date | None:
    """Last calendar day of (year, month); None if the values are invalid."""
    try:
        return date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        return None


def _month_end_from(month_token: str, year_token: str) -> date | None:
    """Build the month-end date from a (month-name, 4-digit-year) idiom match."""
    month = _MONTHS.get(month_token[:3].lower())
    if month is None:
        return None
    return _month_end(int(year_token), month)


def _bb_report_date(text: str) -> date | None:
    """BB monthly update → the LATEST report month named (the current edition is
    the most recent month; a comparison to a prior edition must never win)."""
    for rx in (_BB_MONTHLY_RE, _BB_VOLUME_RE):
        dates = [d for m in rx.finditer(text)
                 if (d := _month_end_from(m.group(1), m.group(2))) is not None]
        if dates:
            return max(dates)
    return None


def _mof_report_date(text: str, indicator_id: str = "") -> date | None:
    """MoF Debt Bulletin → the LATEST reporting quarter-end among all idiom matches.

    Preferred: the explicit "As of DD Month YYYY" form (exact day). If that idiom
    is PRESENT but every occurrence has an out-of-range day, the report's own date
    is corrupt — return None rather than borrowing the unrelated "up to FY" chart
    label (which can name a different month)."""
    as_of_seen = False
    as_of_dates: list[date] = []
    for m in _MOF_AS_OF_RE.finditer(text):
        as_of_seen = True
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month is None:
            continue
        try:
            as_of_dates.append(date(int(m.group(3)), month, int(m.group(1))))
        except ValueError:
            logger.debug("MoF as-of day out of range for %s: %s", indicator_id, m.group(0))
    if as_of_dates:
        return max(as_of_dates)  # newest current-period date, never an older comparison one
    if as_of_seen:
        return None  # preferred idiom present but malformed → safe failure, don't fall through
    # Fallback: "up to <Mon> FY<NN>" beside the debt-stock table. BD fiscal year
    # FY<NN> = Jul (NN-1) … Jun (NN): Jul–Dec fall in the prior calendar year,
    # Jan–Jun in the FY year itself.
    fy_dates: list[date] = []
    for m in _MOF_UP_TO_FY_RE.finditer(text):
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month is None:
            continue
        fy = 2000 + int(m.group(2))
        fy_dates.append(_month_end(fy if month <= 6 else fy - 1, month))
    valid = [d for d in fy_dates if d is not None]
    return max(valid) if valid else None


def _recover_report_date(text: str, indicator_id: str = "") -> date | None:
    """Recover the report's as-of date from its full text. Identify the report by
    its title marker, then apply that report's idiom. Returns None for any
    unrecognised report or missing date — never fabricates one."""
    low = text.lower()
    if "major economic indicators" in low:
        return _bb_report_date(text)
    if "quarterly debt bulletin" in low:
        return _mof_report_date(text, indicator_id)
    return None


def _safe_recover(text: str, indicator_id: str) -> date | None:
    """Run date recovery without ever breaking a successful value parse."""
    try:
        return _recover_report_date(text, indicator_id)
    except Exception as exc:  # noqa: BLE001 — date recovery is non-essential
        logger.debug("source_as_of recovery failed for %s: %s", indicator_id, exc)
        return None


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
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            page = pdf.pages[page_idx]
            tables = []
            for settings in _TABLE_SETTINGS:
                tables = page.extract_tables(settings) if settings else page.extract_tables()
                if tables:
                    break
        if tbl_idx >= len(tables):
            raise ParseError(f"table {ins['table']} > {len(tables)} on page {ins['page']}")
        source_as_of = _safe_recover(full_text, artifact.indicator_id)
        for row in tables[tbl_idx]:
            if row and row[0] and row_label.lower() in str(row[0]).lower():
                if col >= len(row):
                    raise ParseError(f"row has {len(row)} cols, need {ins['col']}")
                cell = row[col]
                if cell is None:
                    raise ParseError(f"cell at col {col} is empty")
                return ParseResult(
                    value=_to_number(str(cell)),
                    _parse_strategy="pdf_table_row",
                    source_as_of=source_as_of,
                )
        raise ParseError(f"row {row_label!r} not found in page {ins['page']} table {ins['table']}")

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Recover the report's as-of date even when value extraction fails and the
        LLM path supplies the value (mirrors pdf_component). Best-effort: any read
        error yields None rather than breaking the parse."""
        try:
            with pdfplumber.open(artifact.artifact_path) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as exc:  # noqa: BLE001 — recovery must never be fatal
            logger.debug(
                "source_as_of recovery could not read PDF for %s: %s",
                artifact.indicator_id, exc,
            )
            return None
        return _recover_report_date(text, artifact.indicator_id)
