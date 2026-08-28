"""Parser for BB's Money Market Reference Rate page (DOMMR + BOFR).

Source: https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate
(verified live 2026-08-28, fetched on the box through fetchers/html_fetcher —
the page sits behind the same F5/TSPD JS challenge as the BB homepage,
AGENTS.md landmine 39d). Server-rendered HTML carrying TWO stacked tables of
the SAME shape (Product | Amount (Crore Taka) | <rate> (%) | Number of Deals),
each introduced by its own ``page_header`` div:

  - "Dhaka Overnight Money Market Rate (DOMMR)" — tenor rows Overnight/1W/1M/3M
  - "Bangladesh Overnight Financing Rate (BOFR)" — tenor rows Overnight/1W

Each tbody groups its data rows under a DATE HEADER row (a single
``colspan`` cell, format ``DD Month, YYYY``, e.g. "27 August, 2026"). The
default GET shows the latest business day only; a POST with ``date_picker``
returns a date range, newest first (scripts/backfill_dommr_bofr.py drives
that path — it reuses ``find_rate_table``/``extract_date_blocks`` below so
backfill and daily parse can never disagree on table anchoring).

Design decisions (owner-approved, 2026-08-28):

- **Anchor each table by its page_header TEXT, never by table order**
  (AGENTS.md landmine 45 discipline). The two tables are shape-identical and
  their 1W rates differ by only ~5bp — a positional pick that silently
  swapped them would publish a plausible-looking wrong number.
- **Returns a DICT of the four moving series**
  ``{"dommr", "dommr_1w", "bofr", "bofr_1w"}`` (Overnight + 1W per table).
  1M/3M are EXCLUDED by decision: BB's minimum-transaction accumulation
  freezes them for days at a time (7 straight identical prints verified),
  which would false-alarm the stillness sentinel (landmine 40).
- **Real value-dating**: ``source_as_of`` is the page's own date-header date
  — never the run date (landmine 47's as_of-forgery class; parsers/
  html_dated_table_row.py is the in-repo model). The fan-out in
  aggregate_latest MUST propagate this date to all four minted ids — see
  ``MONEY_MARKET_REF_RATE_FANOUT_IDS`` there.
- **Fail closed** (ParseError → LLM fallback path) when: either table or its
  header is missing, either required tenor (Overnight/1W) is missing from the
  newest block, the date header is missing/unparseable, or the two tables'
  newest dates DISAGREE (a half-updated page must not publish).
- **Refuse tenor label ``7D``**: BB's pre-launch staging test data (rows
  before 2026-04-15) used ``7D`` where production uses ``1W``. Seeing it on
  the live page means we're looking at test/junk data — refuse the parse.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# The four metric ids minted from this indicator's dict value. Keys double as
# metric_history ids after aggregate_latest's fan-out — keep them stable.
_SERIES_KEYS = frozenset({"dommr", "dommr_1w", "bofr", "bofr_1w"})

# (page_header anchor text, series prefix) per table — matched exactly after
# whitespace/case normalization, NEVER by table position.
DOMMR_HEADER = "Dhaka Overnight Money Market Rate (DOMMR)"
BOFR_HEADER = "Bangladesh Overnight Financing Rate (BOFR)"

# Tenor labels (normalized) this source uses in production.
_TENOR_OVERNIGHT = "overnight"
_TENOR_1W = "1w"
# The staging-test-data tell: pre-launch rows (before 2026-04-15) label the
# one-week tenor "7D"; production has always used "1W".
_TEST_DATA_TENOR = "7d"

# Date-header format: "27 August, 2026" (zero-padded day observed; tolerate
# an unpadded day too — strptime %d accepts both).
_DATE_HEADER_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+),\s*(\d{4})$")


def _normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def parse_header_date(text: str) -> date | None:
    """Parse a date-header cell like ``27 August, 2026`` → date. None if not one."""
    m = _DATE_HEADER_RE.match(_normalize(text))
    if not m:
        return None
    day, month_name, year = m.groups()
    try:
        return datetime.strptime(f"{day} {month_name} {year}", "%d %B %Y").date()
    except ValueError:
        return None


def find_rate_table(soup: BeautifulSoup, header_text: str):
    """Return the <table> introduced by the ``page_header`` div whose text
    equals ``header_text`` (normalized). Anchored on the HEADER, never on
    table order — raises ParseError if the header or its table is missing.
    """
    target = _normalize(header_text).lower()
    for el in soup.find_all(class_="page_header"):
        if _normalize(el.get_text()).lower() != target:
            continue
        table = el.find_next("table")
        if table is None:
            raise ParseError(f"header {header_text!r} found but no <table> follows it")
        return table
    raise ParseError(f"page_header {header_text!r} not found (page structure changed?)")


def extract_date_blocks(table) -> list[tuple[date, dict[str, float]]]:
    """Walk one rate table's rows into per-date blocks, document order
    (the page prints newest first — the daily GET has exactly one block).

    Returns ``[(value_date, {normalized_tenor_label: rate_pct}), ...]``.
    A date-header row (single ``colspan`` cell parsing as ``DD Month, YYYY``)
    starts a new block; 4-cell data rows are Product | Amount | Rate | Deals
    and contribute ``tenor → rate``. Data rows before any date header, and
    rows whose rate cell isn't numeric, are skipped. The ``7D`` staging
    label is RETAINED in the output so callers can act on it (the daily
    parser refuses it; the backfill hard-filters it).
    """
    blocks: list[tuple[date, dict[str, float]]] = []
    current: dict[str, float] | None = None
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        if len(cells) == 1:
            header_date = parse_header_date(cells[0].get_text(strip=True))
            if header_date is not None:
                current = {}
                blocks.append((header_date, current))
            continue
        texts = [c.get_text(strip=True) for c in cells]
        # Data row shape: Product | Amount (Crore Taka) | rate (%) | Deals.
        if len(texts) < 3 or current is None:
            continue
        tenor = _normalize(texts[0]).lower()
        if not tenor or tenor == "product":
            continue
        try:
            rate = float(texts[2].replace(",", ""))
        except ValueError:
            continue
        current[tenor] = rate
    return blocks


def _newest_block(
    table, *, label: str
) -> tuple[date, dict[str, float]]:
    """The FIRST (newest) date block of ``table``, with the fail-closed checks
    the daily parse needs: at least one dated block, no ``7D`` staging label
    anywhere, and both required tenors present in the newest block.
    """
    blocks = extract_date_blocks(table)
    if not blocks:
        raise ParseError(f"{label}: no date-header block found in table")
    if any(_TEST_DATA_TENOR in tenors for _d, tenors in blocks):
        raise ParseError(
            f"{label}: tenor label '7D' present — that is BB's pre-launch "
            "staging test data, not production (production uses '1W'); refusing"
        )
    value_date, tenors = blocks[0]
    missing = [t for t in (_TENOR_OVERNIGHT, _TENOR_1W) if t not in tenors]
    if missing:
        raise ParseError(
            f"{label}: required tenor(s) {missing} missing from newest block "
            f"({value_date.isoformat()}); saw {sorted(tenors)}"
        )
    return value_date, tenors


@register("html_money_market_ref_rate")
class HtmlMoneyMarketRefRateParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        soup = BeautifulSoup(
            artifact.artifact_path.read_text(encoding="utf-8"), "html.parser"
        )
        dommr_table = find_rate_table(soup, DOMMR_HEADER)
        bofr_table = find_rate_table(soup, BOFR_HEADER)

        dommr_date, dommr = _newest_block(dommr_table, label="DOMMR")
        bofr_date, bofr = _newest_block(bofr_table, label="BOFR")

        if dommr_date != bofr_date:
            raise ParseError(
                f"DOMMR newest date {dommr_date.isoformat()} != BOFR newest "
                f"date {bofr_date.isoformat()} — half-updated page, refusing"
            )

        value = {
            "dommr": dommr[_TENOR_OVERNIGHT],
            "dommr_1w": dommr[_TENOR_1W],
            "bofr": bofr[_TENOR_OVERNIGHT],
            "bofr_1w": bofr[_TENOR_1W],
        }
        return ParseResult(
            value=value,
            _parse_strategy="html_money_market_ref_rate",
            source_as_of=dommr_date,
        )

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        """Best-effort value-date recovery for the LLM-extract fallback path
        (hybrid._recover_source_as_of; AGENTS.md landmine 26: the LLM path
        must not drop the real date). The fallback fires precisely when the
        table structure changed, so this is deliberately tolerant: the NEWEST
        parseable ``DD Month, YYYY`` date-header anywhere on the page. None
        when no such cell parses — the writer's run-date fallback then
        applies, and _build_source_as_of_map warns.
        """
        try:
            soup = BeautifulSoup(
                artifact.artifact_path.read_text(encoding="utf-8"), "html.parser"
            )
            dates = [
                d
                for el in soup.find_all(class_="page_header")
                for d in [parse_header_date(el.get_text(strip=True))]
                if d is not None
            ]
            return max(dates) if dates else None
        except Exception:  # noqa: BLE001 — recovery is best-effort, never raises
            return None
