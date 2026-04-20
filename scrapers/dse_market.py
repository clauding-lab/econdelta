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
from utils.parser import parse_number
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
    _slugify = lambda s: re.sub(r"\s+", "", s.lower())

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


def parse_market_stats(html: str) -> DseMarket:
    """Extract turnover/trades/advancing/declining/unchanged from market-statistics.php.

    The data is inside a <code> element nested in a table. Contents are preformatted
    plaintext under the heading "TOTAL TRANSACTIONS" and "All Category".
    Turnover is in Taka — divide by _TAKA_PER_CRORE (10M) to get crore.
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

    text = code_block.get_text("\n")

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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    today = date.today()
    holidays = load_holidays(HOLIDAYS_PATH)

    if not is_bd_trading_day(today, holidays):
        logger.info("non-trading day %s; skipping", today.isoformat())
        snapshot = DseSnapshot(
            schema_version="1.0",
            date=today,
            scraped_at=datetime.now(timezone.utc),
            trading_day=False,
            indices=None,
            market=None,
            source_url="https://www.dse.com.bd/",
        )
        write_snapshot(snapshot)
        return 0

    with CONFIG_PATH.open() as f:
        sources = json.load(f)["sources"]
    summary_url: str = sources["dse_market_summary"]["url"]
    homepage_url = "https://www.dse.com.bd/"

    thresholds = load_thresholds(THRESHOLDS_PATH)

    try:
        home_html = DEFAULT_CLIENT.fetch_html(homepage_url)
        indices = parse_homepage_indices(home_html)
        logger.info(
            "Parsed indices: DSEX=%.5f DS30=%.5f DSES=%.5f",
            indices.dsex,
            indices.ds30 or 0,
            indices.dses or 0,
        )

        stats_html = DEFAULT_CLIENT.fetch_html(summary_url)
        market = parse_market_stats(stats_html)
        logger.info(
            "Parsed market: trades=%d turnover=%.4f crore adv=%d dec=%d unc=%d",
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

    # Anomaly check vs previous trading day
    prev = load_previous_snapshot_for(today, holidays)
    if prev is not None and prev.indices is not None:
        for metric, new_val, old_val in [
            ("dsex", indices.dsex, prev.indices.dsex),
            ("ds30", indices.ds30, prev.indices.ds30),
            ("dses", indices.dses, prev.indices.dses),
        ]:
            if old_val is None or new_val is None:
                continue
            ok, pct = check_threshold(metric, new_val, old_val, thresholds)
            if not ok:
                notify(
                    "warning",
                    "dse_market anomaly — write skipped",
                    f"{metric}: {old_val} → {new_val} ({pct:.2%} exceeds threshold)",
                )
                return 2

    snapshot = DseSnapshot(
        schema_version="1.0",
        date=today,
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
    sys.exit(main())
