"""Pure freshness assessment — no I/O, so it retro-tests against synthetic data.

Takes the raw (metric_id, as_of, ingested_at) rows from both history tables and
produces a ``FreshnessReport`` classifying every metric as fresh / breached /
unmapped. ``main.py`` owns the Supabase reads and the Discord post; this module
owns the logic, which is the part that must be provably correct against the four
historical freeze clusters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from utils.calendar import previous_trading_day

from .cadence import GRACE_DAYS_BY_CADENCE, resolve_cadence

# Daily cadence tolerates this many DSE trading sessions of lag before breach.
_DAILY_TRADING_DAY_GRACE = GRACE_DAYS_BY_CADENCE["daily"]

# Metrics whose SOURCE publishes with a structural lag longer than any sane
# cadence grace — their staleness is CORRECT, not a pipeline fault, so they must
# never fire the daily breach alert (that would be unactionable alert-fatigue,
# poisoning the very channel the run_logs dead-man's-switch relies on). A genuine
# scraper failure is still caught by the scraper's own error path + run_logs, not
# by data-freshness here. The two fiscal ratios below are unconsumed parity
# metrics (fetched, not yet displayed on any surface) — see the bb_npl_structure
# block further down for a different reason (structural source lag, not parity):
#   - tax_gdp_ratio: World Bank GC.TAX.TOTL.GD.ZS for BD stops at 2021 (~4-5y lag).
#   - rev_gdp_ratio: IMF DataMapper "rev" for BD carries no forward projection, so
#     its latest actual (currently 2024) breaches the fiscal_year grace for a
#     ~4-month window each year until the next annual vintage lands.
# See scrapers/fiscal_gdp_ratios.py and sentinel/cadence.py.
# Metrics with a KNOWN future-dated row that is not a live bug -- a stale
# mis-parse (debt_gdp_ratio's 2031-12-31, landmine 40) that keeps re-surfacing
# every run until someone corrects the underlying row. Without this
# exclusion, future_dated (below) would turn the sentinel into a daily nag
# about a known, already-diagnosed defect instead of alerting on a genuinely
# NEW future-dated id -- the exact alert-fatigue failure mode
# ACCEPTED_STALE_METRIC_IDS above already exists to prevent for ordinary
# staleness. BOTH ids are CONFIRMED load-bearing against production
# metric_history (anon SELECT, 2026-08-22 round-2 review): debt_gdp_ratio_proj
# carries six future-dated rows (2026-12-31=41.8 ... 2031-12-31=48.8), the
# same mis-parse family as debt_gdp_ratio. The id does NOT appear in
# config/sources-v3.json -- it exists only as DB rows, which is exactly why
# the sentinel (which reads Supabase, not the scraper registry) needs the
# exclusion. Do NOT remove either entry until the underlying rows are
# corrected, or the daily-nag defect (HIGH-2) comes straight back.
ACCEPTED_FUTURE_DATED_METRIC_IDS: frozenset[str] = frozenset(
    {"debt_gdp_ratio", "debt_gdp_ratio_proj"}
)

ACCEPTED_STALE_METRIC_IDS: frozenset[str] = frozenset(
    {"tax_gdp_ratio", "rev_gdp_ratio"}
    # bb_npl_structure (2026-08-03 spec amendment): structural source lag —
    # FSR annual ~6mo lag (22 ids) / press-only seed series with no schedule
    # (13 ids), 35 total. Never in briefing.config.CORE_METRIC_IDS or
    # config/sources-v3.json — owner decision, non-gating. See
    # docs/superpowers/specs/2026-08-03-bb-npl-structure-design.md.
    | {
        "gross_npl_stock",
        "lending_share_sector_agriculture",
        "lending_share_sector_capital_market",
        "lending_share_sector_consumer_credit",
        "lending_share_sector_industrial_mfg",
        "lending_share_sector_industrial_services",
        "lending_share_sector_nbfi",
        "lending_share_sector_other",
        "lending_share_sector_trade_commerce",
        "loans_outstanding_band_1_10cr",
        "loans_outstanding_band_gt50cr",
        "loans_outstanding_band_lt1cr",
        "npl_rate_band_10_20cr",
        "npl_rate_band_1_10cr",
        "npl_rate_band_20_30cr",
        "npl_rate_band_30_40cr",
        "npl_rate_band_40_50cr",
        "npl_rate_band_gt50cr",
        "npl_rate_band_lt1cr",
        "npl_rate_cmsme_cottage",
        "npl_rate_cmsme_medium",
        "npl_rate_cmsme_overall",
        "npl_rate_sector_agriculture",
        "npl_rate_sector_capital_market",
        "npl_rate_sector_consumer_credit",
        "npl_rate_sector_industrial_mfg",
        "npl_rate_sector_industrial_services",
        "npl_rate_sector_nbfi",
        "npl_rate_sector_other",
        "npl_rate_sector_trade_commerce",
        "npl_rate_sub_construction",
        "npl_rate_sub_housing_finance",
        "npl_rate_sub_rmg",
        "npl_rate_sub_smc_industries",
        "total_bank_advances",
    }
)

# Metrics whose id has fallen out of its SOURCE's tracked universe — a
# DIFFERENT reason for never-alerting than ACCEPTED_STALE_METRIC_IDS above
# (source-lag-by-design vs. an id with no live producer left at all), so it
# gets its own frozenset rather than being merged into that one.
# dse_close_KOHINOOR / dse_close_LINDEBD / dse_close_UNIQUEHRL fell out of the
# DS30 constituents at the ~2026-07-16 rebalance; scripts/backfill_dse_dayend.py
# fetches the LIVE constituent list with no delisting handling, so these three
# ids simply stopped being written and now breach the daily grace forever.
# Routed to the same silent `accepted_stale` bucket. A genuinely dead scraper
# for any OTHER dse_close_* id is NOT covered by this set and still falls
# through to `breaches`/`unmapped` normally.
RETIRED_METRIC_IDS: frozenset[str] = frozenset(
    {"dse_close_KOHINOOR", "dse_close_LINDEBD", "dse_close_UNIQUEHRL"}
)

# 2026-08-08 frozen-charts incident triage (AGENTS.md landmine 50): 4 of the
# metric_history_monthly chart-feeding ids that froze alongside the CPI-trio/
# remittance series (which DID get a live appender, see
# aggregate_latest._write_macro_monthly_append) have no live source to
# refresh against at all -- their staleness is structural, not a pipeline
# fault, so routing them to `breaches` would be exactly the kind of
# unactionable daily nag ACCEPTED_STALE_METRIC_IDS exists to prevent.
#   - cpi_12m_food_monthly / cpi_12m_nonfood_monthly: the 12-month-average
#     food/non-food splits have no live source anywhere post-seed-death (the
#     dead macro_observer_seed site was the only writer either id ever had;
#     unlike the headline cpi_12m_avg_monthly, EconDelta's own daily pipeline
#     has no equivalent food/non-food 12-month-average extraction to derive
#     from -- only the point-to-point food/non-food ids are safe daily
#     sources, see cpi_p2p_food_monthly/cpi_p2p_nonfood_monthly).
#   - imports_usd_mn_monthly: BB publishes cif imports on a ~2-month lag and
#     no standalone monthly import figure exists beyond May 2026 -- a
#     structural source lag, not a scraper regression.
#   - exports_usd_mn_monthly: backfilled to Jun 2026 from EPB press figures
#     (scripts/backfill_monthly_chart_series.py); the EPB portal itself is
#     JS-rendered/unscrapeable, so there is no live writer yet. Ongoing
#     source research is PARKED, not abandoned -- revisit note: check whether
#     EPB or BSS ever exposes a scrapeable monthly export table before
#     assuming this stays accepted-stale forever.
ACCEPTED_STALE_METRIC_IDS = ACCEPTED_STALE_METRIC_IDS | frozenset(
    {
        "cpi_12m_food_monthly",
        "cpi_12m_nonfood_monthly",
        "imports_usd_mn_monthly",
        "exports_usd_mn_monthly",
    }
)

# The metric_history_monthly ids The Brief's charts + the EconDelta PWA's
# /macro tab actually render. 2026-08-08 incident (AGENTS.md landmine 50):
# one of these (the whole chart-feeding tier) froze for 5 months, invisible
# because it was buried inside a 41-item freshness digest with no way to
# tell "a metric a reader can SEE is broken" apart from "an internal parity
# metric nobody looks at is stale". sentinel/report.py surfaces breaches in
# this set FIRST, under their own heading, so a chart-feeding freeze can
# never again hide behind a wall of lower-stakes breaches.
CHART_FEEDING_METRIC_IDS: frozenset[str] = frozenset(
    {
        "remittance_usd_mn_monthly",
        "exports_usd_mn_monthly",
        "imports_usd_mn_monthly",
        "cpi_12m_avg_monthly",
        "cpi_p2p_food_monthly",
        "cpi_p2p_nonfood_monthly",
        "tbill_91d_yield_monthly",
        "tbill_182d_yield_monthly",
        "tbill_364d_yield_monthly",
        "yield_2y_monthly",
        "yield_5y_monthly",
        "yield_10y_monthly",
        "yield_15y_monthly",
        "yield_20y_monthly",
        "gross_reserves_usd_bn_monthly",
        "net_reserves_bpm6_usd_bn_monthly",
    }
)

# The DAILY-table (`metric_history`) sibling of CHART_FEEDING_METRIC_IDS: ids
# The Brief's daily SPA sections actually render, not just its monthly
# charts. Real CPI breaches were observed printing at digest rank 28-54 —
# inside an undifferentiated "…and 37 more" line — because nothing marked
# them as reader-visible; this set is what lets sentinel/report.py put them
# above the fold instead. Hardcoded (not imported from aggregate_latest at
# runtime) to keep this module import-light and "pure" per its own module
# docstring, the same way CHART_FEEDING_METRIC_IDS above is hardcoded rather
# than derived. Sourced from two verified places, as of this PR:
#   - every `aggregate_latest.BRIEF_ALIASES` / `BRIEF_CONVERSIONS` key (the
#     brief-side names those dicts exist specifically to feed — see AGENTS.md
#     landmine 8: a new scraper meant to reach a Brief section is REQUIRED to
#     register in one of those dicts, so their key set IS "what The Brief
#     reads", by the repo's own design contract);
#   - a handful of Tier-1/DSE ids The Brief's builders read DIRECTLY under
#     their EconDelta name with no alias in between (confirmed by reading
#     the-brief/brief/builders/{dse,fx}.py's own metric-spec tuples).
# Re-check both sources when either dict grows a new Brief-facing entry, the
# same maintenance discipline landmine 8 already asks of BRIEF_ALIASES itself.
BRIEF_SURFACED_METRIC_IDS: frozenset[str] = frozenset(
    {
        # aggregate_latest.BRIEF_ALIASES keys
        "banking_broad_money", "banking_call_money_rate", "banking_car_pct",
        "banking_deposits", "banking_excess_liquid", "banking_money_multiplier",
        "banking_npl_pct", "banking_reserve_money",
        "dam_chicken", "dam_egg", "dam_flour", "dam_lentil", "dam_oil",
        "dam_onion", "dam_rice_coarse", "dam_sugar",
        "food_atta_packet_bdt", "food_chicken_farm_bdt", "food_egg_red_bdt",
        "food_lentil_moong_bdt", "food_oil_soybean_bdt", "food_onion_local_bdt",
        "food_rice_coarse_bdt", "food_sugar_local_bdt",
        "gsec_next_auction_cr",
        "macro_cpi_food", "macro_cpi_headline", "macro_cpi_nonfood",
        "macro_credit_growth",
        "nbr_fytd_collected_cr",
        "tbill_91d_yield_pct", "tbond_bond_10y", "tbond_bond_5y",
        "tbond_tbill_182d", "tbond_tbill_364d", "tbond_tbill_91d",
        # aggregate_latest.BRIEF_CONVERSIONS keys
        "fiscal_bank_borrow_trn", "fiscal_foreign_borrow_trn",
        "fiscal_govt_borrow_trn", "fiscal_nbr_collected_trn",
        "fiscal_nsc_outstanding", "nbr_customs_bn", "nbr_it_bn", "nbr_vat_bn",
        "remit_fy_mn", "remit_monthly_mn", "tbill_outstanding_cr",
        "tbond_outstanding_cr",
        # Direct-read Tier-1/DSE ids (no alias) confirmed in the-brief's own
        # builders/{dse,fx}.py metric-spec tuples
        "dsex", "ds30", "dses", "dsex_change_pct", "turnover_crore",
        "advancing", "declining", "unchanged",
        "usd_bdt_mid", "gross_reserves_usd_bn", "reserves_date",
        "brent_crude_usd_barrel", "gold_usd_oz", "wti_crude_usd_barrel",
    }
)


@dataclass(frozen=True)
class MetricFreshness:
    """One metric's freshness verdict."""

    metric_id: str
    cadence: str | None
    latest_as_of: date | None
    latest_ingested_at: datetime | None
    age_days: int | None
    breach: bool
    tables: tuple[str, ...]


@dataclass(frozen=True)
class FreshnessReport:
    """Classified outcome of one sentinel run."""

    breaches: list[MetricFreshness] = field(default_factory=list)
    fresh: list[MetricFreshness] = field(default_factory=list)
    unmapped: list[MetricFreshness] = field(default_factory=list)
    accepted_stale: list[MetricFreshness] = field(default_factory=list)
    # A metric whose max non-future as_of ALSO has a future-dated row sitting
    # alongside it (e.g. debt_gdp_ratio's known 2031-12-31 mis-parse). This is
    # a SEPARATE, cross-cutting flag, not a replacement classification: the
    # metric still lands in breaches/fresh/unmapped/accepted_stale above based
    # on its real (non-future) as_of exactly as before -- discarding the
    # future row from THAT computation is still correct (a projection must
    # not read as this week's vintage). What changes is that the future row
    # is no longer silently thrown away with no record anywhere; it surfaces
    # here as its own breach type instead.
    future_dated: list[MetricFreshness] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total(self) -> int:
        return (
            len(self.breaches)
            + len(self.fresh)
            + len(self.unmapped)
            + len(self.accepted_stale)
        )


def _nth_previous_trading_day(d: date, n: int, holidays: set[date] | None) -> date:
    """The date that is ``n`` trading days strictly before ``d``."""
    cur = d
    for _ in range(n):
        cur = previous_trading_day(cur, holidays)
    return cur


def is_breach(
    latest_as_of: date,
    cadence: str,
    today: date,
    holidays: set[date] | None = None,
) -> bool:
    """True if ``latest_as_of`` is older than the cadence's grace window allows.

    Daily cadence is judged in TRADING days (a Fri/Sat/holiday gap is not stale):
    breach when the data is older than ``_DAILY_TRADING_DAY_GRACE`` DSE sessions.
    Every other cadence uses a plain calendar-day window from
    ``GRACE_DAYS_BY_CADENCE``. An unknown cadence is never a breach here (it is
    surfaced as "unmapped" upstream).
    """
    if cadence == "daily":
        floor = _nth_previous_trading_day(today, _DAILY_TRADING_DAY_GRACE, holidays)
        return latest_as_of < floor
    grace = GRACE_DAYS_BY_CADENCE.get(cadence)
    if grace is None:
        return False
    return (today - latest_as_of).days > grace


def _parse_date(raw: object) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _parse_ts(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _aggregate(
    rows: list[dict],
    table: str,
    today: date,
    acc: dict[str, dict],
) -> None:
    """Fold one table's rows into per-metric max(as_of ≤ today) + max(ingested_at).

    A future-dated as_of (e.g. debt_gdp_ratio's known 2031-12-31 mis-parse) is
    excluded from the ``as_of`` computed here — a projection must not read as
    this week's vintage — but is NOT thrown away silently: the row's own
    (max) future date is tracked separately in ``future_as_of`` so ``assess``
    can flag it as its own breach type instead of discarding it.
    """
    for row in rows:
        mid = row.get("metric_id")
        if not mid:
            continue
        as_of = _parse_date(row.get("as_of"))
        ing = _parse_ts(row.get("ingested_at"))
        entry = acc.setdefault(
            mid,
            {"as_of": None, "ingested_at": None, "future_as_of": None, "tables": set()},
        )
        entry["tables"].add(table)
        if as_of is not None and as_of <= today:
            if entry["as_of"] is None or as_of > entry["as_of"]:
                entry["as_of"] = as_of
        elif as_of is not None:  # as_of > today
            if entry["future_as_of"] is None or as_of > entry["future_as_of"]:
                entry["future_as_of"] = as_of
        if ing is not None and (entry["ingested_at"] is None or ing > entry["ingested_at"]):
            entry["ingested_at"] = ing


def assess(
    *,
    rows_daily: list[dict],
    rows_monthly: list[dict],
    cadence_map: dict[str, str],
    today: date,
    holidays: set[date] | None = None,
    now: datetime | None = None,
) -> FreshnessReport:
    """Classify every metric across both tables into fresh / breach / unmapped.

    A metric is:
      * unmapped — cadence can't be resolved, OR it has no non-future as_of to
        judge (both are actionable dedupe/retire/projection-split signals);
      * accepted_stale — in ``ACCEPTED_STALE_METRIC_IDS`` (source lags by
        design) or ``RETIRED_METRIC_IDS`` (the id's producer no longer writes
        it): either way a breach here is not actionable and must never alert;
      * breach   — latest_as_of is older than its cadence grace allows;
      * fresh    — otherwise.
    """
    acc: dict[str, dict] = {}
    # Fold monthly first so a metric present in both tables is still correctly
    # flagged as appearing in metric_history (order doesn't affect the max).
    _aggregate(rows_monthly, "metric_history_monthly", today, acc)
    _aggregate(rows_daily, "metric_history", today, acc)

    breaches: list[MetricFreshness] = []
    fresh: list[MetricFreshness] = []
    unmapped: list[MetricFreshness] = []
    accepted_stale: list[MetricFreshness] = []
    future_dated: list[MetricFreshness] = []

    for mid, entry in acc.items():
        tables = tuple(sorted(entry["tables"]))
        only_monthly = tables == ("metric_history_monthly",)
        cadence = resolve_cadence(mid, cadence_map, from_monthly_table=only_monthly)
        latest_as_of = entry["as_of"]
        latest_ing = entry["ingested_at"]

        # Cross-cutting flag, independent of the breach/fresh/unmapped/
        # accepted_stale classification below (a metric can be BOTH correctly
        # fresh on its real as_of AND carry a rogue future-dated row). Never
        # `continue`s -- the normal classification still runs on this
        # metric's real (non-future) as_of exactly as before this flag
        # existed.
        future_as_of = entry.get("future_as_of")
        # ACCEPTED_FUTURE_DATED_METRIC_IDS is applied HERE, before
        # should_send ever sees the report -- a known, already-diagnosed
        # mis-parse (debt_gdp_ratio) must never itself make the sentinel
        # speak on an otherwise-quiet non-heartbeat day (HIGH-2, 2026-08-22
        # round-1 review). A genuinely NEW future-dated id is unaffected and
        # still surfaces normally.
        if future_as_of is not None and mid not in ACCEPTED_FUTURE_DATED_METRIC_IDS:
            future_dated.append(
                MetricFreshness(
                    metric_id=mid,
                    cadence=cadence,
                    latest_as_of=future_as_of,
                    latest_ingested_at=latest_ing,
                    age_days=(today - future_as_of).days,  # negative -- it's in the future
                    breach=True,
                    tables=tables,
                )
            )

        if cadence is None or latest_as_of is None:
            unmapped.append(
                MetricFreshness(
                    metric_id=mid,
                    cadence=cadence,
                    latest_as_of=latest_as_of,
                    latest_ingested_at=latest_ing,
                    age_days=(today - latest_as_of).days if latest_as_of else None,
                    breach=False,
                    tables=tables,
                )
            )
            continue

        # Source-lag metrics: their staleness is by design, so never let them
        # reach `breaches` (which would fire an unactionable daily alert). They
        # DO have a cadence + a real vintage — a scraper that stopped writing
        # entirely falls to `unmapped` above, so this can't mask a dead scraper.
        if mid in ACCEPTED_STALE_METRIC_IDS:
            accepted_stale.append(
                MetricFreshness(
                    metric_id=mid,
                    cadence=cadence,
                    latest_as_of=latest_as_of,
                    latest_ingested_at=latest_ing,
                    age_days=(today - latest_as_of).days,
                    breach=False,
                    tables=tables,
                )
            )
            continue

        # Retired ids (see RETIRED_METRIC_IDS above): a different reason than
        # source-lag for the same silent treatment — kept as a separate check
        # so each set's rationale stays legible and one can be edited without
        # touching the other's semantics.
        if mid in RETIRED_METRIC_IDS:
            accepted_stale.append(
                MetricFreshness(
                    metric_id=mid,
                    cadence=cadence,
                    latest_as_of=latest_as_of,
                    latest_ingested_at=latest_ing,
                    age_days=(today - latest_as_of).days,
                    breach=False,
                    tables=tables,
                )
            )
            continue

        age = (today - latest_as_of).days
        breached = is_breach(latest_as_of, cadence, today, holidays)
        mf = MetricFreshness(
            metric_id=mid,
            cadence=cadence,
            latest_as_of=latest_as_of,
            latest_ingested_at=latest_ing,
            age_days=age,
            breach=breached,
            tables=tables,
        )
        (breaches if breached else fresh).append(mf)

    breaches.sort(key=lambda m: (m.age_days is None, -(m.age_days or 0)))
    fresh.sort(key=lambda m: m.metric_id)
    unmapped.sort(key=lambda m: m.metric_id)
    accepted_stale.sort(key=lambda m: m.metric_id)
    future_dated.sort(key=lambda m: m.metric_id)
    return FreshnessReport(
        breaches=breaches,
        fresh=fresh,
        unmapped=unmapped,
        accepted_stale=accepted_stale,
        future_dated=future_dated,
        checked_at=now or datetime.now(timezone.utc),
    )
