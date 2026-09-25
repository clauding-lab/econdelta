"""IMF DataMapper debt/GDP history scraper — NO BD egress, runs on any host.

Why a ``scrapers/`` one-shot and not a ``fetchers/`` helper or a ``_fetch_one``
edit: the IMF DataMapper API returns JSON, not HTML/PDF. ``fetch_all._fetch_one``
dispatches ONLY ``html``/``pdf`` (anything else logs "unsupported fetch.type"
and yields nothing), so a JSON puller cannot ride the v3 fetch→parse pipeline.
This mirrors ``scrapers/commodity_prices.py`` / ``scrapers/dse_market.py``,
which already run as standalone scripts outside that dispatch.

What it does: pulls the IMF general-government gross-debt-as-%-of-GDP series
(indicator ``GGXWDG_NGDP``) for Bangladesh, archives the full response with its
retrieval time, then upserts completed Bangladesh fiscal years into
``metric_history`` under ``imf_general_govt_debt_pct_gdp``. Bangladesh fiscal
years end on 30 June, so each year is stamped ``as_of = <year>-06-30``.

IMF general-government estimates and the MoF Debt Bulletin public-debt ratio
have different coverage and remain separate series. The IMF series includes
estimates or projections where designated by the IMF and must not be described
as a set of audited actuals. Future fiscal years remain in the local raw-payload
archive for evidence but are not written into ordinary history.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from utils.ipv4 import force_ipv4_only
from utils.notifier import notify
from utils.supabase_writer import upsert_metric_history, verify_landed_count

logger = logging.getLogger("imf_debt_gdp")

# IMF DataMapper REST endpoint. The series indicator code for
# "General Government Gross Debt, % of GDP" is GGXWDG_NGDP. The API ignores any
# trailing /COUNTRY path segment and returns ALL countries, so we filter the
# Bangladesh (ISO-3 BGD) slice client-side. (A ?country= query param is rejected
# by IMF's WAF — verified — so the bare-indicator URL + client filter is the
# only working shape.)
IMF_INDICATOR = "GGXWDG_NGDP"
IMF_COUNTRY = "BGD"
IMF_URL = f"https://www.imf.org/external/datamapper/api/v1/{IMF_INDICATOR}"

# MoF's source registry continues to own this stable key. IMF estimates have a
# separate ID because their government coverage and source status differ.
MOF_METRIC_ID = "debt_gdp_ratio"
IMF_METRIC_ID = "imf_general_govt_debt_pct_gdp"
IMF_SOURCE = "IMF DataMapper (WEO; estimates/projections where applicable)"
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "archive" / "imf_debt_gdp"
BDT = ZoneInfo("Asia/Dhaka")

# Reject obviously-wrong values defensively (mirrors the config valid_range).
VALID_RANGE = (10.0, 100.0)

# (connect, read) seconds — fast-fail on a stalled connect rather than one 30s
# budget per dead address; defense-in-depth alongside the IPv4 force below
# (www.imf.org's IPv6 is blackholed from the ExonVPS box).
_TIMEOUT: tuple[int, int] = (10, 30)

# IMPORTANT: the IMF DataMapper API sits behind Akamai EdgeSuite, which BLOCKS
# spoofed browser User-Agents (a fake "Mozilla/5.0 ... Chrome" UA returns HTTP
# 403 "Access Denied") but ALLOWS honest non-browser clients. So we send NO
# custom User-Agent and let requests use its default `python-requests/...` UA
# (verified HTTP 200 from this Mac, 2026-05-31). Do NOT add a browser UA here —
# that is precisely what Akamai rejects. This is the opposite of BB's CAPTCHA
# wall; the two must not be "fixed" with the same browser-UA trick.


class FetchError(Exception):
    pass


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


def parse_imf_series(
    payload: dict,
    *,
    indicator: str = IMF_INDICATOR,
    country: str = IMF_COUNTRY,
    today: date,
    valid_range: tuple[float, float] = VALID_RANGE,
) -> dict[int, float]:
    """Extract completed Bangladesh fiscal-year {end_year: value} observations.

    Pure (no I/O) so it unit-tests against the captured fixture with no egress.
    A year is eligible only once its June 30 fiscal-year end is on or before
    ``today``. Drops any out-of-range value and any non-year key. Raises
    FetchError if the indicator/country slice is missing or empty.
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
        year = int(year_key)
        if year < 1000:
            continue
        if date(year, 6, 30) > today:
            continue
        if lo <= val <= hi:
            out[year] = val
        else:
            logger.warning("dropping out-of-range %s %s = %s", country, year_key, val)
    if not out:
        raise FetchError(f"no in-range yearly values parsed for {country}/{indicator}")
    return out


def upsert_history(series: dict[int, float], *, today: date) -> int:
    """Upsert eligible IMF estimates under their own key at Bangladesh FY end.

    metric_history's PK is (metric_id, as_of); a single flat ``data`` dict can
    only carry one as_of per metric_id, so — like backfill_dse_dayend — we call
    upsert_metric_history once per year. Returns the total rows written.
    """
    # One write timestamp for the whole multi-year run so the E2.2 read-back
    # counts every year's row this run wrote (scoped to IMF_METRIC_ID — the other
    # Sunday-23:xx writers can't inflate the count).
    write_ts = datetime.now(timezone.utc)
    total = 0
    for year in sorted(series):
        as_of = date(year, 6, 30)
        if as_of > today:
            continue
        total += upsert_metric_history(
            data={IMF_METRIC_ID: series[year]},
            as_of=as_of,
            source=IMF_SOURCE,
            source_as_of_map={IMF_METRIC_ID: as_of},
            ingested_at=write_ts,
            # Plain JSON API parse — no LLM call.
            provenance="deterministic",
        )
    verify_landed_count(total, since=write_ts, metric_ids=[IMF_METRIC_ID], source_label="imf_debt_gdp")
    return total


def archive_fetched_payload(
    payload: dict,
    *,
    retrieved_at: datetime,
    archive_dir: Path = ARCHIVE_DIR,
) -> Path:
    """Atomically retain the complete API body and a UTC retrieval timestamp."""
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone")
    retrieved_utc = retrieved_at.astimezone(timezone.utc)
    stamp = retrieved_utc.strftime("%Y%m%dT%H%M%S.%fZ")
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / f"{IMF_INDICATOR}_{IMF_COUNTRY}_{stamp}.json"
    temporary = destination.with_suffix(".json.tmp")
    envelope = {
        "indicator": IMF_INDICATOR,
        "country": IMF_COUNTRY,
        "source_url": IMF_URL,
        "retrieved_at": retrieved_utc.isoformat(),
        "payload": payload,
    }
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        temporary.replace(destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise FetchError(f"could not archive IMF source payload: {exc}") from exc
    return destination


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        payload = fetch_imf_payload()
        retrieved_at = datetime.now(timezone.utc)
        archived_path = archive_fetched_payload(payload, retrieved_at=retrieved_at)
        today = datetime.now(BDT).date()
        series = parse_imf_series(payload, today=today)
    except FetchError as e:
        logger.exception("IMF debt/GDP fetch/parse failed")
        notify("error", "imf_debt_gdp fetch failed", str(e))
        return 1

    latest_year = max(series)
    logger.info(
        "archived source payload at %s; parsed %d completed fiscal-year debt/GDP estimates "
        "for %s (%d-%d); latest %d = %.1f%%",
        archived_path,
        len(series),
        IMF_COUNTRY,
        min(series),
        latest_year,
        latest_year,
        series[latest_year],
    )

    written = upsert_history(series, today=today)
    logger.info("upserted %d %s rows into metric_history", written, IMF_METRIC_ID)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("imf_debt_gdp", "econdelta-imf-debt.service", main))
