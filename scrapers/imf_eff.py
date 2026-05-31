"""IMF Extended-Fund-Facility (EFF) outstanding scraper — NO BD egress.

Why a ``scrapers/`` one-shot and not a config indicator / ``fetchers`` helper:
the IMF Fund-credit figures live on the IMF "Financial Position in the Fund"
page (``np/fin/tad/exfin2.aspx``), an ASP.NET HTML report keyed by an internal
member id + an explicit month-end ``date1key``. It is NOT a v3 ``html``/``pdf``
config source (there is no per-edition discovery and the value is one labelled
table cell, not a daily/monthly PDF print), and the IMF *DataMapper* API does
NOT carry Fund-credit/lending series at all (DataMapper is WEO/FM macro
forecasts only — verified: its 132-indicator catalogue has no EFF/ECF/RSF/credit
line). So this mirrors ``scrapers/imf_debt_gdp.py`` / ``commodity_prices.py``:
a standalone NO-egress script that fetches + parses + upserts directly to
``metric_history`` under its own id, outside the fetch_all/parse_all dispatch.

What it captures: Bangladesh's **Extended Arrangements** (EFF) outstanding under
the combined ECF/EFF/RSF programme — the specific facility the YieldScope Fiscal
"IMF-EFF tranche" tile names. Reported by the IMF in **SDR Million** (the page's
native unit), so the metric id carries the unit explicitly (``_sdr_mn``) — there
is no ``amount_sdr_mn`` value_type in ``claude_max/validators.py`` and we do NOT
convert SDR→USD here (the SDR/USD rate drifts daily; a converted figure would be
a moving target divorced from the IMF's own published number). The page also
lists RSF (666.68), RCF (159.99) and ECF (686.64) outstanding alongside; this
script captures only the EFF row — extend ``EFF_LABEL`` / add sibling parses if
the other facilities are wanted later.

as_of: the page title carries the true reporting date ("... as of April 30,
2026"); we parse it so ``metric_history.as_of`` reflects the IMF's month-end
position date, not the run date. Requesting a non-month-end / future date
gracefully returns the latest available position (verified: date1key=2026-05-31
returned the April-30 print), so the script asks for "today" and lets the IMF
serve whatever the latest published position is.
"""

from __future__ import annotations

import html as _html
import logging
import re
import sys
from datetime import date, datetime

import requests

from utils.notifier import notify
from utils.supabase_writer import upsert_metric_history

logger = logging.getLogger("imf_eff")

# IMF "Financial Position in the Fund" report. ``memberKey1`` is the IMF's
# internal member id (Bangladesh = 55, read off the exfin1.aspx member dropdown,
# verified 2026-05-31). ``date1key`` must be present or the report renders empty;
# a future/non-month-end date is clamped server-side to the latest published
# position, so we pass today's date and take whatever the IMF serves.
IMF_MEMBER_KEY = 55
IMF_URL_TEMPLATE = (
    "https://www.imf.org/external/np/fin/tad/exfin2.aspx"
    "?memberKey1={member}&date1key={datekey}"
)

# The facility row this metric tracks. The page lists each outstanding facility
# on its own line under "Outstanding Purchases and Loans:" as
#   "<Facility> <SDR-million> <%-of-quota>".
EFF_LABEL = "Extended Arrangements"

METRIC_ID = "imf_eff_outstanding_sdr_mn"

# Bangladesh's EFF outstanding sits around SDR 1,373 mn (Apr-2026); a generous
# band rejects a parse that grabbed the wrong cell (e.g. the %-quota column ~129)
# or a totally different number.
VALID_RANGE = (100.0, 5000.0)

_TIMEOUT = 30

# Like imf_debt_gdp: the IMF host sits behind Akamai. Send NO custom browser
# User-Agent (a spoofed Chrome UA gets a 403) — the default python-requests UA
# is accepted (verified HTTP 200 from this Mac, 2026-05-31).


class FetchError(Exception):
    pass


def _build_url(member_key: int = IMF_MEMBER_KEY, *, on: date | None = None) -> str:
    datekey = (on or date.today()).isoformat()
    return IMF_URL_TEMPLATE.format(member=member_key, datekey=datekey)


def fetch_imf_position_html(
    *, url: str | None = None, session: requests.Session | None = None
) -> str:
    """GET the IMF Financial-Position HTML report. Raises FetchError on failure."""
    sess = session or requests.Session()
    target = url or _build_url()
    try:
        resp = sess.get(target, timeout=_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise FetchError(f"network error fetching IMF position: {e}") from e
    if resp.status_code != 200:
        raise FetchError(f"IMF position page returned HTTP {resp.status_code}")
    return resp.text


def _flatten(html_text: str) -> str:
    """Strip tags, unescape entities, collapse whitespace into one line."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_eff_outstanding(
    html_text: str,
    *,
    label: str = EFF_LABEL,
    valid_range: tuple[float, float] = VALID_RANGE,
) -> tuple[float, date | None]:
    """Extract the EFF outstanding (SDR mn) + the page's reporting date.

    Pure (no I/O) so it unit-tests against a captured HTML fixture with no egress.
    Header-LABEL matching (landmine E): finds the named facility row by its label,
    NOT a fixed column/offset. Returns (value, as_of) where as_of is the date
    parsed from the page title or None if absent. Raises FetchError when the
    facility row is missing or the captured value is out of range.
    """
    flat = _flatten(html_text)

    # The facility line shape: "<label> <SDR-million> <%-quota>", e.g.
    # "Extended Arrangements 1,373.26 128.75". Capture the FIRST numeric token
    # after the label (the SDR-million column).
    pattern = re.escape(label) + r"\s+([\d,]+(?:\.\d+)?)"
    m = re.search(pattern, flat)
    if not m:
        raise FetchError(f"facility row {label!r} not found in IMF position page")
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError as e:
        raise FetchError(f"could not parse {label!r} value {m.group(1)!r}: {e}") from e

    lo, hi = valid_range
    if not (lo <= value <= hi):
        raise FetchError(
            f"{label} outstanding {value} SDR mn out of range [{lo}, {hi}]"
        )

    return value, _parse_as_of(html_text)


def _parse_as_of(html_text: str) -> date | None:
    """Pull the reporting date from the page title 'Financial Position ... as of <Month DD, YYYY>'."""
    m = re.search(r"as of ([A-Z][a-z]+ \d{1,2}, \d{4})", html_text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").date()
    except ValueError:
        return None


def upsert_eff(value: float, as_of: date) -> int:
    """Upsert the EFF outstanding as one metric_history row stamped at as_of."""
    return upsert_metric_history(
        data={METRIC_ID: value},
        as_of=as_of,
        source="IMF Financial Position in the Fund",
        source_as_of_map={METRIC_ID: as_of},
        url=_build_url(),
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        html_text = fetch_imf_position_html()
        value, page_as_of = parse_eff_outstanding(html_text)
    except FetchError as e:
        logger.exception("IMF EFF fetch/parse failed")
        notify("error", "imf_eff fetch failed", str(e))
        return 1

    as_of = page_as_of or date.today()
    logger.info(
        "parsed Bangladesh EFF outstanding = %.2f SDR mn (as of %s)", value, as_of
    )

    written = upsert_eff(value, as_of)
    logger.info("upserted %d %s row into metric_history", written, METRIC_ID)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run

    sys.exit(wrap_run("imf_eff", "econdelta-imf-eff.service", main))
