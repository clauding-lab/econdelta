"""One-time backfill: monthly call-money WAR from Bangladesh Bank PDFs.

Target table: ``metric_history_monthly`` (monthly-first, same convention as
CPI / reserves / yields seeded by ``scripts/seed_macro_monthly.py``). Rows are
``{metric_id, as_of, value, source, source_as_of}`` and ``as_of`` is the
month-FIRST date (``YYYY-MM-01``) per ``normalise_as_of`` — idempotent on
``(metric_id, as_of)``.

Two BB sources, both empirically validated against live PDFs (see
``validation_evidence`` in the delivery note):

  1. Money Market Dynamics — ``/pub/monthly/moneymarket/money market dynamics_<mon><yyyy>.pdf``
     The "Summary of Money Market Dynamics (Interest Rates)" table gives, for
     the snapshot month, the WAR of:
       - A. Call Money Transaction      -> call_money_rate_monthly      (headline)
       - 1. Overnight                   -> call_money_rate_1d_monthly   (~1 day)
       - 2. Short notice (2 to 14 days) -> call_money_rate_14d_monthly  (2-14d bucket)
       - 3. Term (15 days and above)    -> call_money_rate_90d_monthly  (15-364d bucket)
     It also embeds ~10-month trailing WAR tables for the Short-notice and Term
     tenors (the headline + overnight trailing series live only in charts, so
     they are NOT machine-extractable from this PDF). Firecrawl renders these
     trailing tables in a non-deterministic position relative to their section
     headings across re-fetches, so each table is attributed to its bucket by
     matching its snapshot-month WAR against the summary table — NOT by heading
     proximity (see parse_mmd_trailing).

  2. Monthly Economic Trends — ``/pub/monthly/econtrds/et<mon><yy>.pdf``
     Table XVI "Monthly Average Call Money Market Rates (Weighted Average)".
     The "Average" column (Borrowing-rate average == Lending-rate average; BB
     prints them identically) is the headline monthly rate, with rows back to
     2009 -> call_money_rate_monthly.

TENOR-BUCKET CAVEAT (flagged): BB reports call money as three transaction
*buckets* (Overnight / Short-notice 2-14d / Term 15-364d), NOT as point tenors
on a curve. We map them onto _1d / _14d / _90d metric_ids as the closest
representative point. _14d is the WAR of the WHOLE 2-14d bucket; _90d is the
WAR of the WHOLE 15-364d bucket (not a 90-day point). Treat them as bucket
proxies, not exact-tenor quotes.

FILENAME DRIFT (landmine, observed): constructed Money Market Dynamics URLs do
NOT all resolve — ``apr2025`` resolves to a real PDF; ``dec2025`` / ``jan2026``
redirect to the BB homepage (HTTP 200, contentType text/html). The BB
publication archive index that lists the true filenames is CAPTCHA-walled from
automated fetchers. So this script takes an explicit MANIFEST of known-good
URLs (``--manifest``) and VERIFIES every fetch is a real PDF before parsing
(contentType application/pdf + numPages present); homepage-redirects are
skipped with a warning rather than silently producing 0 rows.

NETWORK: BB PDF endpoints are Akamai/Radware challenge-walled — plain
requests/urllib return the JS-challenge HTML, not the PDF (the same reason
``fetchers/pdf_fetcher_stealth.py`` exists). This backfill fetches via the
Firecrawl ``scrape`` API with ``parsers=["pdf"]`` + ``proxy=stealth``, which
returns the parsed PDF as markdown. Set ``FIRECRAWL_API_KEY`` in the env.
``--from-files`` lets tests / re-runs parse saved markdown with no network.

USAGE (validate only — NEVER writes to Supabase in this mode):
    PYTHONPATH=/path/to/econdelta /path/to/.venv/bin/python \
        backfill_call_money_monthly.py --dry-run --manifest manifest.json

This is a ONE-TIME backfill. It is NOT wired into parse_all.py / aggregate_latest.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import requests

logger = logging.getLogger("backfill_call_money_monthly")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_TABLE = "metric_history_monthly"
DEFINITIONS_TABLE = "metric_definitions_monthly"
DEFAULT_SOURCE = "bb_call_money_backfill"
DOMAIN = "credit_money"  # matches seed_macro_monthly's money-market domain
SOURCE_ATTRIBUTION = "Bangladesh Bank · Monetary Policy Dept / Debt Management Dept"

# metric_id -> (display_name, notes) for metric_definitions_monthly
METRIC_DEFS: dict[str, tuple[str, str]] = {
    "call_money_rate_monthly": (
        "Call money rate (WAR)",
        "Monthly weighted-average call money rate. Headline = all-tenor WAR.",
    ),
    "call_money_rate_1d_monthly": (
        "Call money rate — overnight (WAR)",
        "Overnight (~1 day) call money WAR. Bucket proxy, not a curve point.",
    ),
    "call_money_rate_14d_monthly": (
        "Call money rate — short notice 2-14d (WAR)",
        "WAR of the entire 2-14 day short-notice bucket. Bucket proxy, not a 14d point.",
    ),
    "call_money_rate_90d_monthly": (
        "Call money rate — term 15-364d (WAR)",
        "WAR of the entire 15-364 day term bucket. Bucket proxy, not a 90d point.",
    ),
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

# Defence-in-depth sanity bound on any extracted rate (percent per annum).
# Table XVI has a 2010 annual 'Highest' of 190.00 (a real outlier in the
# Highest/Lowest columns) — but we ONLY read the Average column, which has
# never exceeded ~13% in the published history. Keep a generous gate.
_RATE_MIN = 0.0
_RATE_MAX = 60.0


class BackfillError(Exception):
    """Raised when the backfill cannot complete a non-network step."""


# ---------------------------------------------------------------------------
# Date helpers (mirrors scripts/seed_macro_monthly.normalise_as_of)
# ---------------------------------------------------------------------------


def normalise_as_of(year: int, month: int) -> date:
    """Return the month-FIRST date (YYYY-MM-01) for a year/month."""
    return date(year, month, 1)


def parse_month_token(token: str) -> tuple[int, int] | None:
    """Parse 'Apr-25', 'Jul-24', 'April', 'September' style tokens.

    Returns (year, month) for 'Mon-YY' tokens. For a bare month name the year
    is unknown here (the Table XVI walker supplies the year context), so this
    returns (-1, month) and the caller fills the year.
    Returns None if the token isn't a month.
    """
    t = token.strip().lower()
    # 'apr-25' / 'apr-2025'
    m = re.match(r"^([a-z]{3,9})[-\s]?(\d{2,4})$", t)
    if m and m.group(1) in _MONTHS:
        yy = int(m.group(2))
        year = 2000 + yy if yy < 100 else yy
        return year, _MONTHS[m.group(1)]
    # bare 'april'
    if t in _MONTHS:
        return -1, _MONTHS[t]
    return None


def _to_float(cell: str | None) -> float | None:
    """Coerce a markdown table cell to float, or None for blank/placeholder."""
    if cell is None:
        return None
    text = str(cell).strip().replace(",", "")
    if not text or set(text) <= {"-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _split_md_row(line: str) -> list[str]:
    """Split a markdown table row '| a | b |' into ['a','b'] (trim outer pipes)."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

_MMD_BASE = "https://www.bb.org.bd/pub/monthly/moneymarket/money%20market%20dynamics_{mon}{yyyy}.pdf"
_ET_BASE = "https://www.bb.org.bd/pub/monthly/econtrds/et{mon}{yy}.pdf"


def mmd_url(year: int, month: int) -> str:
    """Construct a Money Market Dynamics URL (e.g. ..._apr2025.pdf).

    NB: construction is best-effort only — see FILENAME DRIFT in module docstring.
    Always verify the fetch is a real PDF.
    """
    mon = [k for k, v in _MONTHS.items() if v == month and len(k) == 3][0]
    return _MMD_BASE.format(mon=mon, yyyy=year)


def et_url(year: int, month: int) -> str:
    """Construct an Economic Trends URL (e.g. etjuly24.pdf — full month name, 2-digit year)."""
    mon = [k for k, v in _MONTHS.items() if v == month and len(k) > 3][0]
    return _ET_BASE.format(mon=mon, yy=year % 100)


# ---------------------------------------------------------------------------
# Parsers — pure functions over Firecrawl PDF markdown
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    metric_id: str
    as_of: date
    value: float
    source_label: str  # e.g. "mmd_summary apr2025" — provenance for the dry-run print

    def to_supabase(self, source: str = DEFAULT_SOURCE) -> dict:
        iso = self.as_of.isoformat()
        return {
            "metric_id": self.metric_id,
            "as_of": iso,
            "value": self.value,
            "source": source,
            "source_as_of": iso,
        }


# Interest Rates summary table: row-label substring -> metric_id.
_MMD_SUMMARY_MAP = [
    ("a. call money transaction", "call_money_rate_monthly"),
    ("1. overnight", "call_money_rate_1d_monthly"),
    ("2. short notice", "call_money_rate_14d_monthly"),
    ("3. term", "call_money_rate_90d_monthly"),
]


def _validate_rate(value: float, context: str) -> float:
    if not (_RATE_MIN <= value <= _RATE_MAX):
        raise BackfillError(
            f"rate {value} from {context} outside sanity range [{_RATE_MIN}, {_RATE_MAX}]"
        )
    return value


def parse_mmd_summary(markdown: str, year: int, month: int) -> list[Row]:
    """Parse the 'Summary of Money Market Dynamics (Interest Rates)' table.

    The table layout (validated apr2025):
      | Money Market | Min. | Max. | WAR | CV |
      | A. Call Money Transaction | 9.91 | 10.31 | 10.07 | 1.24% |
      | 1. Overnight | 9.83 | 10.01 | 9.93 | 0.52% |
      | 2. Short notice(2 to 14 days) | 10.27 | 11.70 | 10.80 | 4.02% |
      | 3. Term(15 days and above) | 7.00 | 13.00 | 11.34 | 14.96% |

    We pick the WAR column = 3rd numeric (index after the label). The label is
    column 0; Min/Max/WAR are columns 1/2/3.
    """
    as_of = normalise_as_of(year, month)
    label_tag = f"{['','jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'][month]}{year}"

    # Isolate the Interest-Rates summary section so we don't accidentally read
    # the Turnover summary (same row labels, different numbers). The exact
    # heading also appears in the Table of Contents, so we anchor on the LAST
    # occurrence (the real section), not the first (the TOC line).
    low = markdown.lower()
    start = low.rfind("summary of money market dynamics (interest rates)")
    section = markdown[start:] if start != -1 else markdown

    rows: list[Row] = []
    seen: set[str] = set()
    for line in section.splitlines():
        if "|" not in line:
            continue
        cells = _split_md_row(line)
        if len(cells) < 4:
            continue
        label = cells[0].lower().replace("\\", "").strip()
        for needle, metric_id in _MMD_SUMMARY_MAP:
            if metric_id in seen:
                continue
            if label.startswith(needle):
                war = _to_float(cells[3])  # WAR is the 3rd value column
                if war is None:
                    continue
                _validate_rate(war, f"mmd_summary {label_tag} {metric_id}")
                rows.append(Row(metric_id, as_of, war, f"mmd_summary {label_tag}"))
                seen.add(metric_id)
                break
    if "call_money_rate_monthly" not in seen:
        raise BackfillError(
            f"mmd_summary {label_tag}: headline 'A. Call Money Transaction' row not found"
        )
    return rows


def parse_mmd_trailing(markdown: str, bucket_anchors: dict[str, float]) -> list[Row]:
    """Parse the embedded ~10-month trailing WAR tables for Short-notice & Term.

    Each tenor section has two parallel tables (Turnover row + WAR row) keyed by
    month columns 'Jul-24 ... Apr-25'. The WAR table looks like:
      |  | Jul-24 | ... | Apr-25 |
      | Turnover | 9996 | ... | 10390 |
      | WAR | 10.08 | ... | 10.80 |

    LAYOUT IS NON-DETERMINISTIC: across Firecrawl re-fetches of the SAME PDF the
    trailing tables migrate between section headings (observed: the Short-notice
    WAR table rendered under the '3. Term Call Money' heading). So we do NOT
    trust heading proximity. Instead we assign each WAR table to a bucket by
    matching its FINAL-column value (the snapshot month) against the summary
    table's WAR for that bucket (``bucket_anchors`` = metric_id -> snapshot WAR).
    A table whose last value matches no anchor is dropped (with a warning) rather
    than mis-attributed.
    """
    rows: list[Row] = []
    lines = markdown.splitlines()

    pending_months: list[tuple[int, int] | None] | None = None
    seen_keys: set[tuple[str, str]] = set()

    # invert anchors: snapshot-WAR -> metric_id (tenor buckets only)
    tenor_anchors = {
        v: k for k, v in bucket_anchors.items()
        if k in ("call_money_rate_14d_monthly", "call_money_rate_90d_monthly")
    }

    for raw in lines:
        if "|" not in raw:
            continue
        cells = _split_md_row(raw)
        month_cols = [parse_month_token(c) for c in cells]
        n_months = sum(1 for mc in month_cols if mc and mc[0] != -1)
        if n_months >= 6:
            pending_months = month_cols
            continue
        if pending_months and cells and cells[0].lower().strip() in ("war", "wa r"):
            # Identify the bucket by the LAST month's value (snapshot month).
            last_val: float | None = None
            for col_idx in range(len(pending_months) - 1, -1, -1):
                mc = pending_months[col_idx]
                if mc and mc[0] != -1 and col_idx < len(cells):
                    last_val = _to_float(cells[col_idx])
                    if last_val is not None:
                        break
            metric_id = tenor_anchors.get(last_val) if last_val is not None else None
            if metric_id is None:
                logger.warning(
                    "mmd_trailing: WAR table (last value=%r) matched no summary "
                    "bucket anchor %r — dropping to avoid mis-attribution",
                    last_val, sorted(tenor_anchors),
                )
                pending_months = None
                continue
            for col_idx, mc in enumerate(pending_months):
                if not mc or mc[0] == -1 or col_idx >= len(cells):
                    continue
                val = _to_float(cells[col_idx])
                if val is None:
                    continue
                _validate_rate(val, f"mmd_trailing {metric_id}")
                yr, mo = mc
                key = (metric_id, f"{yr}-{mo}")
                if key in seen_keys:
                    continue
                rows.append(Row(metric_id, normalise_as_of(yr, mo), val,
                                f"mmd_trailing {metric_id}"))
                seen_keys.add(key)
            pending_months = None
    return rows


def parse_et_table_xvi(markdown: str) -> list[Row]:
    """Parse Table XVI 'Monthly Average Call Money Market Rates'.

    Layout (validated etjuly24.pdf):
      | Period | Borrowing Rate Highest | Lowest | Average | Lending Highest | Lowest | Average |
      | 2009 | 19.00 | 0.05 | 4.39 | 19.00 | 0.05 | 4.39 |
      ...
      | 2022 | 7.75 | 1.00 | 4.65 | ... |        <- annual row, sets year context
      | January | 5.25 | 1.00 | 2.43 | ... |      <- monthly rows belong to 2022
      ...
      | 2023 | 9.75 | 4.25 | 6.68 | ... |
      | January | 7.50 | 5.25 | 6.66 | ... |      <- now belong to 2023

    We emit ONLY monthly rows (a 4-digit annual row just sets the year context).
    The headline value is the Borrowing-rate 'Average' = column index 3.
    """
    rows: list[Row] = []
    seen: set[str] = set()
    # The heading 'Monthly Average Call Money Market Rates' also appears in the
    # Table of Contents; anchor on the LAST occurrence (the real table).
    up = markdown.upper()
    start = up.rfind("MONTHLY AVERAGE CALL MONEY")
    if start == -1:
        return rows
    section = markdown[start:]

    current_year: int | None = None
    for line in section.splitlines():
        if "|" not in line:
            # The table ends at the next non-table block ("Source: ...").
            if rows and ("source" in line.lower() or "table-" in line.lower()):
                break
            continue
        cells = _split_md_row(line)
        if len(cells) < 4:
            continue
        period = cells[0].strip()
        # Annual row: a bare 4-digit year sets the context for following months.
        if re.fullmatch(r"\d{4}", period):
            current_year = int(period)
            continue
        parsed = parse_month_token(period)
        if not parsed:
            continue
        _, month = parsed
        if current_year is None:
            continue  # monthly row before any year header — skip defensively
        avg = _to_float(cells[3])  # Borrowing-rate Average column
        if avg is None:
            continue
        _validate_rate(avg, f"et_xvi {current_year}-{month}")
        key = f"{current_year}-{month}"
        if key in seen:
            continue
        rows.append(Row("call_money_rate_monthly", normalise_as_of(current_year, month),
                        avg, f"et_xvi {current_year}-{month:02d}"))
        seen.add(key)
    return rows


# ---------------------------------------------------------------------------
# Fetch layer — Firecrawl scrape API
# ---------------------------------------------------------------------------

_FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v2/scrape"


def fetch_pdf_markdown(
    url: str,
    *,
    api_key: str | None = None,
    timeout: int = 120,
    session: requests.Session | None = None,
) -> str | None:
    """Fetch a BB PDF via Firecrawl and return its parsed markdown.

    Returns None (with a warning) if the response is NOT a real PDF — i.e. the
    constructed filename 404'd and BB served the homepage HTML. The caller
    skips such URLs (the FILENAME-DRIFT guard).
    """
    api_key = api_key or os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise BackfillError("FIRECRAWL_API_KEY not set (needed to fetch BB PDFs from here)")
    sess = session or requests.Session()
    payload = {
        "url": url,
        "formats": ["markdown"],
        "parsers": ["pdf"],
        "proxy": "stealth",
        "maxAge": 0,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = sess.post(_FIRECRAWL_ENDPOINT, json=payload, headers=headers, timeout=timeout)
    if resp.status_code >= 300:
        raise BackfillError(f"firecrawl HTTP {resp.status_code} for {url}: {resp.text[:200]}")
    body = resp.json()
    data = body.get("data", body)
    meta = data.get("metadata", {})
    content_type = (meta.get("contentType") or "").lower()
    if "application/pdf" not in content_type or not meta.get("numPages"):
        logger.warning(
            "SKIP %s — not a real PDF (contentType=%r, redirected url=%r); "
            "filename drift, construct from the archive index instead",
            url, content_type, meta.get("url"),
        )
        return None
    return data.get("markdown", "")


# ---------------------------------------------------------------------------
# Manifest + orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    source: str          # 'mmd' or 'et'
    url: str
    year: int
    month: int


def load_manifest(path: Path) -> list[ManifestEntry]:
    """Load a manifest JSON: list of {source, url, year, month}.

    'url' is optional — if absent it is constructed from year/month (best-effort).
    """
    raw = json.loads(path.read_text())
    entries: list[ManifestEntry] = []
    for item in raw:
        src = item["source"]
        year = int(item["year"])
        month = int(item["month"])
        url = item.get("url")
        if not url:
            url = mmd_url(year, month) if src == "mmd" else et_url(year, month)
        entries.append(ManifestEntry(src, url, year, month))
    return entries


def parse_entry(source: str, markdown: str, year: int, month: int) -> list[Row]:
    """Dispatch a single fetched PDF to the right parser(s)."""
    if source == "mmd":
        rows = parse_mmd_summary(markdown, year, month)
        # Anchor trailing tables on the summary WARs for this snapshot month, so
        # firecrawl's non-deterministic table placement can't mis-attribute them.
        anchors = {r.metric_id: r.value for r in rows}
        # Trailing tables are a bonus — never fail the whole entry on them.
        try:
            rows += parse_mmd_trailing(markdown, anchors)
        except Exception as e:  # noqa: BLE001 — trailing series is best-effort
            logger.warning("mmd trailing parse skipped for %d-%02d: %s", year, month, e)
        return rows
    if source == "et":
        return parse_et_table_xvi(markdown)
    raise BackfillError(f"unknown manifest source {source!r}")


def dedupe_rows(rows: Iterable[Row]) -> list[Row]:
    """Collapse duplicate (metric_id, as_of). MMD summary (current month, all 4
    buckets) wins over MMD trailing and ET headline, since the summary table is
    the most authoritative for its own snapshot month. Resolution order is the
    order rows are produced: summary first within an entry, and the caller is
    expected to order entries newest-first if it cares. Last-write-wins here
    would clobber the better source, so FIRST-write-wins."""
    out: dict[tuple[str, str], Row] = {}
    for r in rows:
        key = (r.metric_id, r.as_of.isoformat())
        if key not in out:
            out[key] = r
    return list(out.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_dry_run(rows: list[Row]) -> None:
    by_metric: dict[str, list[Row]] = {}
    for r in rows:
        by_metric.setdefault(r.metric_id, []).append(r)
    print("\n=== DRY RUN — parsed rows ({} total, NO Supabase writes) ===".format(len(rows)))
    for metric_id in sorted(by_metric):
        mrows = sorted(by_metric[metric_id], key=lambda r: r.as_of)
        dates = [r.as_of.isoformat() for r in mrows]
        print(f"\n{metric_id}  ({len(mrows)} rows, {dates[0]} .. {dates[-1]})")
        # Print up to first 3 and last 3 as evidence.
        sample = mrows[:3] + (mrows[-3:] if len(mrows) > 6 else mrows[3:])
        for r in sample:
            print(f"    {{metric_id: {r.metric_id}, month: {r.as_of.isoformat()}, "
                  f"value: {r.value}}}  [{r.source_label}]")


def _upsert(rows: list[dict], table: str, on_conflict: str, *, timeout: int = 60) -> int:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise BackfillError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for a real write")
    endpoint = f"{url.rstrip('/')}/rest/v1/{table}?on_conflict={on_conflict}"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    resp = requests.post(endpoint, json=rows, headers=headers, timeout=timeout)
    if resp.status_code >= 300:
        raise BackfillError(f"upsert {table} HTTP {resp.status_code}: {resp.text[:300]}")
    return len(rows)


def build_definition_rows() -> list[dict]:
    out = []
    for metric_id, (display_name, notes) in METRIC_DEFS.items():
        out.append({
            "metric_id": metric_id,
            "display_name": display_name,
            "unit": "%",
            "source_url": "https://www.bb.org.bd/en/index.php/publication/publictn/2/30",
            "source_attribution": SOURCE_ATTRIBUTION,
            "domain": DOMAIN,
            "description": display_name,
            "notes": notes,
        })
    return out


def run(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse + print sample rows; NO Supabase writes")
    p.add_argument("--manifest", type=Path,
                   help="JSON list of {source('mmd'|'et'), year, month, url?}")
    p.add_argument("--from-files", type=Path, nargs="*",
                   help="parse saved markdown files instead of fetching "
                        "(filename must encode source+year+month: e.g. mmd_2025_04.md)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    all_rows: list[Row] = []

    if args.from_files:
        for f in args.from_files:
            m = re.match(r"^(mmd|et)_(\d{4})_(\d{2})", f.stem)
            if not m:
                raise BackfillError(f"--from-files name must start with <src>_<yyyy>_<mm>: {f}")
            src, yy, mm = m.group(1), int(m.group(2)), int(m.group(3))
            all_rows += parse_entry(src, f.read_text(), yy, mm)
    elif args.manifest:
        entries = load_manifest(args.manifest)
        # Fetch newest-first so MMD-summary current-month rows win dedupe.
        entries.sort(key=lambda e: (e.year, e.month), reverse=True)
        for e in entries:
            logger.info("fetching %s %d-%02d -> %s", e.source, e.year, e.month, e.url)
            md = fetch_pdf_markdown(e.url)
            if md is None:
                continue  # filename drift — skipped with warning
            all_rows += parse_entry(e.source, md, e.year, e.month)
            time.sleep(1)  # be polite to firecrawl / BB
    else:
        raise BackfillError("provide --manifest <file> or --from-files <md...>")

    rows = dedupe_rows(all_rows)

    if args.dry_run or not rows:
        _print_dry_run(rows)
        if args.dry_run:
            logger.info("--dry-run: no writes performed (%d rows would upsert to %s)",
                        len(rows), TARGET_TABLE)
            return 0

    # Real write path (NOT exercised under --dry-run).
    supa_rows = [r.to_supabase() for r in rows]
    sent = _upsert(supa_rows, TARGET_TABLE, "metric_id,as_of")
    defs = build_definition_rows()
    sent_defs = _upsert(defs, DEFINITIONS_TABLE, "metric_id")
    logger.info("upsert ok: %d history rows -> %s, %d definitions -> %s",
                sent, TARGET_TABLE, sent_defs, DEFINITIONS_TABLE)
    return 0


if __name__ == "__main__":
    sys.exit(run())
