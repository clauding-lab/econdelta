"""IMF DataMapper debt/GDP history scraper — NO BD egress, runs on any host.

Why a ``scrapers/`` one-shot and not a ``fetchers/`` helper or a ``_fetch_one``
edit: the IMF DataMapper API returns JSON, not HTML/PDF. ``fetch_all._fetch_one``
dispatches ONLY ``html``/``pdf`` (anything else logs "unsupported fetch.type"
and yields nothing), so a JSON puller cannot ride the v3 fetch→parse pipeline.
This mirrors ``scrapers/commodity_prices.py`` / ``scrapers/dse_market.py``,
which already run as standalone scripts outside that dispatch.

What it does: pulls the IMF general-government gross-debt-as-%-of-GDP series
(indicator ``GGXWDG_NGDP``) for Bangladesh, then upserts the multi-year history
into ``metric_history`` under ``debt_gdp_ratio`` — one row per year, each stamped
``as_of = <year>-12-31`` (a distinct (metric_id, as_of) PK per year, exactly the
shape ``scripts/backfill_dse_dayend.py`` uses). YieldScope reads this back via
``fetchSeries(METRIC.DEBT_GDP_RATIO)`` for the Fiscal history chart.

Scope decision (per plan S4 task 2): this seeds a SHORT accumulated series into
``metric_history`` (the daily-namespace history backend), NOT the monthly
backfill system (``metric_history_monthly``) — mixing IMF years into the monthly
namespace would be a namespace-boundary change (landmine D) needing sign-off.
The latest BD-OFFICIAL print of ``debt_gdp_ratio`` still comes from the MoF Debt
Bulletin PDF leg (config entry, BD egress); this IMF leg supplies the back-history
context only. IMF figures are general-government gross debt (a wider definition
than MoF central-government debt) so they print a touch higher.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

import requests

from utils.notifier import notify
from utils.supabase_writer import upsert_metric_history

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

# The metric these years land under in metric_history. Matches the config id so
# the MoF Debt Bulletin latest-print leg and this IMF back-history share one id.
METRIC_ID = "debt_gdp_ratio"

# Reject obviously-wrong values defensively (mirrors the config valid_range).
VALID_RANGE = (10.0, 100.0)

_TIMEOUT = 30

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
    valid_range: tuple[float, float] = VALID_RANGE,
) -> dict[int, float]:
    """Extract {year: value} for one country from an IMF DataMapper payload.

    Pure (no I/O) so it unit-tests against the captured fixture with no egress.
    Drops any value outside ``valid_range`` (defensive — IMF occasionally carries
    forecast outliers) and any year key that isn't a 4-digit integer. Raises
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
        if lo <= val <= hi:
            out[int(year_key)] = val
        else:
            logger.warning("dropping out-of-range %s %s = %s", country, year_key, val)
    if not out:
        raise FetchError(f"no in-range yearly values parsed for {country}/{indicator}")
    return out


def upsert_history(series: dict[int, float]) -> int:
    """Upsert each {year: value} as a debt_gdp_ratio row stamped <year>-12-31.

    metric_history's PK is (metric_id, as_of); a single flat ``data`` dict can
    only carry one as_of per metric_id, so — like backfill_dse_dayend — we call
    upsert_metric_history once per year. Returns the total rows written.
    """
    total = 0
    for year in sorted(series):
        as_of = date(year, 12, 31)
        total += upsert_metric_history(
            data={METRIC_ID: series[year]},
            as_of=as_of,
            source="IMF DataMapper",
            source_as_of_map={METRIC_ID: as_of},
            url=IMF_URL,
        )
    return total


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        payload = fetch_imf_payload()
        series = parse_imf_series(payload)
    except FetchError as e:
        logger.exception("IMF debt/GDP fetch/parse failed")
        notify("error", "imf_debt_gdp fetch failed", str(e))
        return 1

    latest_year = max(series)
    logger.info(
        "parsed %d yearly debt/GDP points for %s (%d-%d); latest %d = %.1f%%",
        len(series),
        IMF_COUNTRY,
        min(series),
        latest_year,
        latest_year,
        series[latest_year],
    )

    written = upsert_history(series)
    logger.info("upserted %d %s rows into metric_history", written, METRIC_ID)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("imf_debt_gdp", "econdelta-imf-debt.service", main))
