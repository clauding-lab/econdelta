"""Aggregates latest snapshot from each scraper into data/latest.json — the canonical
file The Brief reads. Atomic write, Pydantic-validated, with per-source status."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from utils.anomaly import check_corridor_coherence
from utils.calendar import last_trading_close, load_holidays
from utils.notifier import notify
from utils.opus_review import archive_latest, load_history, review_data
from utils.schema import (
    Alert,
    CommoditySnapshot,
    DseSnapshot,
    ForexReserves,
    ForexSnapshot,
    FreshnessByCadence,
    FreshnessSummary,
    LatestBundle,
    SourceStatus,
)
from utils.staleness import check_value_staleness, check_watchlist_staleness

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
ARCHIVE_DIR = DATA_DIR / "archive"
# Cross-run tracker for the stillness alarm (utils/staleness.py). Not a data
# artifact — losing it only costs the alarm its warm-up window.
STALENESS_STATE_PATH = DATA_DIR / "staleness_state.json"
# Separate cross-run tracker for the watchlist staleness gate
# (utils/staleness.check_watchlist_staleness) — a different shape from the
# stillness alarm's state file, so it gets its own file rather than sharing.
WATCHLIST_STALENESS_STATE_PATH = DATA_DIR / "watchlist_staleness_state.json"
CONFIG_PATH = REPO_ROOT / "config" / "sources.json"
SOURCES_V3_PATH = REPO_ROOT / "config" / "sources-v3.json"
HOLIDAYS_PATH = REPO_ROOT / "config" / "holidays_2026.json"

# Sources whose data only exists on DSE trading days (Sun–Thu). Their freshness is
# judged against the last *closed* trading session, not raw age — so a Fri/Sat/holiday
# gap is NOT flagged stale. Everything else uses the plain age threshold.
_TRADING_DAY_SOURCES = frozenset({"dse_market"})

# Deterministic parsers for which date recovery is NOT YET IMPLEMENTED (13
# registry indicators as of this writing) -- these parsers do not currently
# extract a publication date from the page they scrape, so a missing
# source_as_of from them is an EXPECTED gap, not a bug, regardless of the
# indicator's nominal registry cadence (a few of these back "weekly"/
# "monthly" entries too, e.g. treasury_bill_outstanding, bop_summary -- the
# underlying scrape is still same-day HTML). tbond_5y_yield/tbond_10y_yield/
# tbill_182d_yield/tbill_364d_yield/bill_bond_rates are NO LONGER in this
# category as of PR-C (build-brief item 3, AGENTS.md landmine 49): they
# left the sources-v3.json/html_table_row pipeline entirely and are now
# derived from auction_results by _derive_daily_yields_from_auctions, which
# supplies a genuine source_as_of (the real auction date) via its own
# dedicated merge into source_as_of_map in main(), not this allow-list.
# NOTE: several of the live pages behind these parsers DO print a
# recoverable date (treasury tables carry an "Issue date" column, the
# interbank repo page an "Auction date", BoP/current-account pages a period
# header, the call-money page a date cell in its page header) -- extracting
# it is follow-up work, not ruled out. `_build_source_as_of_map`'s undated-
# metric warning exempts this set so extending that warning to every cadence
# doesn't turn into daily noise for indicators nothing currently dates (see
# feedback_observability_allow_list_pattern.md: warn-on-X needs an allow-list
# of by-design non-X shapes first). Parsers NOT listed here (dam_ticker,
# html_footer_ticker, pdf_table_row, pdf_table_latest, pdf_component) DO
# already attempt source_as_of recovery, so a miss from them is real signal
# -- BUT NOT "every PDF parser": pdf_table_column_latest, pdf_mfr_row, and
# pdf_table_total emit no source_as_of at all and are NOT in this allow-list
# either (a genuine, currently-undocumented gap, not a by-design exemption --
# see AGENTS.md landmine 47). Being absent from this allow-list only means
# "the undated-metric warning fires for this strategy" -- it does NOT mean
# metric_history.as_of stops being stamped with the run date; that fallback
# in upsert_metric_history fires identically whether or not the strategy is
# listed here. Adding a strategy to this set silences the warning; it never
# fixes the underlying as_of forgery.
_NEVER_DATED_PARSE_STRATEGIES = frozenset({
    "html_table_row",
    "html_call_money",
    "dse_sector_heat",
})

STALE_THRESHOLD_HOURS = 24.0

STALE_THRESHOLDS_HOURS_BY_CADENCE: dict[str, float] = {
    "daily": 24.0,
    "weekly": 8 * 24.0,       # 192h
    "monthly": 35 * 24.0,     # 840h
    "quarterly": 100 * 24.0,  # 2400h
    "fy": 400 * 24.0,         # 9600h
}

# Cumulative-figure guard: a fiscal-year-to-date total can only rise within a FY.
CUMULATIVE_DROP_TOLERANCE = 0.05   # >5% same-FY drop ⇒ implausible
FISCAL_YEAR_START_MONTH = 7        # Bangladesh FY = July–June
# Months (by PUBLICATION date) in which a cumulative series may still be
# reporting the first figures of a newly-started fiscal year, i.e. a period in
# which a large drop against the prior FY's closing total is EXPECTED rather
# than a parse error. July–October: BB's Monthly Economic Trends publishes
# July's numbers in late August, and a slipped issue can push the first
# post-reset figure into September or October. Outside this window a cumulative
# series that falls is a genuine problem and gets no grace. See landmine 56.
FY_RESET_GRACE_MONTHS = frozenset({7, 8, 9, 10})
# Granular Opus reject: quarantine up to this many flagged fields; more ⇒ hard reject.
MAX_QUARANTINE_FIELDS = 5

logger = logging.getLogger("aggregate_latest")

# Derived reserve-utilisation ratios (S2). Computed at runtime from the
# already-scraped BB MEI scalars below — EconDelta has NO scraped maintenance-%
# cell, so these are minted in `_build_v3_blocks` and land in metric_history
# under their own ids. The exact statutory CRR/SLR bases are policy constants
# that shift, so each ratio is labelled by what it ACTUALLY divides (no
# hardcoded statutory rate): the held/excess balance expressed as a % of total
# system deposits, NOT the regulated maintenance ratio.
RESERVE_UTIL_DERIVED: dict[str, tuple[str, str]] = {
    # derived_id -> (numerator_id, denominator_id)
    "crr_utilisation_pct": ("deposits_held_with_bb_crr", "deposits_of_the_system"),
    "slr_utilisation_pct": ("excess_liquid_asset_total_minimum", "deposits_of_the_system"),
}


SCRAPER_SPEC = {
    # key -> (subdir, schema_class, sources.json key for URL lookup)
    "bb_forex": ("bb_forex", ForexSnapshot, "bb_exchange_rates"),
    "dse_market": ("dse_market", DseSnapshot, "dse_market_summary"),
    "commodity_prices": ("commodity_prices", CommoditySnapshot, None),
}


def find_latest_snapshot(subdir: Path) -> Path | None:
    """Return the newest JSON file in subdir (by filename lexicographic — dates sort correctly).

    Ignores .tmp files and any non-JSON files.
    """
    if not subdir.exists():
        return None
    candidates = sorted(
        (p for p in subdir.glob("*.json") if not p.name.endswith(".tmp.json")),
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_snapshot(path: Path, schema_class: type) -> Any:
    """Parse JSON file into Pydantic model. Returns None on error."""
    try:
        with path.open() as f:
            data = json.load(f)
        return schema_class.model_validate(data)
    except (json.JSONDecodeError, ValidationError, FileNotFoundError, OSError) as e:
        logger.warning("failed to load %s: %s", path, e)
        return None


def compute_status(
    snapshot: Any,
    url: str | None,
    now: datetime,
    *,
    key: str | None = None,
    holidays: set[date] | None = None,
) -> SourceStatus:
    """Derive SourceStatus from a loaded snapshot + current time.

    Trading-day-bound sources (``_TRADING_DAY_SOURCES``, i.e. DSE) are judged against
    the last *closed* DSE session, not raw age: DSE has no Fri/Sat/holiday data, so a
    weekend gap is fresh, not stale. All other sources use the plain age threshold.
    """
    if snapshot is None:
        return SourceStatus(
            status="missing",
            last_success=None,
            age_hours=None,
            url=url,
            error="no snapshot found or validation failed",
        )
    scraped_at = snapshot.scraped_at
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    age_hours = (now - scraped_at).total_seconds() / 3600.0
    if key in _TRADING_DAY_SOURCES:
        try:
            status = "ok" if scraped_at >= last_trading_close(now, holidays) else "stale"
        except RuntimeError:  # broken holiday data — fall back to raw age, never crash
            status = "ok" if age_hours <= STALE_THRESHOLD_HOURS else "stale"
    else:
        status = "ok" if age_hours <= STALE_THRESHOLD_HOURS else "stale"
    return SourceStatus(
        status=status,
        last_success=scraped_at,
        age_hours=round(age_hours, 2),
        url=url,
        error=None,
    )


def flatten_data(snapshots: dict[str, Any]) -> dict[str, Any]:
    """Flatten the three snapshots into a single dict for The Brief to consume."""
    data: dict[str, Any] = {}

    forex = snapshots.get("bb_forex")
    if forex is not None:
        data["usd_bdt_mid"] = forex.rates.usd_bdt_mid
        data["usd_bdt_buy"] = forex.rates.usd_bdt_buy
        data["usd_bdt_sell"] = forex.rates.usd_bdt_sell
        data["eur_bdt"] = forex.rates.eur_bdt
        data["gbp_bdt"] = forex.rates.gbp_bdt
        if forex.reserves is not None:
            data["gross_reserves_usd_bn"] = forex.reserves.gross_reserves_usd_bn
            data["import_cover_months"] = forex.reserves.import_cover_months
            data["reserves_date"] = forex.reserves.reserves_date.isoformat()

    dse = snapshots.get("dse_market")
    if dse is not None:
        data["trading_day"] = dse.trading_day
        if dse.indices is not None:
            data["dsex"] = dse.indices.dsex
            data["dsex_change"] = dse.indices.dsex_change
            data["dsex_change_pct"] = dse.indices.dsex_change_pct
            data["ds30"] = dse.indices.ds30
            data["dses"] = dse.indices.dses
        if dse.market is not None:
            data["turnover_crore"] = dse.market.turnover_crore
            data["total_trades"] = dse.market.total_trades
            data["advancing"] = dse.market.advancing
            data["declining"] = dse.market.declining
            data["unchanged"] = dse.market.unchanged

    commodities = snapshots.get("commodity_prices")
    if commodities is not None:
        for key, cp in commodities.prices.items():
            unit_suffix = f"{cp.currency.lower()}_{cp.unit.replace(' ', '_')}"
            data[f"{key}_{unit_suffix}"] = cp.price
        change_pcts = {
            key: cp.change_pct
            for key, cp in commodities.prices.items()
            if cp.change_pct is not None
        }
        if change_pcts:
            data["commodity_change_pct"] = change_pcts

    return data


def _load_v3_registry() -> list[dict]:
    """Load the v3 indicator registry from config/sources-v3.json.

    Returns an empty list if the file does not exist (pre-v3 installs).
    """
    if not SOURCES_V3_PATH.exists():
        return []
    try:
        return json.loads(SOURCES_V3_PATH.read_text()).get("indicators", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load v3 registry: %s", e)
        return []


def _load_v3_snapshot(indicator_id: str) -> dict | None:
    """Return the latest per-indicator snapshot dict, or None if unavailable."""
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    candidates = sorted(d.glob("*.json"), reverse=True)
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to load v3 snapshot for %s: %s", indicator_id, e)
        return None


def _is_bad_snapshot(snapshot: dict) -> bool:
    """True if the snapshot represents a failed parse (sentinel 0.0 or missing value)."""
    if snapshot.get("_provenance") == "needs_review":
        return True
    if snapshot.get("_parse_strategy") == "extract_failed":
        return True
    if snapshot.get("value") in (None, 0, 0.0):
        return True
    return False


def _load_last_good_snapshot(indicator_id: str, *, max_days_back: int = 60) -> dict | None:
    """Walk back through this indicator's per-day snapshots for the most recent good one.

    A 'good' snapshot is one where _is_bad_snapshot() is False — i.e. real
    extracted data, not the 0.0 placeholder the parser writes when extraction
    fails. Returns the snapshot dict (with the original date in _stale_from
    annotation) or None if no good snapshot exists in the lookback window.
    """
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    candidates = sorted(d.glob("*.json"), reverse=True)
    cutoff_age_days = max_days_back
    today = datetime.now(timezone.utc).date()
    for path in candidates:
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if _is_bad_snapshot(blob):
            continue
        # Check it's not too far in the past
        try:
            scraped = datetime.fromisoformat(
                blob["scraped_at"].replace("Z", "+00:00")
            ).date()
            if (today - scraped).days > cutoff_age_days:
                return None  # too old, give up (history is sorted newest-first)
        except (KeyError, ValueError):
            continue
        # Annotate with stale-fallback metadata
        blob["_provenance"] = "stale_fallback"
        blob["_stale_from"] = path.stem  # e.g. "2026-04-29"
        return blob
    return None


def _prior_good_snapshot(indicator_id: str, today: date) -> dict | None:
    """Most-recent good snapshot strictly BEFORE `today` (by scraped_at date).

    Unlike _load_last_good_snapshot, this excludes today's own snapshot — the
    cumulative guard must compare today's value against a genuinely prior value.
    """
    d = DATA_DIR / indicator_id
    if not d.exists():
        return None
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if _is_bad_snapshot(blob):
            continue
        try:
            scraped = datetime.fromisoformat(
                blob["scraped_at"].replace("Z", "+00:00")
            ).date()
        except (KeyError, ValueError):
            continue
        if scraped < today:
            return blob
    return None


def _is_fresh(snapshot: dict, now: datetime) -> bool:
    """Return True if the snapshot is within its cadence staleness threshold."""
    cadence = snapshot.get("cadence", "daily")
    threshold = STALE_THRESHOLDS_HOURS_BY_CADENCE.get(cadence, 24.0)
    try:
        scraped_at = datetime.fromisoformat(snapshot["scraped_at"].replace("Z", "+00:00"))
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)
        age_hours = (now - scraped_at).total_seconds() / 3600.0
        return age_hours <= threshold
    except (KeyError, ValueError):
        return False


def _fiscal_year(d: date) -> int:
    """Bangladesh fiscal year (July–June). Returns the FY-start calendar year."""
    return d.year if d.month >= FISCAL_YEAR_START_MONTH else d.year - 1


def _month_end(d: date) -> date:
    """Last calendar day of d's month (e.g. 2026-05-01 -> 2026-05-31,
    2024-02-01 -> 2024-02-29)."""
    return d.replace(day=monthrange(d.year, d.month)[1])


def _is_cumulative_regression(
    today_value: object,
    prior_value: object,
    today_date: date,
    prior_date: date,
) -> bool:
    """True if a cumulative (FYTD) figure dropped implausibly within the same FY.

    A cumulative fiscal-year-to-date total can only rise within a fiscal year.
    A drop beyond CUMULATIVE_DROP_TOLERANCE in the SAME fiscal year is a parse
    error. A drop across the July FY boundary is the legitimate annual reset.

    ``today_date`` and ``prior_date`` MUST be publication dates (the period the
    figures describe), not scrape dates -- see ``_cumulative_guard_dates`` and
    landmine 56. Passing scrape dates makes every 1 July reset look like a
    same-FY regression, because both scrapes happen in the same fiscal year.
    """
    if not isinstance(today_value, (int, float)) or isinstance(today_value, bool):
        return False
    if not isinstance(prior_value, (int, float)) or isinstance(prior_value, bool):
        return False
    if prior_value <= 0:
        return False
    if _fiscal_year(today_date) != _fiscal_year(prior_date):
        return False  # FY reset — drop is legitimate
    return today_value < prior_value * (1 - CUMULATIVE_DROP_TOLERANCE)


def _cumulative_guard_dates(
    snapshot: dict,
    prior: dict,
    today: date,
    prior_scraped: date | None,
) -> tuple[date | None, date | None, bool]:
    """Pick the dates the cumulative guard should compare, and say how good they are.

    Returns ``(today_date, prior_date, publication_dated)``.

    A fiscal-year-to-date total is anchored to the period it REPORTS, not to the
    day we happened to download it. ``source_as_of`` carries that period; when
    both sides have one, the guard compares real publication dates and the July
    reset is recognised for what it is. When either side lacks one we fall back
    to scrape dates, and the caller must treat that answer as unreliable inside
    the FY reset window -- two August scrapes always land in the same fiscal
    year, so the fallback cannot tell a reset from a regression.
    """
    # _parse_monthly_row_date is a plain ISO-date parser despite the name.
    today_pub = _parse_monthly_row_date(snapshot.get("source_as_of"))
    prior_pub = _parse_monthly_row_date(prior.get("source_as_of"))
    if today_pub is not None and prior_pub is not None:
        return today_pub, prior_pub, True
    return today, prior_scraped, False


def _cumulative_indicator_ids() -> set[str]:
    """Registry ids explicitly marked as fiscal-year-to-date cumulative totals."""
    return {
        ind["id"]
        for ind in _load_v3_registry()
        if ind.get("cumulative") and ind.get("id")
    }


def _drop_expected_fy_resets(
    flagged: list[str],
    data: dict[str, Any],
    history: list[dict[str, Any]],
    source_as_of_map: dict[str, date],
    cumulative_ids: set[str],
) -> tuple[list[str], list[str]]:
    """Remove flagged ids whose "collapse" is just the 1 July fiscal-year reset.

    Returns ``(still_flagged, excused)``.

    The Opus reviewer has no concept of Bangladesh's July–June fiscal year, so
    a year-to-date total restarting near zero reads to it as a ~90% overnight
    collapse. In August 2026 that cost `categorywise_export` and
    `remittance_by_country` eight days: each run quarantined them back to the
    prior FY's closing total, and because quarantine rewrites the `data` block
    that becomes tomorrow's history, the next run compared the correct value
    against its own substitution and quarantined it again. A quarantine is
    meant to be a one-day patch; nothing in the design ended this one.

    So the excuse is deterministic and date-driven rather than a plea to the
    model. An id is excused when ALL of:

      * the registry marks it ``cumulative`` (an explicit opt-in, never a guess
        from the id's name or its shape);
      * its fresh value is strictly BELOW the most recent historical value —
        only a drop can be a reset, an unexplained spike is never excused;
      * its own ``source_as_of`` falls in ``FY_RESET_GRACE_MONTHS``, i.e. the
        figure is reporting one of the first months of a fiscal year.

    Note what this deliberately does NOT do: it never substitutes a value and
    never lets a value through that the upstream cumulative guard rejected. The
    worst case is that a genuinely bad July figure survives the reviewer's
    veto — and that figure has already passed ``_is_cumulative_regression`` in
    ``_build_v3_blocks``, which screens the same series against its own history
    on publication dates. This is the second layer, and it is the layer that
    only ever refuses to overwrite fresh data with stale data.

    The window is bounded on purpose: outside July–October a falling cumulative
    total is a real problem and gets no grace at all.
    """
    still: list[str] = []
    excused: list[str] = []
    for fid in flagged:
        if fid not in cumulative_ids:
            still.append(fid)
            continue
        today_value = data.get(fid)
        if not isinstance(today_value, (int, float)) or isinstance(today_value, bool):
            still.append(fid)
            continue
        as_of = source_as_of_map.get(fid)
        if as_of is None or as_of.month not in FY_RESET_GRACE_MONTHS:
            still.append(fid)
            continue
        prior_value = None
        for snap in reversed(history):  # newest-last ⇒ reversed = newest-first
            v = (snap.get("data") or {}).get(fid)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                prior_value = v
                break
        if prior_value is None or not today_value < prior_value:
            still.append(fid)
            continue
        excused.append(fid)
    return still, excused


def _quarantine_flagged(
    data: dict[str, Any],
    flagged_ids: list[str],
    history: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], bool]:
    """Quarantine Opus-flagged fields instead of rejecting the whole snapshot.

    Returns (cleaned_data, quarantined_ids, hard_reject).
    hard_reject is True when the verdict is untrustworthy or too broad:
      * any flagged id is not present in `data`, or
      * more than MAX_QUARANTINE_FIELDS ids are flagged.
    Otherwise each flagged id is replaced with its most-recent good value from
    `history` (newest-last list of archived `.data` dicts); if no historical
    value exists, the field is dropped.
    """
    present = [fid for fid in flagged_ids if fid in data]
    if len(present) != len(flagged_ids):
        return data, [], True   # unmappable flagged id ⇒ don't trust the verdict
    if len(present) > MAX_QUARANTINE_FIELDS:
        return data, [], True   # too broadly broken to publish

    cleaned = dict(data)
    quarantined: list[str] = []
    for fid in present:
        last_good = None
        for snap in reversed(history):  # newest-last ⇒ reversed = newest-first
            v = (snap.get("data") or {}).get(fid)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                last_good = v
                break
        if last_good is not None:
            cleaned[fid] = last_good
        else:
            cleaned.pop(fid, None)
        quarantined.append(fid)
    return cleaned, quarantined, False


def _compute_reserve_utilisation(data_additions: dict[str, Any]) -> None:
    """Mint derived CRR/SLR utilisation ratios into ``data_additions`` in place.

    S2: the Liquidity panel wants CRR/SLR utilisation %, but EconDelta scrapes
    only the LEVELS (``deposits_held_with_bb_crr``, ``excess_liquid_asset_total_minimum``,
    ``deposits_of_the_system``) — there is no scraped maintenance-% cell. So we
    compute the ratio here, after the snapshot loop has populated the level
    scalars and BEFORE the Supabase writer's scalar-only filter, so each ratio
    lands in ``metric_history`` under its own id.

    Each ratio = numerator / denominator × 100, expressed as a % of total system
    deposits — labelled by what it actually divides (no hardcoded statutory CRR/SLR
    rate, which would be a shifting policy constant). Null-safe and idempotent:

      * a missing/non-numeric numerator or denominator → skip (no key written),
        so a missing month renders as a missing metric rather than a bogus 9999%;
      * a zero (or non-positive) denominator → skip (no divide-by-zero);
      * a derived id already present in ``data_additions`` is left untouched.
    """
    for derived_id, (numerator_id, denominator_id) in RESERVE_UTIL_DERIVED.items():
        if derived_id in data_additions:
            continue
        numerator = data_additions.get(numerator_id)
        denominator = data_additions.get(denominator_id)
        if not isinstance(numerator, (int, float)) or isinstance(numerator, bool):
            continue
        if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
            continue
        if denominator <= 0:
            continue
        data_additions[derived_id] = round(numerator / denominator * 100, 4)


def _build_v3_blocks(
    now: datetime,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], FreshnessSummary, list[Alert]]:
    """Build the v3 data additions, domains, freshness summary, and alerts.

    Returns:
        data_additions: flat {indicator_id: value} dict to merge into data
        domains:        nested {domain: {indicator_id: snapshot}} dict
        freshness:      FreshnessSummary with per-cadence counts
        alerts:         list of Alert objects for anomalous indicators
    """
    registry = _load_v3_registry()
    data_additions: dict[str, Any] = {}
    domains: dict[str, dict[str, Any]] = {}
    cadence_buckets: dict[str, dict] = {}
    indicators_total = 0
    indicators_fresh = 0
    indicators_stale = 0
    indicators_failed = 0
    alerts: list[Alert] = []

    for ind in registry:
        indicator_id = ind["id"]
        domain = ind.get("domain", "macro")
        cadence = ind.get("cadence", "daily")
        cadence_buckets.setdefault(cadence, {"fresh": 0, "expected": 0, "stale_ids": []})
        cadence_buckets[cadence]["expected"] += 1
        indicators_total += 1

        snapshot = _load_v3_snapshot(indicator_id)
        if snapshot is None:
            indicators_failed += 1
            continue

        # Stale-fallback: if today's snapshot is bad (parser wrote 0.0 with
        # provenance=needs_review), walk back through history for the most
        # recent successful extraction and use THAT instead, marked stale.
        # If no good historical snapshot exists, skip the indicator entirely
        # — better the brief shows a missing key than a misleading 0.0.
        if _is_bad_snapshot(snapshot):
            indicators_failed += 1
            historical = _load_last_good_snapshot(indicator_id)
            if historical is None:
                logger.info(
                    "skipping %s — today bad and no good historical snapshot in last 60 days",
                    indicator_id,
                )
                continue
            logger.info(
                "stale-fallback for %s: using %s (today is needs_review)",
                indicator_id,
                historical.get("_stale_from", "?"),
            )
            snapshot = historical

        # Cumulative-monotonicity guard: a FYTD/cumulative total can't fall within
        # a fiscal year. If it did (parser/LLM mis-read), fall back to the prior
        # good value, marked stale — see docs/.../nbr-guard-granular-reject.
        elif ind.get("cumulative"):
            prior = _prior_good_snapshot(indicator_id, now.date())
            if prior is not None:
                try:
                    prior_scraped = datetime.fromisoformat(
                        prior["scraped_at"].replace("Z", "+00:00")
                    ).date()
                except (KeyError, ValueError):
                    prior_scraped = None
                today_date, prior_date, publication_dated = _cumulative_guard_dates(
                    snapshot, prior, now.date(), prior_scraped
                )
                if today_date is None or prior_date is None:
                    pass  # nothing to compare against — leave the snapshot alone
                elif (
                    not publication_dated
                    and today_date.month in FY_RESET_GRACE_MONTHS
                ):
                    # Scrape-dated fallback inside the FY reset window: both
                    # sides sit in the same fiscal year by construction, so the
                    # guard cannot tell July's legitimate restart from a parse
                    # error. Declining to act is the cheaper mistake — letting a
                    # bad figure through gets caught by the Opus review and by
                    # tomorrow's run, whereas clobbering a correct reset
                    # republishes last year's closing total as this year's and
                    # never self-heals (landmine 56).
                    logger.info(
                        "cumulative guard stood down for %s: no source_as_of on "
                        "both sides and %s is inside the FY reset window — "
                        "cannot distinguish a July restart from a regression",
                        indicator_id, today_date.isoformat(),
                    )
                elif _is_cumulative_regression(
                    snapshot.get("value"), prior.get("value"), today_date, prior_date
                ):
                    logger.error(
                        "cumulative regression for %s: today=%s (%s) < prior-good=%s "
                        "(%s, same FY) — stale-fallback to %s",
                        indicator_id, snapshot.get("value"), today_date.isoformat(),
                        prior.get("value"), prior_date.isoformat(),
                        prior.get("scraped_at", "?"),
                    )
                    indicators_failed += 1
                    prior = {**prior, "_provenance": "stale_fallback",
                             "_stale_from": prior.get("scraped_at")}
                    snapshot = prior

        fresh = _is_fresh(snapshot, now) and snapshot.get("_provenance") != "stale_fallback"
        if fresh:
            indicators_fresh += 1
            cadence_buckets[cadence]["fresh"] += 1
        else:
            indicators_stale += 1
            cadence_buckets[cadence]["stale_ids"].append(indicator_id)

        # Add to flat data dict (for The Brief — opportunistic read with no code changes)
        value = snapshot.get("value")
        if isinstance(value, (int, float, str, dict)):
            data_additions[indicator_id] = value

        # Add to domains block grouped by domain
        domains.setdefault(domain, {})[indicator_id] = snapshot

        # Anomaly detection: alert when change_pct exceeds the per-indicator threshold
        change_pct = snapshot.get("change_pct")
        threshold = ind.get("anomaly_threshold")
        if change_pct is not None and threshold is not None and abs(change_pct) >= threshold:
            alerts.append(
                Alert(
                    indicator_id=indicator_id,
                    type="anomaly",
                    severity="warn",
                    value=snapshot.get("value"),
                    previous=snapshot.get("previous_value"),
                    change_pct=change_pct,
                )
            )

    # Derived reserve-utilisation ratios (S2): minted from the level scalars
    # loaded above, BEFORE the writer's scalar-only filter, so they persist to
    # metric_history under their own ids. Null/zero-denominator safe.
    _compute_reserve_utilisation(data_additions)

    freshness = FreshnessSummary(
        indicators_total=indicators_total,
        indicators_fresh=indicators_fresh,
        indicators_stale=indicators_stale,
        indicators_failed=indicators_failed,
        by_cadence={
            c: FreshnessByCadence(
                fresh=v["fresh"],
                expected=v["expected"],
                stale_ids=v["stale_ids"],
            )
            for c, v in cadence_buckets.items()
        },
    )
    return data_additions, domains, freshness, alerts


def _build_source_as_of_map(domains: dict[str, dict[str, Any]]) -> dict[str, date]:
    """Extract per-metric publication dates from the v3 domains snapshot dict.

    Each v3 snapshot written by ``parsers/hybrid.py:_build_snapshot`` may carry
    a ``source_as_of`` string (ISO date, e.g. "2025-09-30") when the parser could
    recover the true publication date from the source document. This function
    collects those dates and returns a metric_id → date mapping that
    ``upsert_metric_history`` uses to override the global run-date ``as_of``.

    Metrics without a ``source_as_of`` key (daily scrapers, fallback runs) are
    simply absent from the returned dict — the writer falls back to today.

    Malformed or missing date strings are silently skipped (logged at DEBUG).
    """
    result: dict[str, date] = {}
    # Quarterly/fiscal_year ids missing a date this run, collected for ONE
    # batched Discord alert after the loop below (not one notify() per id).
    undated_slow_cadence_ids: list[str] = []
    for _domain, indicators in domains.items():
        for indicator_id, snapshot in indicators.items():
            raw = snapshot.get("source_as_of")
            if not raw:
                # A metric with no recovered date is stamped with today's run
                # date, which makes a stale value look fresh on The Brief.
                # Originally this only warned for "quarterly"/"fiscal_year"
                # (11 registry indicators, 6+5); extended to ALL cadences
                # below, because a daily/monthly parser that silently stops
                # recovering its date is exactly as dangerous. The allow-list
                # guard is what makes that safe: `_NEVER_DATED_PARSE_STRATEGIES`
                # are parsers whose date recovery isn't implemented yet
                # (source_as_of absent is EXPECTED, not a bug) — without it,
                # extending to all cadences would warn on 13 registry
                # indicators every single run.
                if snapshot.get("_parse_strategy") in _NEVER_DATED_PARSE_STRATEGIES:
                    continue
                cadence = snapshot.get("cadence")
                logger.warning(
                    "%s is %s cadence but carries no source_as_of — its "
                    "metric_history row will be stamped with today's run date "
                    "(a stale value can read as fresh). Check the parser's "
                    "date recovery for this source.",
                    indicator_id, cadence,
                )
                # Quarterly/fiscal_year metrics are few (11 in the registry),
                # so a Discord alert is useful signal — an undated FSAR/fiscal
                # figure usually means a parser date-recovery regression worth
                # acting on same-day. Batched into ONE notify below (not one
                # per id): systemd starts a fresh process per run, so the
                # notifier's (level, title) dedup can't collapse repeats
                # across runs — a per-id title would re-fire every single
                # run forever for any indicator that's chronically undated
                # (e.g. the landmine-26 set: debt_gdp_ratio, gdp, fy_*, debt
                # stocks). Daily/weekly/monthly can number in the dozens on a
                # single upstream outage, so those stay log-only (still
                # visible in logs/econdelta-aggregate-systemd.log) — the
                # repo's alert-noise rule.
                if cadence in ("quarterly", "fiscal_year"):
                    undated_slow_cadence_ids.append(indicator_id)
                continue
            try:
                result[indicator_id] = date.fromisoformat(str(raw)[:10])
            except (ValueError, TypeError):
                logger.debug(
                    "skipping malformed source_as_of=%r for %s", raw, indicator_id
                )

    if undated_slow_cadence_ids:
        ids = sorted(undated_slow_cadence_ids)
        notify(
            "warning",
            "aggregate — undated slow-cadence indicators",
            f"{len(ids)} quarterly/fiscal_year metric(s) carry no source_as_of "
            "this run — their metric_history rows will be stamped with today's "
            "run date (a stale value can read as fresh). Check each parser's "
            "date recovery:\n" + "\n".join(ids),
        )

    # DOMMR/BOFR fan-out dates: ``_flatten_dict_indicators`` mints the four
    # per-series ids (dommr/dommr_1w/bofr/bofr_1w) from the
    # ``money_market_ref_rate`` dict, but those minted keys are NOT v3
    # registry ids — the loop above only ever dates INDICATOR ids, so without
    # this propagation the writer's ``as_of=today`` fallback would silently
    # re-forge run-date stamps on all four fanned rows while the parent
    # carried the page's real date (the exact alias-layer forgery the
    # 2026-08-23 H3 yield-alias fix closed — landmine 52 corrections — and
    # the landmine 26 alias-date precedent before it). Same date for all
    # four: the parser guarantees both tables' newest blocks share one
    # value date (it refuses the parse otherwise).
    mmrr_date = result.get("money_market_ref_rate")
    if mmrr_date is not None:
        for fanned_id in MONEY_MARKET_REF_RATE_FANOUT_IDS:
            result.setdefault(fanned_id, mmrr_date)

    # The brief reads brief-side keys (e.g. banking_npl_pct), not the EconDelta
    # indicator ids — _apply_brief_aliases copies the VALUE to those keys but not
    # the date. Propagate each override to its alias / conversion target so the
    # write lands at the right as_of for the key the SPA actually reads. A unit
    # conversion changes the value, not the reporting period — so same date.
    for brief_key, econdelta_key in BRIEF_ALIASES.items():
        if econdelta_key in result and brief_key not in result:
            result[brief_key] = result[econdelta_key]
    for brief_key, (source_key, _mult) in BRIEF_CONVERSIONS.items():
        if source_key in result and brief_key not in result:
            result[brief_key] = result[source_key]
    return result


def _build_tier1_source_as_of_map(
    snapshots: dict[str, Any], *, bb_forex_ok: bool
) -> dict[str, date]:
    """Per-metric publication dates for the 3 Tier-1 SCRAPER_SPEC sources.

    ``_build_source_as_of_map`` above only sees the v3 registry's ``domains``
    dict. The three Tier-1 snapshots (bb_forex, dse_market, commodity_prices —
    ``SCRAPER_SPEC``) never enter that dict, so their ``flatten_data`` keys
    could never receive a publication-date override: every aggregate run
    stamped them with today's run date regardless of how stale the underlying
    snapshot file actually was ("as_of forgery" — a frozen BB reserves figure,
    or a Fri/Sat/failed-scrape DSEX carry-forward, read as fresh on The Brief
    every single day). This mirrors ``_build_source_as_of_map``'s
    metric_id -> date shape for those keys.

    Args:
        bb_forex_ok: True when ``sources_status["bb_forex"].status == "ok"``
            (see ``main()``). Must be the SAME gate that decides whether the
            force-overwrite alias block (``usd_bdt_exchange_rate`` /
            ``fx_reserve_gross_and_bpm6``, minted directly from bb_forex)
            actually overwrites the v3 pipeline's own value for those two
            ids. Review round 1 caught the bug of dating those two aliases
            unconditionally from bb_forex while gating their VALUE on
            freshness: when bb_forex is stale, the alias falls back to
            whatever the v3 registry produced (fresh, undated, or absent) —
            but the date override still fired every time, stamping a FRESH
            v3 value with bb_forex's STALE date. The date must follow the
            (gated) value, so these two keys are only set here when
            ``bb_forex_ok`` — everything else in this function (the raw
            rates/reserves/dse/commodity keys, always sourced straight from
            their own snapshot regardless of freshness) is unaffected.
            Required (no default, review round 2, item 3): a future caller
            that forgets to pass it should get a TypeError at the call site,
            not a silently-wrong "no alias date" fallback.
    """
    result: dict[str, date] = {}

    forex = snapshots.get("bb_forex")
    if forex is not None:
        # `forex.date` (the scraper's own calendar-day field, set once at
        # ``date.today()`` when it wrote the snapshot), not
        # ``forex.scraped_at.date()`` (a UTC timestamp). Both agree under the
        # current retry-writer pattern (the 00:0x UTC retry slot dominates),
        # but `forex.date` stays correct if the primary ~23:05 UTC slot ever
        # succeeds on the BDT-local box: scraped_at's UTC calendar date would
        # then be a day behind the intended BDT reporting day.
        rates_date = forex.date
        for rate_key in ("usd_bdt_mid", "usd_bdt_buy", "usd_bdt_sell", "eur_bdt", "gbp_bdt"):
            result[rate_key] = rates_date
        if bb_forex_ok:
            # The force-overwrite alias (main(), ~1130) mints
            # usd_bdt_exchange_rate straight from forex.rates.usd_bdt_mid —
            # same date — but ONLY when bb_forex_ok (see docstring above).
            result["usd_bdt_exchange_rate"] = rates_date
        if forex.reserves is not None:
            # BB's headline reserves figure is the END-of-month stock.
            # reserves_date parses the source's month label to the 1st (e.g.
            # "May 2026" -> 2026-05-01); stamping the 1st would age the row
            # ~30 extra days against the sentinel's 45-day monthly grace
            # (sentinel/cadence.py GRACE_DAYS_BY_CADENCE["monthly"]), so use
            # the month's last day instead.
            result["gross_reserves_usd_bn"] = _month_end(forex.reserves.reserves_date)
            # import_cover_months is flatten_data's other reserves-block key
            # (set unconditionally alongside gross_reserves_usd_bn, not part
            # of the freshness-gated alias block) -- same reporting period.
            result["import_cover_months"] = result["gross_reserves_usd_bn"]
            if bb_forex_ok:
                # The other force-overwrite alias mints
                # fx_reserve_gross_and_bpm6 straight from
                # forex.reserves.gross_reserves_usd_bn — same date, same gate.
                result["fx_reserve_gross_and_bpm6"] = result["gross_reserves_usd_bn"]

    dse = snapshots.get("dse_market")
    if dse is not None:
        # MEDIUM-7 (2026-08-22 round-1 review): corrected -- the previous
        # version of this comment described PRE-fix/date-integrity-monitoring
        # behaviour that no longer exists. scrapers/dse_market.py now parses
        # the SOURCE page's own "TODAY'S SHARE MARKET : YYYY-MM-DD" trading
        # date and stamps DseSnapshot.date with it -- never date.today(), and
        # there is no longer a non-trading "write a trading_day=False marker
        # dated today" path at all (a non-trading/already-seen session is a
        # pure no-op now: nothing new is written). What still distinguishes a
        # real trading session is `indices`/`market` being populated, exactly
        # as checked below. Where `date` still matters most is the FAILURE
        # case: if the scraper doesn't run at all today (or the parsed
        # session is already on disk and it no-ops), `find_latest_snapshot`
        # returns the newest EXISTING file, and THAT file's `date` field
        # honestly reflects the trading day it was captured for — not today.
        # Using `scraped_at` here would be wrong on exactly that carry-
        # forward path, where a stale file re-read today would otherwise get
        # today's timestamp instead of the session it actually describes.
        if dse.indices is not None:
            for dse_key in ("dsex", "dsex_change", "dsex_change_pct", "ds30", "dses"):
                result[dse_key] = dse.date
        if dse.market is not None:
            for dse_key in (
                "turnover_crore", "total_trades", "advancing", "declining", "unchanged",
            ):
                result[dse_key] = dse.date

    commodities = snapshots.get("commodity_prices")
    if commodities is not None:
        # `commodities.date` (the scraper's own quote-date field), not
        # `commodities.scraped_at.date()` (a UTC timestamp). Since the
        # date-integrity fix (fix/date-integrity-monitoring),
        # scrapers/commodity_prices.py sets `date` to the yfinance QUOTE's own
        # trading date (history()'s DatetimeIndex, MAXED across brent/WTI/
        # gold) -- never date.today() unless every ticker's history() call
        # failed this run, in which case it degrades to the run date exactly
        # as before this fix. `scraped_at` remains a plain UTC capture
        # timestamp and stays unsuitable as an as_of source regardless: the
        # commodity timer fires ~23:08 UTC, close enough to the UTC day
        # boundary that scraped_at's UTC calendar date can land a day behind
        # either the quote date or the intended local reporting day.
        #
        # L1 (2026-08-22 round-1 review): prefer each commodity's OWN
        # `cp.quote_date` over the snapshot-wide max when it's available --
        # brent/WTI/gold virtually always agree, but on the rare run where
        # one ticker's quote genuinely lags the others, this stamps THAT
        # metric with its own true date instead of borrowing a sibling
        # ticker's (possibly later) one. Falls back to the snapshot-wide
        # `commodity_date` only for a ticker whose own quote_date is None
        # (its history() call failed this run) -- never leaves a metric
        # entirely undated when the snapshot itself has SOME usable date.
        commodity_date = commodities.date
        for key, cp in commodities.prices.items():
            unit_suffix = f"{cp.currency.lower()}_{cp.unit.replace(' ', '_')}"
            result[f"{key}_{unit_suffix}"] = cp.quote_date or commodity_date

    return result


def _apply_media_overrides(
    data: dict[str, Any],
    source_as_of_map: dict[str, date],
    *,
    writer=None,
    reader=None,
    set_status=None,
) -> None:
    """Re-assert approved media overrides into metric_history AFTER the normal
    upsert, so a human-approved press value wins until BB's pipeline supersedes
    it (spec D6). EconDelta stays the sole writer; the override write reuses
    _apply_brief_aliases so it reaches the brief keys. Best-effort."""
    from datetime import date as _date

    from media_screen.supersede import is_superseded
    from utils.supabase_reader import get_active_media_review
    from utils.supabase_writer import (
        SupabaseWriteError,
        set_media_review_status,
        upsert_metric_history,
    )

    writer = writer or upsert_metric_history
    reader = reader or get_active_media_review
    set_status = set_status or set_media_review_status

    try:
        rows = reader()
    except Exception as e:  # noqa: BLE001 — overrides must never break aggregate
        logger.warning("media overrides: could not read active rows: %s", e)
        return

    for r in rows:
        mid = r["metric_id"]
        press_as_of = _date.fromisoformat(str(r["press_as_of"])[:10])
        automated_value = data.get(mid)
        automated_value = float(automated_value) if isinstance(automated_value, (int, float)) else None
        parsed_baseline = float(r["parsed_value"]) if r.get("parsed_value") is not None else None
        if is_superseded(
            kind=r["kind"],
            press_as_of=press_as_of,
            parsed_baseline=parsed_baseline,
            automated_value=automated_value,
            automated_as_of=source_as_of_map.get(mid),
        ):
            set_status(r["id"], "superseded")
            logger.info("media override %s (%s @ %s) superseded by BB", r["id"], mid, press_as_of)
            continue
        override_data = {mid: float(r["press_value"])}
        _apply_brief_aliases(override_data)
        try:
            writer(
                data=override_data,
                as_of=press_as_of,
                source=f"media-approved:{r.get('source_outlet') or 'press'}",
            )
        except SupabaseWriteError as e:
            logger.warning("media override write failed for %s: %s", mid, e)
            continue
        if r["status"] == "approved":
            set_status(r["id"], "applied", applied=True)
        logger.info("media override applied: %s = %s @ %s", mid, r["press_value"], press_as_of)


# ============================================================================
# Reserves gross/BPM6 monthly split (D5, reserves-memo-2026-08-05)
# ----------------------------------------------------------------------------
# Writes the two series The Brief's chartConfigs.ts reservesConfig() already
# expects into the MONTHLY namespace (metric_history_monthly /
# metric_definitions_monthly -- AGENTS.md landmine 20), completely SEPARATE
# from the existing daily gross_reserves_usd_bn / fx_reserve_gross_and_bpm6
# write above (flatten_data / the ~1200-line alias block in main()), which
# this section does not touch. Additive only: an older bb_forex snapshot
# with no bpm6_reserves_usd_bn (pre-dating this PR) simply produces no
# monthly write, same as any other new metric before its first successful
# scrape -- no existing metric_history/metric_history_monthly row is ever
# rewritten or restated here.
# ============================================================================

RESERVES_MONTHLY_GROSS_ID = "gross_reserves_usd_bn_monthly"
RESERVES_MONTHLY_BPM6_ID = "net_reserves_bpm6_usd_bn_monthly"
RESERVES_MONTHLY_SOURCE = "bb_forex"
RESERVES_MONTHLY_SOURCE_URL = "https://www.bb.org.bd/en/index.php/econdata/intreserve"
# Mirrors scrapers.bb_forex._BPM6_GROSS_RATIO_MIN/MAX -- see that module for
# how this band was calibrated. Duplicated (not imported) on purpose: this is
# a defensive RE-check, not the source of truth (parse time is), and keeping
# aggregate_latest decoupled from bb_forex's internals matches how the
# bpm6 < gross direction re-check below was already duplicated.
_RESERVES_MONTHLY_RATIO_MIN = 0.70
_RESERVES_MONTHLY_RATIO_MAX = 0.95
# metric_definitions_monthly / metric_definitions grace window for a monthly
# cadence (sentinel/cadence.py GRACE_DAYS_BY_CADENCE["monthly"], docs/
# data-contract.md §10 Block 1). v_metric_freshness COALESCEs grace_days from
# metric_definitions_monthly -- a NULL there makes is_fresh permanently
# unknown (NULL), not merely wrong, for these two ids (2026-08-05 review M5).
_RESERVES_MONTHLY_GRACE_DAYS = 45


def _reserves_monthly_definitions() -> list[dict]:
    """metric_definitions_monthly rows for the two reserves-split ids.

    display_name/unit/domain/notes are kept BYTE-IDENTICAL to
    scripts/seed_macro_monthly.py's KEY_MAP entries for these exact same ids
    (fxReserve/fxBPM6) on purpose (2026-08-05 review L1): both writers use
    merge-duplicates upsert on metric_id, so whichever writer ran most
    recently wins for source_url/source_attribution -- that's the ONE field
    pair genuinely left to last-writer-wins, and it's fine because it's
    provenance metadata, not anything a viewer reads as the label/unit/
    domain/notes. Keeping those four fields identical between the two
    writers means the last-writer-wins behaviour on the other two fields is
    invisible to anyone reading metric_definitions_monthly.
    """
    return [
        {
            "metric_id": RESERVES_MONTHLY_GROSS_ID,
            "display_name": "FX reserves (gross)",
            "unit": "USD bn",
            "source_url": RESERVES_MONTHLY_SOURCE_URL,
            "source_attribution": "Bangladesh Bank",
            "domain": "external",
            "description": "Gross foreign exchange reserves (BB headline measure).",
            "notes": "",
            "grace_days": _RESERVES_MONTHLY_GRACE_DAYS,
        },
        {
            "metric_id": RESERVES_MONTHLY_BPM6_ID,
            "display_name": "FX reserves (BPM6/net)",
            "unit": "USD bn",
            "source_url": RESERVES_MONTHLY_SOURCE_URL,
            "source_attribution": "Bangladesh Bank",
            "domain": "external",
            "description": "Foreign exchange reserves per IMF BPM6 methodology.",
            "notes": "Sparse — BB began reporting BPM6 ~2021; nulls for earlier months.",
            "grace_days": _RESERVES_MONTHLY_GRACE_DAYS,
        },
    ]


def _write_reserves_monthly_split(reserves: ForexReserves | None) -> int:
    """Write the two-series reserves split into metric_history_monthly (D5).

    gross_reserves_usd_bn_monthly and net_reserves_bpm6_usd_bn_monthly are
    DECOUPLED (2026-08-05 review M1): gross does not depend on BPM6 being
    present, so a reserves read with bpm6_reserves_usd_bn=None (an older
    pre-this-PR snapshot, or a genuine future BB layout without that column)
    still writes gross alone -- only the BPM6 row is withheld, and why is
    logged (H4: this must never be silent).

    The bpm6 < gross direction invariant AND the bpm6/gross ratio band are
    both enforced at PARSE time (scrapers/bb_forex.py -- a violation there
    raises ParseError and no new snapshot is written at all, so bad data
    can't even reach here), but both are re-checked defensively before
    writing here too. Either violation blocks BOTH series (it signals
    corruption, not absence, per M1) -- unlike the bpm6=None case above.

    Best-effort like the rest of main()'s Supabase write block --
    SupabaseWriteError propagates to the caller's existing try/except.

    Returns the number of history rows upserted (0, 1, or 2).
    """
    if reserves is None:
        logger.warning("reserves monthly split: forex.reserves is None -- nothing to write")
        return 0

    gross = reserves.gross_reserves_usd_bn
    bpm6 = reserves.bpm6_reserves_usd_bn
    as_of_iso = _month_end(reserves.reserves_date).isoformat()

    write_bpm6 = bpm6 is not None
    if bpm6 is None:
        logger.warning(
            "reserves monthly split: bpm6_reserves_usd_bn is None for %s -- "
            "writing %s only, withholding %s",
            reserves.reserves_date.isoformat(), RESERVES_MONTHLY_GROSS_ID, RESERVES_MONTHLY_BPM6_ID,
        )
    elif bpm6 >= gross:
        logger.warning(
            "reserves monthly split: bpm6 (%.4f) >= gross (%.4f) for %s -- "
            "refusing BOTH monthly writes (column-identification failure)",
            bpm6, gross, reserves.reserves_date.isoformat(),
        )
        return 0
    elif not (_RESERVES_MONTHLY_RATIO_MIN <= bpm6 / gross <= _RESERVES_MONTHLY_RATIO_MAX):
        logger.warning(
            "reserves monthly split: bpm6/gross ratio %.4f for %s is outside "
            "[%.2f, %.2f] -- refusing BOTH monthly writes (magnitude/unit "
            "corruption, not a column swap)",
            bpm6 / gross, reserves.reserves_date.isoformat(),
            _RESERVES_MONTHLY_RATIO_MIN, _RESERVES_MONTHLY_RATIO_MAX,
        )
        return 0

    from utils.supabase_writer import (
        upsert_metric_definitions_monthly,
        upsert_metric_history_monthly,
    )

    rows = [
        {
            "metric_id": RESERVES_MONTHLY_GROSS_ID,
            "as_of": as_of_iso,
            "value": gross,
            "source": RESERVES_MONTHLY_SOURCE,
            "source_as_of": as_of_iso,
        },
    ]
    if write_bpm6:
        rows.append({
            "metric_id": RESERVES_MONTHLY_BPM6_ID,
            "as_of": as_of_iso,
            "value": bpm6,
            "source": RESERVES_MONTHLY_SOURCE,
            "source_as_of": as_of_iso,
        })
    upsert_metric_definitions_monthly(_reserves_monthly_definitions())
    return upsert_metric_history_monthly(rows)


# ============================================================================
# Macro monthly LIVE APPENDER (2026-08-08 frozen-charts incident, AGENTS.md
# landmine 50, AGENT_LEARNINGS.md 2026-08-08 entry)
# ----------------------------------------------------------------------------
# 5 of the metric_history_monthly chart-feeding series (remittance, exports,
# and the CPI trio) were seeded ONCE from a dead third-party site
# (macro_observer_seed) and froze at as_of=2026-03-01 -- no live writer ever
# kept them moving. scripts/backfill_monthly_chart_series.py fills the
# Apr-Jun 2026 gap with owner-verified official values (a ONE-TIME,
# owner-run backfill); THIS function is the ONGOING writer that keeps two of
# those five series moving every day after -- the CPI trio (derived from our
# own daily metric_history, never from the third-party site) and remittance
# (fetched live from BB's official monthly table). exports_usd_mn_monthly
# and imports_usd_mn_monthly have NO live writer here -- see the
# sentinel/freshness.py accepted-stale entries for why.
#
# APPEND-ONLY BY DESIGN: every write checks metric_history_monthly for an
# existing (metric_id, as_of) row first and skips if present. This is
# load-bearing, not a style choice -- the backfill patches two known-bad
# daily cells (non_food_inflation April 2026, general_inflation June 2026;
# see the equality guard below) with hand-verified official values, and this
# appender must never overwrite them with a re-derived daily value on a
# later run.
#
# Same call-site gating pattern as _write_reserves_monthly_split above: own
# try/except at the call site, its own distinct notify() message on write
# failure so a responder can tell which appender failed, never crashes the
# daily run. Sub-path (CPI read / remittance fetch) failures are handled
# INSIDE this function -- they degrade gracefully (skip that sub-path only)
# rather than aborting the whole appender, and each notifies with its own
# message so "the CPI read failed" and "BB's remittance page changed shape"
# are distinguishable incidents.
# ============================================================================

# Daily metric_history id -> monthly chart-feeding id it feeds. Only
# general_inflation/food_inflation/non_food_inflation -- point_to_point_inflation
# is read too (see below) but ONLY to power the equality guard, never written
# itself.
_CPI_DAILY_TO_MONTHLY: dict[str, str] = {
    "general_inflation": "cpi_12m_avg_monthly",
    "food_inflation": "cpi_p2p_food_monthly",
    "non_food_inflation": "cpi_p2p_nonfood_monthly",
}
_CPI_MONTHLY_SOURCE = "econdelta_daily_cpi"
# Range check per spec: 0 < v < 30 (strict on both ends -- a 0.0 or 30.0
# reading is exactly as suspicious as a negative or triple-digit one for a
# CPI YoY/12m-average percentage).
_CPI_VALUE_MIN = 0.0
_CPI_VALUE_MAX = 30.0

_REMITTANCE_MONTHLY_ID = "remittance_usd_mn_monthly"
_REMITTANCE_URL = "https://www.bb.org.bd/en/index.php/econdata/wageremitance"
_REMITTANCE_SOURCE = "bb_wageremitance"
_REMITTANCE_VALUE_MIN = 500.0
_REMITTANCE_VALUE_MAX = 6000.0
# scripts/backfill_monthly_chart_series.py owns Apr-Jun 2026; this appender
# only ever writes data months from July 2026 onward (the append-only
# skip-if-exists check would no-op on Apr-Jun anyway, but pinning the floor
# here means a future re-run can never even attempt to touch pre-backfill
# months).
_REMITTANCE_APPEND_FROM = date(2026, 7, 1)

_REMIT_MONTH_NAME_TO_NUM: dict[str, int] = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}
# BB's fiscal-year row-group header, e.g. "2025-2026" (BD fiscal year =
# July-June; "2025-2026" spans July 2025 .. June 2026).
_REMIT_FY_HEADER_RE = re.compile(r"(\d{4})\s*-\s*(\d{4})")


def _latest_value_as_of(rows: list[dict]) -> tuple[float, date] | None:
    """Parse the first (newest) row from get_metric_history into (value, as_of).

    Returns None on an empty list or a malformed row -- callers treat that
    identically to "no daily row" (they must not distinguish missing from
    unparseable; both mean "nothing safe to derive from").
    """
    if not rows:
        return None
    row = rows[0]
    try:
        value = float(row["value"])
        as_of = date.fromisoformat(str(row["as_of"])[:10])
    except (KeyError, TypeError, ValueError):
        return None
    return value, as_of


def _parse_monthly_row_date(raw: object) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _cpi_monthly_append_rows(
    *,
    general_row: tuple[float, date] | None,
    food_row: tuple[float, date] | None,
    nonfood_row: tuple[float, date] | None,
    p2p_row: tuple[float, date] | None,
    existing_pairs: set[tuple[str, date]],
    today: date,
) -> tuple[list[dict], list[str]]:
    """Pure transform: latest daily CPI rows -> metric_history_monthly append
    candidates for the CPI trio.

    Applies, in order: month-end vintage check (a daily row whose as_of isn't
    the last day of its month isn't a true monthly reading -- see AGENTS.md
    landmine 26/47 on as_of forgery), a CLOSED-MONTH check (2026-08-08 Opus
    review H2 -- the month-end check alone is spoofable: a run-date-forged
    as_of on the 28th-31st of a 28/30/31-day month can coincidentally EQUAL
    that month's real last day, e.g. today=2026-08-31 forging as_of=2026-08-31,
    which IS August's month-end by coincidence even though it describes
    today's run, not a recovered vintage. Requiring the described month to
    already be STRICTLY BEFORE today's month closes this regardless of which
    day-of-month the forgery happens to land on -- BB publishes month M
    during M+1, so nothing legitimate is ever rejected), the [0, 30) range
    check, a general_inflation == point_to_point_inflation coincidence guard
    (see its own comment below for what it now defends against -- Opus
    review round 1, H5, corrects an earlier rationale that no longer
    applies) -- made FAIL-CLOSED (2026-08-08 review L6): if point_to_point_
    inflation is unavailable, the guard cannot be evaluated, so
    cpi_12m_avg_monthly is skipped out of caution rather than written
    unverified -- and finally the append-only skip-if-exists check.

    SPLIT VINTAGE, BY DESIGN (Opus review round 1, H5 -- 2026-08-23,
    corrects a stale claim below this docstring carried since PR-C):
    general_inflation/point_to_point_inflation and food_inflation/
    non_food_inflation are NO LONGER extracted from the same document in
    the same run. PR-C repointed the first pair to BB's live `econdata/
    inflation` HTML page (parsers/html_dated_table_row.py), which
    typically has the CURRENT month before the MEI PDF does; the second
    pair still reads the MEI PDF, which runs ~1-2 months behind. The
    OLD claim here -- "all three daily ids come from the SAME PDF in the
    SAME parse run, so their as_of values naturally align" -- is no longer
    true, and was never enforced by this function anyway (there was never
    a forced-alignment step). The three ids are, and always were, appended
    fully independently: each surviving row is written under its OWN
    correctly-derived as_of regardless of what the others resolve to, and
    it is now the NORMAL case (not a rare edge case) for
    cpi_12m_avg_monthly to advance a month or more ahead of cpi_p2p_food_
    monthly/cpi_p2p_nonfood_monthly for a while. Reader-side mitigation is
    the-brief's job, not this pipeline's: The Brief marks each series'
    own staleness independently per-series (the-brief PR #166) rather than
    assuming the CPI trio shares one vintage.

    Returns (rows_to_write, skip_reasons) -- skip_reasons feed the caller's
    logging so a responder can see WHY a metric wasn't appended this run.
    """
    rows: list[dict] = []
    reasons: list[str] = []
    daily_rows = {
        "general_inflation": general_row,
        "food_inflation": food_row,
        "non_food_inflation": nonfood_row,
    }
    for daily_id, monthly_id in _CPI_DAILY_TO_MONTHLY.items():
        row = daily_rows[daily_id]
        if row is None:
            reasons.append(f"{monthly_id}: no daily {daily_id} row available")
            continue
        value, as_of = row
        if as_of != _month_end(as_of):
            reasons.append(
                f"{monthly_id}: latest {daily_id} as_of={as_of} is not a "
                "month-end vintage -- skipping (not a true monthly reading)"
            )
            continue
        if as_of.replace(day=1) >= today.replace(day=1):
            reasons.append(
                f"{monthly_id}: latest {daily_id} as_of={as_of} describes the "
                f"CURRENT (not-yet-closed) month relative to today={today} -- "
                "skipping (closed-month guard, H2: a month-end-coincidence "
                "forged as_of would otherwise pass the vintage check above)"
            )
            continue
        if not (_CPI_VALUE_MIN < value < _CPI_VALUE_MAX):
            reasons.append(
                f"{monthly_id}: value {value} outside ({_CPI_VALUE_MIN}, {_CPI_VALUE_MAX})"
            )
            continue
        if daily_id == "general_inflation":
            # Coincidence guard, NOT a wrong-column detector anymore (Opus
            # review round 1, H5 -- 2026-08-23 comment correction). This
            # guard was built for the OLD MEI-PDF extraction, where
            # general_inflation and point_to_point_inflation both came from
            # ONE ambiguous table with two column-groups sharing the
            # "General" leaf label (landmine 49) -- an equal reading meant
            # the extractor had likely grabbed the wrong column-group.
            # Since PR-C, general_inflation/point_to_point_inflation read
            # TWO SEPARATE, unambiguous ROWS on BB's live econdata/inflation
            # page (parsers/html_dated_table_row.py) -- that specific
            # failure mode is now structurally impossible, not just
            # unlikely. The guard is KEPT anyway as cheap, near-zero-cost
            # coincidence insurance: two genuinely different BB concepts
            # reading EXACTLY equal for the same month is itself worth a
            # second look (it happened for real in June 2026 under the old
            # extraction), and removing a working guard to "clean up" a
            # stale rationale would be pure downside for the one line of
            # protection it costs.
            if p2p_row is None:
                reasons.append(
                    f"{monthly_id}: point_to_point_inflation unavailable -- "
                    "cannot verify the general==p2p coincidence guard; "
                    "skipping out of caution (fail-closed, not fail-open, "
                    "review L6)"
                )
                continue
            p2p_value, p2p_as_of = p2p_row
            if p2p_as_of == as_of and p2p_value == value:
                reasons.append(
                    f"{monthly_id}: general_inflation ({value}) exactly equals "
                    f"point_to_point_inflation for {as_of} -- suspicious "
                    "coincidence (the historical June-2026 incident was a "
                    "wrong-column read under the OLD MEI-PDF extraction, no "
                    "longer structurally possible post-PR-C, but an exact "
                    "match is still worth refusing rather than trusting); "
                    "skipping cpi_12m_avg_monthly this month"
                )
                continue
        month_start = as_of.replace(day=1)
        if (monthly_id, month_start) in existing_pairs:
            # Append-only: already have this month (backfill or a prior run).
            continue
        month_start_iso = month_start.isoformat()
        rows.append({
            "metric_id": monthly_id,
            "as_of": month_start_iso,
            "value": value,
            "source": _CPI_MONTHLY_SOURCE,
            "source_as_of": as_of.isoformat(),
        })
    return rows, reasons


_REMIT_USD_HEADER_MARKER = "million us dollar"


def _resolve_remittance_value_column(table) -> int:
    """Resolve the USD-value column's TRUE index (matching the data rows'
    own column numbering) by HEADER TEXT ('million US dollar'), never a
    hardcoded position (2026-08-08 Opus review H4: an inserted cumulative
    column upstream of the value column would otherwise write an in-range
    but WRONG value permanently under a hardcoded cells[1] -- AGENTS.md
    landmine 45's "select columns by header-text semantics, never by
    position" applies here too).

    The <thead> is a GROUPED header (rowspan'd "Year/Month" over 2 rows,
    colspan'd "Remittances" over 2 columns, then a second row spelling out
    "In million US dollar" / "In billion Taka") -- the value label's
    position WITHIN ITS OWN <tr> is NOT its true column index, because the
    rowspan'd Year/Month column doesn't repeat in that row. This expands
    the header's rowspan/colspan grid to find the label's real column
    index, which then lines up with the data rows' own cell order.

    Raises ValueError if there is no <thead>, no header rows, no cell
    containing the USD marker text anywhere in the expanded grid, OR (2026-
    08-08 review R2) more than ONE distinct header cell matches -- e.g. a
    table with BOTH "Cumulative (in million US dollar)" and "Monthly (in
    million US dollar)" columns. The prior version returned the FIRST match
    in grid-iteration order, which would silently pick whichever of the two
    happened to sit first (re-opening H4 under a different disguise: still
    an in-range, plausible-looking, permanently WRONG value). Ambiguity
    must raise, never guess.
    """
    thead = table.find("thead")
    if thead is None:
        raise ValueError("remittance table has no <thead> -- cannot resolve value column")
    header_rows = thead.find_all("tr")
    if not header_rows:
        raise ValueError("remittance table <thead> has no rows")

    occupied: dict[int, set[int]] = {}
    grid: dict[tuple[int, int], object] = {}
    for row_idx, tr in enumerate(header_rows):
        occupied.setdefault(row_idx, set())
        col_idx = 0
        for cell in tr.find_all(["td", "th"]):
            while col_idx in occupied[row_idx]:
                col_idx += 1
            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)
            for r in range(row_idx, row_idx + rowspan):
                occupied.setdefault(r, set())
                for c in range(col_idx, col_idx + colspan):
                    occupied[r].add(c)
                    grid[(r, c)] = cell
            col_idx += colspan

    # Collect ALL distinct matching cells (deduped by object identity, since
    # a single cell spanning colspan/rowspan appears at multiple (row, col)
    # grid positions but is still only ONE logical column) -> its smallest
    # (first-encountered) column index.
    matches: dict[int, object] = {}
    seen_cell_ids: set[int] = set()
    for (_row_idx, col_idx), cell in grid.items():
        if id(cell) in seen_cell_ids:
            continue
        if _REMIT_USD_HEADER_MARKER in cell.get_text(strip=True).lower():
            seen_cell_ids.add(id(cell))
            matches[col_idx] = cell

    if not matches:
        raise ValueError('remittance table header has no "million US dollar" column')
    if len(matches) > 1:
        raise ValueError(
            f'remittance table header has {len(matches)} columns matching "million US '
            f"dollar\" (column indices {sorted(matches)}) -- ambiguous, refusing to "
            "guess which one is the true monthly value (review R2)"
        )
    return next(iter(matches))


def parse_remittance_table(html: str) -> list[tuple[date, float]]:
    """Pure parse: BB wage-remittance page HTML -> [(as_of, value_usd_mn), ...].

    The table (id="sortableTable", verified live 2026-08-08 -- see
    tests/fixtures/bb_wageremitance.html for a trimmed real capture) is
    FY-ROW-GROUPED: a one-cell header row "YYYY-YYYY" (BD fiscal year,
    July-June -- e.g. "2025-2026" = July 2025 .. June 2026) precedes 12
    three-cell month rows (month name, USD mn, BDT bn) in reverse-
    chronological order (June down to July). July-December belong to the
    FIRST year in the header pair; January-June belong to the SECOND year --
    this is what makes "July = first month of the NEXT FY" correct (a
    "2026-2027" header's July row is July 2026, not July 2027).

    Month labels and years come ENTIRELY from the table's own text -- never
    inferred from the run date (AGENTS.md landmine 26/47). Raises ValueError
    if no table id="sortableTable" is found (2026-08-08 review M3 -- NO
    fallback to "the first <table> on the page": a decoy/unrelated table
    elsewhere on the page could otherwise be silently parsed instead, the
    same class of bug landmine 45 documents for BB's BoP page), it has no
    <tbody>, its <thead> can't resolve the value column (H4), or the parse
    produces ZERO total rows (2026-08-08 review H3 -- a partially-changed
    table, e.g. the newest fiscal-year block's header row rendering as
    <th> instead of a single colspan <td>, would otherwise silently drop
    exactly the months this appender needs while returning cleanly). The
    caller treats any exception here as "parse failed, notify, write
    nothing" and never crashes the aggregate run.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="sortableTable")
    if table is None:
        raise ValueError(
            'no <table id="sortableTable"> found in page HTML (page structure changed?)'
        )
    tbody = table.find("tbody")
    if tbody is None:
        raise ValueError("remittance table has no <tbody>")
    usd_col = _resolve_remittance_value_column(table)

    rows: list[tuple[date, float]] = []
    fy_start_year: int | None = None
    fy_end_year: int | None = None
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        if len(cells) == 1:
            # FY header row, e.g. "2025-2026".
            m = _REMIT_FY_HEADER_RE.search(cells[0].get_text(strip=True))
            if m:
                fy_start_year, fy_end_year = int(m.group(1)), int(m.group(2))
            continue
        # Data row: month name + value columns (USD column resolved above).
        month_name = cells[0].get_text(strip=True)
        month_num = _REMIT_MONTH_NAME_TO_NUM.get(month_name)
        if month_num is None or fy_start_year is None or len(cells) <= usd_col:
            continue
        value_text = cells[usd_col].get_text(strip=True).replace(",", "")
        try:
            value = float(value_text)
        except ValueError:
            continue
        year = fy_start_year if month_num >= 7 else fy_end_year
        rows.append((date(year, month_num, 1), value))

    if not rows:
        raise ValueError(
            "remittance table parsed to ZERO rows despite a valid <table>/<thead>/"
            "<tbody> -- likely a structural change (e.g. an FY header row no "
            "longer matching the expected shape) silently dropped every month; "
            "refusing to return an empty result silently (review H3)"
        )
    return rows


def _previous_month_start(today: date) -> date:
    """First day of the calendar month before ``today``'s month.

    2026-08-08 Opus review M6: used to gate the browser launch -- if the
    previous COMPLETE month's remittance row already exists in
    metric_history_monthly, there's nothing new to fetch (BB publishes one
    new month roughly monthly), so the Playwright fetch can be skipped
    entirely for that run.
    """
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_prev_month.replace(day=1)


def _select_new_remittance_rows(
    parsed: list[tuple[date, float]],
    *,
    existing_as_of: set[date],
    today: date,
    min_as_of: date = _REMITTANCE_APPEND_FROM,
) -> tuple[list[dict], list[str]]:
    """Filter parsed (as_of, value) pairs to genuinely new rows to append:
    as_of >= min_as_of (the backfill's cutoff) and <= today's month-start
    (2026-08-08 review M2 -- rejects a corrupted/future FY header, e.g. a
    "2030-2031" block, from writing a nonsense future as_of; BB cannot have
    published a month that hasn't happened yet), not already in
    metric_history_monthly (append-only), and within [500, 6000] USD mn.

    Returns (rows_to_write, skip_reasons).
    """
    rows: list[dict] = []
    reasons: list[str] = []
    future_floor = today.replace(day=1)
    for as_of, value in parsed:
        if as_of < min_as_of or as_of in existing_as_of:
            continue
        if as_of > future_floor:
            reasons.append(
                f"{_REMITTANCE_MONTHLY_ID}: {as_of} is in the future relative to "
                f"today ({today}) -- skipping (review M2, corrupted/future FY "
                "header guard)"
            )
            continue
        if not (_REMITTANCE_VALUE_MIN <= value <= _REMITTANCE_VALUE_MAX):
            reasons.append(
                f"{_REMITTANCE_MONTHLY_ID}: {as_of} value {value} outside "
                f"[{_REMITTANCE_VALUE_MIN}, {_REMITTANCE_VALUE_MAX}]"
            )
            continue
        as_of_iso = as_of.isoformat()
        rows.append({
            "metric_id": _REMITTANCE_MONTHLY_ID,
            "as_of": as_of_iso,
            "value": value,
            "source": _REMITTANCE_SOURCE,
            "source_as_of": as_of_iso,
        })
    return rows, reasons


def _fetch_remittance_html() -> str:
    """Live-fetch BB's wage-remittance page. BB's Akamai/TSPD JS challenge
    means a plain requests.get() returns the challenge page, not the table
    (verified live 2026-08-08) -- fetchers.html_fetcher.fetch_html clears it
    the same way bb_forex.py does for BB's other econdata pages. Raises
    FetchError on network/challenge failure; the caller treats that as
    "fetch failed, notify, write nothing."

    2026-08-08 review M6: the caller GATES this call behind an existing-rows
    check (_previous_month_start) so it only actually launches a browser on
    the ~1/30 runs where the previous complete month isn't recorded yet --
    the daily-snapshot volume under data/_html/bb_wageremitance_monthly/
    rides fetch_html's existing one-file-per-day convention (same as every
    other fetchers.html_fetcher caller), so no extra retention handling was
    added here (review L7).
    """
    from fetchers.html_fetcher import fetch_html

    snapshot_dir = DATA_DIR / "_html" / "bb_wageremitance_monthly"
    result = fetch_html(
        url=_REMITTANCE_URL, indicator_id="bb_wageremitance_monthly", snapshot_dir=snapshot_dir,
    )
    return result.artifact_path.read_text(encoding="utf-8")


# ============================================================================
# Imports monthly-chart LIVE APPENDER (PR-C, build-brief item 1) --
# imports_usd_mn_monthly froze at as_of=2026-03-01 alongside remittance/
# exports/CPI in the 2026-08-08 incident (AGENTS.md landmine 50) and was
# routed to sentinel.ACCEPTED_STALE_METRIC_IDS because no live derivation
# existed. One now does: BB's own Selected Macroeconomic Indicators (MEI)
# monthly PDF -- the SAME publication 19 other sources-v3.json ids already
# fetch 19x/day via discover=latest_pdf_link -- carries a "Custom based
# import (c&f)" monthly time series (page 22 per the document's own
# numbering, verified live 2026-08-22 against the June-2026 issue: April
# 2026=7066.10, May 2026=6108.22). No new fetcher needed -- this reuses the same
# fetchers.pdf_discovery/fetchers.pdf_fetcher primitives fetch_all.py's
# MEI-driven ids already call, just from inside aggregate_latest so the
# parse can run in the SAME function as the splice-check + append
# (mirroring the remittance leg's shape immediately above).
# ============================================================================

_IMPORTS_MONTHLY_ID = "imports_usd_mn_monthly"
_IMPORTS_MEI_INDEX_URL = "https://www.bb.org.bd/en/index.php/publication/publictn/3/11"
_IMPORTS_SOURCE = "bb_mei_imports_cf"
_IMPORTS_HEADER_MARKER = "custom based import (c&f)"
# Real historical monthly range observed in the June-2026 MEI PDF's FY26
# block: 5222.73 (Aug) .. 7066.10 (Apr) -- generous headroom both ways so a
# genuine step-change month doesn't get rejected as "out of range".
_IMPORTS_VALUE_MIN = 2000.0
_IMPORTS_VALUE_MAX = 15000.0
# imports_usd_mn_monthly's last real (pre-freeze) row is March 2026 -- this
# leg is only ever allowed to append months AFTER that point (landmine 50's
# append-only discipline, applied at the FLOOR rather than relying solely
# on the per-row existing-pairs check, so a corrupted PDF table can never
# smuggle a bogus row into the ALREADY-SETTLED pre-freeze history either).
_IMPORTS_APPEND_FROM = date(2026, 4, 1)
# MANDATORY pre-write splice check (build-brief item 1): the freshly-parsed
# PDF's own reading for some already-recorded month must independently
# agree with the DB's own value for that SAME month within 2% before ANY
# new month is appended. Same all-or-nothing-refusal philosophy as
# landmine 51's yield-ladder guard: a splice that doesn't check out means
# something about EITHER the PDF's table shape OR the DB's history is not
# what this leg assumes, and the correct response is to refuse the whole
# write and notify -- never to publish new months on top of an unverified
# continuity. The anchor month itself is DYNAMIC (Opus review round 1, H1
# -- see _imports_splice_check's docstring for why a hardcoded anchor
# self-destructs at BB's fiscal-year roll).
_IMPORTS_SPLICE_TOLERANCE_PCT = 0.02

# L2 (Opus review round 1): tolerates a trailing footnote digit after the
# P/R letter (e.g. "FY26P2", confirmed live on this same MEI PDF's Import
# LCs opening/settlement columns -- footnote 2 marks their OIMS retrieval
# date). The 'Custom based import (c&f)' columns this parser reads have
# never carried a footnote digit themselves (verified live 2026-08-22),
# but tolerating one costs nothing and removes a plausible future trap if
# BB ever adds one here too.
_IMPORTS_HEADER_RE = re.compile(r"\bFY(\d{2})([PR])\d*\b", re.IGNORECASE)


def _find_imports_table_from_tables(tables: list[list[list]]) -> list[list]:
    """Pure half of _find_imports_table: given every table already
    extracted from every page, return the ONE whose header names a
    "Custom based import (c&f)" column. Raises ValueError if zero or more
    than one matches -- ambiguity must never be guessed (the same
    discipline landmine 45/46 apply to BB's other multi-table pages;
    verified live 2026-08-22 that the document's OTHER "custom-based
    import" mentions -- the executive summary prose on an earlier page,
    and the category-wise breakdown table on a later one -- do not carry
    this exact header text and so never collide with the real target
    here).
    """
    matches: list[list[list]] = [
        table
        for table in tables
        if any(
            cell and _IMPORTS_HEADER_MARKER in re.sub(r"\s+", " ", str(cell)).strip().lower()
            for row in table
            for cell in (row or [])
        )
    ]
    if not matches:
        raise ValueError(
            f"no table with a {_IMPORTS_HEADER_MARKER!r} header found in the MEI PDF "
            "(page structure changed?)"
        )
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} tables match the {_IMPORTS_HEADER_MARKER!r} header -- "
            "ambiguous, refusing to guess (landmine 46 discipline)"
        )
    return matches[0]


def _find_imports_table(pdf) -> list[list]:
    """Scan every page of the MEI PDF for the ONE table whose header names
    a "Custom based import (c&f)" column. Never trusts a fixed page number
    (AGENTS.md landmine 46 -- BB's own page count/numbering has drifted
    between editions before); this document's Table of Contents currently
    names it page 22, which pdfplumber currently resolves to its own page
    25 (a fixed +3 cover/ToC offset that is itself NOT relied upon here).
    Delegates the actual matching to _find_imports_table_from_tables (the
    pure half, unit-tested independently of pdfplumber).
    """
    tables = [table for page in pdf.pages for table in page.extract_tables()]
    return _find_imports_table_from_tables(tables)


def _to_imports_float(cell: str | None) -> float | None:
    if cell is None:
        return None
    text = str(cell).strip()
    if not text or set(text) <= {"-"}:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _parse_imports_p_and_r_rows(
    table: list[list],
) -> tuple[list[tuple[date, float]], dict[date, float]]:
    """Pure transform: the 'Custom based import (c&f)' table's raw
    pdfplumber rows -> (p_rows, r_by_month).

    ``p_rows`` is [(as_of, value_usd_mn), ...] for every REAL, PROVISIONAL
    month row found -- this is the ONLY data this leg ever WRITES.
    ``r_by_month`` is {as_of: value_usd_mn} for the SAME table's REVISED
    prior-year comparator column -- ANCHOR-USE ONLY (Opus review round 2,
    HIGH-1), NEVER written to metric_history_monthly. See
    _imports_splice_check for why the revised column exists as a fallback
    anchor source at all.

    Table shape (verified live 2026-08-22 against the June-2026 MEI PDF; a
    round-2 review simulation additionally confirmed the POST-ROLL shape
    against the real fixture re-labelled forward one fiscal year): a GROUP
    header cell reading 'Custom based import (c&f)' opens a span of TWO
    columns -- the CURRENT fiscal year's provisional actual ('FYnnP') and
    the PRIOR fiscal year's revised comparator ('FYnnR', the SAME months
    one year earlier, printed purely so the document's own prose can quote
    a y/y percentage). Critically, BB does NOT duplicate the monthly block
    when the fiscal year rolls -- there is only ever ONE active monthly
    block at a time, and its OWN two columns simply get RE-LABELLED
    (e.g. 'FY26P'/'FY25R' becomes 'FY27P'/'FY26R' the moment the first FY27
    month is published), never both bracketed at once.

    'Month' sub-header rows re-declare which of the group's two columns is
    'P' vs 'R' for the block of month-name rows that follows -- the table
    interleaves an ANNUAL 'July-June' comparison block first (itself
    P-or-R-labelled but never contributing rows, since 'July-June' isn't a
    month name), then the in-progress fiscal year's monthly block -- so
    which column is 'P' vs 'R' is re-resolved at each 'Month' row, never
    assumed constant for the whole table. Both columns are tracked
    independently: a table could in principle carry only one of the two
    (though every real capture so far has carried both).

    Fiscal year: BD's FY runs July-June (e.g. 'FY26' = July 2025-June
    2026), so a month row's real calendar year is derived from EACH
    column's OWN 'FYnn' label independently -- July-December belong to
    (nn-1), January-June belong to nn. Never inferred from the run clock
    (landmine 26/47). This is why the P and R columns for the SAME row
    resolve to DIFFERENT real months one year apart (e.g. post-roll,
    'July' under 'FY27P'/'FY26R' gives P=2026-07-01 and R=2025-07-01).

    A row whose first cell isn't an exact month name (the annual
    'July-June'/'July-May' summary rows, blank rows, the Source/Note
    footer) is skipped -- it is not a month row.
    """
    p_rows: list[tuple[date, float]] = []
    r_by_month: dict[date, float] = {}
    group_col: int | None = None
    active_p_col: int | None = None
    active_p_fy_end: int | None = None
    active_r_col: int | None = None
    active_r_fy_end: int | None = None

    for row in table:
        if not row:
            continue
        if group_col is None:
            for idx, cell in enumerate(row):
                if cell and _IMPORTS_HEADER_MARKER in re.sub(r"\s+", " ", str(cell)).strip().lower():
                    group_col = idx
                    break
            continue  # the header-search rows (incl. the group-header row itself) carry no data

        first = (row[0] or "").strip()
        if first.lower() == "month":
            # L1 (Opus review round 1): collect EVERY 'P' (and, HIGH-1
            # round 2, every 'R') candidate among the group's two columns
            # and prefer the MAX FY-end-year for each, never just the
            # first one found. Both columns being the SAME letter isn't
            # the normal shape (the group is provisional-vs-revised, so
            # normally the two differ), but a transition edition that
            # briefly shows two columns of the same letter side by side
            # should never silently pick the OLDER one just because it
            # happens to sit first in iteration order.
            p_candidates: list[tuple[int, int]] = []  # (fy_end, col_idx)
            r_candidates: list[tuple[int, int]] = []
            for idx in (group_col, group_col + 1):
                if idx >= len(row) or row[idx] is None:
                    continue
                m = _IMPORTS_HEADER_RE.search(str(row[idx]))
                if not m:
                    continue
                fy_end = 2000 + int(m.group(1))
                (p_candidates if m.group(2).upper() == "P" else r_candidates).append((fy_end, idx))
            active_p_fy_end, active_p_col = max(p_candidates) if p_candidates else (None, None)
            active_r_fy_end, active_r_col = max(r_candidates) if r_candidates else (None, None)
            continue

        if active_p_col is None and active_r_col is None:
            continue
        month_num = _REMIT_MONTH_NAME_TO_NUM.get(first)
        if month_num is None:
            continue
        if active_p_col is not None and active_p_col < len(row):
            value = _to_imports_float(row[active_p_col])
            if value is not None:
                year = active_p_fy_end - 1 if month_num >= 7 else active_p_fy_end
                p_rows.append((date(year, month_num, 1), value))
        if active_r_col is not None and active_r_col < len(row):
            value = _to_imports_float(row[active_r_col])
            if value is not None:
                year = active_r_fy_end - 1 if month_num >= 7 else active_r_fy_end
                r_by_month[date(year, month_num, 1)] = value

    return p_rows, r_by_month


def _require_imports_p_rows(p_rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Raises ValueError if ``p_rows`` (the provisional/writable column) is
    empty -- a structural change silently dropped every month, mirroring
    parse_remittance_table's own H3 guard. The revised column is anchor-
    use only and never gates this check on its own."""
    if not p_rows:
        raise ValueError(
            "imports table parsed to ZERO provisional month rows despite a "
            f"matching {_IMPORTS_HEADER_MARKER!r} header -- likely a structural "
            "change silently dropped every month (mirrors parse_remittance_table's H3 guard)"
        )
    return p_rows


def _parse_imports_rows(table: list[list]) -> list[tuple[date, float]]:
    """Back-compat convenience: the provisional ('P') column only -- what
    this leg WRITES. See _parse_imports_p_and_r_rows for the full P+R
    extraction (needed by the splice check's R-fallback, HIGH-1 round 2)."""
    p_rows, _r_by_month = _parse_imports_p_and_r_rows(table)
    return _require_imports_p_rows(p_rows)


def parse_imports_c_and_f_table(pdf_path: Path) -> tuple[list[tuple[date, float]], dict[date, float]]:
    """Pure parse: the MEI PDF's 'Custom based import (c&f)' table -> (p_rows,
    r_by_month). ``p_rows`` -- [(as_of, value_usd_mn), ...] for every real,
    provisional month found -- is what gets WRITTEN. ``r_by_month`` --
    {as_of: value_usd_mn} for the revised prior-year comparator column --
    is ANCHOR-USE ONLY for _imports_splice_check's fallback (HIGH-1 round
    2); it is NEVER written to metric_history_monthly. Raises ValueError
    if ``p_rows`` is empty -- the caller treats any exception the same way
    parse_remittance_table's caller does: parse failed, notify, write
    nothing.
    """
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        table = _find_imports_table(pdf)
    p_rows, r_by_month = _parse_imports_p_and_r_rows(table)
    return _require_imports_p_rows(p_rows), r_by_month


def _download_mei_index_html() -> str:
    """Fetch the BB MEI publication index page. Verified live 2026-08-22:
    unlike BB's econdata/* pages (landmine 39; the egress note in the PR-C
    build brief), this publication-index page does NOT sit behind BB's F5/
    TSPD JS challenge -- fetch_all.py's OWN 19 MEI-driven indicators fetch
    it the exact same plain-GET way (fetch_all._download_index_html), no
    Playwright needed.
    """
    from urllib.request import Request, urlopen

    from fetchers.tls import ssl_context_for

    req = Request(_IMPORTS_MEI_INDEX_URL, headers={"User-Agent": "EconDelta/3.0"})
    with urlopen(req, timeout=60, context=ssl_context_for(_IMPORTS_MEI_INDEX_URL)) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_imports_mei_pdf() -> Path:
    """Live-fetch the latest BB MEI PDF and return its local path. Mirrors
    _fetch_remittance_html's role for the remittance leg, but for a PDF:
    discover the latest issue link from the publication index (BB reflows
    this monthly), then download+cache it the SAME way fetch_all.py's 19
    other MEI-driven indicators already do (fetchers.pdf_fetcher.fetch_pdf,
    sha256-deduped, so a same-day re-run is a cache hit). Raises FetchError/
    ValueError on network or discovery failure; the caller treats that as
    "fetch failed, notify, write nothing."
    """
    from fetchers.pdf_discovery import discover_latest_pdf
    from fetchers.pdf_fetcher import fetch_pdf

    html = _download_mei_index_html()
    pdf_url, period = discover_latest_pdf(html=html, base_url=_IMPORTS_MEI_INDEX_URL)
    as_of_month = datetime.now(timezone.utc).strftime("%Y-%m")
    result = fetch_pdf(
        url=pdf_url, indicator_id="bb_mei_imports_monthly", snapshot_dir=DATA_DIR,
        as_of_month=as_of_month, period=period,
    )
    return result.artifact_path


def _imports_splice_check(
    pdf_rows: dict[date, float],
    db_rows: dict[date, float],
    pdf_revised_rows: dict[date, float] | None = None,
) -> str | None:
    """MANDATORY pre-write guard (build-brief item 1) -- see the module
    constants above for the full rationale.

    DYNAMIC ANCHOR, PROVISIONAL-THEN-REVISED FALLBACK (Opus review round 2,
    HIGH-1 -- 2026-08-23 fix, replacing round 1's P-only dynamic anchor):
    prefer the LATEST month present in BOTH the PDF's provisional ('P')
    column and the DB's already-appended history -- ``max(pdf_rows &
    db_rows)``. round 1 believed this alone "survives the [fiscal-year]
    roll with no code change needed at rollover time" -- FALSE, per a
    round-2 reviewer simulation against the REAL post-roll table shape:
    BB does NOT duplicate the monthly block when the fiscal year rolls: it
    RE-LABELS the SAME block's two columns in place (e.g. 'FY26P'/'FY25R'
    becomes 'FY27P'/'FY26R' the instant the first FY27 month publishes).
    At that instant, EVERY FY26 month the DB already has (including
    whatever the append-only progression most recently wrote) moves from
    'P' to 'R' in the SAME table update that introduces the first FY27
    month into 'P' -- so ``pdf_rows`` (P only) and ``db_rows`` share NO
    month at all, forever: the DB can never gain an FY27 month without
    passing this check, and the check can never pass without the DB
    already having one. A P-only anchor is a permanent deadlock, not a
    one-run gap.

    The fix: when the provisional column has no overlap with the DB, fall
    back to ``max(pdf_revised_rows & db_rows)`` -- the REVISED column,
    which (structurally, by construction of how BB labels the block)
    carries the SAME months the DB already has, one calendar year removed
    in the table's own printed dates but resolved to their TRUE real
    months by _parse_imports_p_and_r_rows (which reads each column's own
    'FYnn' label independently, never assuming the two differ by exactly
    one year -- though in every observed case they do). This is exactly
    as sound as the provisional-column check: it is still comparing the
    PDF's own printed reading for a specific month against the DB's own
    reading for that SAME month, just sourced from the column BB happens
    to be using for it Right now. The 2% band is identical either way.

    Returns None when the check passes; an explanation string when it
    doesn't (the caller treats a non-None return as "refuse the whole leg
    this run"). No overlap in EITHER column (a narrower, genuinely-
    unresolvable window -- e.g. the DB is missing enough recent history
    that not even the revised column reaches back far enough) is itself a
    fail-closed condition -- there is nothing to verify continuity
    against, so nothing is written, and the caller notifies.
    """
    pdf_revised_rows = pdf_revised_rows or {}
    overlap_p = set(pdf_rows) & set(db_rows)
    if overlap_p:
        anchor = max(overlap_p)
        pdf_value = pdf_rows[anchor]
        anchor_column = "provisional (P)"
    else:
        overlap_r = set(pdf_revised_rows) & set(db_rows)
        if not overlap_r:
            return (
                "splice check unavailable: no month is present in the DB AND "
                "either the PDF's provisional (P) or revised (R) column -- "
                f"refusing to write any new {_IMPORTS_MONTHLY_ID} row without a "
                "shared month to verify continuity against (fail-closed)"
            )
        anchor = max(overlap_r)
        pdf_value = pdf_revised_rows[anchor]
        anchor_column = "revised (R) fallback"
        logger.info(
            "macro monthly append: imports splice check anchored on the "
            "REVISED (R) column at %s -- the provisional (P) column had no "
            "overlap with the DB (expected right after a BB fiscal-year roll)",
            anchor,
        )
    db_value = db_rows[anchor]
    if db_value == 0:
        return f"splice check: db value for {anchor} is 0 -- cannot compute a ratio"
    diff_pct = abs(pdf_value - db_value) / abs(db_value)
    if diff_pct > _IMPORTS_SPLICE_TOLERANCE_PCT:
        return (
            f"splice check FAILED at anchor {anchor} (matched via the "
            f"{anchor_column} column): PDF's c&f reading ({pdf_value}) differs "
            f"from the DB's own value ({db_value}) by {diff_pct:.2%}, exceeding "
            f"the {_IMPORTS_SPLICE_TOLERANCE_PCT:.0%} tolerance -- refusing to "
            "write ANY new month this run"
        )
    return None


def _select_new_imports_rows(
    parsed: list[tuple[date, float]],
    *,
    existing_as_of: set[date],
    today: date,
    min_as_of: date = _IMPORTS_APPEND_FROM,
) -> tuple[list[dict], list[str]]:
    """Filter parsed (as_of, value) pairs to genuinely new rows to append.
    Mirrors _select_new_remittance_rows: as_of >= min_as_of (the March-2026
    freeze point this leg is allowed to grow past), not already in
    metric_history_monthly (append-only), <= today's month-start (never a
    future month), and within the sanity value range.
    """
    rows: list[dict] = []
    reasons: list[str] = []
    future_floor = today.replace(day=1)
    for as_of, value in parsed:
        if as_of < min_as_of or as_of in existing_as_of:
            continue
        if as_of > future_floor:
            reasons.append(
                f"{_IMPORTS_MONTHLY_ID}: {as_of} is in the future relative to "
                f"today ({today}) -- skipping"
            )
            continue
        if not (_IMPORTS_VALUE_MIN <= value <= _IMPORTS_VALUE_MAX):
            reasons.append(
                f"{_IMPORTS_MONTHLY_ID}: {as_of} value {value} outside "
                f"[{_IMPORTS_VALUE_MIN}, {_IMPORTS_VALUE_MAX}]"
            )
            continue
        as_of_iso = as_of.isoformat()
        rows.append({
            "metric_id": _IMPORTS_MONTHLY_ID, "as_of": as_of_iso, "value": value,
            "source": _IMPORTS_SOURCE, "source_as_of": as_of_iso,
        })
    return rows, reasons


# ============================================================================
# M2 growth monthly LIVE APPENDER (PR-C, build-brief item 4) --
# m2_growth_yoy_monthly froze at Feb 2026 (10.52) the same seed-without-
# appender way the CPI trio did (landmine 50); a live daily source now
# exists (m2_growth_yoy_pct, repointed BB econdata/moneysupply HTML table).
# Structurally identical to the CPI trio's single-id derivation (one daily
# id -> one monthly id, month-end vintage check, append-only) -- no
# cross-column equality guard is needed here (unlike the CPI trio's
# general/p2p confusion, landmine 49) since M2 has no sibling column to be
# confused with.
#
# KNOWN GAP (Opus review round 2, MEDIUM-2, 2026-08-23): BB's econdata/
# moneysupply page only ever carries the LATEST published month -- there is
# no archive table to read back-months from, and this PR ships no separate
# backfill source for the Feb-to-Jun 2026 stretch that piled up while this
# id sat frozen. The live leg above will append forward from whatever month
# the page reads as of first run onward, but the chart will show an HONEST
# GAP across Feb-Jun 2026 rather than a silently-interpolated or
# hand-guessed value. If an owner later finds a trustworthy point-in-time
# source for those specific months, backfill it the same
# hand-verified-official-values way scripts/backfill_imports_monthly.py
# and scripts/backfill_cpi_july_2026.py did -- never derive it from a proxy.
#
# CADENCE (Opus review round 2, MEDIUM-2): m2_growth_yoy_monthly's
# freshest-POSSIBLE as_of already lags ~83 days by the time this leg's
# closed-month guard lets it write (BB's own multi-month publication lag on
# top of the guard only accepting an already-closed month) -- the default
# "monthly" cadence the `_monthly`-suffix prefix rule would otherwise assign
# (45-day grace, sentinel/cadence.py) brands every fresh row stale on
# arrival, before the leg has even had a chance to fail for real. Given a
# `quarterly` override below (165-day grace, sentinel/cadence.py's
# _SCRAPER_CADENCE), mirroring imports_usd_mn_monthly's identical rationale
# (build-brief item 1) for the identical reason: a structural source lag,
# not a staleness exemption -- a live leg that genuinely stops working can
# still breach, just not on every healthy run.
# ============================================================================

_M2_DAILY_ID = "m2_growth_yoy_pct"
_M2_MONTHLY_ID = "m2_growth_yoy_monthly"
_M2_MONTHLY_SOURCE = "econdelta_daily_m2"
_M2_VALUE_MIN = -10.0
_M2_VALUE_MAX = 40.0


def _m2_monthly_append_rows(
    *, m2_row: tuple[float, date] | None, existing_pairs: set[tuple[str, date]], today: date,
) -> tuple[list[dict], list[str]]:
    """Pure transform: the latest daily m2_growth_yoy_pct row ->
    metric_history_monthly append candidates. Mirrors
    _cpi_monthly_append_rows' month-end vintage + closed-month + range +
    append-only guards exactly (minus the CPI-specific equality guard,
    which has no M2 analogue)."""
    reasons: list[str] = []
    if m2_row is None:
        return [], [f"{_M2_MONTHLY_ID}: no daily {_M2_DAILY_ID} row available"]
    value, as_of = m2_row
    if as_of != _month_end(as_of):
        return [], [
            f"{_M2_MONTHLY_ID}: latest {_M2_DAILY_ID} as_of={as_of} is not a "
            "month-end vintage -- skipping (not a true monthly reading)"
        ]
    if as_of.replace(day=1) >= today.replace(day=1):
        return [], [
            f"{_M2_MONTHLY_ID}: latest {_M2_DAILY_ID} as_of={as_of} describes the "
            f"CURRENT (not-yet-closed) month relative to today={today} -- skipping "
            "(closed-month guard, mirrors the CPI trio's H2 fix)"
        ]
    if not (_M2_VALUE_MIN <= value <= _M2_VALUE_MAX):
        return [], [f"{_M2_MONTHLY_ID}: value {value} outside [{_M2_VALUE_MIN}, {_M2_VALUE_MAX}]"]
    month_start = as_of.replace(day=1)
    if (_M2_MONTHLY_ID, month_start) in existing_pairs:
        return [], reasons  # append-only: already have this month
    month_start_iso = month_start.isoformat()
    return [{
        "metric_id": _M2_MONTHLY_ID, "as_of": month_start_iso, "value": value,
        "source": _M2_MONTHLY_SOURCE, "source_as_of": as_of.isoformat(),
    }], reasons


def _write_macro_monthly_append(today: date | None = None) -> int:
    """Live appender for the CPI trio + remittance + imports + M2 growth
    chart-feeding monthly series (2026-08-08 incident, landmine 50; imports
    + M2 added PR-C, build-brief items 1 and 4). Returns the number of new
    metric_history_monthly rows written this run.

    0 is the NORMAL outcome on most days: these are monthly-cadence series,
    so a daily run usually finds nothing new (the daily CPI/M2 ids haven't
    rolled to a new month-end vintage yet; BB hasn't published a new
    remittance/imports month yet). The four sub-paths (CPI trio, remittance,
    imports, M2) are independent -- a failure in one degrades gracefully
    and does not block the others; each notifies with its own message so a
    responder can tell which one needs attention.

    ``today`` defaults to the current UTC date (matching this module's other
    "now" usage in main()); pass it explicitly for deterministic tests of
    the H2/M2 closed-month/future-date guards and the M6 fetch-skip gate.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    # No SupabaseReadError import here on purpose (review R1, 2026-08-08
    # re-review): both sub-path try/excepts below catch a broad `Exception`
    # rather than that one type, so nothing in this function names it.
    from utils.supabase_reader import get_metric_history, get_metric_history_monthly
    from utils.supabase_writer import upsert_metric_history_monthly

    rows_to_write: list[dict] = []
    skip_reasons: list[str] = []

    # --- (a) CPI trio, derived from our own daily metric_history -----------
    try:
        general = _latest_value_as_of(get_metric_history("general_inflation", days=1))
        food = _latest_value_as_of(get_metric_history("food_inflation", days=1))
        nonfood = _latest_value_as_of(get_metric_history("non_food_inflation", days=1))
        p2p = _latest_value_as_of(get_metric_history("point_to_point_inflation", days=1))
        existing_cpi: set[tuple[str, date]] = set()
        for monthly_id in _CPI_DAILY_TO_MONTHLY.values():
            for row in get_metric_history_monthly(monthly_id):
                as_of = _parse_monthly_row_date(row.get("as_of"))
                if as_of is not None:
                    existing_cpi.add((monthly_id, as_of))
        cpi_rows, cpi_reasons = _cpi_monthly_append_rows(
            general_row=general, food_row=food, nonfood_row=nonfood, p2p_row=p2p,
            existing_pairs=existing_cpi, today=today,
        )
        rows_to_write.extend(cpi_rows)
        skip_reasons.extend(cpi_reasons)
    except Exception as e:  # noqa: BLE001 -- 2026-08-08 review M1: requests'
        # JSONDecodeError (a 200-with-HTML-body PostgREST/CDN incident)
        # escapes utils.supabase_reader._get uncaught -- it is NOT a
        # SupabaseReadError, so narrowing this except to that one type lets
        # it propagate past this whole function and crash main(). Broadened
        # to Exception so ANY failure in the CPI read sub-path is contained
        # here, never the whole aggregate run.
        logger.warning("macro monthly append: CPI trio read failed: %s", e)
        skip_reasons.append(f"CPI trio: read failed ({type(e).__name__}: {e})")
        notify(
            "warning",
            "aggregate — macro monthly append: CPI read failed",
            "Could not read general_inflation/food_inflation/non_food_inflation/"
            f"point_to_point_inflation from metric_history; CPI trio skipped this "
            f"run. {type(e).__name__}: {e}",
        )

    # --- (b) Remittance, from BB's official monthly table -------------------
    # 2026-08-08 review M6: read metric_history_monthly's EXISTING rows
    # FIRST and gate the browser launch on them -- if the previous complete
    # month is already recorded, there's nothing new to fetch (~29/30 runs),
    # so the ~200s-worst-case Chromium fetch is skipped entirely, well
    # inside the unit's 600s TimeoutStartSec budget (deploy/
    # econdelta-aggregate.service, C1). Review M4: the existing-rows READ
    # failure gets its OWN notify message, distinct from a fetch/parse
    # failure -- "can't check Supabase" and "BB's page changed shape" are
    # different incidents needing different responses.
    #
    # Review R1 (2026-08-08 re-review): broadened from `except
    # SupabaseReadError` to `except Exception`, matching the CPI trio's own
    # M1 fix above -- a JSONDecodeError here (same 200-with-HTML-body class
    # M1 fixed) is NOT a SupabaseReadError and would otherwise escape this
    # try block entirely, aborting _write_macro_monthly_append with an
    # unhandled exception BEFORE its final `return upsert_metric_history_
    # monthly(rows_to_write)` -- discarding the CPI trio's already-computed
    # rows_to_write along with it, even though the CPI sub-path succeeded
    # cleanly above. The M4-specific "remittance read failed" message is
    # unchanged; only the caught exception TYPE is broadened.
    try:
        existing_remit_rows = get_metric_history_monthly(_REMITTANCE_MONTHLY_ID)
    except Exception as e:  # noqa: BLE001 -- review R1: must not let ANY
        # exception here escape and discard the CPI trio's already-computed
        # rows_to_write (see comment above).
        logger.warning("macro monthly append: remittance existing-rows read failed: %s", e)
        skip_reasons.append(f"remittance: existing-rows read failed ({type(e).__name__}: {e})")
        notify(
            "warning",
            "aggregate — macro monthly append: remittance read failed",
            "Could not read remittance_usd_mn_monthly from metric_history_monthly "
            "(the append-only existing-rows check); remittance skipped this run "
            f"(browser fetch never attempted). {type(e).__name__}: {e}",
        )
        existing_remit_rows = None

    if existing_remit_rows is not None:
        existing_remit: set[date] = set()
        for row in existing_remit_rows:
            as_of = _parse_monthly_row_date(row.get("as_of"))
            if as_of is not None:
                existing_remit.add(as_of)

        prev_month_start = _previous_month_start(today)
        if prev_month_start in existing_remit:
            logger.info(
                "macro monthly append: remittance %s already present -- "
                "skipping the live fetch this run (review M6)", prev_month_start,
            )
        else:
            try:
                html = _fetch_remittance_html()
                parsed = parse_remittance_table(html)
            except Exception as e:  # noqa: BLE001 -- fetch/parse must never crash the daily run
                logger.warning("macro monthly append: remittance fetch/parse failed: %s", e)
                skip_reasons.append(f"remittance: fetch/parse failed ({type(e).__name__}: {e})")
                notify(
                    "warning",
                    "aggregate — macro monthly append: remittance fetch/parse failed",
                    "Could not fetch or parse BB's wage-remittance page "
                    f"({_REMITTANCE_URL}); remittance chart-feeding series skipped "
                    f"this run. {type(e).__name__}: {e}",
                )
            else:
                remit_rows, remit_reasons = _select_new_remittance_rows(
                    parsed, existing_as_of=existing_remit, today=today,
                )
                rows_to_write.extend(remit_rows)
                skip_reasons.extend(remit_reasons)
                if not remit_rows and not remit_reasons:
                    # 2026-08-08 review H3: the parse succeeded and returned
                    # SOME rows, but none were new and none were flagged
                    # invalid. Usually just "BB hasn't published a new month
                    # yet" (the normal case most days) -- but it is EXACTLY
                    # the signature a partial structural change would also
                    # produce (e.g. only the newest FY block's header
                    # rendering differently, silently dropping just the
                    # months this appender needs while older blocks still
                    # parse fine). Never silent either way; mirrors D5's H4
                    # "0 rows despite fresh input" check.
                    logger.warning(
                        "macro monthly append: remittance parse returned %d "
                        "row(s) but 0 were new and 0 were flagged invalid -- "
                        "normal if BB hasn't published %s yet; if this persists "
                        "past BB's usual publish window, check for a partial "
                        "table-structure change (review H3)",
                        len(parsed), prev_month_start,
                    )

    # --- (c) Imports, from BB's own MEI PDF (build-brief item 1) -----------
    # Own try/except around the existing-rows read (needed for BOTH the
    # append-only check AND the mandatory splice check below) -- mirrors
    # remittance's M4 pattern: a read failure here must not discard the
    # CPI trio's already-computed rows_to_write, and gets its own distinct
    # notify message so a responder can tell which sub-path needs attention.
    try:
        existing_import_rows = get_metric_history_monthly(_IMPORTS_MONTHLY_ID)
    except Exception as e:  # noqa: BLE001 -- same R1/M1 reasoning as the CPI/remittance sub-paths above
        logger.warning("macro monthly append: imports existing-rows read failed: %s", e)
        skip_reasons.append(f"imports: existing-rows read failed ({type(e).__name__}: {e})")
        notify(
            "warning",
            "aggregate — macro monthly append: imports read failed",
            f"Could not read {_IMPORTS_MONTHLY_ID} from metric_history_monthly "
            "(the append-only/splice-check read); imports skipped this run "
            f"(PDF fetch never attempted). {type(e).__name__}: {e}",
        )
        existing_import_rows = None

    if existing_import_rows is not None:
        existing_imports: dict[date, float] = {}
        for row in existing_import_rows:
            as_of = _parse_monthly_row_date(row.get("as_of"))
            if as_of is None:
                continue
            try:
                existing_imports[as_of] = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue

        # M5 (Opus review round 1): mirror the remittance leg's own M6 gate
        # -- if the previous complete month is already recorded, there's
        # nothing new to fetch, so skip the PDF download+parse entirely
        # this run. Imports' real BB publication lag runs closer to 2
        # months than remittance's ~1, so this gate fires less often here
        # (the fetch is still attempted on more days than remittance's),
        # but it's the same no-op-when-caught-up saving on the days it does.
        prev_month_start = _previous_month_start(today)
        if prev_month_start in existing_imports:
            logger.info(
                "macro monthly append: imports %s already present -- "
                "skipping the PDF fetch this run (mirrors remittance's M6)",
                prev_month_start,
            )
        else:
            try:
                pdf_path = _fetch_imports_mei_pdf()
                parsed_imports, revised_imports = parse_imports_c_and_f_table(pdf_path)
            except Exception as e:  # noqa: BLE001 -- fetch/parse must never crash the daily run
                logger.warning("macro monthly append: imports fetch/parse failed: %s", e)
                skip_reasons.append(f"imports: fetch/parse failed ({type(e).__name__}: {e})")
                notify(
                    "warning",
                    "aggregate — macro monthly append: imports fetch/parse failed",
                    f"Could not fetch or parse BB's MEI PDF ({_IMPORTS_MEI_INDEX_URL}); "
                    f"imports chart-feeding series skipped this run. {type(e).__name__}: {e}",
                )
            else:
                pdf_imports = dict(parsed_imports)
                # HIGH-1 (Opus review round 2): pass the revised (R) column
                # too, as the splice check's fallback anchor source for the
                # BB fiscal-year-roll window (see _imports_splice_check).
                splice_problem = _imports_splice_check(pdf_imports, existing_imports, revised_imports)
                if splice_problem is not None:
                    logger.warning("macro monthly append: %s", splice_problem)
                    skip_reasons.append(splice_problem)
                    notify(
                        "error",
                        "aggregate — macro monthly append: imports splice check failed",
                        splice_problem,
                    )
                else:
                    import_rows, import_reasons = _select_new_imports_rows(
                        parsed_imports, existing_as_of=set(existing_imports), today=today,
                    )
                    rows_to_write.extend(import_rows)
                    skip_reasons.extend(import_reasons)

    # --- (d) M2 growth, derived from our own daily metric_history ----------
    try:
        m2 = _latest_value_as_of(get_metric_history(_M2_DAILY_ID, days=1))
        existing_m2: set[tuple[str, date]] = set()
        for row in get_metric_history_monthly(_M2_MONTHLY_ID):
            as_of = _parse_monthly_row_date(row.get("as_of"))
            if as_of is not None:
                existing_m2.add((_M2_MONTHLY_ID, as_of))
        m2_rows, m2_reasons = _m2_monthly_append_rows(
            m2_row=m2, existing_pairs=existing_m2, today=today,
        )
        rows_to_write.extend(m2_rows)
        skip_reasons.extend(m2_reasons)
    except Exception as e:  # noqa: BLE001 -- same R1/M1 reasoning as the CPI trio sub-path above
        logger.warning("macro monthly append: M2 read failed: %s", e)
        skip_reasons.append(f"M2: read failed ({type(e).__name__}: {e})")
        notify(
            "warning",
            "aggregate — macro monthly append: M2 read failed",
            f"Could not read {_M2_DAILY_ID} from metric_history; M2 growth "
            f"skipped this run. {type(e).__name__}: {e}",
        )

    if skip_reasons:
        logger.info(
            "macro monthly append: %d skip reason(s): %s",
            len(skip_reasons), "; ".join(skip_reasons),
        )

    if not rows_to_write:
        return 0
    return upsert_metric_history_monthly(rows_to_write)


# ============================================================================
# Yield-ladder LIVE APPENDER (Phase 2 of the 2026-08-08 frozen-charts
# incident, AGENTS.md landmine 51)
# ----------------------------------------------------------------------------
# 8 metric_history_monthly ids (the T-bill/T-bond yield curve The Brief's
# ladder chart renders: tbill_91d/182d/364d + yield_2y/5y/10y/15y/20y) froze
# at as_of=2026-04-01 -- a DIFFERENT root cause than Phase 1's series:
# scrapers/bb_auction.py has captured ALL 8 tenors into auction_results
# daily since May 2026, but nothing ever promoted that table into the
# monthly namespace ("live-but-unpromoted", AGENT_LEARNINGS.md Phase 2
# addendum). scripts/backfill_yield_ladder_monthly.py fills the May-July
# 2026 gap (a ONE-TIME, owner-run backfill); THIS function is the ongoing
# writer that keeps the ladder moving forward every month after.
#
# A SIBLING function to _write_macro_monthly_append, not a leg folded into
# it, and called from its OWN try/except at the call site (own distinct
# notify message) -- deliberately so a yield-ladder failure can never
# prevent the CPI/remittance legs from reaching THEIR upsert, and vice
# versa: each leg is a fully separate function call with its own upsert,
# sequenced one after another in main(), so nothing about one leg's
# internals can affect another's.
#
# ALL-OR-NOTHING BY DESIGN (the load-bearing rule here, not append-only
# alone): The Brief's ladder chart takes the UNION of all 8 tenors' dates
# and renders the last TWO with spanGaps -- if only SOME of the 8 tenors
# got a new month written, the chart would fabricate a curve shape that no
# single auction day ever actually quoted. If ANY tenor has NO
# auction_results row on or before the target month-end (not even a
# carried-forward one), this appender writes NOTHING for ANY tenor that
# month and notifies -- carry-forward across months (a tenor's most recent
# auction predating the target month) is expected and fine, since T-bill/
# T-bond tenors auction roughly monthly-to-quarterly, not every calendar
# month; "no row at all" means the source table itself is broken.
#
# Append-only on top of that: a tenor that already has THIS month's row is
# never re-written, even when the all-or-nothing derivation succeeds for
# every tenor -- these are two separate concerns (derivation-completeness
# vs. never-clobber-an-existing-value), checked in that order.
# ============================================================================

# BB auction_results.tenor label -> monthly chart-feeding id (see
# scrapers/bb_auction.py's _TBILL_TENORS/_TBOND_TENORS for the exact label
# strings this table is written with).
_YIELD_TENOR_TO_MONTHLY_ID: dict[str, str] = {
    "91d": "tbill_91d_yield_monthly",
    "182d": "tbill_182d_yield_monthly",
    "364d": "tbill_364d_yield_monthly",
    "2y": "yield_2y_monthly",
    "5y": "yield_5y_monthly",
    "10y": "yield_10y_monthly",
    "15y": "yield_15y_monthly",
    "20y": "yield_20y_monthly",
}
_YIELD_LADDER_SOURCE = "bb_auction"
# Range check per spec: 0 < v < 25 (a T-bond cutoff yield north of 25% or
# non-positive is exactly as suspicious as it would be for the CPI trio's
# own [0, 30) band -- a different ceiling because yields and CPI prints
# occupy different plausible ranges).
_YIELD_VALUE_MIN = 0.0
_YIELD_VALUE_MAX = 25.0
# H1 (2026-08-08 re-review): carry-forward across months is EXPECTED (see
# the module-level docstring), but UNBOUNDED carry-forward is not. Proven
# live: with auction_results dead since some past date, the appender would
# happily keep writing new months using an ever-more-stale cutoff forever
# -- as_of advances every month, the sentinel classes all 8 ids as FRESH
# (it only looks at as_of, never at how old the underlying auction is), and
# CHART_FEEDING_METRIC_IDS's alert tier never fires. A frozen ladder would
# become an INVISIBLY FABRICATED-FRESH one -- worse than the original
# frozen-charts incident, not better. 6 calendar months is generous even
# for the thinnest-traded tenor (20y bonds auction far less often than
# 91d bills) while still catching a genuinely dead source within two
# fiscal quarters.
_YIELD_LADDER_MAX_CARRY_FORWARD_MONTHS = 6

# Distinct notify() titles for the two read-failure points below (2026-08-08
# review M3 -- utils.notifier.notify dedups on (level, title) for 3600s, so
# sharing one title would silently suppress the second failure of a run).
# Hoisted to module constants (2026-08-08 re-review N1) so the ONLY place
# either string is spelled out is here -- a test asserting these two
# constants differ is then a real regression guard against the two messages
# ever being accidentally re-merged, not a tautology re-typing the same
# literals inside the test itself.
_YIELD_EXISTING_ROWS_READ_FAILED_TITLE = (
    "aggregate — macro monthly append: yield ladder existing-rows read failed"
)
_YIELD_AUCTION_READ_FAILED_TITLE = (
    "aggregate — macro monthly append: yield ladder auction_results read failed"
)


def _yield_ladder_staleness_floor(month_end: date) -> date:
    """``month_end`` shifted back ``_YIELD_LADDER_MAX_CARRY_FORWARD_MONTHS``
    calendar months, clamping the day-of-month if the target month is
    shorter (mirrors ``_month_end``'s own use of ``monthrange``). A tenor
    whose latest auction predates this floor is treated as ABSENT by the
    H1 staleness guard, not as a valid (if old) carry-forward value.
    """
    months = _YIELD_LADDER_MAX_CARRY_FORWARD_MONTHS
    year = month_end.year
    month = month_end.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(month_end.day, monthrange(year, month)[1])
    return date(year, month, day)


def _has_auction_in_window(
    auction_rows: list[dict], *, start: date, end: date,
) -> bool:
    """True when at least one row in ``auction_rows`` is a KNOWN tenor whose
    ``auction_date`` falls in ``[start, end]`` inclusive.

    Gates the open-month leg of the yield-ladder appender. Rows for tenors
    the ladder doesn't plot don't count -- they can't move the curve, so
    they must not be what unfreezes it.
    """
    for row in auction_rows:
        if row.get("tenor") not in _YIELD_TENOR_TO_MONTHLY_ID:
            continue
        auction_date = _parse_monthly_row_date(row.get("auction_date"))
        if auction_date is not None and start <= auction_date <= end:
            return True
    return False


def _yield_ladder_rows_for_month(
    auction_rows: list[dict],
    *,
    month_start: date,
    month_end: date,
    existing_pairs: set[tuple[str, date]],
    existing_values: dict[tuple[str, date], float] | None = None,
    existing_source_as_of: dict[tuple[str, date], date] | None = None,
    refresh: bool = False,
    require_newer_source: bool = False,
) -> tuple[list[dict], list[str]]:
    """Pure transform: auction_results rows (already filtered to
    auction_date <= month_end, newest first) -> the 8-tenor yield-ladder
    append candidates for month_start.

    Derivation rule (controller-verified, matches the seed's own
    convention): monthly[tenor] = the cutoff yield of the LATEST auction
    with auction_date <= month_end. Since ``auction_rows`` is newest-first,
    this is simply the FIRST row seen per tenor.

    Stage 1 -- ALL-OR-NOTHING DERIVATION: if any tenor has no matching row,
    its latest auction predates the H1 staleness floor (more than
    ``_YIELD_LADDER_MAX_CARRY_FORWARD_MONTHS`` months before month_end --
    see that constant's comment for why unbounded carry-forward is a
    correctness bug, not just a staleness one), or its cutoff fails the
    [0, 25) range check, NO rows are returned for ANY tenor and a single
    reason names every failing tenor (never partial -- see the module-level
    docstring above for why a partial ladder is worse than no update at
    all).

    Stage 2 -- APPEND-ONLY FILTER (only reached if Stage 1 fully succeeds):
    drops any (monthly_id, month_start) pair already present in
    ``existing_pairs`` -- never overwrites an already-written value, even
    though Stage 1 re-derived it. A SEPARATE concern from Stage 1: Stage 1
    guards against fabricating a partial CURVE; this guards against
    clobbering an existing value for a tenor that happens to already have
    this exact month.

    ``refresh=True`` relaxes Stage 2: "already written" must not mean
    "final". A pair is still dropped when its stored value already EQUALS
    the freshly derived one (so an ordinary day writes nothing at all and
    ``ingested_at`` doesn't churn), but a CHANGED value is re-emitted and
    the upsert updates it in place. ``existing_values`` supplies the stored
    numbers for that comparison; it is ignored entirely when ``refresh`` is
    False.

    Two callers use it, for two different reasons.

    The OPEN month (2026-08-31) is a moving figure by definition -- every
    new auction inside the month changes its curve.

    The COMPLETED month (2026-09-02, landmine 54) is not moving, but the
    runs that happened INSIDE it could never see its final auction: the
    aggregate fires at 03:00 BDT, hours before that day's auction is
    published. Left append-only, M-1's rung froze at the second-to-last
    auction of the month and no later run would ever correct it.

    ``require_newer_source=True`` is the extra guard that makes refreshing
    a CLOSED month safe, and only the completed-month leg passes it. A row
    is emitted only when the derived ``source_as_of`` is STRICTLY NEWER
    than the stored one (``existing_source_as_of``). That makes the rewrite
    monotonic in the underlying auction date, so it can only ever move a
    rung FORWARD onto a later auction -- never re-derive a settled month
    from degraded data. If ``auction_results`` were to lose rows, the
    derivation would fall back to an OLDER auction, the guard would fail,
    and the stored value would survive untouched. A missing or unparseable
    stored ``source_as_of`` also fails the guard: the safe direction for
    history is to leave it alone.

    This cannot walk backwards through history either: the caller only ever
    hands this function the open month and the one immediately before it.

    ``source_as_of`` on each written row is the REAL auction_date for that
    tenor (2026-08-08 review M2), not month_start -- matches the CPI leg's
    true-vintage convention and makes the H1 staleness guard auditable
    directly from the row (a chart-feeding row whose source_as_of trails
    its as_of by nearly 6 months is exactly the H1 scenario approaching its
    limit).

    Returns (rows_to_write, skip_reasons) -- an empty reasons list with an
    empty (or partial, 1-8 row) rows list means "derivation fully
    succeeded, some/all rows already existed" (the normal append-only
    no-op case, not a problem); a non-empty reasons list means the
    all-or-nothing rule refused the WHOLE month.
    """
    latest_by_tenor: dict[str, tuple[date, float]] = {}
    for row in auction_rows:
        tenor = row.get("tenor")
        if tenor not in _YIELD_TENOR_TO_MONTHLY_ID:
            continue
        if tenor in latest_by_tenor:
            # auction_rows is newest-first; the first row seen per tenor
            # IS the latest auction on or before month_end.
            continue
        auction_date = _parse_monthly_row_date(row.get("auction_date"))
        if auction_date is None or auction_date > month_end:
            continue
        try:
            cutoff = float(row["cutoff"])
        except (KeyError, TypeError, ValueError):
            continue
        latest_by_tenor[tenor] = (auction_date, cutoff)

    staleness_floor = _yield_ladder_staleness_floor(month_end)
    problems: list[str] = []
    values: dict[str, float] = {}
    auction_dates: dict[str, date] = {}
    for tenor, monthly_id in _YIELD_TENOR_TO_MONTHLY_ID.items():
        found = latest_by_tenor.get(tenor)
        if found is None:
            problems.append(
                f"{tenor} ({monthly_id}): no auction_results row on or before {month_end}"
            )
            continue
        auction_date, cutoff = found
        if auction_date < staleness_floor:
            problems.append(
                f"{tenor} ({monthly_id}): latest auction_date {auction_date} is more "
                f"than {_YIELD_LADDER_MAX_CARRY_FORWARD_MONTHS} months before "
                f"{month_end} -- treating as absent (H1 staleness guard)"
            )
            continue
        if not (_YIELD_VALUE_MIN < cutoff < _YIELD_VALUE_MAX):
            problems.append(
                f"{tenor} ({monthly_id}): latest cutoff {cutoff} outside "
                f"({_YIELD_VALUE_MIN}, {_YIELD_VALUE_MAX})"
            )
            continue
        values[tenor] = cutoff
        auction_dates[tenor] = auction_date

    if problems:
        return [], [
            f"yield ladder incomplete for {month_start.isoformat()} -- writing "
            "NOTHING for any tenor this month (all-or-nothing, landmine 51): "
            + "; ".join(problems)
        ]

    month_start_iso = month_start.isoformat()
    stored = existing_values or {}
    stored_source = existing_source_as_of or {}
    rows: list[dict] = []
    for tenor, monthly_id in _YIELD_TENOR_TO_MONTHLY_ID.items():
        if (monthly_id, month_start) in existing_pairs:
            if not refresh:
                continue  # append-only: already have this tenor for this month
            prior = stored.get((monthly_id, month_start))
            if prior is not None and prior == values[tenor]:
                continue  # unchanged since the last run -- no write
            if require_newer_source:
                # Closed month: only ever move a rung FORWARD onto a later
                # auction. No stored date, or one that is not strictly older
                # than the derived one, means leave history alone.
                prior_source = stored_source.get((monthly_id, month_start))
                if prior_source is None or prior_source >= auction_dates[tenor]:
                    continue
        rows.append({
            "metric_id": monthly_id,
            "as_of": month_start_iso,
            "value": values[tenor],
            "source": _YIELD_LADDER_SOURCE,
            "source_as_of": auction_dates[tenor].isoformat(),
        })
    return rows, []


def _write_yield_ladder_monthly_append(today: date | None = None) -> int:
    """Live appender for the 8-tenor yield-ladder chart-feeding monthly
    series (Phase 2, landmine 51). Returns the number of
    metric_history_monthly rows written this run (0-16).

    TWO legs, in this order:

    * **Completed month** M-1 (``_previous_month_start``, the same helper
      Phase 1's remittance leg uses) -- REFRESHED, not append-only, since
      2026-09-02 (landmine 54). A month that is over can no longer acquire
      auctions, but the runs that happened INSIDE it could not see its last
      one: the aggregate fires at 03:00 BDT, before that day's auction is
      published. So M-1's rung is re-derived until it matches, which takes
      exactly one run of month M and is a no-op read thereafter.
    * **Open month** M (``today.replace(day=1)``, cut off at ``today``, not
      at the calendar month end which is still in the future) -- REFRESHED
      on every run. Added 2026-08-31: the previous behaviour published only
      completed months, so on the 31st of a month The Brief's ladder still
      plotted the curve from two months back while auction_results already
      held that month's real cutoffs. A month-old curve presented as the
      latest reading is a wrong number on the page, not merely a late one.

    Both legs share ONE ``auction_results`` read (through ``today``, a
    superset of both cutoffs -- each leg re-filters on its own
    ``month_end``), so the extra leg costs no extra round-trip.

    The open-month leg does mean the read now happens on EVERY run rather
    than only on the days a completed month was still unwritten (the L3
    note below). Writes stay rare regardless: the refresh path emits a row
    only when a tenor's derived value actually DIFFERS from the stored one,
    so a day with no new auction writes nothing.

    Pure DB reads only -- no Playwright, no live HTTP fetch (unlike the
    remittance leg). Two separate read failure points, each with its OWN
    distinct notify TITLE (2026-08-08 review M3 -- the two originally
    shared one title; ``utils.notifier.notify`` dedups on ``(level,
    title)`` for 3600s, so the second failure of a run would have been
    silently suppressed by the first): the append-only existing-rows
    check, and the auction_results read itself.

    L3 (2026-08-08 re-review): each run re-reads auction_results' FULL
    history through the cutoff (not just rows since the last check) --
    acceptable for now given the table's size, but a real cost if it grows
    much larger.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    from utils.supabase_reader import get_auction_results_through, get_metric_history_monthly
    from utils.supabase_writer import upsert_metric_history_monthly

    prev_month_start = _previous_month_start(today)
    prev_month_end = _month_end(prev_month_start)
    # The open month is cut off at TODAY, never at its calendar month end --
    # that date is still in the future, and cutting there would silently
    # promise "the whole month" while only part of it has happened.
    open_month_start = today.replace(day=1)
    monthly_ids = list(_YIELD_TENOR_TO_MONTHLY_ID.values())

    try:
        existing: set[tuple[str, date]] = set()
        existing_values: dict[tuple[str, date], float] = {}
        existing_source_as_of: dict[tuple[str, date], date] = {}
        for monthly_id in monthly_ids:
            for row in get_metric_history_monthly(monthly_id):
                as_of = _parse_monthly_row_date(row.get("as_of"))
                if as_of is None:
                    continue
                existing.add((monthly_id, as_of))
                # Stored source_as_of backs the completed month's
                # monotonicity guard. Absent or unparseable leaves the pair
                # out of the map, which FAILS the guard and leaves the stored
                # row alone -- the safe direction for a closed month.
                source_as_of = _parse_monthly_row_date(row.get("source_as_of"))
                if source_as_of is not None:
                    existing_source_as_of[(monthly_id, as_of)] = source_as_of
                # Stored value backs the "did it actually change?" check. A
                # row whose value won't coerce is left out of the map, which
                # makes the comparison miss and the row get rewritten -- the
                # safe direction for the OPEN month (and still gated by the
                # guard above for the completed one).
                try:
                    existing_values[(monthly_id, as_of)] = float(row["value"])
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception as e:  # noqa: BLE001 -- R1/M1 lesson: broad on purpose,
        # a JSONDecodeError-class failure here must not escape and crash
        # the caller (or block the CPI/remittance legs, which run as fully
        # separate function calls regardless of what happens here).
        logger.warning("yield ladder append: existing-rows read failed: %s", e)
        notify(
            "warning",
            _YIELD_EXISTING_ROWS_READ_FAILED_TITLE,
            "Could not read metric_history_monthly for the yield-ladder "
            f"append-only check; yield ladder skipped this run. {type(e).__name__}: {e}",
        )
        return 0

    try:
        # ONE read for both legs: through `today` is a superset of the
        # completed month's cutoff, and _yield_ladder_rows_for_month
        # re-filters on the month_end it is handed. L3: still a
        # full-history re-read -- see this function's docstring.
        auction_rows = get_auction_results_through(today)
    except Exception as e:  # noqa: BLE001 -- same reasoning as above.
        logger.warning("yield ladder append: auction_results read failed: %s", e)
        notify(
            "warning",
            _YIELD_AUCTION_READ_FAILED_TITLE,
            f"Could not read auction_results (through {today}) for the "
            f"yield-ladder append; skipped this run. {type(e).__name__}: {e}",
        )
        return 0

    rows: list[dict] = []
    reasons: list[str] = []

    # Leg 1 -- the completed month, REFRESHED until it settles.
    #
    # Append-only until 2026-09-02 (landmine 54). A month's LAST auction is
    # invisible to every run that happens inside that month: the aggregate
    # fires at 03:00 BDT, hours before the day's auction is published, so
    # August's final run only ever saw auctions through 24-27 Aug. The first
    # run of September then skipped August entirely -- its rows existed -- and
    # the 31 Aug auction was lost from August's rung permanently. It happened
    # for real: August's three bill rungs sat at the 24 Aug cutoffs (8.829 /
    # 8.92 / 9.07) while the 31 Aug auction had cleared ~10bp lower (8.7289 /
    # 8.8199 / 8.8901), so The Brief's ladder drew a month-end curve that was
    # never the month's end.
    #
    # `require_newer_source=True` is what makes rewriting a CLOSED month
    # safe, and it is the whole difference from the open month's refresh: a
    # rung moves only onto a STRICTLY LATER auction date. The rewrite is
    # therefore monotonic -- it can carry a month forward onto its final
    # auction, and it cannot re-derive a settled month from degraded data. If
    # auction_results ever lost rows, the derivation would fall back to an
    # older auction, the guard would fail, and the stored value would stand.
    #
    # Self-terminating too: one run of month M corrects the month just ended,
    # and every run after that finds nothing newer and writes nothing.
    prev_rows, prev_reasons = _yield_ladder_rows_for_month(
        auction_rows,
        month_start=prev_month_start,
        month_end=prev_month_end,
        existing_pairs=existing,
        existing_values=existing_values,
        existing_source_as_of=existing_source_as_of,
        refresh=True,
        require_newer_source=True,
    )
    rows.extend(prev_rows)
    reasons.extend(prev_reasons)

    # Leg 2 -- the open month, refreshed every run. Its all-or-nothing
    # derivation and staleness floor are the completed month's, unchanged;
    # only Stage 2 differs (see `refresh` in _yield_ladder_rows_for_month).
    #
    # Gated on the open month having had at least ONE auction of its own.
    # Without this gate the first days of a month would publish a rung
    # derived ENTIRELY by carry-forward -- i.e. a byte-identical copy of the
    # month before it, drawn as a second line on the chart. Nothing is lost
    # by waiting: if this month has had no auction, the newest real curve
    # IS last month's, and last month's rung already plots it.
    if _has_auction_in_window(auction_rows, start=open_month_start, end=today):
        open_rows, open_reasons = _yield_ladder_rows_for_month(
            auction_rows,
            month_start=open_month_start,
            month_end=today,
            existing_pairs=existing,
            existing_values=existing_values,
            refresh=True,
        )
        rows.extend(open_rows)
        reasons.extend(open_reasons)
    else:
        logger.info(
            "yield ladder append: no auction_results row in %s..%s -- open-month "
            "leg deferred (its curve would be a pure carry-forward duplicate of "
            "the completed month)", open_month_start, today,
        )

    if reasons:
        logger.warning("yield ladder append: %s", "; ".join(reasons))
        notify(
            "warning",
            "aggregate — macro monthly append: yield ladder incomplete",
            "; ".join(reasons),
        )

    if not rows:
        return 0
    return upsert_metric_history_monthly(rows)


# ============================================================================
# Daily yield-curve DERIVATION from auction_results (PR-C, build-brief item
# 3 -- AGENTS.md landmine 49's two-yield-column trap). The BB treasury page
# (monetaryactivity/treasury) prints bond rows with BOTH a "Cut off yield"
# AND a "Standard/Devolvement Yield" column (bills have no Standard
# column, and were never affected) -- html_table_row/the LLM fallback had
# no deterministic way to choose between the two adjacent columns, so the
# scrape flapped: tbond_5y_yield shipped 9.15 (=Standard; the real cut-off
# was 9.3496), and tbond_10y_yield flapped 10.24 -> 10.25 -> 9.42
# (=Standard) -> 9.234 across successive runs.
#
# Fix: derive all 5 ids from auction_results instead -- the SAME table
# scrapers/bb_auction.py already writes daily, and the SAME derivation rule
# _write_yield_ladder_monthly_append already uses for its monthly ladder
# (latest cutoff per tenor at/before the target date). The 5 corresponding
# sources-v3.json scrape entries are REMOVED in this same PR -- this
# function is now the ONLY writer for these 5 ids. Unlike the monthly
# ladder, this is NOT all-or-nothing: each of the 5 ids was already
# independently scraped before (a term-structure "curve" built from 5
# separately-latest treasury-page reads already had this property), so a
# tenor with no auction_results row is simply left out of the write rather
# than blocking the other 4. No unbounded-carry-forward risk either
# (landmine 51's H1 lesson): as_of is set to the REAL auction date, never
# advanced to "today" or "month start", so a dead auction_results table
# would leave as_of frozen at the last real auction -- correctly caught by
# the ordinary freshness sentinel rather than invisibly reading as fresh.
# ============================================================================

_DAILY_YIELD_TENOR_TO_ID: dict[str, str] = {
    "91d": "bill_bond_rates",
    "182d": "tbill_182d_yield",
    "364d": "tbill_364d_yield",
    "5y": "tbond_5y_yield",
    "10y": "tbond_10y_yield",
}
# Same range as the yield ladder's own guard (landmine 51) -- yields and
# CPI prints occupy different plausible ranges, but this ceiling is shared
# with that sibling derivation on purpose (same underlying table, same
# tenors, same sanity bound).
_DAILY_YIELD_VALUE_MIN = 0.0
_DAILY_YIELD_VALUE_MAX = 25.0


def _daily_yields_from_auction_rows(
    auction_rows: list[dict],
) -> tuple[dict[str, float], dict[str, date]]:
    """Pure transform: auction_results rows (newest-first, auction_date <=
    today) -> ({metric_id: latest cutoff}, {metric_id: auction_date}) for
    the 5 daily yield ids landmine 49 retired from HTML/LLM scraping.

    Reuses the same derivation rule as _yield_ladder_rows_for_month (the
    first row seen per tenor in a newest-first list IS the latest auction
    on or before the cutoff date) but with none of that function's month-
    window / all-or-nothing semantics -- see the module-level comment above
    for why these 5 ids are independent rather than a bundle.
    """
    values: dict[str, float] = {}
    source_as_of: dict[str, date] = {}
    seen_tenors: set[str] = set()
    for row in auction_rows:
        tenor = row.get("tenor")
        metric_id = _DAILY_YIELD_TENOR_TO_ID.get(tenor)
        if metric_id is None or tenor in seen_tenors:
            continue
        seen_tenors.add(tenor)
        auction_date = _parse_monthly_row_date(row.get("auction_date"))
        if auction_date is None:
            continue
        try:
            cutoff = float(row["cutoff"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (_DAILY_YIELD_VALUE_MIN < cutoff < _DAILY_YIELD_VALUE_MAX):
            continue
        values[metric_id] = cutoff
        source_as_of[metric_id] = auction_date
    return values, source_as_of


def _derive_daily_yields_from_auctions(
    today: date | None = None,
) -> tuple[dict[str, float], dict[str, date]]:
    """Read auction_results and return the (values, source_as_of overrides)
    for the 5 daily yield ids -- the caller merges these into `data` /
    `source_as_of_map` BEFORE the single metric_history upsert in main(),
    so no separate Supabase write call is needed here (the existing
    source_as_of_map mechanism, landmine 26/47, already handles per-metric
    as_of overrides on that one write).

    On a read failure, returns ({}, {}) and notifies -- never crashes the
    daily run (same containment philosophy as every other appender in this
    module).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    from utils.supabase_reader import get_auction_results_through

    try:
        auction_rows = get_auction_results_through(today)
    except Exception as e:  # noqa: BLE001 -- same containment philosophy as
        # every other Supabase-read sub-path in this module (M1/R1 lesson).
        logger.warning("daily yield derivation: auction_results read failed: %s", e)
        notify(
            "warning",
            "aggregate — daily yield derivation: auction_results read failed",
            "Could not read auction_results for the 5 daily yield ids "
            "(bill_bond_rates/tbill_182d_yield/tbill_364d_yield/tbond_5y_yield/"
            f"tbond_10y_yield); those ids get no value this run. {type(e).__name__}: {e}",
        )
        return {}, {}

    values, source_as_of = _daily_yields_from_auction_rows(auction_rows)
    missing = sorted(set(_DAILY_YIELD_TENOR_TO_ID.values()) - set(values))
    if missing:
        logger.warning("daily yield derivation: no auction_results row for %s", missing)
    return values, source_as_of


# EconDelta indicator-id ↔ brief metric_id alias map. The brief expects a
# specific naming convention per section (`macro_*`, `remit_*`, `fiscal_*`,
# `banking_*`, `food_*`); EconDelta keeps its own indicator IDs authoritative.
# Pure 1:1 aliases (no unit conversion) live here.
BRIEF_ALIASES: dict[str, str] = {
    # macro
    "macro_cpi_food":      "food_inflation",
    "macro_cpi_headline":  "general_inflation",
    "macro_cpi_nonfood":   "non_food_inflation",
    # YoY % credit growth — repointed PR-C (build-brief item 4) to BB's live
    # econdata/monetarysurvey HTML page ("Claims on Private Sector (DMBs)"),
    # not derived from the absolute private_sector_credit BDT-crore value.
    #
    # OWNER DECISION FLAG (2026-08-22, PR-C): June 2026 has a genuine
    # conflict between BB's own machine-readable table and unanimous press
    # coverage of the same concept. BB's econdata/monetarysurvey table
    # ("Claims on Private Sector (DMBs)" YoY column) reads 4.53%; every
    # press outlet quoted BB's own ADJUSTED headline figure of 4.47% for
    # the same month. This PR ships 4.53% (the BB table -- machine-
    # readable, matches the series' own prior-month trajectory: Mar 4.72,
    # Apr 4.75, May 4.98) as the live value, per the source scout's
    # recommendation. Do NOT average the two, and do NOT silently swap to
    # 4.47% without a fresh sign-off -- this is a data-source judgment
    # call on a number The Brief publishes as "private credit growth", not
    # an engineering decision. See AGENT_LEARNINGS.md/AGENTS.md landmine 52
    # for the fuller writeup.
    "macro_credit_growth": "private_sector_credit_yoy_pct",
    # remittance — bn→mn unit conversion is in BRIEF_CONVERSIONS below.
    # fiscal — crore→trillion conversions are in BRIEF_CONVERSIONS below.
    # NBR FYTD canonical: tax_revenue from the BB PDF (deterministic parse,
    # 5% anomaly threshold). News corroborators (nbr_fytd_collected_tbs,
    # nbr_fytd_collected_dailystar) retired 2026-05-25 — both tag-listing
    # pages drifted onto articles covering different fiscal-year windows,
    # so the cross-check flapped.
    "nbr_fytd_collected_cr":    "tax_revenue",
    # banking primitives
    "banking_broad_money":      "broad_money",
    "banking_reserve_money":    "reserve_money",
    "banking_money_multiplier": "money_multiplier",
    "banking_excess_liquid":    "excess_liquid_asset_total_minimum",
    "banking_deposits":         "deposits_of_the_system",
    "banking_call_money_rate":  "call_money_rate",
    # banking ratios (FSAR — quarterly)
    "banking_npl_pct":          "gross_npl_ratio",
    "banking_car_pct":          "banking_sector_crar",
    # money market — yield headline (daily)
    "tbill_91d_yield_pct":      "bill_bond_rates",
    "gsec_next_auction_cr":     "gsec_auction",
    # money market — brief metric_id forms (the brief's tbond builder
    # uses ``tbond_tbill_91d``; brief's nbr/dam builders use ``dam_*``)
    "tbond_tbill_91d":          "bill_bond_rates",
    # multi-tenor T-Bill / T-Bond yields — feed §07 yield curve chart
    "tbond_tbill_182d":         "tbill_182d_yield",
    "tbond_tbill_364d":         "tbill_364d_yield",
    "tbond_bond_5y":            "tbond_5y_yield",
    "tbond_bond_10y":           "tbond_10y_yield",
    # DAM retail food prices (daily, BDT/kg or BDT/4-pcs for eggs)
    "food_rice_coarse_bdt":     "food_rice_coarse",
    "food_atta_packet_bdt":     "food_atta_packet",
    "food_egg_red_bdt":         "food_egg_red",
    "food_chicken_farm_bdt":    "food_chicken_farm",
    "food_oil_soybean_bdt":     "food_oil_soybean",
    "food_onion_local_bdt":     "food_onion_local",
    "food_lentil_moong_bdt":    "food_lentil_moong",
    "food_sugar_local_bdt":     "food_sugar_local",
    # DAM retail food prices — brief metric_id forms (`dam_*`)
    "dam_rice_coarse":          "food_rice_coarse",
    "dam_lentil":               "food_lentil_moong",
    "dam_oil":                  "food_oil_soybean",
    "dam_sugar":                "food_sugar_local",
    "dam_onion":                "food_onion_local",
    "dam_egg":                  "food_egg_red",
    "dam_chicken":              "food_chicken_farm",
    "dam_flour":                "food_atta_packet",
}

# Aliases that need a unit conversion (source unit → brief unit).
# Format: brief_key → (source_key, multiplier).
BRIEF_CONVERSIONS: dict[str, tuple[str, float]] = {
    # T-Bill / T-Bond outstanding: gsom reports BDT million; brief expects
    # BDT crore (1 crore = 10 million → multiplier 0.1).
    "tbill_outstanding_cr": ("treasury_bill_outstanding", 0.1),
    "tbond_outstanding_cr": ("treasury_bond_outstanding", 0.1),
    # Fiscal: EconDelta indicators are BDT crore, brief renders BDT trillion.
    # 1 trillion BDT = 100,000 crore → multiplier 0.00001.
    "fiscal_nbr_collected_trn":  ("tax_revenue", 0.00001),
    "fiscal_govt_borrow_trn":    ("domestic_borrowing_for_budget_deficit", 0.00001),
    "fiscal_foreign_borrow_trn": ("foreign_borrowing_for_budget_deficit", 0.00001),
    "fiscal_bank_borrow_trn":    ("bank_borrowing_for_deficit_financing", 0.00001),
    "fiscal_nsc_outstanding":    ("nsc_outstanding", 0.00001),
    # Remittance: EconDelta source is USD billion, brief renders USD million.
    # 1 billion = 1,000 million → multiplier 1000.
    "remit_monthly_mn": ("monthly_remittance", 1000.0),
    "remit_fy_mn":      ("fy_remittance", 1000.0),
    # NBR component decomposition (Phase 3.2): articles report BDT crore,
    # brief's §12 expects BDT bn. 1 bn = 100 crore → multiplier 0.01.
    "nbr_vat_bn":       ("nbr_vat_collected_cr", 0.01),
    "nbr_it_bn":        ("nbr_it_collected_cr", 0.01),
    "nbr_customs_bn":   ("nbr_customs_collected_cr", 0.01),
}

# The four metric_history ids minted from the ``money_market_ref_rate``
# indicator's dict value (DOMMR/BOFR Overnight + 1W). Must stay identical to
# parsers/html_money_market_ref_rate._SERIES_KEYS — a drift-guard test
# (tests/test_dommr_bofr_fanout.py) enforces the equality, because these keys
# are BOTH the fan-out mint allow-list in ``_flatten_dict_indicators`` AND
# the date-propagation target list in ``_build_source_as_of_map``. Missing a
# key in the latter silently re-forges that fanned row's ``as_of`` to the run
# date (the landmine 26/47 class this source's real value-dating exists to
# prevent).
MONEY_MARKET_REF_RATE_FANOUT_IDS: tuple[str, ...] = (
    "dommr",
    "dommr_1w",
    "bofr",
    "bofr_1w",
)


def _flatten_dict_indicators(data: dict) -> None:
    """Explode dict-shaped indicator values into per-key numeric entries.

    Phase 3.1: ``dse_sector_heat`` arrives as ``{Banks: -1.4, NBFI: -1.1, ...}``
    from the parser, but Supabase ``metric_history`` only persists numerics
    (the writer filters dicts/strings). We mint one numeric key per sector
    so each lands in Supabase and the brief can read them via the standard
    history path. Idempotent: per-sector keys already in `data` are left
    alone.

    Same treatment for ``call_money_rate``: the parser returns a 4-tenor
    dict ``{1D, 7D, 14D, 90D}``; we fan it out to per-tenor numeric keys
    (``call_money_rate_1d``, ``_7d``, ``_14d``, ``_90d``) AND promote the
    1D (overnight) value to the scalar ``call_money_rate`` itself — BB
    convention: "call money rate" without modifier means overnight. The
    promotion replaces the dict in place so the Supabase writer's
    scalar-only filter persists the headline rate, which in turn makes
    the existing ``BRIEF_ALIASES["banking_call_money_rate"] = "call_money_rate"``
    mapping start working.
    """
    sector_heat = data.get("dse_sector_heat")
    if isinstance(sector_heat, dict):
        for sector, pct in sector_heat.items():
            if not isinstance(pct, (int, float)):
                continue
            key = "dse_sector_heat_" + str(sector).lower().replace(" ", "_")
            if key not in data:
                data[key] = float(pct)

    call_money = data.get("call_money_rate")
    if isinstance(call_money, dict):
        for tenor, rate in call_money.items():
            if not isinstance(rate, (int, float)):
                continue
            key = "call_money_rate_" + str(tenor).lower()
            if key not in data:
                data[key] = float(rate)
        overnight = call_money.get("1D")
        if isinstance(overnight, (int, float)):
            # Mutate dict → scalar so the Supabase writer (scalars only)
            # persists the headline overnight rate as ``call_money_rate``.
            data["call_money_rate"] = float(overnight)

    # Same treatment for ``money_market_ref_rate`` (DOMMR/BOFR): the parser
    # returns {"dommr", "dommr_1w", "bofr", "bofr_1w"} → mint each as its own
    # top-level metric_history id, then promote the DOMMR Overnight rate as
    # the parent indicator's scalar headline (call_money precedent: "the"
    # money-market reference rate without modifier means DOMMR overnight).
    # UNLIKE the call_money fan-out (which prefixes every dict key), these
    # keys land as top-level ids verbatim — so minting is restricted to the
    # canonical allow-list; an unexpected key (LLM-path drift, structure
    # change) is never published under a made-up id. Their as_of dates are
    # propagated separately in ``_build_source_as_of_map`` — fanned keys are
    # NOT v3-registry ids, so without that propagation the writer would forge
    # run-date stamps on all four (landmine 26/47 class).
    mmrr = data.get("money_market_ref_rate")
    if isinstance(mmrr, dict):
        for series_id in MONEY_MARKET_REF_RATE_FANOUT_IDS:
            rate = mmrr.get(series_id)
            if isinstance(rate, bool) or not isinstance(rate, (int, float)):
                continue
            if series_id not in data:
                data[series_id] = float(rate)
        headline = mmrr.get("dommr")
        if isinstance(headline, (int, float)) and not isinstance(headline, bool):
            # Mutate dict → scalar so the Supabase writer (scalars only)
            # persists the DOMMR overnight rate as ``money_market_ref_rate``.
            data["money_market_ref_rate"] = float(headline)
        else:
            # Partial-null day (2026-08-28 review finding 4): the LLM-extract
            # fallback legitimately preserves null tenors, so a dict with
            # dommr null but healthy siblings reaches here. Leaving the
            # parent AS A DICT would have the writer's scalar-only filter
            # silently drop it — a zero-row day on the headline series buried
            # in a writer debug line while the siblings look healthy (the
            # PR-#31 failure class). Remove the parent key and ANNOUNCE the
            # hole instead.
            logger.warning(
                "money_market_ref_rate: DOMMR overnight missing/null in "
                "today's dict (%s run) — parent headline NOT published; "
                "non-null fanned series still mint", date.today().isoformat(),
            )
            notify(
                "warning",
                "money_market_ref_rate — headline hole today",
                "The DOMMR overnight value was null/missing in the "
                f"{date.today().isoformat()} run's dict, so the parent "
                "headline row is deliberately NOT written (an announced "
                "hole, not a silent writer drop). Any non-null fanned "
                "series (dommr_1w/bofr/bofr_1w) were still minted.",
            )
            data.pop("money_market_ref_rate", None)

    _flatten_ownership_cluster(
        data,
        source_key="npl_by_ownership",
        key_prefix="npl_",
        key_suffix="_pct",
    )
    _flatten_ownership_cluster(
        data,
        source_key="deposits_by_ownership",
        key_prefix="deposits_",
        key_suffix="_cr",
    )


def _flatten_ownership_cluster(
    data: dict, *, source_key: str, key_prefix: str, key_suffix: str
) -> None:
    """Explode a 4-way bank-ownership cluster dict into per-segment scalars (S10).

    The ``pdf_fsr_ownership_cluster`` parser returns a dict keyed by the four
    canonical ownership segments — ``{"socb": .., "pcb": .., "fcb": ..,
    "specialised": ..}`` — for two FSR clusters:

      - ``npl_by_ownership``      → ``npl_socb_pct`` / ``npl_pcb_pct`` /
                                    ``npl_fcb_pct`` / ``npl_specialised_pct``
                                    (per-segment NPL ratio, percent).
      - ``deposits_by_ownership`` → ``deposits_socb_cr`` / ``deposits_pcb_cr`` /
                                    ``deposits_fcb_cr`` / ``deposits_specialised_cr``
                                    (per-segment deposit LEVEL, BDT crore — NOT
                                    a share; the donut computes shares downstream
                                    so they stay consistent with
                                    ``deposits_of_the_system``).

    Mirrors the ``call_money_rate`` / ``dse_sector_heat`` fan-out: we mint one
    numeric key per segment BEFORE the Supabase writer's scalar-only filter
    drops the dict (landmine C). Idempotent: a per-segment key already in
    ``data`` is left alone. No-op when the cluster indicator is absent or the
    value isn't a dict.
    """
    cluster = data.get(source_key)
    if not isinstance(cluster, dict):
        return
    for segment, value in cluster.items():
        if not isinstance(value, (int, float)):
            continue
        key = f"{key_prefix}{str(segment).lower()}{key_suffix}"
        if key not in data:
            data[key] = float(value)


def _apply_brief_aliases(data: dict) -> None:
    """Mutate `data` in place: surface EconDelta keys under brief-key names
    and apply unit conversions. Idempotent: if a brief_key already exists
    it's left untouched (so a hand-set value upstream wins).
    """
    _flatten_dict_indicators(data)

    for brief_key, econdelta_key in BRIEF_ALIASES.items():
        if econdelta_key in data and brief_key not in data:
            data[brief_key] = data[econdelta_key]

    for brief_key, (source_key, mult) in BRIEF_CONVERSIONS.items():
        if source_key in data and brief_key not in data:
            v = data[source_key]
            if isinstance(v, (int, float)):
                data[brief_key] = round(v * mult, 2)

    if "nbr_fytd_collected_cr" in data and "nbr_fytd_cross_check" not in data:
        data["nbr_fytd_cross_check"] = "single_source_tax_revenue"


def _titleize(metric_id: str) -> str:
    """Convert 'banking_npl_pct' -> 'Banking Npl Pct'."""
    return " ".join(word.capitalize() for word in metric_id.split("_"))


# metric_definitions rows for runtime-derived metrics (no sources-v3.json
# config entry — they have no fetch). `_build_definition_seeds` appends these
# so the catalog/Supabase definitions stay in sync with the values minted in
# `_build_v3_blocks`. Keyed by metric_id for idempotent merging.
DERIVED_DEFINITION_SEEDS: list[dict] = [
    {
        "metric_id": "crr_utilisation_pct",
        "label": "CRR balance as % of system deposits",
        "short_label": None,
        "unit": "%",
        "domain": "monetary_aggregates",
        "cadence": "monthly",
        "description": (
            "Derived (S2): deposits_held_with_bb_crr / deposits_of_the_system × 100. "
            "CRR balance held with Bangladesh Bank expressed as a % of total system "
            "deposits — NOT the regulated statutory maintenance ratio (no hardcoded "
            "policy rate). Computed in aggregate_latest._compute_reserve_utilisation."
        ),
        "source": "BB MEI (derived)",
        "source_url": None,
    },
    {
        "metric_id": "slr_utilisation_pct",
        "label": "Excess liquid assets as % of system deposits",
        "short_label": None,
        "unit": "%",
        "domain": "monetary_aggregates",
        "cadence": "monthly",
        "description": (
            "Derived (S2): excess_liquid_asset_total_minimum / deposits_of_the_system "
            "× 100. Excess liquid assets held over the statutory SLR minimum, expressed "
            "as a % of total system deposits — NOT the regulated maintenance ratio. "
            "Computed in aggregate_latest._compute_reserve_utilisation."
        ),
        "source": "BB MEI (derived)",
        "source_url": None,
    },
    # DOMMR/BOFR per-series ids — fanned out from the money_market_ref_rate
    # indicator's dict value in _flatten_dict_indicators (no config entry of
    # their own). Listing them here also gives sentinel/cadence.py's
    # DERIVED_DEFINITION_SEEDS pass their "daily" cadence, so the freshness
    # sentinel can judge them instead of reporting them unmapped. Accepted,
    # recorded gap (2026-08-28 review 6a): freshness is ALL the sentinel
    # coverage the fanned ids get — utils/staleness.py's value-stillness
    # alarm iterates v3 REGISTRY indicators only, so dommr_1w/bofr/bofr_1w
    # have no stillness coverage; the parent headline (= dommr) is the one
    # series it watches, via the registry id money_market_ref_rate.
    {
        "metric_id": "dommr",
        "label": "Dhaka Overnight Money Market Rate (Overnight)",
        "short_label": "DOMMR",
        "unit": "%",
        "domain": "money_market",
        "cadence": "daily",
        "description": (
            "DOMMR Overnight tenor — volume-weighted overnight money market "
            "reference rate for Dhaka, from BB's Money Market Reference Rate "
            "page. Fanned out from money_market_ref_rate (which also carries "
            "this value as its scalar headline); as_of is the page's own "
            "business-day date header, never the run date."
        ),
        "source": "Bangladesh Bank",
        "source_url": "https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate",
    },
    {
        "metric_id": "dommr_1w",
        "label": "Dhaka Overnight Money Market Rate (1W)",
        "short_label": "DOMMR 1W",
        "unit": "%",
        "domain": "money_market",
        "cadence": "daily",
        "description": (
            "DOMMR 1-week tenor from BB's Money Market Reference Rate page. "
            "Fanned out from money_market_ref_rate; as_of is the page's own "
            "business-day date header. 1M/3M tenors are deliberately NOT "
            "captured: BB accumulates them toward a minimum transaction "
            "volume across multiple days, so their prints are multi-day "
            "accumulations, not daily observations."
        ),
        "source": "Bangladesh Bank",
        "source_url": "https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate",
    },
    {
        "metric_id": "bofr",
        "label": "Bangladesh Overnight Financing Rate (Overnight)",
        "short_label": "BOFR",
        "unit": "%",
        "domain": "money_market",
        "cadence": "daily",
        "description": (
            "BOFR Overnight tenor — overnight financing reference rate from "
            "BB's Money Market Reference Rate page (the collateralised "
            "companion to DOMMR). Fanned out from money_market_ref_rate; "
            "as_of is the page's own business-day date header."
        ),
        "source": "Bangladesh Bank",
        "source_url": "https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate",
    },
    {
        "metric_id": "bofr_1w",
        "label": "Bangladesh Overnight Financing Rate (1W)",
        "short_label": "BOFR 1W",
        "unit": "%",
        "domain": "money_market",
        "cadence": "daily",
        "description": (
            "BOFR 1-week tenor from BB's Money Market Reference Rate page. "
            "Fanned out from money_market_ref_rate; as_of is the page's own "
            "business-day date header."
        ),
        "source": "Bangladesh Bank",
        "source_url": "https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate",
    },
]


def _build_definition_seeds(sources_v3_cfg: dict) -> list[dict]:
    """Build metric_definitions rows from sources-v3.json indicators.

    Conservative defaults: label falls back to titleized id, sort_order=100,
    is_hero=False. Tunable in Supabase Studio post-insert.

    Runtime-derived metrics (CRR/SLR utilisation — minted in `_build_v3_blocks`,
    no config entry) are appended from ``DERIVED_DEFINITION_SEEDS`` so their
    Supabase definitions stay in sync with the values that land in metric_history.
    Idempotent on metric_id: a derived id already produced from config wins.
    """
    seeds = []
    seen_ids: set[str] = set()
    for ind in sources_v3_cfg.get("indicators", []):
        seeds.append({
            "metric_id": ind["id"],
            "label": ind.get("label") or _titleize(ind["id"]),
            "short_label": ind.get("short_label"),
            "unit": ind.get("unit"),
            "domain": ind.get("domain", "Other"),
            "cadence": ind.get("cadence"),
            "description": ind.get("description"),
            "source": ind.get("source"),
            "source_url": (ind.get("fetch") or {}).get("url"),
        })
        seen_ids.add(ind["id"])

    for derived in DERIVED_DEFINITION_SEEDS:
        if derived["metric_id"] not in seen_ids:
            seeds.append(dict(derived))

    return seeds


def write_latest(bundle: LatestBundle) -> None:
    """Atomic write: .tmp -> os.replace."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LATEST_PATH.with_suffix(".json.tmp")
    payload = bundle.model_dump(mode="json")
    with tmp_path.open("w") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp_path, LATEST_PATH)


def _run_chart_feeding_monthly_appenders() -> None:
    """Run the two chart-feeding metric_history_monthly appenders.

    Extracted from ``main()`` on 2026-08-31 so it can be called from BOTH the
    happy path and the Opus ``hard_reject`` path (landmine 53). Neither leg
    reads this run's ``bundle``/``data``: the yield ladder promotes rows out of
    ``auction_results`` (written by econdelta-auction.service) and the macro
    leg reads the CPI trio back out of ``metric_history``. So an Opus verdict
    about today's export/treasury numbers has no bearing on whether these two
    are correct, and must not stop them running.

    Every failure is contained and notified per-leg — this function never
    raises, so a caller can invoke it immediately before its own ``return``
    without changing that return value.
    """
    # Macro monthly LIVE APPENDER (2026-08-08 frozen-charts incident,
    # landmine 50) -- CPI trio + remittance chart-feeding series. Own
    # try/except (mirrors D5 in main()): a failure here must notify with its
    # OWN distinct message, not get conflated with the daily
    # metric_history failure or the reserves-split failure -- three
    # different tables/paths, three different responder actions. Gated
    # the same way as the daily metric_history write (not tied to
    # bb_forex_ok -- this appender is independent of bb_forex) and called
    # AFTER it on the happy path so a CPI value that changed THIS run is
    # already persisted to the daily table before the appender reads it back.
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") != "1":
        try:
            macro_rows = _write_macro_monthly_append()
            if macro_rows:
                logger.info(
                    "upserted %d row(s) to Supabase metric_history_monthly "
                    "(macro monthly append: CPI trio + remittance)", macro_rows,
                )
        except Exception as e:  # noqa: BLE001 -- 2026-08-08 review M1/L4b:
            # defense-in-depth final backstop. _write_macro_monthly_append's
            # own sub-path try/excepts already contain every known failure
            # mode (CPI read, remittance existing-rows read, remittance
            # fetch/parse) -- by construction, only the final
            # upsert_metric_history_monthly call (SupabaseWriteError) should
            # ever reach here. Broadened from that single type to Exception
            # so a future refactor that accidentally lets something else
            # escape still can't crash the whole daily aggregate run.
            logger.warning(
                "macro monthly append failed: %s — continuing with local "
                "archive only", e,
            )
            notify(
                "error",
                "aggregate — macro monthly append write failed",
                "metric_history_monthly upsert (CPI trio / remittance appender) "
                "failed; The Brief's inflation/remittance charts will serve "
                f"stale data until the next successful run. {type(e).__name__}: {e}",
            )

    # Yield-ladder LIVE APPENDER (Phase 2, landmine 51) -- the 8-tenor
    # T-bill/T-bond curve, promoted from auction_results. Own try/except
    # (mirrors the macro-append block above and D5 in main()): a fully
    # SEPARATE function call with its own upsert, so a failure here can
    # never prevent the CPI/remittance legs above from having already
    # reached THEIR upsert (they already did, by the time this block
    # runs), and a failure THERE could never have prevented this leg
    # from running either -- each leg's try/except fully contains its
    # own failures before the next leg's call even starts.
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") != "1":
        try:
            yield_rows = _write_yield_ladder_monthly_append()
            if yield_rows:
                logger.info(
                    "upserted %d row(s) to Supabase metric_history_monthly "
                    "(yield ladder append, Phase 2)", yield_rows,
                )
        except Exception as e:  # noqa: BLE001 -- same R1/M1 reasoning as
            # the macro-append call site above: by construction only the
            # final upsert_metric_history_monthly call should reach here,
            # but broadened to Exception as a defense-in-depth backstop.
            logger.warning(
                "yield ladder append failed: %s — continuing with local "
                "archive only", e,
            )
            notify(
                "error",
                "aggregate — yield ladder append write failed",
                "metric_history_monthly upsert (yield ladder appender, Phase 2) "
                "failed; The Brief's yield-curve chart will serve stale data "
                f"until the next successful run. {type(e).__name__}: {e}",
            )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    now = datetime.now(timezone.utc)

    with CONFIG_PATH.open() as f:
        sources_cfg = json.load(f)["sources"]

    # Trading-day-aware DSE staleness skips Fri/Sat + public holidays. Fall back to
    # weekend-only (holidays=None) if the holiday file is missing/malformed.
    try:
        holidays = load_holidays(HOLIDAYS_PATH)
    except (FileNotFoundError, ValueError) as e:
        logger.warning("holidays load failed (%s); DSE staleness uses weekends only", e)
        holidays = None

    snapshots: dict[str, Any] = {}
    sources_status: dict[str, SourceStatus] = {}

    for key, (subdir_name, schema_class, url_key) in SCRAPER_SPEC.items():
        subdir = DATA_DIR / subdir_name
        latest_file = find_latest_snapshot(subdir)
        snapshot = load_snapshot(latest_file, schema_class) if latest_file else None
        snapshots[key] = snapshot
        url = sources_cfg.get(url_key, {}).get("url") if url_key else None
        sources_status[key] = compute_status(snapshot, url, now, key=key, holidays=holidays)

    data = flatten_data(snapshots)

    # v3 expansion: registry-driven domain blocks, freshness, alerts;
    # v3 indicator values also land in the flat `data` dict for The Brief.
    data_additions, domains, freshness, alerts = _build_v3_blocks(now)
    data.update(data_additions)

    # Forex-source aliases AFTER the v3 merge: the parse-stage versions of these
    # indicators come from BB PDFs and frequently fail (Akamai TSPD challenge,
    # PDF format drift) — leaving 0.0 in data_additions which would shadow the
    # working bb_forex.py-direct scrape. Apply the alias here so it wins.
    #
    # Freshness-gated: only overwrite when bb_forex's OWN status is "ok". A
    # stale direct scrape shouldn't clobber the v3 registry's own (possibly
    # fresher) independent parse of the same concept just because the direct
    # scrape is usually more reliable. When stale, whatever the v3 pipeline
    # produced is left as-is — and the underlying usd_bdt_mid /
    # gross_reserves_usd_bn keys (set unconditionally by flatten_data above)
    # still flow regardless, now honestly dated via
    # _build_tier1_source_as_of_map, which is the actual point of this guard.
    #
    # bb_forex_ok is reused below (Supabase write block) as the SAME gate for
    # _build_tier1_source_as_of_map's alias dates — the date must follow the
    # (gated) value, or a fresh v3 value can end up wearing bb_forex's stale
    # date (review round 1, item 1).
    forex = snapshots.get("bb_forex")
    forex_status = sources_status.get("bb_forex")
    bb_forex_ok = forex_status is not None and forex_status.status == "ok"
    if forex is not None and bb_forex_ok:
        data["usd_bdt_exchange_rate"] = forex.rates.usd_bdt_mid
        if forex.reserves is not None:
            data["fx_reserve_gross_and_bpm6"] = forex.reserves.gross_reserves_usd_bn

    # Daily yield-curve DERIVATION from auction_results (landmine 49's two-
    # column trap, PR-C build-brief item 3) -- must run BEFORE
    # _apply_brief_aliases (the brief-facing tbond_tbill_*/tbond_bond_* keys
    # read straight off these 5 ids) and before write_latest below, so
    # data/latest.json reflects the same value metric_history gets rather
    # than a UI/DB split. Gated on ECONDELTA_SKIP_SUPABASE like every other
    # Supabase-touching enrichment in this module (tests/conftest.py
    # defaults that env var to "1", so the whole test suite never makes a
    # real auction_results read unless a test explicitly opts in).
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") != "1":
        yield_values, yield_source_as_of = _derive_daily_yields_from_auctions(today=now.date())
        data.update(yield_values)
    else:
        yield_source_as_of = {}

    _apply_brief_aliases(data)

    # H3 (Opus review round 1, 2026-08-23): propagate the derived yields'
    # real auction dates to their brief-facing alias/conversion keys too
    # (tbond_bond_10y, tbond_tbill_182d, tbill_91d_yield_pct, ...) -- values
    # already flow to these keys via _apply_brief_aliases above, but their
    # DATES did not: neither key is a v3-registry id or a Tier-1 flatten
    # key, so _build_source_as_of_map/_build_tier1_source_as_of_map below
    # never see them, and upsert_metric_history's as_of=today fallback would
    # forge a run-date stamp on top of an honestly-dated base id -- one
    # alias hop away from the exact unbounded-carry-forward risk landmine
    # 51 exists to prevent (an alias's as_of silently advancing to "today"
    # every run regardless of how stale the real auction actually is).
    for brief_key, econ_key in BRIEF_ALIASES.items():
        if econ_key in yield_source_as_of:
            yield_source_as_of[brief_key] = yield_source_as_of[econ_key]
    for brief_key, (src_key, _mult) in BRIEF_CONVERSIONS.items():
        if src_key in yield_source_as_of:
            yield_source_as_of[brief_key] = yield_source_as_of[src_key]

    # Cross-metric health check (E1.4): the BB policy corridor's three legs
    # (SDF floor / repo / SLF ceiling) are each parsed independently, so no
    # single parser ever sees all three. Now that the flat `data` dict holds
    # every latest value in one place, verify SDF <= repo <= SLF and alert
    # loudly on a violation. Detect-only — the legs already landed at parse
    # time, so this never rejects the run.
    check_corridor_coherence(data)

    # Per-metric publication-date overrides, built once here (moved up from
    # the Supabase-write block below, which still uses this SAME variable) so
    # the watchlist staleness check can see each id's real as_of even on a
    # dry-run/test invocation that skips the Supabase write entirely (the
    # value-only stillness alarm below has never needed this; the watchlist
    # check below it does, since predicates (a)/(b) are as_of-aware).
    source_as_of_map = {
        **_build_tier1_source_as_of_map(snapshots, bb_forex_ok=bb_forex_ok),
        **_build_source_as_of_map(domains),
        **yield_source_as_of,
    }

    # Stillness alarm: the threshold checks above all ask "did this value move
    # too much?". Every freeze this project has shipped — 93 days of identical
    # food prices, 65 days of a pre-cut policy rate — was a failure of the
    # opposite kind, and nothing was watching for it. Detect-and-alert only;
    # runs after the corridor check so one bad run reports both problems.
    try:
        sources_v3_registry = (
            json.loads(SOURCES_V3_PATH.read_text()).get("indicators", [])
            if SOURCES_V3_PATH.exists()
            else []
        )
        check_value_staleness(
            data,
            sources_v3_registry,
            today=now.date(),
            state_path=STALENESS_STATE_PATH,
        )
    except Exception as e:  # observability must never take down the aggregate
        logger.warning("staleness check failed: %s: %s", type(e).__name__, e)

    # Watchlist staleness: a sharper, as_of/ingest-aware test for a small set
    # of financially load-bearing ids the blanket check above either can't
    # reach at all (gross_reserves_usd_bn / nbr_fytd_collected_cr are Tier-1/
    # alias-derived keys, never v3 registry ids) or can only judge by raw
    # value equality. See utils/staleness.py's module docstring for the three
    # predicates. Detect-and-alert only, same as the stillness alarm above.
    try:
        check_watchlist_staleness(
            data,
            source_as_of_map,
            today=now.date(),
            state_path=WATCHLIST_STALENESS_STATE_PATH,
        )
    except Exception as e:  # observability must never take down the aggregate
        logger.warning("watchlist staleness check failed: %s: %s", type(e).__name__, e)

    try:
        bundle = LatestBundle(
            schema_version="3.0",
            updated_at=now,
            sources_status=sources_status,
            data=data,
            domains=domains,
            freshness=freshness,
            alerts=alerts,
        )
    except ValidationError as e:
        logger.exception("bundle validation failed")
        notify("error", "aggregator validation failed", str(e))
        return 1

    # Opus 4.6 high-effort review: compare proposed `data` against the last 5 days
    # of archived latest.json. If reject, exit 1 without overwriting — the existing
    # latest.json (yesterday's last good run) becomes the rollback. The systemd
    # retry timers will give Step 1+2 a second pass; if they still produce a reject,
    # the brief publishes against yesterday's data with stale-section markers.
    if os.environ.get("ECONDELTA_SKIP_OPUS_REVIEW") == "1":
        logger.info("ECONDELTA_SKIP_OPUS_REVIEW=1 — skipping Opus review")
    else:
        history = load_history(ARCHIVE_DIR, days=5)
        if not history:
            logger.info("no archive history yet — skipping Opus review on this run")
        else:
            cumulative_ids = _cumulative_indicator_ids()
            verdict = review_data(data, history, cumulative_ids=cumulative_ids)
            status = verdict.get("status", "ok")
            reason = verdict.get("reason", "")
            if verdict.get("skipped"):
                # Involuntary skip (broken binary, timeout, malformed output) —
                # distinct from the ECONDELTA_SKIP_OPUS_REVIEW kill-switch above,
                # which never reaches this branch. The review stays advisory
                # (never blocks publication), but a self-disabled safety net
                # must not fail silently for months — surface it loudly.
                logger.warning("opus review involuntarily skipped: %s", reason)
                notify(
                    "warning",
                    "EconDelta Opus review skipped itself",
                    f"reason: {reason}\nthe review is advisory and did not block "
                    f"publication — but it did not run either.",
                )
            elif status == "reject":
                missing = verdict.get("missing", []) or []
                anomalies = verdict.get("anomalies", []) or []
                flagged = [a.get("indicator") for a in anomalies if a.get("indicator")]
                flagged = list({*flagged, *missing})
                # Fiscal-year reset override (landmine 56): drop any flagged id
                # whose "collapse" is a cumulative total restarting on 1 July,
                # BEFORE quarantine gets the chance to substitute last FY's
                # closing figure — that substitution is what poisons tomorrow's
                # history and makes the misread permanent.
                flagged, fy_excused = _drop_expected_fy_resets(
                    flagged, data, history, source_as_of_map, cumulative_ids
                )
                if fy_excused:
                    logger.warning(
                        "opus review overridden for %d cumulative field(s): %s — "
                        "reporting an early fiscal-year period, so the drop is the "
                        "1 July reset, not an anomaly | reason: %s",
                        len(fy_excused), fy_excused, reason,
                    )
                if not flagged:
                    logger.warning(
                        "opus review reject fully explained by the fiscal-year "
                        "reset (%s) — publishing today's data unchanged | reason: %s",
                        fy_excused, reason,
                    )
                    notify(
                        "warning",
                        "EconDelta published over an Opus reject (fiscal-year reset)",
                        f"reason: {reason}\nfields: {fy_excused}\n"
                        f"these are fiscal-year-to-date totals reporting an early "
                        f"month of the new FY — the drop is the 1 July reset. "
                        f"Nothing was quarantined; today's values published as-is.",
                    )
                cleaned, quarantined, hard_reject = _quarantine_flagged(data, flagged, history)
                if hard_reject:
                    logger.error(
                        "opus review REJECTED (hard): %s | missing=%s | anomalies=%d "
                        "(unmappable or >%d fields) — keeping yesterday's latest.json",
                        reason, missing[:5], len(anomalies), MAX_QUARANTINE_FIELDS,
                    )
                    notify(
                        "warning",
                        "EconDelta Opus review rejected today's data",
                        f"reason: {reason}\nmissing: {missing[:5]}\nanomalies: {len(anomalies)}\n"
                        f"keeping yesterday's latest.json — retry timers will re-run.",
                    )
                    # Landmine 53 (2026-08-31 frozen-yield-ladder incident): the
                    # chart-feeding monthly appenders do NOT read this run's
                    # bundle. The ladder promotes rows out of auction_results and
                    # the macro leg reads the CPI trio back out of metric_history
                    # — neither is implicated by an Opus verdict about today's
                    # export/treasury numbers. Before this call existed, a
                    # hard_reject `return 1` here skipped both appenders (they sat
                    # ~190 lines below), so an unrelated export anomaly silently
                    # froze The Brief's yield-curve and inflation charts. August
                    # 2026's rung went missing for exactly this reason and had to
                    # be written by hand. The function contains and notifies its
                    # own failures, so this cannot change the `return 1` below.
                    _run_chart_feeding_monthly_appenders()
                    return 1
                # Granular path: quarantine the flagged fields, publish the rest.
                # `quarantined` can legitimately be empty now — the fiscal-year
                # override above may have excused every flagged id — in which
                # case this run publishes clean and the notify it already sent
                # is the whole story. Don't follow it with an empty
                # "fields quarantined" alert.
                if quarantined:
                    logger.warning(
                        "opus review reject → quarantined %d field(s): %s | reason: %s",
                        len(quarantined), quarantined, reason,
                    )
                    notify(
                        "warning",
                        "EconDelta published with fields quarantined",
                        f"reason: {reason}\nquarantined: {quarantined}\n"
                        f"these fields use last-good values; the rest published fresh.",
                    )
                data = cleaned
                bundle = LatestBundle(
                    schema_version="3.0",
                    updated_at=now,
                    sources_status=sources_status,
                    data=data,
                    domains=domains,
                    freshness=freshness,
                    alerts=alerts,
                )
            else:
                logger.info("opus review OK: %s (confidence=%s)", reason, verdict.get("confidence"))

    write_latest(bundle)
    # Archive a daily copy for tomorrow's Opus review. Same-day runs overwrite,
    # so the LAST successful aggregate of the day is what tomorrow compares against.
    archived = archive_latest(LATEST_PATH, ARCHIVE_DIR)
    if archived is not None:
        logger.info("archived to %s", archived.name)

    # Seed metric_definitions for any new indicators (idempotent).
    from utils.supabase_writer import upsert_metric_definitions_seed
    sources_v3 = json.loads(SOURCES_V3_PATH.read_text()) if SOURCES_V3_PATH.exists() else {"indicators": []}
    seeds = _build_definition_seeds(sources_v3)
    inserted = upsert_metric_definitions_seed(seeds)
    if inserted:
        logger.info("Seeded %d new metric_definitions rows", inserted)

    # Persist to Supabase metric_history (warm queryable history). Best-effort:
    # local archive (above) is the cold backup, and the next aggregate retry
    # idempotently re-upserts the same (metric_id, as_of) rows. ECONDELTA_SKIP_SUPABASE=1
    # disables the call (set in tests/conftest.py and any dev runs).
    if os.environ.get("ECONDELTA_SKIP_SUPABASE") != "1":
        try:
            from utils.supabase_writer import (
                SupabaseWriteError,
                upsert_metric_history,
                verify_landed_count,
            )
            # source_as_of_map is built earlier in main() now (immediately
            # before the stillness/watchlist staleness checks, which need it
            # too) — reused here unchanged. Slow-cadence metrics (quarterly
            # FSAR, monthly news) carry source_as_of from the parser so
            # metric_history.as_of reflects the true publication date rather
            # than today's run date — fixing the freshness-pill lie. Merged
            # from the Tier-1 map (bb_forex/dse_market/commodity_prices —
            # SCRAPER_SPEC, which never enters the v3 `domains` dict and so
            # could never get an override here otherwise) and the v3 map;
            # Tier-1 keys and v3 registry keys should never collide (the two
            # pipelines cover disjoint indicator ids), but if sources-v3.json
            # ever grows an entry that shadows a Tier-1 flatten_data key, the
            # v3-recovered date (parsed from the source document's own text)
            # wins — it was merged LAST when this map was built.
            # Explicit write timestamp so the E2.2 landed-count read-back counts
            # exactly this upsert's rows.
            write_ts = datetime.now(timezone.utc)
            # No provenance= here on purpose: this one call flattens the WHOLE
            # snapshot — deterministic Tier-1 flatten, config-driven regex/table
            # parsers, AND the LLM-extraction fallback (hybrid.parse_one) all
            # land in the same `data` dict with no per-metric extraction-method
            # tag surviving to this point. Stamping the batch would be a guess,
            # not a fact — see AGENTS.md / utils/supabase_writer.py on provenance
            # vs source. Per-metric provenance here is a real follow-up (would
            # need parse_all/aggregate to carry the tag alongside each value,
            # analogous to source_as_of_map above), not something to fake now.
            n_rows = upsert_metric_history(
                data=data, as_of=now.date(), source_as_of_map=source_as_of_map,
                ingested_at=write_ts,
            )
            logger.info(
                "upserted %d rows to Supabase metric_history (as_of=%s, overrides=%d)",
                n_rows, now.date(), len(source_as_of_map),
            )
            # Landed-count invariant (E2.2): a 2xx / "wrote N" log is not proof of
            # persistence (landmine 22). Re-query BEFORE media overrides so the
            # count is this upsert's rows only. Aggregate is the sole writer in
            # its 07:00 window, so an unscoped ingested_at>= count is exact.
            verify_landed_count(n_rows, since=write_ts, source_label="aggregate")
            _apply_media_overrides(data, source_as_of_map)

        except SupabaseWriteError as e:
            logger.warning(
                "Supabase write failed: %s — continuing with local archive only", e,
            )
            # Alert loudly: a swallowed write failure means consumers silently
            # serve yesterday's data (rotated key, PostgREST outage) with no
            # signal. Keep continuing — the local archive is the right fallback;
            # the silence was the bug (E1.6).
            notify(
                "error",
                "aggregate — Supabase write failed",
                "metric_history upsert failed; consumers will serve the previous "
                f"snapshot until the next successful run. {type(e).__name__}: {e}",
            )

        # Reserves gross/BPM6 monthly split (D5) -- same freshness gate as the
        # fx_reserve_gross_and_bpm6 alias above: only write from a bb_forex
        # read that's actually fresh, not a carried-forward file. Own
        # try/except (2026-08-05 review L3): a monthly-namespace write
        # failure must notify with its OWN distinct message, not get
        # conflated with a daily metric_history failure above -- both are
        # "Supabase write failed" superficially, but they're different
        # tables and a responder needs to know which one to check.
        if os.environ.get("ECONDELTA_SKIP_SUPABASE") != "1" and forex is not None and bb_forex_ok:
            from utils.supabase_writer import SupabaseWriteError

            try:
                monthly_rows = _write_reserves_monthly_split(forex.reserves)
                if monthly_rows:
                    logger.info(
                        "upserted %d row(s) to Supabase metric_history_monthly "
                        "(reserves split, D5)", monthly_rows,
                    )
                else:
                    # H4: bb_forex is fresh and forex.reserves is present, yet
                    # nothing landed -- this must never be silent. The specific
                    # reason (bpm6 missing / invariant or ratio-band refused /
                    # reserves somehow None) was already logged inside
                    # _write_reserves_monthly_split; this confirms it's visible
                    # at the top-level run log too.
                    logger.warning(
                        "reserves monthly split: 0 rows written this run despite "
                        "bb_forex being fresh -- see the warning above for why",
                    )
            except SupabaseWriteError as e:
                logger.warning(
                    "Supabase monthly-namespace write failed: %s — continuing "
                    "with local archive only", e,
                )
                notify(
                    "error",
                    "aggregate — Supabase monthly write failed",
                    "metric_history_monthly upsert (reserves split, D5) failed; "
                    "The Brief's gross/BPM6 chart will serve stale data until "
                    f"the next successful run. {type(e).__name__}: {e}",
                )

        # The two chart-feeding monthly appenders (CPI trio + remittance,
        # then the 8-tenor yield ladder). Body lives in
        # `_run_chart_feeding_monthly_appenders` so the hard_reject path can
        # call it too -- see landmine 53. Called here, after the daily
        # metric_history write above, so a CPI value that changed THIS run is
        # already persisted to the daily table before the macro leg reads it
        # back.
        _run_chart_feeding_monthly_appenders()

    summary = " ".join(
        f"{k}={s.status}({s.age_hours}h)" if s.age_hours is not None else f"{k}={s.status}"
        for k, s in sources_status.items()
    )
    logger.info("wrote %s -- %s", LATEST_PATH, summary)

    bad = {k: s.status for k, s in sources_status.items() if s.status != "ok"}
    if bad:
        notify(
            "warning",
            "aggregator -- sources not OK",
            "\n".join(f"{k}: {v}" for k, v in bad.items()),
        )

    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("aggregate", "econdelta-aggregate.service", main))
