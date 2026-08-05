"""Parser for BB WSEI/MEI tabular rows where the latest period is the last
"absolute" numeric value on the row, followed by smaller pct-change columns.

Instruction syntax: ``row="<label>" [min=<float>] [page=<int>]``

The row label is matched case-insensitively as a substring on each line of
the extracted PDF text. ``min`` (default ``0``) filters out small numbers
(e.g. percentage-change columns) so the parser returns the last value whose
``abs(value) >= min``. ``page`` (optional, 1-indexed — same convention as
``pdf_table_row``'s ``page=``) scopes the line search to a single page
instead of the whole document: BB's own prose narrative below a table can
restate the row's label in a sentence ("Money multiplier was lower at 4.92
at the end of May 2026...") — on the real MEI PDF that sentence happens to
follow the data row on the SAME page, so first-match-in-document-order still
finds the table row first today, but only by document-layout luck, not by
guarantee. Page-scoping doesn't eliminate that same-page ambiguity (see
``_find_latest_in_text``'s docstring), but it does close the wider risk of a
label coincidentally matching a line on some OTHER, unrelated page — the
scoping ``pdf_table_row`` already relies on for the same document family.

Example for the WSEI Item 11 row:

    a) Reserve Money (RM) (BDT in crore) 374602.90 413179.00 424618.80 13.35 2.77 -9.44 -0.11

With ``row="a) Reserve Money" min=1000`` the parser keeps only the three
absolute values (≥ 1000) and returns ``424618.80`` — the latest period.

**Calibrating ``min=`` for a new row (the rule, not just an example):** set
``min`` to roughly HALF the row's current smallest level value — never a
fixed constant copied from another row. Levels (the absolute values you
actually want) grow over time; the flow/pct-change columns you're filtering
out are a FRACTION of the level and don't track it 1:1, so a threshold tuned
for one row's flow-vs-level gap does not transfer to another row's. Set it
too low (e.g. reusing WSEI Item 11's ``min=1000`` on a row whose own flow
columns are themselves BDT-crore-scale, not single/double-digit percentages)
and the "last value ≥ min" can silently return a flow figure instead of the
level — the exact class of bug this parser exists to avoid, just relocated
into the config instead of fixed. Recheck the calibration whenever the row's
magnitude shifts meaningfully (a currency devaluation, a fiscal-year
definition change, etc.).

``source_as_of`` is recovered from the BB "Major Economic Indicators: Monthly
Update" cover idiom, gated on the document's own title (mirrors
``pdf_component.py``'s content gate — see that module's docstring for why a
loose marker is unsafe. This parser only ever serves the MEI PDF today, so
the FSAR-collision that motivated the fuller marker there doesn't currently
apply here, but the same fuller marker is used anyway for consistency and to
fail safe if this parser is ever pointed at a different document family).
"""
from __future__ import annotations

import calendar
import re
from datetime import date

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Match a signed number with optional thousands separators and decimal.
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_ROW_RE = re.compile(r'row="([^"]+)"')
_MIN_RE = re.compile(r"min=(-?\d+(?:\.\d+)?)")
_PAGE_RE = re.compile(r"page=(\d+)")

# BB "Major Economic Indicators: Monthly Update" idiom — duplicated from
# pdf_component.py (not imported) to keep independently-registered parsers
# decoupled, matching that module's own stated convention.
_MEI_MONTHLY_RE = re.compile(r"Monthly Update\s*\(\s*([A-Za-z]+)\s+(\d{4})\s*\)", re.IGNORECASE)
_MEI_VOLUME_RE = re.compile(r"Volume\s+\d{1,2}/\d{4}\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)
_MEI_TITLE_MARK = "major economic indicators: monthly update"

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _mei_report_date(text: str) -> date | None:
    """BB "Monthly Update" idiom -> the LATEST month named, mapped to
    month-end. Gated on the full title (_MEI_TITLE_MARK) by the caller."""
    for rx in (_MEI_MONTHLY_RE, _MEI_VOLUME_RE):
        dates: list[date] = []
        for m in rx.finditer(text):
            month = _MONTH_NAMES.get(m.group(1).lower())
            if month is None:
                continue
            try:
                dates.append(date(int(m.group(2)), month, calendar.monthrange(int(m.group(2)), month)[1]))
            except ValueError:
                continue
        if dates:
            return max(dates)
    return None


def _recover_report_date(text: str) -> date | None:
    """Content-gated date recovery — see module docstring."""
    if _MEI_TITLE_MARK in text.lower():
        return _mei_report_date(text)
    return None


def _parse_instruction(instruction: str) -> tuple[str, float, int | None]:
    m = _ROW_RE.search(instruction)
    if not m:
        raise ParseError(f'instruction must include row="<label>": {instruction!r}')
    row_label = m.group(1)
    min_match = _MIN_RE.search(instruction)
    min_value = float(min_match.group(1)) if min_match else 0.0
    page_match = _PAGE_RE.search(instruction)
    page = int(page_match.group(1)) if page_match else None
    return row_label, min_value, page


def _find_latest_in_text(text: str, row_label: str, min_value: float) -> float | None:
    """Return the last number on a line containing ``row_label`` whose
    ``abs(value) >= min_value``. ``None`` if no matching line/number found.

    Returns on the FIRST matching line in document order — see module
    docstring on why ``page=`` narrows but doesn't fully eliminate the
    same-page label-collision risk this relies on.
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

        row_label, min_value, page = _parse_instruction(instruction)
        with pdfplumber.open(artifact.artifact_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            if page is not None:
                page_idx = page - 1
                if page_idx < 0 or page_idx >= len(pdf.pages):
                    raise ParseError(f"page {page} out of range (doc has {len(pdf.pages)} pages)")
                search_text = pdf.pages[page_idx].extract_text() or ""
            else:
                search_text = full_text
        value = _find_latest_in_text(search_text, row_label, min_value)
        if value is None:
            raise ParseError(
                f"no row matching {row_label!r} with a number "
                f"of magnitude >= {min_value} found" + (f" on page {page}" if page else "")
            )
        return ParseResult(
            value=value,
            _parse_strategy="pdf_table_latest",
            source_as_of=_recover_report_date(full_text),
        )

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Recover the reporting period-end date even when value extraction
        fails and the LLM path supplies the value (mirrors pdf_component /
        pdf_table_row). Best-effort — any read error yields None."""
        import pdfplumber

        try:
            with pdfplumber.open(artifact.artifact_path) as pdf:
                text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
        except Exception:  # noqa: BLE001 — recovery must never be fatal
            return None
        return _recover_report_date(text)
