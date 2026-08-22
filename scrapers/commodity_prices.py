"""Commodity prices scraper — yfinance, anomaly-gated, schema-validated."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yfinance as yf

from utils.anomaly import check_threshold, load_thresholds
from utils.notifier import notify
from utils.schema import CommodityPrice, CommoditySnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "commodity_prices"
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
THRESHOLDS_PATH = REPO_ROOT / "config" / "thresholds.json"

logger = logging.getLogger("commodity_prices")

# Maps internal snapshot key -> (ticker, currency, unit, threshold_key)
COMMODITY_SPEC: dict[str, tuple[str, str, str, str]] = {
    "brent_crude": ("BZ=F", "USD", "barrel", "brent_crude"),
    "wti_crude":   ("CL=F", "USD", "barrel", "wti_crude"),
    "gold":        ("GC=F", "USD", "oz",     "gold"),
}


class FetchError(Exception):
    pass


def _fetch_history(t: yf.Ticker):
    """Best-effort ``history(period="5d")`` fetch; None on any failure.

    Isolated so both the date-recovery step and the fast_info-failure
    fallback below can share ONE history() call instead of two. Returns a
    pandas DataFrame (or None) -- pandas isn't imported here for a type hint
    alone since it's only ever passed through, never constructed.
    """
    try:
        return t.history(period="5d", auto_adjust=False)
    except Exception:  # noqa: BLE001 -- any yfinance/network failure, never fatal here
        return None


def _quote_date_from_history(hist) -> date | None:
    """The latest trading day yfinance's own history() reports for this ticker.

    ``fast_info.last_price`` carries NO associated timestamp (confirmed against
    yfinance's own source: FastInfo has no last-trade-date property) -- the
    DatetimeIndex on history() is the only place yfinance exposes a real
    per-quote date. Returns None (never date.today()) when history is
    unavailable, empty, or its index isn't date-like for any reason -- this
    is pure date-RECOVERY, so a failure here must never bubble up and break
    the price extraction it runs alongside.
    """
    if hist is None or hist.empty:
        return None
    try:
        return hist.index[-1].date()
    except (AttributeError, TypeError, IndexError):
        return None


def fetch_commodity(ticker: str) -> tuple[float, float | None, date | None]:
    """Return (latest_price, prev_close, quote_date) from yfinance.

    Tries yf.Ticker(ticker).fast_info for a cheap price lookup first, falling
    back to yf.Ticker(ticker).history(period="5d") if fast_info is missing
    fields. ``quote_date`` is always sourced from history() (see
    _quote_date_from_history) regardless of which path supplied the price,
    since fast_info itself never carries a date. Returns prev_close=None /
    quote_date=None if unavailable. Raises FetchError if latest price cannot
    be determined by EITHER path.
    """
    t = yf.Ticker(ticker)
    hist = _fetch_history(t)
    quote_date = _quote_date_from_history(hist)

    try:
        fi = t.fast_info
        # fast_info exposes attributes; also supports dict-style access on some versions.
        # Try dict-style first, then attribute access.
        if "last_price" in fi:
            last = float(fi["last_price"])
        elif hasattr(fi, "last_price") and fi.last_price is not None:
            last = float(fi.last_price)
        else:
            raise KeyError("last_price not found in fast_info")

        prev: float | None = None
        if "previous_close" in fi:
            raw_prev = fi["previous_close"]
        elif hasattr(fi, "previous_close"):
            raw_prev = fi.previous_close
        else:
            raw_prev = None

        if raw_prev is not None:
            prev = float(raw_prev)

        return last, prev, quote_date

    except (KeyError, TypeError, ValueError, AttributeError):
        # Fallback to history (reuse the same fetch — do not call history() again)
        if hist is None or hist.empty:
            raise FetchError(f"no data returned for {ticker}")
        close_col = hist["Close"]
        last = float(close_col.iloc[-1])
        prev = float(close_col.iloc[-2]) if len(close_col) >= 2 else None
        return last, prev, quote_date


def load_previous_snapshot(today: date) -> CommoditySnapshot | None:
    """Return the most recent snapshot file strictly older than today, or None."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    candidates = sorted(DATA_DIR.glob("*.json"))
    today_str = today.isoformat()
    for path in reversed(candidates):
        # Skip temp files
        if path.suffix != ".json" or path.stem.startswith("."):
            continue
        if path.stem < today_str:
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw)
                return CommoditySnapshot.model_validate(data)
            except Exception as exc:
                logger.warning("Could not parse previous snapshot %s: %s", path, exc)
    return None


def write_snapshot(snapshot: CommoditySnapshot) -> Path:
    """Write snapshot to DATA_DIR/<date>.json atomically via .tmp + os.replace."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / f"{snapshot.date.isoformat()}.json"
    tmp = dest.with_suffix(".json.tmp")
    payload = snapshot.model_dump(mode="json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    thresholds = load_thresholds(THRESHOLDS_PATH)

    prices: dict[str, CommodityPrice] = {}
    errors: list[str] = []
    quote_dates: list[date] = []

    for key, (ticker, currency, unit, _threshold_key) in COMMODITY_SPEC.items():
        try:
            last, prev, quote_date = fetch_commodity(ticker)
            change_pct = ((last - prev) / prev) if (prev is not None and prev != 0) else None
            prices[key] = CommodityPrice(
                price=last,
                prev_close=prev,
                change_pct=change_pct,
                currency=currency,
                unit=unit,
            )
            if quote_date is not None:
                quote_dates.append(quote_date)
            logger.info(
                "%s (%s): %.4f %s (prev=%.4f, chg=%.2f%%, quote_date=%s)",
                key,
                ticker,
                last,
                currency,
                prev if prev is not None else float("nan"),
                (change_pct * 100) if change_pct is not None else float("nan"),
                quote_date.isoformat() if quote_date is not None else "unavailable",
            )
        except Exception as exc:
            logger.warning("fetch failed for %s (%s): %s", key, ticker, exc)
            errors.append(f"{key}={ticker}: {exc}")

    if not prices:
        notify("error", "commodity_prices all tickers failed", "\n".join(errors))
        return 1

    # Snapshot date = the QUOTE's own trading date (yfinance history()'s latest
    # DatetimeIndex entry), not the run date -- brent/WTI/gold trade nearly
    # 24/5, so their quote dates virtually always agree; take the latest when
    # they don't (a genuinely live market beats a stale one). Falls back to
    # date.today() ONLY when every ticker's history() call failed this run --
    # the source genuinely offered no date this time, so the run date is the
    # best available label, same as the pre-fix behaviour.
    snapshot_date = max(quote_dates) if quote_dates else date.today()

    # Anomaly check vs previous snapshot
    prev_snap = load_previous_snapshot(snapshot_date)
    if prev_snap is not None:
        anomalies: list[str] = []
        for key, cp in prices.items():
            prev_entry = prev_snap.prices.get(key)
            prev_price = prev_entry.price if prev_entry is not None else None
            ok, pct = check_threshold(key, cp.price, prev_price, thresholds)
            if not ok:
                anomalies.append(f"{key}: {prev_price} -> {cp.price} ({pct:.2%})")
        if anomalies:
            notify("warning", "commodity anomaly — write skipped", "\n".join(anomalies))
            return 2

    snapshot = CommoditySnapshot(
        schema_version="1.0",
        date=snapshot_date,
        scraped_at=datetime.now(timezone.utc),
        prices=prices,
        provider="yfinance",
    )
    path = write_snapshot(snapshot)
    logger.info(
        "wrote %s (%d commodities, %d errors)",
        path,
        len(prices),
        len(errors),
    )
    if errors:
        notify("warning", "commodity_prices partial fetch", "\n".join(errors))
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("commodity_prices", "econdelta-commodity.service", main))
