"""Parser for the Department of Agricultural Marketing daily-price ticker.

The DAM portal at http://market.dam.gov.bd/market_daily_price_report renders
a top-of-page ticker like:

    আমন চাল - মোটা :&nbsp;৪৮.০০ - ৫০.০০ ▲০.০০%
    চিনি (দেশী) :&nbsp;১৩২.০০ - ১৩৫.০০ ▲০.০০%

Each item is `<bengali-label> : <low> - <high> <arrow><pct>%` with Bengali
digits (০-৯). The brief consumes a single midpoint price per item, so this
parser converts Bengali digits → Western digits, finds the row whose label
matches the instruction, and returns the (low+high)/2 midpoint.

Instruction format: a literal Bengali label as it appears on the page,
e.g. ``চিনি (দেশী)``. Any whitespace and parens are matched literally
(no regex metacharacters from the user — we re.escape).

source_as_of: the parser extracts the "Date of report: DD-MM-YYYY" header
that the DAM portal renders above the ticker. This is the true publication
date of the report, not the EconDelta run date.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date

from bs4 import BeautifulSoup

from fetchers.base import FetchResult
from parsers.base import ParseError, ParseResult
from parsers.registry import register

# Bengali digit ০-৯ ↔ Western 0-9. Built once at import.
_BN_TO_EN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# DAM report-date header: "Date of report: 04-05-2026" or "Date of report: 04/05/2026"
# Captures DD (group 1), MM (group 2), YYYY (group 3).
_DATE_REPORT_RE = re.compile(
    r"date\s+of\s+report\s*:?\s*([0-9০-৯]{1,2})[-/।]([0-9০-৯]{1,2})[-/।]([0-9০-৯]{4})",
    re.IGNORECASE,
)
_BANNER_DATE_RE = re.compile(
    r"daily\s+average\s+retail\s+price\s*:?\s*([0-9০-৯]{1,2})[-/।]([0-9০-৯]{1,2})[-/।]([0-9০-৯]{4})",
    re.IGNORECASE,
)
# Map each pinned commodity to (official DAM retail-unit ID, price unit).
# The sale-unit ID validates the source row; the price unit includes currency.
_VERIFIED_RETAIL_UNITS: dict[int, tuple[int, str]] = {
    604: (2, "BDT/kg"),
    628: (2, "BDT/kg"),
    682: (5, "BDT/4 pieces"),
    676: (2, "BDT/kg"),
    831: (2, "BDT/kg"),
    760: (2, "BDT/kg"),
}


def _normalize(text: str) -> str:
    """Strip HTML tags, decode &nbsp;, convert Bengali digits → Western,
    apply Unicode NFC normalization.

    NFC matters because the DAM portal's source HTML uses composed Bengali
    codepoints (e.g. য় = U+09DF, single-codepoint 'ya with nukta') while
    a config string typed in an editor may produce the decomposed form
    (য + ◌় = U+09AF + U+09BC). Without NFC, ``re.search`` misses real
    matches.
    """
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = plain.replace("&nbsp;", " ")
    plain = plain.translate(_BN_TO_EN_DIGITS)
    plain = re.sub(r"\s+", " ", plain)
    return unicodedata.normalize("NFC", plain)


def _extract_report_date(plain: str) -> date | None:
    """Return the DAM report date from the normalized page text, or None."""
    m = _DATE_REPORT_RE.search(plain)
    if not m:
        return None
    day, month, year = (int(part.translate(_BN_TO_EN_DIGITS)) for part in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_date_parts(match: re.Match[str]) -> date | None:
    day, month, year = (int(part.translate(_BN_TO_EN_DIGITS)) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_instruction(instruction: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*commodity_id=(\d+)\s+unit_retail=(\d+)\s*", instruction)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _json_body(raw: str) -> str | None:
    candidate = raw.strip()
    if not candidate.startswith("{"):
        # Playwright's HTML viewer puts a JSON response in a <pre> element.
        candidate = BeautifulSoup(raw, "html.parser").get_text().strip()
    return candidate if candidate.startswith("{") else None


def _parse_portal_json(raw: str, instruction: str) -> ParseResult:
    """Parse the official anonymous DAM ticker response for a pinned identity.

    Retail unit IDs are pinned from the portal's public commodity definitions
    (kg=2, litre=3, four eggs=5); the dated ticker supplies the matching
    commodity ID, actual observation date, and retail range.
    """
    identity = _parse_instruction(instruction)
    if identity is None:
        raise ParseError("DAM JSON source requires commodity_id and unit_retail")
    commodity_id, unit_retail = identity
    verified_unit = _VERIFIED_RETAIL_UNITS.get(commodity_id)
    if verified_unit is None or verified_unit[0] != unit_retail:
        raise ParseError(f"DAM commodity {commodity_id} has no verified retail unit contract")
    try:
        payload = json.loads(raw)
        if payload.get("success") is not True or not isinstance(payload.get("data"), list):
            raise ValueError("response is not a successful dated price list")
        matches = [
            row for row in payload["data"]
            if isinstance(row, dict) and row.get("commodity_id") == commodity_id
        ]
        dated: list[tuple[date, dict]] = []
        for row in matches:
            row_date = row.get("price_date")
            if isinstance(row_date, str):
                try:
                    dated.append((date.fromisoformat(row_date), row))
                except ValueError:
                    continue
        if not dated:
            raise ValueError(f"commodity {commodity_id} has no valid price_date")
        feed_dates = {
            date.fromisoformat(row["price_date"])
            for row in payload["data"]
            if isinstance(row, dict)
            and isinstance(row.get("price_date"), str)
            and _is_iso_date(row["price_date"])
        }
        if not feed_dates:
            raise ValueError("price list has no valid observation date")
        feed_latest_date = max(feed_dates)
        latest_date = max(item_date for item_date, _ in dated)
        if latest_date != feed_latest_date:
            raise ValueError("DAM commodity row is older than the current ticker period")
        latest = [row for item_date, row in dated if item_date == latest_date]
        if len(latest) != 1:
            raise ValueError(f"commodity {commodity_id} has ambiguous rows for {latest_date}")
        row = latest[0]
        low = float(row["a_r_lowestPrice"])
        high = float(row["a_r_howestPrice"])
        if low <= 0 or high < low:
            raise ValueError("retail price range is invalid")
        banner_date = payload.get("report_date") or payload.get("banner_date")
        if banner_date is not None and date.fromisoformat(banner_date) != latest_date:
            raise ValueError("DAM report and ticker dates disagree")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ParseError(f"DAM dated portal price invalid: {exc}") from exc
    midpoint = round((low + high) / 2.0, 2)
    return ParseResult(
        value=midpoint, _parse_strategy="dam_ticker", source_as_of=latest_date,
        unit=verified_unit[1],
    )


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


@register("dam_ticker")
class DamTickerParser:
    def parse(self, artifact: FetchResult, instruction: str) -> ParseResult:
        raw = artifact.artifact_path.read_text(encoding="utf-8", errors="replace")
        json_text = _json_body(raw)
        if json_text is not None:
            return _parse_portal_json(json_text, instruction)
        plain = _normalize(raw)
        instruction_nfc = unicodedata.normalize("NFC", instruction)
        # Pattern: <label> : <low> - <high>
        # Numbers can be int or decimal; Bengali digits already translated to ASCII.
        pattern = (
            re.escape(instruction_nfc)
            + r"\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)"
        )
        m = re.search(pattern, plain)
        if not m:
            raise ParseError(
                f"DAM ticker label {instruction!r} not found in normalized HTML"
            )
        low = float(m.group(1))
        high = float(m.group(2))
        midpoint = round((low + high) / 2.0, 2)
        source_as_of = _extract_report_date(plain)
        if source_as_of is None:
            raise ParseError("DAM ticker has no actual report date")
        banner_match = _BANNER_DATE_RE.search(plain)
        if banner_match:
            banner_date = _parse_date_parts(banner_match)
            if banner_date is None or banner_date != source_as_of:
                raise ParseError("DAM report and ticker dates disagree")
        return ParseResult(value=midpoint, _parse_strategy="dam_ticker", source_as_of=source_as_of)

    def recover_source_as_of(self, artifact: FetchResult) -> date | None:
        raw = artifact.artifact_path.read_text(encoding="utf-8", errors="replace")
        json_text = _json_body(raw)
        if json_text is not None:
            try:
                payload = json.loads(json_text)
                dates = {
                    date.fromisoformat(row["price_date"])
                    for row in payload.get("data", [])
                    if isinstance(row, dict) and isinstance(row.get("price_date"), str)
                }
                return max(dates) if len(dates) == 1 else None
            except (TypeError, ValueError):
                return None
        plain = _normalize(raw)
        return _extract_report_date(plain)
