"""Extract a 4-way bank-ownership cluster (SOCB / PCB / FCB / Specialised) from
the Bangladesh Bank Financial Stability Report (FSR) PDF.

ONE parser feeds TWO EconDelta indicators — both are 4-row ownership clusters in
the same FSR body, selected by the instruction's ``cluster=`` token:

  - ``cluster=npl``      → per-segment Gross NPL **ratio in percent**
                           (bad loans / that segment's loans). NPL is inherently
                           a ratio, published as a %, so the segment value IS a
                           percent — there is no "level" to store.
  - ``cluster=deposits`` → per-segment deposit **level in BDT crore**. Levels
                           (NOT shares) — the downstream donut computes shares
                           from the levels so they stay consistent with
                           ``deposits_of_the_system`` and always sum to 100%.

Output is a dict keyed by the four canonical ownership segments::

    {"socb": <num>, "pcb": <num>, "fcb": <num>, "specialised": <num>}

``aggregate_latest._flatten_dict_indicators`` then explodes each dict into four
per-segment scalar metrics (the ``call_money_rate`` / ``dse_sector_heat``
precedent — landmine C) BEFORE the Supabase writer's scalar-only filter drops
the dict.

Why a NEW parser (not ``pdf_table_row`` / ``pdf_component``): the ownership
splits live deep in the FSR body as multi-column tables (a row per ownership
group, columns per period) or charts. ``pdf_table_row`` returns a single cell;
``pdf_component`` matches one labelled scalar. Neither yields the 4-way cluster.
This parser scans for the ownership-group LABELS (landmine E — label matching
over absolute page/index) and reads the most-recent numeric cell on each row,
falling through to ``ParseError`` (never None) so hybrid hits the LLM extract.

BD egress: the FSR is fetched on ExonVPS Dhaka, not this Mac. The live
page-number / row-label / column-order shape is VPS-deferred; the deterministic
scan here is label-anchored so it survives an edition-to-edition page shift
(landmine E), and the LLM fallback covers chart-only layouts.
"""
from __future__ import annotations

import re
from datetime import date

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Canonical ownership segments and the label variants BB uses across FSR
# editions. Matched case- and whitespace-insensitively against a row's leading
# text. Order matters only for documentation — the dict is keyed canonically.
_SEGMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "socb": (
        "state owned commercial bank",
        "state-owned commercial bank",
        "state owned bank",
        "socb",
        "scb",
    ),
    "pcb": (
        "private commercial bank",
        "private bank",
        "pcb",
    ),
    "fcb": (
        "foreign commercial bank",
        "foreign bank",
        "fcb",
    ),
    "specialised": (
        "specialised bank",
        "specialized bank",
        "development financial institution",
        "specialised development",
        "sb",
        "sdb",
    ),
}

# A numeric cell as it appears in an FSR table: "12.3", "1,234.5", "9,87,654".
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")

# FSR cover / executive-summary date: "Quarter ending 30 September 2025" or
# "as on/of 30 June 2025" or "end-June 2025" — recover the publication date so
# metric_history.as_of reflects the true quarter-end (not the run date).
_QUARTER_END_RE = re.compile(
    r"(?:quarter\s+ending|as\s+(?:on|of)|end[-\s])\s*"
    r"(\d{1,2})?\s*([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # quarter-end months commonly abbreviated on FSR cover pages
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Quarter-end day for a recovered "<Month> <Year>" with no explicit day.
_QUARTER_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}


def _extract_as_of(text: str) -> date | None:
    """Recover the FSR reporting period-end date, or None if unrecoverable."""
    m = _QUARTER_END_RE.search(text)
    if not m:
        return None
    month = _MONTH_NAMES.get(m.group(2).lower())
    if month is None:
        return None
    year = int(m.group(3))
    day = int(m.group(1)) if m.group(1) else _QUARTER_END_DAY.get(month, 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _which_segment(label_text: str) -> str | None:
    """Return the canonical segment key whose alias matches ``label_text``.

    Checks the more specific aliases first per segment; a row leading with
    "Specialised Banks" must not be mis-claimed by the bare "bank" of another
    alias — every alias here is specific enough (contains the distinguishing
    word) to avoid cross-claims.
    """
    low = re.sub(r"\s+", " ", label_text).strip().lower()
    for seg, aliases in _SEGMENT_ALIASES.items():
        for alias in aliases:
            if alias in low:
                return seg
    return None


def _parse_cluster_token(instruction: str) -> str:
    """Read the ``cluster=npl|deposits`` token from the instruction."""
    for token in instruction.split():
        if token.startswith("cluster="):
            # The token is embedded in a prose instruction (also fed to the LLM),
            # e.g. "... cluster=npl. Return the four ..." — strip surrounding
            # whitespace AND trailing sentence punctuation so "cluster=npl."
            # yields "npl", not "npl." (which fails the npl|deposits check).
            return token.split("=", 1)[1].strip().strip(".,;:").lower()
    raise ParseError(
        f"instruction missing cluster=npl|deposits token: {instruction!r}"
    )


def _last_number(cells: list[str]) -> float | None:
    """Return the right-most numeric value across a row's cells.

    FSR tables run periods left→right with the most-recent period last, so the
    right-most numeric cell is the latest quarter (landmine E — read by position
    within the row, after the label, not by an absolute column index).
    """
    found: float | None = None
    for cell in cells:
        for raw in _NUMBER_RE.findall(cell):
            cleaned = raw.replace(",", "")
            if cleaned in ("", "-", ".", "-."):
                continue
            try:
                found = float(cleaned)
            except ValueError:
                continue
    return found


def _extract_from_tables(tables: list[list[list]]) -> dict[str, float]:
    """Scan extracted pdfplumber tables for the 4 ownership-group rows."""
    out: dict[str, float] = {}
    for table in tables:
        for row in table:
            cells = [str(c) if c is not None else "" for c in row]
            if not cells:
                continue
            seg = _which_segment(cells[0])
            if seg is None or seg in out:
                continue
            value = _last_number(cells[1:])
            if value is not None:
                out[seg] = value
    return out


@register("pdf_fsr_ownership_cluster")
class PdfFsrOwnershipClusterParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        # Validate the instruction selects a known cluster before doing IO.
        cluster = _parse_cluster_token(instruction)
        if cluster not in ("npl", "deposits"):
            raise ParseError(f"unknown cluster {cluster!r} (want npl|deposits)")

        import pdfplumber

        as_of: date | None = None
        cluster_values: dict[str, float] = {}
        with pdfplumber.open(artifact.artifact_path) as pdf:
            cover_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
            as_of = _extract_as_of(cover_text)
            for page in pdf.pages:
                tables = page.extract_tables() or []
                found = _extract_from_tables(tables)
                for seg, val in found.items():
                    cluster_values.setdefault(seg, val)
                if len(cluster_values) == 4:
                    break

        if len(cluster_values) < 4:
            raise ParseError(
                f"FSR {cluster} cluster incomplete: got "
                f"{sorted(cluster_values)} (need socb/pcb/fcb/specialised)"
            )
        return ParseResult(
            value=cluster_values,
            _parse_strategy="pdf_fsr_ownership_cluster",
            source_as_of=as_of,
        )
