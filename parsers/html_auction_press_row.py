"""Extract one instrument's accepted amount from a BB auction-result press release.

Source: the daily Bangladesh Bank press release "Result of the Auction of Repo,
ALS, SLF, SDF and IBLF held on <date>" (``mediaroom/press_release_details/rrpt/<id>``).
The same page carries the accepted amounts for several standing-facility
instruments — SLF, SDF, Repo, ALS, IBLF — so ONE press release feeds multiple
EconDelta metrics, each selecting its own instrument row.

Why a new parser (not ``html_table_row``): BB renders these releases
inconsistently — sometimes as an HTML ``<table>``, sometimes as prose
("Total accepted amount of SLF stood at Tk. 1,234.5 crore"). ``html_table_row``
only handles a clean ``<tr>`` grid. This parser tries the table first, then
falls back to a label-anchored regex over the page text, so the deterministic
path survives either layout (landmine E — label matching over absolute index).

NULL-vs-ZERO contract (S7, landmine C): BB has largely stopped routine daily
*Repo* lending (shifting to SLF/ALS), so on many days the Repo line is **absent**
from the release. When the requested instrument's row/label is not present, this
parser raises ``ParseError`` — it NEVER fabricates a measured ``0``. The hybrid
orchestrator then falls through to the LLM extract, whose prompt is instructed to
return ``null`` for an absent instrument; a ``null`` extract produces a
``needs_review`` snapshot that ``aggregate_latest`` drops (no row written that
day) rather than carrying forward a stale value or minting a fake zero. A
genuine measured ``0`` (an instrument present in the release with an accepted
amount of zero) is a DIFFERENT case and IS returned as ``0.0``.

Instruction grammar (machine tokens, whitespace-separated):

    instrument=<label> [unit=crore]

  - ``instrument`` : the facility label to match (case- and whitespace-
                     insensitive substring), e.g. ``instrument=SLF`` or
                     ``instrument=Repo``. Required.
  - ``unit``       : documentation hint only (the press release reports Tk
                     crore); not used to convert — the raw crore figure is
                     returned as ``amount_bdt_crore``.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# A Tk-crore amount as it appears in BB prose/tables: "1,234.5", "0", "12345".
_AMOUNT_RE = re.compile(r"-?\d[\d,]*\.?\d*")
# "held on 28 May, 2025" / "held on May 28, 2025" — recover the publication date.
_HELD_ON_RE = re.compile(
    r"held\s+on\s+([0-9]{1,2}\s+[A-Za-z]+,?\s+[0-9]{4}"
    r"|[A-Za-z]+\s+[0-9]{1,2},?\s+[0-9]{4})",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _parse_instruction(instruction: str) -> str:
    out: dict[str, str] = {}
    for token in instruction.split():
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    if "instrument" not in out:
        raise ParseError(
            f"instruction missing instrument=<label>: {instruction!r}"
        )
    return out["instrument"]


def _to_number(text: str) -> float:
    cleaned = text.replace(",", "").strip()
    if not cleaned or not re.search(r"\d", cleaned):
        raise ParseError(f"no number in {text!r}")
    return float(cleaned)


def _recover_held_on(text: str):
    from datetime import date

    m = _HELD_ON_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    # Normalise "28 May, 2025" or "May 28, 2025" into a date.
    tokens = re.findall(r"[A-Za-z]+|\d+", raw)
    month = day = year = None
    for t in tokens:
        tl = t.lower()
        if tl in _MONTHS:
            month = _MONTHS[tl]
        elif t.isdigit():
            n = int(t)
            if n > 31:
                year = n
            elif day is None:
                day = n
            else:
                year = n
    if month and day and year:
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _from_table(soup: BeautifulSoup, label: str) -> float | None:
    """Return the first numeric cell on a row whose first cell contains ``label``.

    Returns None (not ParseError) so the caller can fall back to the prose
    scan before deciding the instrument is genuinely absent.
    """
    label_l = label.lower()
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        first = cells[0].get_text(" ", strip=True).lower()
        if label_l not in first:
            continue
        for cell in cells[1:]:
            txt = cell.get_text(" ", strip=True)
            nums = _AMOUNT_RE.findall(txt)
            if nums:
                return _to_number(nums[0])
    return None


# The auction-title line lists EVERY instrument as part of the release name
# ("Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on <date>"), so a
# naive label match hits it and reads the date as the amount. Skip such lines.
_TITLE_LINE_RE = re.compile(r"result\s+of\s+the\s+auction", re.IGNORECASE)
# A prose amount is the figure carrying a Tk / crore context, e.g.
# "Tk. 12,500.0 crore". Restrict to that so the rate ("11.50 percent") and the
# date in the title are never mistaken for the accepted amount.
_TK_CRORE_RE = re.compile(r"(?:tk\.?\s*)?(-?\d[\d,]*\.?\d*)\s*crore", re.IGNORECASE)


def _from_prose(text: str, label: str, *, lookahead: int = 2) -> float | None:
    """Find the Tk-crore accepted amount associated with ``label`` in prose.

    Scans the instrument line for a "<num> crore" figure; when the label wraps
    onto a continuation line (the amount sits a line or two below), looks ahead
    up to ``lookahead`` lines. The auction-title line is skipped so the release
    date is never read as the amount. Returns None when no line mentions the
    label (so the caller raises ParseError = absent instrument).
    """
    label_l = label.lower()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if label_l not in line.lower() or _TITLE_LINE_RE.search(line):
            continue
        for candidate in [line, *lines[i + 1: i + 1 + lookahead]]:
            m = _TK_CRORE_RE.search(candidate)
            if m:
                return _to_number(m.group(1))
    return None


@register("html_auction_press_row")
class HtmlAuctionPressRowParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        label = _parse_instruction(instruction)
        raw = artifact.artifact_path.read_text()
        soup = BeautifulSoup(raw, "html.parser")

        value = _from_table(soup, label)
        if value is None:
            page_text = soup.get_text("\n", strip=True)
            value = _from_prose(page_text, label)
            held_on = _recover_held_on(page_text)
        else:
            held_on = _recover_held_on(soup.get_text("\n", strip=True))

        if value is None:
            # Instrument genuinely absent from this release (e.g. no Repo on a
            # no-repo day). Raise so hybrid falls through to the LLM extract,
            # which returns null -> needs_review -> dropped (no fabricated 0).
            raise ParseError(
                f"instrument {label!r} not found in auction-result press release"
            )
        return ParseResult(
            value=value,
            _parse_strategy="html_auction_press_row",
            source_as_of=held_on,
        )
