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
# by data-freshness here; and both ids below are unconsumed parity metrics
# (fetched, not yet displayed on any surface):
#   - tax_gdp_ratio: World Bank GC.TAX.TOTL.GD.ZS for BD stops at 2021 (~4-5y lag).
#   - rev_gdp_ratio: IMF DataMapper "rev" for BD carries no forward projection, so
#     its latest actual (currently 2024) breaches the fiscal_year grace for a
#     ~4-month window each year until the next annual vintage lands.
# See scrapers/fiscal_gdp_ratios.py and sentinel/cadence.py.
ACCEPTED_STALE_METRIC_IDS: frozenset[str] = frozenset(
    {"tax_gdp_ratio", "rev_gdp_ratio"}
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
    """Fold one table's rows into per-metric max(as_of ≤ today) + max(ingested_at)."""
    for row in rows:
        mid = row.get("metric_id")
        if not mid:
            continue
        as_of = _parse_date(row.get("as_of"))
        ing = _parse_ts(row.get("ingested_at"))
        entry = acc.setdefault(
            mid,
            {"as_of": None, "ingested_at": None, "tables": set()},
        )
        entry["tables"].add(table)
        # Exclude future as_of (e.g. debt_gdp_ratio = 2031-12-31 IMF projection)
        # from the "latest" — a projection must not read as this week's vintage.
        if as_of is not None and as_of <= today:
            if entry["as_of"] is None or as_of > entry["as_of"]:
                entry["as_of"] = as_of
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
      * accepted_stale — in ``ACCEPTED_STALE_METRIC_IDS``: its source lags by
        design, so a breach here is not actionable and must never alert;
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

    for mid, entry in acc.items():
        tables = tuple(sorted(entry["tables"]))
        only_monthly = tables == ("metric_history_monthly",)
        cadence = resolve_cadence(mid, cadence_map, from_monthly_table=only_monthly)
        latest_as_of = entry["as_of"]
        latest_ing = entry["ingested_at"]

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
    return FreshnessReport(
        breaches=breaches,
        fresh=fresh,
        unmapped=unmapped,
        accepted_stale=accepted_stale,
        checked_at=now or datetime.now(timezone.utc),
    )
