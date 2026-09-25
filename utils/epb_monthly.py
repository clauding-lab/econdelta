"""Append official EPB goods-export monthlies; never rewrite accepted history.

Reads the public index each run, then validates the actual workbook's summary,
unit, goods total, fiscal year and single-month headers. No flash/customs/BOP
series is an interchangeable source. The XLSX reader uses the standard library;
only cached numeric cells are accepted (no spreadsheet formula evaluation).
"""

from __future__ import annotations

import calendar
import io
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fetchers.tls import ssl_context_for
from utils.monthly_evidence import record_source_check, revision_diff

logger = logging.getLogger(__name__)

INDEX_URL = "https://epb.gov.bd/views/epb-export-data/-/"
METRIC_ID = "exports_usd_mn_monthly"
SOURCE = "epb_goods_summary"
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.rows: list[tuple[str, list[str]]] = []
        self.parts: list[str] = []
        self.in_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.urls, self.parts, self.in_row = [], [], True
        if tag == "a" and self.in_row:
            href = dict(attrs).get("href") or ""
            parsed = urlparse(href)
            if (
                parsed.scheme == "https"
                and parsed.path.lower().endswith(".xlsx")
                and parsed.hostname == "objectstorage.ap-dcc-gazipur-1.oraclecloud15.com"
                and "/office-epb/" in parsed.path
                and href not in self.urls
            ):
                self.urls.append(href)

    def handle_data(self, data: str) -> None:
        if self.in_row:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self.in_row:
            self.rows.append((" ".join(self.parts), self.urls))
            self.in_row = False


def discover_workbooks(html: str) -> list[str]:
    """Discover current official attachments, including unlabelled workbooks."""
    links = _Links()
    links.feed(html)
    # Rank the index's own edition labels, not its non-chronological row order.
    months = {
        "জুলাই": 7,
        "আগস্ট": 8,
        "অগস্ট": 8,
        "সেপ্টেম্বর": 9,
        "অক্টোবর": 10,
        "নভেম্বর": 11,
        "ডিসেম্বর": 12,
        "জানুয়ারি": 1,
        "জানুয়ারি": 1,
        "ফেব্রুয়ারি": 2,
        "ফেব্রুয়ারি": 2,
        "মার্চ": 3,
        "এপ্রিল": 4,
        "মে": 5,
        "জুন": 6,
    }
    editions = []
    for text, urls in links.rows:
        normalized = text.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
        year = re.search(r"(20\d{2})-(?:20)?\d{2}", normalized)
        matched = [month for name, month in months.items() if name in text]
        if not year or not matched:
            continue
        month = max(matched, key=lambda m: (m - 7) % 12)
        start = int(year[1])
        editions.append((date(start if month >= 7 else start + 1, month, 1), urls))
    if not editions:
        raise ValueError("EPB index edition labels are unverified")
    latest = max(day for day, _ in editions)
    urls = list(dict.fromkeys(url for day, links in editions if day == latest for url in links))
    if not urls or len(urls) > 12:
        raise ValueError("Latest EPB edition has no workbooks or exceeds the fetch budget")
    return urls


def _cells(archive: zipfile.ZipFile, name: str, strings: list[str]) -> dict[str, str]:
    root = ET.fromstring(archive.read(name))
    cells = {}
    for cell in root.findall(".//s:sheetData/s:row/s:c", NS):
        value = cell.find("s:v", NS)
        raw = value.text if value is not None else ""
        if cell.get("t") == "s":
            raw = strings[int(raw)]
        elif cell.get("t") == "inlineStr":
            raw = "".join(cell.itertext())
        cells[cell.attrib["r"]] = " ".join((raw or "").split())
    return cells


def parse_exports_workbook(content: bytes) -> list[tuple[date, float]]:
    """Return current-FY single-month goods totals from a verified Summary Sheet."""
    observations: dict[date, float] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(i.file_size for i in archive.infolist()) > 20_000_000:
            raise ValueError("EPB workbook exceeds decompressed size budget")
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = ["".join(si.itertext()) for si in root.findall("s:si", NS)]
        for name in archive.namelist():
            if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                continue
            cells = _cells(archive, name, strings)
            text = " ".join(cells.values()).lower()
            if "report: summary sheet" not in text:
                continue
            if not (
                "export promotion bureau" in text
                and "value in million us$" in text
                and "primary commodities" in text
                and "manufactured commodities" in text
            ):
                raise ValueError("EPB summary goods basis/unit is unverified")
            period = re.search(r"period:\s*july(?:[-–][a-z]+)?\s+(20\d{2})-(20\d{2})", text)
            if not period or int(period[2]) != int(period[1]) + 1:
                raise ValueError("EPB summary fiscal period is unverified")
            fy_start = int(period[1])
            totals = [
                ref
                for ref, val in cells.items()
                if re.fullmatch(r"all products\s*\(a\s*\+\s*b\)", val, re.I)
            ]
            if len(totals) != 1:
                raise ValueError("EPB summary has ambiguous goods total")
            total_row = re.search(r"\d+$", totals[0])[0]
            for ref, label in cells.items():
                match = re.fullmatch(
                    r"Export Performance for ([A-Za-z]+) (20\d{2})-(\d{2}|20\d{2})", label, re.I
                )
                if not match or int(match[2]) != fy_start:
                    continue  # never cumulative July-Aug or prior-FY comparator
                if int(match[3]) % 100 != (fy_start + 1) % 100:
                    raise ValueError("EPB column fiscal year contradicts summary period")
                month = MONTHS.get(match[1].lower())
                if month is None:
                    raise ValueError("EPB monthly column month is unknown")
                observation = date(fy_start if month >= 7 else fy_start + 1, month, 1)
                column = re.match(r"[A-Z]+", ref)[0]
                value = float(cells[column + total_row])
                if not math.isfinite(value) or not 0 < value < 100_000:
                    raise ValueError("EPB monthly goods total outside USD million range")
                if observation in observations and observations[observation] != value:
                    raise ValueError("EPB summary has conflicting monthly totals")
                observations[observation] = value
    if not observations:
        raise ValueError("No verified EPB goods summary monthly columns")
    return sorted(observations.items())


def _download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "econdelta/0.1"})
    with urlopen(request, timeout=30, context=ssl_context_for(url)) as response:
        content = response.read(10_000_001)
    if len(content) > 10_000_000:
        raise ValueError("EPB response exceeds size budget")
    return content


def fetch_exports() -> tuple[list[tuple[date, float]], str]:
    """Discover editions afresh; region-wise workbooks are never substituted."""
    urls = discover_workbooks(_download(INDEX_URL).decode("utf-8"))
    candidates = []
    for url in urls:
        try:
            content = _download(url)
            rows = parse_exports_workbook(content)
        except Exception as exc:
            # A broken companion must not hide another verified summary.
            # Keep the URL/reason visible; zero surviving summaries still fail.
            logger.warning("EPB attachment rejected %s: %s: %s", url, type(exc).__name__, exc)
            continue
        candidates.append((rows, url))
    if not candidates:
        raise ValueError("EPB index yielded no verified goods summary")
    candidates.sort(key=lambda item: max(day for day, _ in item[0]), reverse=True)
    newest = candidates[0]
    for rows, _ in candidates[1:]:
        if max(day for day, _ in rows) == max(day for day, _ in newest[0]) and rows != newest[0]:
            raise ValueError("Conflicting EPB summaries for the same latest month")
    return newest


def plan_exports(
    parsed: list[tuple[date, float]],
    existing: list[dict],
    today: date,
) -> tuple[list[dict], list[dict]]:
    """Closed months only; accepted pairs remain immutable, revisions are evidence."""
    existing_dates = {row["as_of"][:10] for row in existing}
    candidates = [
        {
            "metric_id": METRIC_ID,
            "as_of": day.isoformat(),
            "value": value,
            "source": SOURCE,
            "source_as_of": day.replace(
                day=calendar.monthrange(day.year, day.month)[1]
            ).isoformat(),
        }
        for day, value in parsed
        if day < today.replace(day=1)
    ]
    return (
        [row for row in candidates if row["as_of"] not in existing_dates],
        revision_diff(candidates, existing),
    )


def write_exports_monthly(today: date | None = None, *, evidence_dir: Path | None = None) -> int:
    """Independent monthly writer; callers contain failure without holding other legs."""
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") == "1":
        return 0
    from utils.supabase_reader import get_metric_history_monthly
    from utils.supabase_writer import upsert_metric_history_monthly

    today = today or datetime.now(timezone.utc).date()
    existing = get_metric_history_monthly(METRIC_ID)
    parsed, source_url = fetch_exports()
    rows, revisions = plan_exports(parsed, existing, today)
    # Record source evidence before persistence: it is not a successful-write receipt.
    try:
        record_source_check(
            METRIC_ID,
            parsed,
            existing,
            today=today,
            revisions=revisions,
            source_url=source_url,
            evidence_kind="upstream-source",
            directory=evidence_dir,
        )
    except OSError as exc:
        logger.warning("EPB source receipt failed; continuing validated append: %s", exc)
    return upsert_metric_history_monthly(rows) if rows else 0
