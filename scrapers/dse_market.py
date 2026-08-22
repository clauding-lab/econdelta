"""DSE daily market scraper — requests-based, trading-day-gated, anomaly-gated."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from utils.anomaly import check_threshold, load_thresholds
from utils.calendar import is_bd_trading_day, load_holidays, previous_trading_day
from utils.http_client import DEFAULT_CLIENT, HttpClient
from utils.notifier import notify
from utils.schema import DseIndices, DseMarket, DseSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "dse_market"
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.json"
HOLIDAYS_PATH = REPO_ROOT / "config" / "holidays_2026.json"

# FetchError lives as a nested class on HttpClient
FetchError = HttpClient.FetchError

logger = logging.getLogger("dse_market")

_TAKA_PER_CRORE = 10_000_000


class ParseError(Exception):
    pass


def _parse_float(text: str) -> float:
    """Strip whitespace and parse a float from a string."""
    cleaned = text.strip().rstrip("%").replace(",", "")
    return float(cleaned)


def parse_homepage_indices(html: str) -> DseIndices:
    """Extract DSEX, DS30, DSES from homepage inline text widget.

    The homepage shows index values in a summary strip inside div.LeftColHome.
    Three consecutive div.midrow elements hold DSEX, DSES, and DS30 in order.
    Each midrow has: m_col-1 (label), m_col-2 (value), m_col-3 (change), m_col-4 (pct).
    """
    soup = BeautifulSoup(html, "html.parser")

    left_col = soup.find("div", class_="LeftColHome")
    if left_col is None:
        raise ParseError("LeftColHome div not found on DSE homepage")

    midrows = left_col.find_all("div", class_="midrow")
    if len(midrows) < 3:
        raise ParseError(
            f"Expected at least 3 midrow divs in LeftColHome, found {len(midrows)}"
        )

    def extract_row(midrow) -> tuple[str, float, float, float]:
        """Return (label_lower, value, change, change_pct) from a midrow div."""
        label_el = midrow.find("div", class_="m_col-1")
        value_el = midrow.find("div", class_="m_col-2")
        change_el = midrow.find("div", class_="m_col-3")
        pct_el = midrow.find("div", class_="m_col-4")

        if not (label_el and value_el and change_el and pct_el):
            raise ParseError(f"Missing m_col elements in midrow: {midrow}")

        label = label_el.get_text(" ", strip=True).lower()
        value = _parse_float(value_el.get_text())
        change = _parse_float(change_el.get_text())
        pct = _parse_float(pct_el.get_text())
        return label, value, change, pct

    # Rows 0, 1, 2 are DSEX, DSES, DS30 respectively.
    # Label text (stripped, no separator): "DSEXIndex", "DSESIndex", "DS30 Index"
    # The <font> tag inside m_col-1 merges the split "X"/"S" character without spacing.
    label0, val0, chg0, pct0 = extract_row(midrows[0])
    label1, val1, chg1, pct1 = extract_row(midrows[1])
    label2, val2, chg2, pct2 = extract_row(midrows[2])

    # Match by canonical slug (case-insensitive, whitespace-collapsed)
    def _slugify(s):
        return re.sub(r"\s+", "", s.lower())

    dsex_val = dsex_chg = dsex_pct = None
    dses_val = None
    ds30_val = None

    for label, val, chg, pct in [
        (label0, val0, chg0, pct0),
        (label1, val1, chg1, pct1),
        (label2, val2, chg2, pct2),
    ]:
        slug = _slugify(label)
        if "dsex" in slug:
            if dsex_val is None:
                dsex_val, dsex_chg, dsex_pct = val, chg, pct
        elif "dses" in slug or ("dse" in slug and "s" in slug and "30" not in slug):
            if dses_val is None:
                dses_val = val
        elif "30" in slug or "ds30" in slug:
            if ds30_val is None:
                ds30_val = val

    # Positional fallback if label matching failed
    if dsex_val is None:
        dsex_val, dsex_chg, dsex_pct = val0, chg0, pct0
    if dses_val is None:
        dses_val = val1
    if ds30_val is None:
        ds30_val = val2

    return DseIndices(
        dsex=dsex_val,
        dsex_change=dsex_chg,
        dsex_change_pct=dsex_pct,
        ds30=ds30_val,
        dses=dses_val,
    )


def _extract_code_block_text(html: str) -> str:
    """Return the plaintext of the <code> block on market-statistics.php.

    Shared by parse_market_stats and parse_trading_date -- both read different
    lines out of the SAME preformatted block, so a bad/missing selector only
    needs fixing in one place.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try progressively looser selectors
    code_block = (
        soup.select_one("table > tbody > tr > td > code")
        or soup.select_one("table code")
        or soup.select_one("code")
    )
    if code_block is None:
        raise ParseError("no <code> block found on market-statistics.php")

    return code_block.get_text("\n")


# DSE stamps every market-statistics.php snapshot with its own session date:
# "                  TODAY'S SHARE MARKET : 2026-04-20". This is the SOURCE's
# own trading date -- extracting it here (rather than trusting date.today())
# is the whole point of this fix. See parse_trading_date's docstring.
_TRADING_DATE_RE = re.compile(
    r"TODAY[’']S\s+SHARE\s+MARKET\s*:\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE
)


def parse_trading_date(text: str) -> date:
    """Extract the page's own trading date from market-statistics.php's code block.

    The systemd timer fires at 19:21 UTC (01:21 BDT the NEXT calendar day) to
    capture that day's already-closed session, so ``date.today()`` at run time
    is reliably one day ahead of the session the page describes -- every
    dse_market snapshot was stamped with the wrong day until this fix (see
    AGENTS.md landmine 33 / AGENT_LEARNINGS.md 2026-08-08 "one trading day
    late"). This function reads the page's own "TODAY'S SHARE MARKET : YYYY-
    MM-DD" line instead, so the snapshot is stamped with the SESSION's real
    date regardless of what the run clock or run-date/BDT-offset math says.

    Args:
        text: the plaintext of the <code> block (see _extract_code_block_text).

    Raises:
        ParseError: if the label is missing or its value isn't a valid ISO
            date. NEVER falls back to date.today() -- a silent run-date
            fallback is exactly the bug this function exists to prevent from
            reappearing.
    """
    m = _TRADING_DATE_RE.search(text)
    if m is None:
        raise ParseError(
            "could not find \"TODAY'S SHARE MARKET\" date in market-statistics code block"
        )
    raw = m.group(1)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ParseError(f"TODAY'S SHARE MARKET date {raw!r} is not a valid ISO date") from exc


def parse_market_stats(html: str) -> DseMarket:
    """Extract turnover/trades/advancing/declining/unchanged from market-statistics.php.

    The data is inside a <code> element nested in a table. Contents are preformatted
    plaintext under the heading "TOTAL TRANSACTIONS" and "All Category".
    Turnover is in Taka — divide by _TAKA_PER_CRORE (10M) to get crore.
    """
    text = _extract_code_block_text(html)

    # --- Trades: "A. NO. OF TRADES : 223903" ---
    trades_m = re.search(r"NO\.\s+OF\s+TRADES\s*:\s*([\d,]+)", text)
    if trades_m is None:
        raise ParseError("could not parse NO. OF TRADES from market-statistics code block")
    total_trades = int(trades_m.group(1).replace(",", ""))

    # --- Turnover: "C. VALUE(Tk) : 8247602308.40" ---
    turnover_m = re.search(r"VALUE\s*\(Tk\)\s*:\s*([\d,\.]+)", text)
    if turnover_m is None:
        raise ParseError("could not parse VALUE(Tk) from market-statistics code block")
    turnover_taka = float(turnover_m.group(1).replace(",", ""))
    turnover_crore = turnover_taka / _TAKA_PER_CRORE

    # --- Advancing/Declining/Unchanged from "All Category" block ---
    # Use the FIRST occurrence of each label (= All Category aggregate)
    adv_m = re.search(r"ISSUES\s+ADVANCED\s*:\s*([\d,]+)", text)
    dec_m = re.search(r"ISSUES\s+DECLINED\s*:\s*([\d,]+)", text)
    unc_m = re.search(r"ISSUES\s+UNCHANGED\s*:\s*([\d,]+)", text)

    if adv_m is None or dec_m is None or unc_m is None:
        raise ParseError("could not parse advancing/declining/unchanged")

    advancing = int(adv_m.group(1).replace(",", ""))
    declining = int(dec_m.group(1).replace(",", ""))
    unchanged = int(unc_m.group(1).replace(",", ""))

    return DseMarket(
        turnover_crore=round(turnover_crore, 4),
        total_trades=total_trades,
        advancing=advancing,
        declining=declining,
        unchanged=unchanged,
    )


def load_previous_snapshot_for(d: date, holidays: set[date]) -> DseSnapshot | None:
    """Find the most recent snapshot file for the previous trading day before d."""
    if not DATA_DIR.exists():
        return None

    prev_day = previous_trading_day(d, holidays)
    snapshot_path = DATA_DIR / f"{prev_day.isoformat()}.json"

    if not snapshot_path.exists():
        logger.info("No previous snapshot found at %s", snapshot_path)
        return None

    try:
        with snapshot_path.open() as fh:
            raw = json.load(fh)
        return DseSnapshot.model_validate(raw)
    except Exception as exc:
        logger.warning("Could not load previous snapshot %s: %s", snapshot_path, exc)
        return None


def write_snapshot(snapshot: DseSnapshot) -> Path:
    """Atomic write: write to .tmp then os.replace."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / f"{snapshot.date.isoformat()}.json"
    tmp = target.with_suffix(".tmp")

    payload = snapshot.model_dump(mode="json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _already_ingested(trading_date: date) -> bool:
    """True if a snapshot for this trading date is already on disk."""
    return (DATA_DIR / f"{trading_date.isoformat()}.json").exists()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    holidays = load_holidays(HOLIDAYS_PATH)

    with CONFIG_PATH.open() as f:
        sources = json.load(f)["sources"]
    summary_url: str = sources["dse_market_summary"]["url"]
    homepage_url = "https://www.dse.com.bd/"

    thresholds = load_thresholds(THRESHOLDS_PATH)

    # Fetch + parse market-statistics FIRST -- it carries the page's own
    # trading date, which is what the gate below evaluates against. There is
    # no cheap way to know the trading date without fetching, so (unlike the
    # old run-date pre-check this replaces) every invocation attempts the
    # fetch; the skip/no-op decision happens AFTER a successful parse, not
    # before it.
    try:
        stats_html = DEFAULT_CLIENT.fetch_html(summary_url)
        trading_date = parse_trading_date(_extract_code_block_text(stats_html))
        market = parse_market_stats(stats_html)
        logger.info(
            "Parsed market: date=%s trades=%d turnover=%.4f crore adv=%d dec=%d unc=%d",
            trading_date.isoformat(),
            market.total_trades,
            market.turnover_crore,
            market.advancing,
            market.declining,
            market.unchanged,
        )
    except (FetchError, ParseError) as e:
        logger.exception("fetch/parse failed")
        notify("error", "dse_market fetch failed", f"{type(e).__name__}: {e}")
        return 1

    # Idempotency gate, evaluated on the PARSED trading date, never the run
    # date. This session may already be on disk -- a re-run later the same
    # day, or DSE re-serving the last real session's page on a weekend/
    # holiday when nothing new traded (the site always reports the latest
    # actual session, so a closed day naturally parses to an already-seen
    # date). Either way there is nothing new to write; no-op cleanly.
    if _already_ingested(trading_date):
        logger.info("session %s already ingested; no-op", trading_date.isoformat())
        return 0

    # This is the ONLY other case the gate considers, and it is observability
    # only -- it never blocks the write. config/holidays_2026.json's Sun-Thu
    # default is a DEFAULT, not a hard rule: DSE runs makeup sessions on
    # weekends around Eid (AGENT_LEARNINGS.md 2026-08-08), and a moon-sighting
    # holiday can also simply be missing from the calendar file. Either way,
    # the source just reported a REAL, never-before-seen session on this
    # date -- trusting the calendar over the source here would silently drop
    # a genuine trading day, which is the exact failure this fix replaces.
    if not is_bd_trading_day(trading_date, holidays):
        logger.warning(
            "parsed trading date %s falls on a day config/holidays_2026.json "
            "treats as non-trading (weekend/uncalendared holiday), but DSE "
            "just reported a new session for it -- writing anyway",
            trading_date.isoformat(),
        )

    try:
        home_html = DEFAULT_CLIENT.fetch_html(homepage_url)
        indices = parse_homepage_indices(home_html)
        logger.info(
            "Parsed indices: DSEX=%.5f DS30=%.5f DSES=%.5f",
            indices.dsex,
            indices.ds30 or 0,
            indices.dses or 0,
        )
    except (FetchError, ParseError) as e:
        logger.exception("fetch/parse failed")
        notify("error", "dse_market fetch failed", f"{type(e).__name__}: {e}")
        return 1

    # Anomaly check vs previous trading day. MEDIUM-2 (2026-08-22 round-1
    # review): the threshold was calibrated for a ONE-trading-day move.
    # config/holidays_2026.json now carries the 7-day Eid-ul-Fitr and
    # Eid-ul-Adha closures (this PR's holiday-calendar completion), so
    # `load_previous_snapshot_for` can legitimately walk back a week or more
    # to find the last real session -- a week's worth of accumulated market
    # movement compressed into one same-day comparison is not the anomaly
    # this threshold exists to catch, and hard-blocking the write would
    # silently skip DSE data for the whole re-opening week. Past a 3-
    # calendar-day baseline gap, downgrade a threshold breach from a write-
    # block to a write+warning: the number still lands, flagged for a human
    # to sanity-check, instead of vanishing. A genuine DSE makeup weekend
    # session (e.g. Sat 23 May 2026) is unaffected either way -- item 1's
    # parsed-date design means the weekday/holiday calendar only ever
    # produces a WARNING there, never a gate; this downgrade is scoped
    # purely to the anomaly-threshold hard-block.
    _ANOMALY_BASELINE_GAP_GRACE_DAYS = 3
    prev = load_previous_snapshot_for(trading_date, holidays)
    if prev is not None and prev.indices is not None:
        baseline_gap_days = (trading_date - prev.date).days
        hard_block = baseline_gap_days <= _ANOMALY_BASELINE_GAP_GRACE_DAYS
        anomalies: list[str] = []
        for metric, new_val, old_val in [
            ("dsex", indices.dsex, prev.indices.dsex),
            ("ds30", indices.ds30, prev.indices.ds30),
            ("dses", indices.dses, prev.indices.dses),
        ]:
            if old_val is None or new_val is None:
                continue
            ok, pct = check_threshold(metric, new_val, old_val, thresholds)
            if not ok:
                detail = f"{metric}: {old_val} → {new_val} ({pct:.2%} exceeds threshold)"
                if hard_block:
                    notify("warning", "dse_market anomaly — write skipped", detail)
                    return 2
                anomalies.append(detail)
        if anomalies:
            notify(
                "warning",
                f"dse_market anomaly across a {baseline_gap_days}-day baseline gap — writing anyway",
                "\n".join(anomalies),
            )

    snapshot = DseSnapshot(
        schema_version="1.0",
        date=trading_date,
        scraped_at=datetime.now(timezone.utc),
        trading_day=True,
        indices=indices,
        market=market,
        source_url=summary_url,
    )
    path = write_snapshot(snapshot)
    logger.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("dse_market", "econdelta-dse.service", main))
