"""Fiscal revenue/GDP ratio scrapers — NO BD egress, runs on any host.

Two long-dormant metric ids, made REAL by sourcing them from durable JSON APIs
(mirrors ``scrapers/imf_debt_gdp.py``). Both write one row per year into
``metric_history``, each stamped ``as_of = <year>-12-31`` (the same year-end
convention as ``imf_debt_gdp.py`` — NOT an FY-end date):

  a) ``rev_gdp_ratio`` — IMF DataMapper indicator ``rev`` (Government revenue,
     % of GDP), country BGD. Nested-dict payload
     ``payload["values"]["rev"]["BGD"] = {"<year>": <value>, ...}`` — the exact
     shape ``imf_debt_gdp.py`` parses. Latest known good: 2024 = 8.34 %.

  b) ``tax_gdp_ratio`` — World Bank API indicator ``GC.TAX.TOTL.GD.ZS`` (Tax
     revenue, % of GDP), country BGD. DIFFERENT payload shape: a JSON array
     ``[meta_obj, [row, row, ...]]`` where each row is
     ``{"date": "2021", "value": 7.64..., "indicator": {...}, ...}`` and
     ``value`` may be ``null`` (nulls are skipped). Latest known good:
     2021 = 7.64 %. The World Bank series is intentionally stale (WB stops at
     2021); that is EXPECTED — we stamp the true vintage, never the run date.

Why a ``scrapers/`` one-shot and not a config indicator / ``fetchers`` helper:
both APIs return JSON, and ``fetch_all._fetch_one`` dispatches ONLY ``html``/
``pdf`` (anything else logs "unsupported fetch.type" and yields nothing). So a
JSON puller cannot ride the v3 fetch→parse pipeline. This mirrors
``scrapers/imf_debt_gdp.py`` / ``scrapers/imf_eff.py`` /
``scrapers/world_bank_pink_sheet.py``: standalone NO-egress scripts that fetch +
parse + upsert directly to ``metric_history`` under their own ids, with NO
``config/sources-v3.json`` entry (the two dead stub entries were removed).

Landmines honoured (each is load-bearing — see AGENTS.md):
  - 23: both ``www.imf.org`` and ``api.worldbank.org`` have IPv6 blackholed from
    the ExonVPS box, so each fetch is wrapped in ``utils.ipv4.force_ipv4_only()``
    — scoped to the fetch only; the global is restored before the Supabase upsert.
  - 22: NEVER pass a source URL as ``upsert_metric_history(url=...)`` — that kwarg
    is the SUPABASE base-URL override and silently misroutes the write.
  - 26: ``source_as_of_map`` is MANDATORY — these are annual/fiscal metrics, so
    without it every row would be stamped with today's run date and read as
    falsely fresh.
  - E2.2: ``verify_landed_count`` after each metric's writes, scoped to that id.

Independence: ``main()`` fetches+parses+upserts EACH source in its own try/except.
If IMF fails, World Bank still writes (and vice-versa); each failure ``notify``s
separately. Returns 1 if EITHER source failed (so ``run_logs`` flags it), 0 only
if both succeeded.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone

import requests

from utils.ipv4 import force_ipv4_only
from utils.notifier import notify
from utils.supabase_writer import (
    SupabaseWriteError,
    upsert_metric_history,
    verify_landed_count,
)

logger = logging.getLogger("fiscal_gdp_ratios")

# --------------------------------------------------------------------------- #
# IMF DataMapper — Government revenue, % of GDP (indicator "rev"), BGD.
# --------------------------------------------------------------------------- #
IMF_INDICATOR = "rev"
IMF_COUNTRY = "BGD"
IMF_URL = f"https://www.imf.org/external/datamapper/api/v1/{IMF_INDICATOR}"
REV_METRIC_ID = "rev_gdp_ratio"
REV_SOURCE = "IMF DataMapper"
# Reject obviously-wrong values defensively (matches the retired config valid_range).
REV_VALID_RANGE = (0.0, 40.0)

# --------------------------------------------------------------------------- #
# World Bank API — Tax revenue, % of GDP (indicator GC.TAX.TOTL.GD.ZS), BGD.
# --------------------------------------------------------------------------- #
WB_INDICATOR = "GC.TAX.TOTL.GD.ZS"
WB_COUNTRY = "BGD"
WB_URL = (
    f"https://api.worldbank.org/v2/country/{WB_COUNTRY}/indicator/{WB_INDICATOR}"
    "?format=json&per_page=100"
)
TAX_METRIC_ID = "tax_gdp_ratio"
TAX_SOURCE = "World Bank"
TAX_VALID_RANGE = (0.0, 30.0)

# (connect, read) seconds — fast-fail on a stalled connect rather than one 30s
# budget per dead address; defense-in-depth alongside the IPv4 force below (both
# www.imf.org and api.worldbank.org have IPv6 blackholed from the ExonVPS box).
_TIMEOUT: tuple[int, int] = (10, 30)

# IMPORTANT: the IMF DataMapper API sits behind Akamai EdgeSuite, which BLOCKS
# spoofed browser User-Agents (a fake "Mozilla/5.0 ... Chrome" UA returns HTTP
# 403 "Access Denied") but ALLOWS honest non-browser clients. So we send NO
# custom User-Agent and let requests use its default `python-requests/...` UA.
# Do NOT add a browser UA here — that is precisely what Akamai rejects. The
# World Bank API is equally happy with the default UA.


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------- #
# IMF DataMapper — fetch + parse (nested-dict payload shape).
# --------------------------------------------------------------------------- #


def fetch_imf_payload(
    *, url: str = IMF_URL, session: requests.Session | None = None
) -> dict:
    """GET the IMF DataMapper JSON. Raises FetchError on network/HTTP failure."""
    sess = session or requests.Session()
    try:
        # www.imf.org's IPv6 is blackholed from the ExonVPS box; resolve IPv4-only
        # for this fetch (the global is restored so the upsert is unaffected).
        with force_ipv4_only():
            resp = sess.get(url, timeout=_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise FetchError(f"network error fetching IMF DataMapper: {e}") from e
    if resp.status_code != 200:
        raise FetchError(f"IMF DataMapper returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise FetchError(f"IMF DataMapper response was not JSON: {e}") from e


def parse_imf_rev_series(
    payload: dict,
    *,
    indicator: str = IMF_INDICATOR,
    country: str = IMF_COUNTRY,
    valid_range: tuple[float, float] = REV_VALID_RANGE,
) -> dict[int, float]:
    """Extract {year: value} for one country from an IMF DataMapper payload.

    Pure (no I/O) so it unit-tests against the captured fixture with no egress.
    Drops any value outside ``valid_range`` and any year key that isn't a 4-digit
    integer. Raises FetchError if the indicator/country slice is missing or empty.
    """
    values = payload.get("values")
    if not isinstance(values, dict):
        raise FetchError("IMF payload has no 'values' object")
    by_country = values.get(indicator)
    if not isinstance(by_country, dict):
        raise FetchError(f"IMF payload missing indicator {indicator!r}")
    series = by_country.get(country)
    if not isinstance(series, dict) or not series:
        raise FetchError(f"IMF payload missing/empty series for country {country!r}")

    lo, hi = valid_range
    out: dict[int, float] = {}
    for year_key, raw in series.items():
        if not (isinstance(year_key, str) and year_key.isdigit() and len(year_key) == 4):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if lo <= val <= hi:
            out[int(year_key)] = val
        else:
            logger.warning("dropping out-of-range %s %s = %s", country, year_key, val)
    if not out:
        raise FetchError(f"no in-range yearly values parsed for {country}/{indicator}")
    return out


# --------------------------------------------------------------------------- #
# World Bank API — fetch + parse ([meta, rows] array payload shape).
# --------------------------------------------------------------------------- #


def fetch_wb_payload(
    *, url: str = WB_URL, session: requests.Session | None = None
) -> list:
    """GET the World Bank indicator JSON. Raises FetchError on network/HTTP failure."""
    sess = session or requests.Session()
    try:
        # api.worldbank.org's IPv6 is blackholed from the ExonVPS box; resolve
        # IPv4-only for this fetch (the global is restored before the upsert).
        with force_ipv4_only():
            resp = sess.get(url, timeout=_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise FetchError(f"network error fetching World Bank API: {e}") from e
    if resp.status_code != 200:
        raise FetchError(f"World Bank API returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise FetchError(f"World Bank API response was not JSON: {e}") from e


def parse_wb_tax_series(
    payload: list,
    *,
    indicator: str = WB_INDICATOR,
    country: str = WB_COUNTRY,
    valid_range: tuple[float, float] = TAX_VALID_RANGE,
) -> dict[int, float]:
    """Extract {year: value} from a World Bank ``[meta, rows]`` payload.

    Pure (no I/O) so it unit-tests against the captured fixture with no egress.
    The World Bank envelope is a 2-element array: ``[meta_obj, [row, ...]]``. Each
    row carries ``indicator.id``, ``countryiso3code``, ``date`` (a 4-digit year),
    and ``value`` (which is ``null`` for years with no observation — those are
    skipped). Drops any value outside ``valid_range``. Raises FetchError if the
    envelope shape is wrong, or NO row matches the requested indicator/country.
    """
    if not (isinstance(payload, list) and len(payload) == 2):
        raise FetchError("World Bank payload is not the [meta, rows] array shape")
    _meta, rows = payload
    if not isinstance(rows, list):
        raise FetchError("World Bank payload rows element is not a list")

    lo, hi = valid_range
    out: dict[int, float] = {}
    matched_any = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_indicator = (row.get("indicator") or {}).get("id")
        # Normalise the ISO-3 casing/whitespace defensively — a country-scoped WB
        # query returns uppercase reliably, but a casing quirk must not silently
        # take the whole tax leg offline.
        row_country = (row.get("countryiso3code") or "").upper().strip()
        if row_indicator != indicator or row_country != country.upper().strip():
            continue
        matched_any = True
        raw = row.get("value")
        if raw is None:
            # No observation for this year — the WB carries a null cell. Skip.
            continue
        date_str = row.get("date")
        if not (isinstance(date_str, str) and date_str.isdigit() and len(date_str) == 4):
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        year = int(date_str)
        if lo <= val <= hi:
            out[year] = val
        else:
            logger.warning("dropping out-of-range %s %s = %s", country, date_str, val)

    if not matched_any:
        raise FetchError(
            f"World Bank payload has no rows for country {country!r} / indicator {indicator!r}"
        )
    if not out:
        raise FetchError(f"no in-range yearly values parsed for {country}/{indicator}")
    return out


# --------------------------------------------------------------------------- #
# Upsert — one row per year, each stamped <year>-12-31 (metric_history PK is
# (metric_id, as_of), so a flat ``data`` dict carries one as_of; call once/year).
# --------------------------------------------------------------------------- #


def _upsert_year_series(
    series: dict[int, float], *, metric_id: str, source: str, source_label: str
) -> int:
    """Upsert each {year: value} as one ``metric_id`` row stamped <year>-12-31.

    One write timestamp for the whole multi-year run so the E2.2 read-back counts
    every year's row this run wrote (scoped to ``metric_id`` — no sibling writer
    can inflate the count). Returns the total rows written.
    """
    write_ts = datetime.now(timezone.utc)
    total = 0
    for year in sorted(series):
        as_of = date(year, 12, 31)
        total += upsert_metric_history(
            data={metric_id: series[year]},
            as_of=as_of,
            source=source,
            source_as_of_map={metric_id: as_of},
            ingested_at=write_ts,
        )
    verify_landed_count(
        total, since=write_ts, metric_ids=[metric_id], source_label=source_label
    )
    return total


def upsert_rev_history(series: dict[int, float]) -> int:
    """Upsert the IMF revenue/GDP series under ``rev_gdp_ratio`` (source IMF DataMapper)."""
    return _upsert_year_series(
        series,
        metric_id=REV_METRIC_ID,
        source=REV_SOURCE,
        source_label="fiscal_gdp_rev",
    )


def upsert_tax_history(series: dict[int, float]) -> int:
    """Upsert the World Bank tax/GDP series under ``tax_gdp_ratio`` (source World Bank)."""
    return _upsert_year_series(
        series,
        metric_id=TAX_METRIC_ID,
        source=TAX_SOURCE,
        source_label="fiscal_gdp_tax",
    )


# --------------------------------------------------------------------------- #
# Per-source runners + main — each source is fully independent.
# --------------------------------------------------------------------------- #


def _run_rev() -> None:
    """Fetch + parse + upsert the IMF revenue/GDP series. Raises on failure."""
    payload = fetch_imf_payload()
    series = parse_imf_rev_series(payload)
    latest_year = max(series)
    logger.info(
        "parsed %d IMF revenue/GDP points for %s (%d-%d); latest %d = %.2f%%",
        len(series),
        IMF_COUNTRY,
        min(series),
        latest_year,
        latest_year,
        series[latest_year],
    )
    written = upsert_rev_history(series)
    logger.info("upserted %d %s rows into metric_history", written, REV_METRIC_ID)


def _run_tax() -> None:
    """Fetch + parse + upsert the World Bank tax/GDP series. Raises on failure."""
    payload = fetch_wb_payload()
    series = parse_wb_tax_series(payload)
    latest_year = max(series)
    logger.info(
        "parsed %d World Bank tax/GDP points for %s (%d-%d); latest %d = %.2f%%",
        len(series),
        WB_COUNTRY,
        min(series),
        latest_year,
        latest_year,
        series[latest_year],
    )
    written = upsert_tax_history(series)
    logger.info("upserted %d %s rows into metric_history", written, TAX_METRIC_ID)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ok = True

    try:
        _run_rev()
    except (FetchError, SupabaseWriteError) as e:
        logger.exception("IMF revenue/GDP source failed")
        notify("error", "fiscal_gdp_ratios: IMF rev source failed", str(e))
        ok = False

    try:
        _run_tax()
    except (FetchError, SupabaseWriteError) as e:
        logger.exception("World Bank tax/GDP source failed")
        notify("error", "fiscal_gdp_ratios: World Bank tax source failed", str(e))
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("fiscal_gdp_ratios", "econdelta-fiscal-gdp.service", main))
