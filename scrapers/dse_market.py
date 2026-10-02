"""DSE daily market scraper — requests-based, session-dated, anomaly-gated.

Source: the JSON feed behind the new www.dse.com.bd site (Next.js, launched
~24-28 Sep 2026). The old PHP pages this scraper used to read
(``market-statistics.php`` + the homepage index widget) now answer HTTP 410
Gone. ``/api/live/market`` carries the indices, totals, breadth AND the
session's own date in one response, so one fetch replaces the old two.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

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

# /api/live/market reports `totals.turnover` in Tk MILLION (the site's own
# "At a glance" panel labels the same number "Total Turnover in Tk. mn").
# 1 crore = 10 million, so crore = mn / 10.
_TK_MN_PER_CRORE = 10


class ParseError(Exception):
    pass


def load_live_market(text: str) -> dict:
    """Decode the /api/live/market body; anything but a JSON object is a ParseError."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ParseError(f"/api/live/market did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParseError(f"/api/live/market returned {type(payload).__name__}, not an object")
    return payload


def parse_session_date(payload: dict) -> date:
    """Return the trading SESSION the payload's numbers belong to.

    ``session.sessionDate`` is the source's own date for the last session.
    DSE runs weekend makeup sessions (AGENT_LEARNINGS.md 2026-08-08) and the
    timer fires at 01:21 BDT the NEXT calendar day, so the date must come from
    the source, never from the run clock. ``session.date`` is merely "today in
    Dhaka" (the calendar/session context; it can be a day later on weekends
    and holidays) and is deliberately ignored.

    Refuses (ParseError) to date a payload while the market is not closed:
    pre-open / open / halted / post-close values are intraday or provisional
    and must never be written as a session's close.

    Raises:
        ParseError: missing/invalid sessionDate, a non-closed session, a
            non-object ``totals`` or non-list ``dailyTotals``, or a
            ``dailyTotals`` row for the same date whose trade count disagrees
            with the headline totals. NEVER falls back to date.today().
    """
    if not isinstance(payload, dict):
        raise ParseError("/api/live/market response must be an object")
    session = payload.get("session")
    if not isinstance(session, dict):
        raise ParseError("/api/live/market has no `session` object (no sessionDate)")
    # The date is validated before the open/closed gate so a payload with no
    # sessionDate always names that defect, whatever its phase fields say.
    raw = session.get("sessionDate")
    if not isinstance(raw, str):
        raise ParseError("/api/live/market session has no `sessionDate` string")
    try:
        session_date = date.fromisoformat(raw)
    except ValueError as exc:
        raise ParseError(f"sessionDate {raw!r} is not a valid ISO date") from exc
    if session.get("isOpen") is not False or session.get("phase") != "closed":
        raise ParseError(
            "DSE session not closed "
            f"(isOpen={session.get('isOpen')!r}, phase={session.get('phase')!r}); "
            "refusing to record intraday values"
        )

    # Cross-check: the payload also carries a per-day `dailyTotals` history.
    # If it has a row for this sessionDate, the headline totals must match it
    # -- otherwise the totals block and the date disagree and we cannot know
    # which day the numbers belong to. This runs before parse_market's shape
    # checks, so a badly shaped `totals` / `dailyTotals` must be refused here
    # as a ParseError (the handled notify-and-exit-1 path), never left to
    # surface as an AttributeError/TypeError that skips notify().
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        raise ParseError("/api/live/market `totals` must be an object")
    history = payload.get("dailyTotals")
    if history is None:
        history = []
    if not isinstance(history, list):
        raise ParseError(
            f"/api/live/market `dailyTotals` must be a list, not {type(history).__name__}"
        )
    for row in history:
        if isinstance(row, dict) and row.get("date") == raw:
            # Compare PARSED counts, not raw JSON: _count accepts a digit
            # string, so "202860" next to 202860 is agreement, not a clash.
            headline = _count(totals, "trades", "totals")
            recorded = _count(row, "trades", f"dailyTotals[{raw}]")
            if headline != recorded:
                raise ParseError(
                    f"totals.trades={totals.get('trades')!r} disagrees with "
                    f"dailyTotals[{raw}].trades={row.get('trades')!r}"
                )
            break
    else:
        logger.warning("dailyTotals has no row for sessionDate %s; cannot cross-check", raw)
    return session_date


def _number(obj: dict, key: str, where: str) -> float:
    """A finite source number; booleans, non-numbers, NaN and infinities are refused."""
    val = obj.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        raise ParseError(f"{where}.{key} missing or not numeric: {val!r}")
    try:
        parsed = float(val)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ParseError(f"{where}.{key} missing or not numeric: {val!r}") from exc
    if not math.isfinite(parsed):
        raise ParseError(f"{where}.{key} must be finite: {val!r}")
    return parsed


def _count(obj: dict, key: str, where: str) -> int:
    """A non-negative whole count; negatives, fractions and booleans are refused.

    ``int(202860.5)`` would silently truncate, so fractions are rejected here
    rather than coerced.
    """
    val = obj.get(key)
    if isinstance(val, bool):
        raise ParseError(f"{where}.{key} must be a non-negative integer: {val!r}")
    if isinstance(val, int):
        count = val
    elif isinstance(val, str) and re.fullmatch(r"\s*\d+\s*", val):
        count = int(val)
    else:
        raise ParseError(f"{where}.{key} must be a non-negative integer: {val!r}")
    if count < 0:
        raise ParseError(f"{where}.{key} must be a non-negative integer: {val!r}")
    return count


def _index_level(row: dict, name: str) -> float:
    level = _number(row, "value", name)
    if level <= 0:
        raise ParseError(f"{name} index level must be positive and finite: {level!r}")
    return level


def parse_indices(payload: dict) -> DseIndices:
    """Map ``indices[{key, value, change, percent}]`` onto DseIndices.

    ``percent`` is already a percentage (-0.33855 means -0.34%), the same unit
    the old homepage widget showed. DSEX is required; DS30/DSES are None when
    absent (never fabricated) but, when present, must be positive and finite.
    """
    rows = payload.get("indices")
    if not isinstance(rows, list):
        raise ParseError("/api/live/market has no `indices` list")
    by_key: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise ParseError("each index row must be an object with a string key")
        by_key[row["key"].upper()] = row
    dsex = by_key.get("DSEX")
    if dsex is None:
        raise ParseError(f"DSEX missing from indices (keys: {sorted(by_key)})")
    ds30 = by_key.get("DS30")
    dses = by_key.get("DSES")
    return DseIndices(
        dsex=_index_level(dsex, "DSEX"),
        dsex_change=_number(dsex, "change", "DSEX"),
        dsex_change_pct=_number(dsex, "percent", "DSEX"),
        ds30=_index_level(ds30, "DS30") if ds30 is not None else None,
        dses=_index_level(dses, "DSES") if dses is not None else None,
    )


def parse_market(payload: dict) -> DseMarket:
    """Map ``totals`` + ``breadth`` onto DseMarket (turnover Tk mn -> crore)."""
    totals = payload.get("totals")
    breadth = payload.get("breadth")
    if not isinstance(totals, dict) or not isinstance(breadth, dict):
        raise ParseError("/api/live/market is missing `totals` or `breadth`")
    turnover_mn = _number(totals, "turnover", "totals")
    if turnover_mn <= 0:
        raise ParseError(f"totals.turnover must be a positive finite number: {turnover_mn!r}")
    return DseMarket(
        turnover_crore=round(turnover_mn / _TK_MN_PER_CRORE, 4),
        total_trades=_count(totals, "trades", "totals"),
        advancing=_count(breadth, "advanced", "breadth"),
        declining=_count(breadth, "declined", "breadth"),
        unchanged=_count(breadth, "unchanged", "breadth"),
    )


def parse_live_market_payload(payload: dict) -> tuple[date, DseIndices, DseMarket]:
    """Parse an already-decoded /api/live/market object into (session_date, indices, market).

    Every REQUIRED part is validated before anything is returned: sessionDate
    and the closed-session gate, DSEX (level, change, percent), totals
    (turnover, trades) and breadth. An invalid required part raises ParseError
    and no snapshot is written (a corrected payload on the next run still
    ingests). DS30 and DSES are OPTIONAL by design (#139, kept at the
    2026-10-02 merge): when absent they come back as None and the snapshot
    still lands with those two levels missing -- never fabricated. When
    present they must be positive and finite like DSEX.
    """
    if not isinstance(payload, dict):
        raise ParseError("/api/live/market response must be an object")
    return parse_session_date(payload), parse_indices(payload), parse_market(payload)


def parse_live_market(text: str) -> tuple[date, DseIndices, DseMarket]:
    """Parse one /api/live/market body into (session_date, indices, market)."""
    return parse_live_market_payload(load_live_market(text))


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

    thresholds = load_thresholds(THRESHOLDS_PATH)

    # One fetch carries the session's own date plus every number we store.
    # The skip/no-op decision happens AFTER a successful parse, never before.
    try:
        trading_date, indices, market = parse_live_market(DEFAULT_CLIENT.fetch_html(summary_url))
        logger.info(
            "Parsed market: date=%s trades=%d turnover=%.4f crore adv=%d dec=%d unc=%d",
            trading_date.isoformat(),
            market.total_trades,
            market.turnover_crore,
            market.advancing,
            market.declining,
            market.unchanged,
        )
        logger.info(
            "Parsed indices: DSEX=%.5f DS30=%.5f DSES=%.5f",
            indices.dsex,
            indices.ds30 or 0,
            indices.dses or 0,
        )
    except (FetchError, ParseError, json.JSONDecodeError) as e:
        logger.exception("fetch/parse failed")
        notify("error", "dse_market fetch failed", f"{type(e).__name__}: {e}")
        return 1

    # Idempotency gate, evaluated on the PARSED session date, never the run
    # date. This session may already be on disk -- a re-run later the same
    # day, or DSE re-serving the last real session on a weekend/holiday when
    # nothing new traded (the feed always reports the latest actual session,
    # so a closed day naturally parses to an already-seen date). Either way
    # there is nothing new to write; no-op cleanly.
    if _already_ingested(trading_date):
        logger.info("session %s already ingested; no-op", trading_date.isoformat())
        return 0

    # Observability only -- it never blocks the write. config/holidays_2026.json's
    # Sun-Thu default is a DEFAULT, not a hard rule: DSE runs makeup sessions on
    # weekends around Eid (AGENT_LEARNINGS.md 2026-08-08), and a moon-sighting
    # holiday can also simply be missing from the calendar file. Trusting the
    # calendar over the source here would silently drop a genuine trading day.
    if not is_bd_trading_day(trading_date, holidays):
        logger.warning(
            "parsed trading date %s falls on a day config/holidays_2026.json "
            "treats as non-trading (weekend/uncalendared holiday), but DSE "
            "just reported a new session for it -- writing anyway",
            trading_date.isoformat(),
        )

    # Anomaly check vs previous trading day. MEDIUM-2 (2026-08-22 round-1
    # review): the threshold was calibrated for a ONE-trading-day move. Past a
    # 3-calendar-day baseline gap (e.g. a 7-day Eid closure), downgrade a
    # threshold breach from a write-block to a write+warning: the number still
    # lands, flagged for a human to sanity-check, instead of vanishing.
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
