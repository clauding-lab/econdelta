"""World Bank "Pink Sheet" commodity-benchmark scraper — NO BD egress, runs on any host.

Why a ``scrapers/`` one-shot and not a config indicator / ``fetchers`` helper:
the Pink Sheet is published only as an ``.xlsx`` workbook (no CSV/JSON, no daily
PDF print to ride the v3 fetch→parse pipeline). ``fetch_all._fetch_one`` dispatches
ONLY ``html``/``pdf`` (anything else logs "unsupported fetch.type" and yields
nothing), so an Excel puller cannot ride that pipeline. This mirrors
``scrapers/imf_debt_gdp.py`` / ``scrapers/imf_eff.py`` / ``scrapers/commodity_prices.py``:
a standalone NO-egress script that fetches + parses + upserts directly to
``metric_history`` under its own ids, outside the fetch_all/parse_all dispatch.

NO new Python dependency (and therefore NO VISION sign-off): the ``.xlsx`` is a
ZIP of XML parts, so it is read with the standard library (``zipfile`` +
``xml.etree``) — NOT openpyxl/pandas (neither is installed for Excel reading;
``pandas.read_excel`` itself needs openpyxl). Do NOT "improve" this by adding an
Excel-reader dependency; the stdlib path is deliberate and dependency-free.

What it captures, from the "Monthly Prices" sheet (header-LABEL matched, landmine E):
  - ``lng_price_usd_mmbtu``  — "Liquefied natural gas, Japan", **USD per mmbtu**
  - ``palm_oil_price_usd_mt`` — "Palm oil", **USD per metric ton**
  - ``wheat_price_usd_mt``    — "Wheat, US SRW", **USD per metric ton**

Units are carried in the metric id (``_mmbtu`` / ``_mt``) exactly like
``imf_eff_outstanding_sdr_mn`` carries ``_sdr_mn`` — the Pink Sheet's native units
(``$/mmbtu`` for LNG, ``$/mt`` for palm oil and wheat) have no matching
``claude_max/validators.py:ValueType``, and converting them would divorce the
stored number from the World Bank's published figure. The catalog documents each
unit via ``scripts/build_catalog.py:DERIVED_KEYS``.

as_of: the data rows are keyed by period ``YYYYMmm`` (e.g. ``2025M12``); we parse
the LATEST period and stamp ``metric_history.as_of`` at that month's last day, so
the vintage reflects the World Bank's reporting month, not the run date.
"""

from __future__ import annotations

import calendar
import logging
import re
import sys
import zipfile
from datetime import date
from io import BytesIO
from xml.etree import ElementTree as ET

import requests
import urllib3.util.connection

from utils.notifier import notify
from utils.supabase_writer import upsert_metric_history

logger = logging.getLogger("world_bank_pink_sheet")

# The OOXML spreadsheet namespaces.
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# The Pink Sheet monthly historical workbook. Stable, openly downloadable from any
# host (NO BD egress wall — World Bank global data; verified HTTP 200 from this Mac
# 2026-05-31). Published only as .xlsx (no CSV/JSON variant exists).
PINK_SHEET_URL = (
    "https://thedocs.worldbank.org/en/doc/"
    "18675f1d1639c7a34d463f59263ba0a2-0050012025/related/"
    "CMO-Historical-Data-Monthly.xlsx"
)

# The data we want lives on the "Monthly Prices" sheet (prices in nominal USD).
PRICES_SHEET_NAME = "Monthly Prices"

# Header-LABEL → (metric_id, valid_range). The label is matched against the
# commodity-name header row (collapse-whitespace, exact match) — NOT a fixed
# column index, because column order can shift edition-to-edition (landmine E).
# valid_range bands reject a parse that grabbed the wrong column.
#   LNG, Japan:   ~$11/mmbtu (Dec-2025)  → band [1, 60]
#   Palm oil:     ~$980/mt  (Dec-2025)   → band [200, 3000]
#   Wheat US SRW: ~$223/mt  (Dec-2025)   → band [80, 1000]
TARGETS: dict[str, tuple[str, tuple[float, float]]] = {
    "Liquefied natural gas, Japan": ("lng_price_usd_mmbtu", (1.0, 60.0)),
    "Palm oil": ("palm_oil_price_usd_mt", (200.0, 3000.0)),
    "Wheat, US SRW": ("wheat_price_usd_mt", (80.0, 1000.0)),
}

# Row where the commodity-name header lives, and where the unit labels live, on the
# "Monthly Prices" sheet (1-indexed). Data rows (period in column A) start after.
_HEADER_ROW = 5
_UNIT_ROW = 6

# (connect, read) seconds. Kept short so a stalled connect fails fast into the
# FetchError → return 1 path instead of hanging ~60s per blackholed address.
_TIMEOUT: tuple[int, int] = (10, 30)

# The period cell shape, e.g. "2025M12".
_PERIOD_RE = re.compile(r"^(\d{4})M(\d{2})$")


class FetchError(Exception):
    pass


def fetch_pink_sheet_bytes(
    *, url: str = PINK_SHEET_URL, session: requests.Session | None = None
) -> bytes:
    """GET the Pink Sheet .xlsx workbook. Raises FetchError on network/HTTP failure."""
    # Force IPv4. The ExonVPS Dhaka host's IPv6 egress is blackholed, and
    # thedocs.worldbank.org (CloudFront) resolves AAAA first — so a default
    # dual-stack connect stalls on the dead IPv6 address until timeout and fetches
    # nothing. Pinning urllib3's module-global HAS_IPV6 makes requests resolve
    # AF_INET only. Scope note: this is a PROCESS-GLOBAL toggle, but this scraper
    # runs as its own one-shot process (wrap_run below), so it cannot bleed into
    # sibling scrapers. If a second outbound call is ever added here, that is fine —
    # all of this module's egress should be IPv4 on the blackholed host.
    urllib3.util.connection.HAS_IPV6 = False
    sess = session or requests.Session()
    try:
        resp = sess.get(url, timeout=_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise FetchError(f"network error fetching Pink Sheet: {e}") from e
    if resp.status_code != 200:
        raise FetchError(f"Pink Sheet returned HTTP {resp.status_code}")
    return resp.content


def _col_letters(cell_ref: str) -> str:
    """'AK798' -> 'AK'."""
    return "".join(ch for ch in cell_ref if ch.isalpha())


def _load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Read xl/sharedStrings.xml into an index→text list (empty if absent)."""
    try:
        raw = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    out: list[str] = []
    for si in ET.fromstring(raw).iter(f"{_MAIN_NS}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{_MAIN_NS}t")))
    return out


def _resolve_sheet_path(zf: zipfile.ZipFile, sheet_name: str) -> str:
    """Map a sheet display name → its worksheet XML path (e.g. 'xl/worksheets/sheet2.xml')."""
    workbook = ET.fromstring(zf.read("xl/workbook.xml"))
    rid = None
    for sheet in workbook.iter(f"{_MAIN_NS}sheet"):
        if sheet.get("name") == sheet_name:
            rid = sheet.get(f"{_REL_NS}id")
            break
    if rid is None:
        raise FetchError(f"sheet {sheet_name!r} not found in workbook")

    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    for rel in rels:
        if rel.get("Id") == rid:
            target = rel.get("Target")
            # Targets are relative to xl/, e.g. "worksheets/sheet2.xml".
            return "xl/" + target.lstrip("/")
    raise FetchError(f"no worksheet target for sheet {sheet_name!r} (rId {rid})")


def _cell_text(cell: ET.Element, shared: list[str]) -> str | None:
    """Decode one <c> cell to a string (shared/inline string or raw numeric text)."""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        is_el = cell.find(f"{_MAIN_NS}is")
        if is_el is None:
            return None
        return "".join(t.text or "" for t in is_el.iter(f"{_MAIN_NS}t")) or None
    v = cell.find(f"{_MAIN_NS}v")
    if v is None or v.text is None:
        return None
    if cell_type == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return None
    return v.text


def _parse_rows(sheet_xml: bytes, shared: list[str]) -> dict[int, dict[str, str]]:
    """Parse a worksheet into {row_number: {col_letter: cell_text}}."""
    root = ET.fromstring(sheet_xml)
    rows: dict[int, dict[str, str]] = {}
    for row in root.iter(f"{_MAIN_NS}row"):
        rnum = int(row.get("r"))
        cells: dict[str, str] = {}
        for cell in row.iter(f"{_MAIN_NS}c"):
            text = _cell_text(cell, shared)
            if text is not None:
                cells[_col_letters(cell.get("r"))] = text
        rows[rnum] = cells
    return rows


def _period_to_as_of(period: str) -> date:
    """'2025M12' -> date(2025, 12, 31) (the month-end of the reporting period)."""
    m = _PERIOD_RE.match(period)
    if not m:
        raise FetchError(f"unrecognised period cell {period!r}")
    year, month = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        raise FetchError(f"period {period!r} has out-of-range month {month}")
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def parse_pink_sheet(
    workbook_bytes: bytes,
    *,
    targets: dict[str, tuple[str, tuple[float, float]]] = TARGETS,
    sheet_name: str = PRICES_SHEET_NAME,
) -> tuple[dict[str, float], date]:
    """Extract {metric_id: latest_value} + the latest-period as_of from the workbook.

    Pure-ish (only reads the in-memory bytes, no network) so it unit-tests against a
    captured fixture with NO egress. Header-LABEL matching (landmine E): each target
    commodity is located by its name in the header row, not a fixed column index.
    Drops a target whose latest value is out of its valid_range (defensive — a band
    miss usually means the column shifted). Raises FetchError if NO target resolves
    or the sheet/headers are missing.
    """
    zf = zipfile.ZipFile(BytesIO(workbook_bytes))
    shared = _load_shared_strings(zf)
    sheet_path = _resolve_sheet_path(zf, sheet_name)
    rows = _parse_rows(zf.read(sheet_path), shared)

    header = rows.get(_HEADER_ROW)
    if not header:
        raise FetchError(f"header row {_HEADER_ROW} missing on sheet {sheet_name!r}")

    # Map each wanted label → its column letter via collapse-whitespace exact match.
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    label_to_col: dict[str, str] = {}
    for col, raw_label in header.items():
        norm = _norm(raw_label)
        if norm in targets:
            label_to_col[norm] = col
    missing_labels = set(targets) - set(label_to_col)
    if missing_labels:
        logger.warning("Pink Sheet header missing labels: %s", sorted(missing_labels))

    # Find the LATEST data row: the max row whose column A is a YYYYMmm period.
    period_rows = [
        (rnum, cells["A"])
        for rnum, cells in rows.items()
        if "A" in cells and _PERIOD_RE.match(cells["A"])
    ]
    if not period_rows:
        raise FetchError(f"no period (YYYYMmm) data rows found on sheet {sheet_name!r}")
    latest_rnum, latest_period = max(period_rows, key=lambda rp: rp[0])
    as_of = _period_to_as_of(latest_period)
    latest_cells = rows[latest_rnum]

    out: dict[str, float] = {}
    for label, (metric_id, (lo, hi)) in targets.items():
        col = label_to_col.get(label)
        if col is None:
            continue
        raw = latest_cells.get(col)
        if raw is None or raw in ("…", "...", "..", ""):
            logger.warning("%s (%s) has no value in latest period %s", label, metric_id, latest_period)
            continue
        try:
            value = float(raw)
        except ValueError:
            logger.warning("could not parse %s value %r for %s", label, raw, metric_id)
            continue
        if not (lo <= value <= hi):
            logger.warning(
                "%s = %s out of range [%s, %s] (likely wrong column) — dropping",
                metric_id, value, lo, hi,
            )
            continue
        out[metric_id] = value

    if not out:
        raise FetchError("no Pink Sheet commodity values parsed (all labels missing or out of range)")
    return out, as_of


def upsert_commodities(values: dict[str, float], as_of: date) -> int:
    """Upsert each {metric_id: value} as one metric_history row stamped at as_of."""
    return upsert_metric_history(
        data=values,
        as_of=as_of,
        source="World Bank Pink Sheet",
        source_as_of_map={metric_id: as_of for metric_id in values},
        url=PINK_SHEET_URL,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        workbook_bytes = fetch_pink_sheet_bytes()
        values, as_of = parse_pink_sheet(workbook_bytes)
    except FetchError as e:
        logger.exception("World Bank Pink Sheet fetch/parse failed")
        notify("error", "world_bank_pink_sheet fetch failed", str(e))
        return 1

    logger.info(
        "parsed %d Pink Sheet commodity benchmarks (as of %s): %s",
        len(values),
        as_of,
        ", ".join(f"{k}={v:.2f}" for k, v in values.items()),
    )

    written = upsert_commodities(values, as_of)
    logger.info("upserted %d rows into metric_history", written)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("world_bank_pink_sheet", "econdelta-pink-sheet.service", main))
