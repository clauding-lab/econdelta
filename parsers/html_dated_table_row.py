"""Parser for BB's plain "econdata" HTML tables (``id="sortableTable"``)
whose header names each data column with a "Mon, YYYY" label, and whose
data rows optionally carry a trailing 2-column "Percentage Changes" group
(a month-over-month %, then a year-over-year %).

Verified live 2026-08-22 against three real BB pages sharing this exact
shape: ``econdata/inflation`` (2 data rows, no pct group), ``econdata/
monetarysurvey`` and ``econdata/moneysupply`` (many data rows, WITH a pct
group). All three sit behind BB's F5/TSPD JS challenge (AGENTS.md landmine
3) — the PDF this used to be scraped from (BB's Selected Macroeconomic
Indicators bulletin, landmine 39's "monthly bulletin cannot carry an
intra-month decision" lesson) publishes with a real lag; these HTML pages
are BB's own live, current-month view.

Unlike ``html_table_row`` (AGENTS.md ``_NEVER_DATED_PARSE_STRATEGIES``),
this parser DOES recover ``source_as_of`` — from the header's own "latest
month" label — because callers of this parser (the CPI-monthly-append leg
in particular) need a genuine month-end vintage, not a forged run-date.

Instruction syntax: ``row="<exact label>" [section="<hint>"] col=<slot>``

- ``row="<label>"``: the FULL row-label text, matched EXACTLY after
  whitespace/case normalization — never a substring (AGENTS.md landmine 46's
  quoted-row fix; a bare-token/substring match on a table this size would
  silently collide with a differently-scoped row of the same name, see
  ``section=`` below).
- ``section="<hint>"`` (optional): a case-insensitive substring matched
  against the text of the NEAREST PRECEDING "section" row — a row whose own
  label starts with a bracketed letter, e.g. ``"(a) BANGLADESH BANK"`` /
  ``"(b) DEPOSIT MONEY BANKS"``. Confirmed live: BB's monetarysurvey table
  repeats the IDENTICAL leaf label ``"Claims on Private Sector"`` once under
  ``(a) BANGLADESH BANK`` (a tiny, near-irrelevant figure) and once under
  ``(b) DEPOSIT MONEY BANKS`` (the number everyone means by "private sector
  credit growth") — these are NOT interchangeable and must never be
  confused (AGENTS.md landmine 45/49 discipline: select by semantics,
  never by first-match-wins position). ``section=`` is REQUIRED whenever
  ``row=`` alone would match more than one row; ``parse()`` raises
  ``ParseError`` on an unresolved ambiguity rather than guessing. NOTE:
  tracking is single-level (the nearest preceding bracketed-letter row
  only, not a full ancestor path) — sufficient for every table shape
  actually observed, but a table nesting the SAME (label, section) pair two
  levels deep would still be ambiguous; none of the three pages this
  parser was built for do that. KNOWN LIMITATION (Opus review round 1, M4,
  documented not fixed — see ``tests/test_html_dated_table_row.py``'s
  ``TestBracketedLetterSectionKnownLimitation``): the section-marker regex
  matches ANY single bracketed Latin letter, so a Roman-numeral-style
  sub-item row like ``"(i) ..."`` between a real lettered section and its
  child rows would incorrectly overwrite the tracked section, making rows
  after it unreachable via their true section. None of the three pages
  this parser currently serves contain such a row; fixing this blind would
  need a real observed table shape to design against rather than a guess.
- ``col=<slot>``: one of
    ``latest``  — the first value column (immediately after the label) —
                  the CURRENT/most-recent month's absolute reading.
    ``yoy_pct`` — the LAST column of the "Percentage Changes" group (the
                  latest month's value vs. the SAME month one year prior).
    ``mom_pct`` — the FIRST column of that same group (latest vs. the
                  immediately preceding month).
  ``yoy_pct``/``mom_pct`` raise ``ParseError`` if the table has no
  "Percentage Changes" group at all (e.g. the inflation table) — never
  silently fall back to some other column.

``source_as_of`` is always derived from the LATEST column's own header
month (month-end), regardless of which ``col=`` slot was requested — a
YoY% figure is still dated to the month it describes, not the comparator
year.

Column positions (once the header is located) are fixed by BB's own
observed convention (label, then latest/prior/year-ago, then optionally
a 2-column pct group) rather than re-resolved per data row — a full
per-row header-text column lookup isn't possible here because the
"Percentage Changes" super-header spans two columns with no per-column
label repeated on every row. The header-row detection above is what stays
robust to a differently-titled document; a genuine mid-table column
insertion within the value/pct block would not be caught by this parser
and is a documented residual risk (see the file-level discussion above).
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

_MONTH_ABBR: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_YEAR_RE = re.compile(r"\b([A-Za-z]{3,9})\.?,?\s*(\d{4})\b")
_SECTION_RE = re.compile(r"^\([a-zA-Z]\)\s")
_PCT_GROUP_LABEL = "percentage changes"

_INSTRUCTION_RE = re.compile(
    r'row="(?P<row>[^"]+)"'
    r'(?:\s+section="(?P<section>[^"]+)")?'
    r"\s+col=(?P<col>\S+)"
)
_VALID_COLS = frozenset({"latest", "yoy_pct", "mom_pct"})


def _normalize(text: str | None) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _parse_instruction(instruction: str) -> tuple[str, str | None, str]:
    m = _INSTRUCTION_RE.search(instruction)
    if not m:
        raise ParseError(
            'instruction must be \'row="<label>" [section="<hint>"] '
            f"col=<latest|yoy_pct|mom_pct>', got {instruction!r}"
        )
    col = m.group("col")
    if col not in _VALID_COLS:
        raise ParseError(f"col= must be one of {sorted(_VALID_COLS)}, got {col!r}")
    return m.group("row"), m.group("section"), col


def _month_year(text: str) -> tuple[int, int] | None:
    """Parse 'Jun, 2026' / 'Jun,2026' -> (year, month). None if unparseable."""
    m = _MONTH_YEAR_RE.search(text or "")
    if not m:
        return None
    month = _MONTH_ABBR.get(m.group(1)[:3].lower())
    if month is None:
        return None
    return int(m.group(2)), month


def _month_end(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _to_number(text: str) -> float:
    stripped = (text or "").strip()
    negative = stripped.startswith("(") and stripped.endswith(")")
    if negative:
        stripped = stripped[1:-1]
    cleaned = re.sub(r"[^0-9.\-]", "", stripped.replace("%", ""))
    if not cleaned or cleaned == "-":
        raise ParseError(f"no number in cell text {text!r}")
    try:
        value = float(cleaned)
    except ValueError as e:
        raise ParseError(f"no number in cell text {text!r}") from e
    return -value if negative else value


def _is_placeholder(text: str | None) -> bool:
    """True for an empty cell or a dash/NA placeholder -- BB's convention
    for "not yet published" (verified live: bare "-"/"--"/"---" on both
    monetarysurvey and moneysupply for still-forming rows)."""
    stripped = (text or "").strip()
    return not stripped or set(stripped) <= {"-"} or stripped.upper() in {"N/A", "NA"}


def _resolve_header(rows: list[list[str]]) -> tuple[list[tuple[int, tuple[int, int]]], int | None]:
    """Locate the primary header row (the first row with >=2 "Mon, YYYY"
    cells AFTER column 0) and return (month_cols, pct_group_col).

    ``month_cols`` is every (column_index, (year, month)) pair found on
    that header row, SORTED NEWEST-FIRST by the actual parsed (year, month)
    value (Opus review round 1, M1) -- never by column position. BB has, in
    every page this parser serves, always put the newest month first
    left-to-right, but a table whose column order doesn't already carry
    that assumption in its own header text (verified live) never needs to
    rely on it: this function reads the dates and sorts them itself.

    Column 0 is EXCLUDED from the month-cell scan: it is always the row's
    own label column by BB's own convention on these pages, never a data/
    month column -- and the inflation table's own label cell ("Rate of
    Inflation (as measured by CPI, from Apr,2023 base 2021-22)") happens to
    contain a real "Apr,2023" substring that would otherwise be
    misidentified as a month column instead of the true header row's real
    "Jul, 2026" (confirmed live 2026-08-22 -- this is not a hypothetical).

    ``pct_group_col`` is the column index of the "Percentage Changes" group
    header cell — which, once trailing month columns are on the header row
    the same way they land on data rows, is also the FIRST of the two pct
    columns on every data row (BB never re-labels the group per row, so
    this position is fixed once located, not re-derived per row). Scoped to
    AFTER every month column (not just the newest), so the group is found
    correctly regardless of which month column happens to sort newest.
    None if the table has no such group.
    """
    for row in rows:
        month_cols_raw = [
            (idx, my)
            for idx, cell in enumerate(row)
            if idx > 0
            for my in [_month_year(cell)]
            if my
        ]
        if len(month_cols_raw) < 2:
            continue
        month_cols = sorted(month_cols_raw, key=lambda t: t[1], reverse=True)
        max_month_idx = max(idx for idx, _my in month_cols)
        pct_group_col = None
        for idx, cell in enumerate(row):
            if idx > max_month_idx and _normalize(cell) == _PCT_GROUP_LABEL:
                pct_group_col = idx
                break
        return month_cols, pct_group_col
    return [], None


def _find_row_cell(
    rows: list[list[str]], *, row_label: str, section_hint: str | None, target_col: int,
) -> str:
    """Locate the row and return its RAW cell text at ``target_col`` --
    NOT yet converted to a number, so a caller can inspect the raw text
    (e.g. detect a dash/placeholder, M3 below) before deciding which
    column to actually convert."""
    target_norm = _normalize(row_label)
    section_hint_norm = _normalize(section_hint) if section_hint else None
    current_section = ""
    matches: list[str] = []
    for row in rows:
        if not row:
            continue
        raw_first = row[0] or ""
        if _SECTION_RE.match(raw_first.strip()):
            current_section = _normalize(raw_first)
            continue
        if _normalize(raw_first) != target_norm:
            continue
        if section_hint_norm is not None and section_hint_norm not in current_section:
            continue
        if target_col >= len(row):
            raise ParseError(
                f"row {row_label!r} has only {len(row)} cell(s), need column {target_col + 1}"
            )
        matches.append(row[target_col])
    if not matches:
        suffix = f" (section={section_hint!r})" if section_hint else ""
        raise ParseError(f"row {row_label!r}{suffix} not found")
    if len(matches) > 1:
        suffix = f" within section={section_hint!r}" if section_hint else ""
        raise ParseError(
            f"{len(matches)} rows match {row_label!r}{suffix} -- ambiguous, "
            "refusing to guess (add/narrow section=)"
        )
    return matches[0]


def _find_row_value(
    rows: list[list[str]], *, row_label: str, section_hint: str | None, target_col: int,
) -> float:
    """Locate + convert in one call -- used by callers (col=yoy_pct/
    mom_pct) that don't need the M3 placeholder fallback below (a
    percentage-change column has no "prior column" concept the same way
    "latest" does)."""
    return _to_number(
        _find_row_cell(rows, row_label=row_label, section_hint=section_hint, target_col=target_col)
    )


@register("html_dated_table_row")
class HtmlDatedTableRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        from bs4 import BeautifulSoup

        row_label, section_hint, col = _parse_instruction(instruction)
        soup = BeautifulSoup(artifact.artifact_path.read_text(encoding="utf-8"), "html.parser")
        table = soup.find("table", id="sortableTable")
        if table is None:
            raise ParseError('no <table id="sortableTable"> found (page structure changed?)')

        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            for tr in table.find_all("tr")
        ]

        month_cols, pct_group_col = _resolve_header(rows)
        if not month_cols:
            raise ParseError("could not locate a header row with >=2 'Mon, YYYY' columns")
        latest_idx, latest_month = month_cols[0]

        if col == "latest":
            # M3 (Opus review round 1): the latest column can be a genuine
            # dash/placeholder cell -- BB has published this month's
            # column header already but not yet the figure itself (row
            # still forming). Falling through to the LLM path on a
            # ParseError here would ask an LLM to invent a number BB
            # hasn't published; using the PRIOR month's column (with ITS
            # own source_as_of, never the latest column's month) is a
            # real, if slightly stale, reading rather than a fabricated
            # fresh one.
            cell_text = _find_row_cell(
                rows, row_label=row_label, section_hint=section_hint, target_col=latest_idx,
            )
            source_month = latest_month
            if _is_placeholder(cell_text) and len(month_cols) >= 2:
                prior_idx, prior_month = month_cols[1]
                cell_text = _find_row_cell(
                    rows, row_label=row_label, section_hint=section_hint, target_col=prior_idx,
                )
                source_month = prior_month
            value = _to_number(cell_text)
        else:
            if pct_group_col is None:
                raise ParseError(
                    f"col={col!r} requested but this table has no 'Percentage "
                    "Changes' column group"
                )
            target_col = pct_group_col if col == "mom_pct" else pct_group_col + 1
            value = _find_row_value(
                rows, row_label=row_label, section_hint=section_hint, target_col=target_col,
            )
            source_month = latest_month

        return ParseResult(
            value=value,
            _parse_strategy="html_dated_table_row",
            source_as_of=_month_end(*source_month),
        )
