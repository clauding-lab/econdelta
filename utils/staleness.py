"""Stillness alarm — catch an indicator whose value has stopped changing.

The existing anomaly check (``utils/anomaly.check_threshold``) asks "did this
value MOVE too much?". Every failure it can see is a failure of motion. But the
expensive failures in this project have all been failures of *stillness*: a
fetcher that keeps returning HTTP 200 and the same stale page, a parser locked
onto a table that no longer updates, an upstream that quietly stopped
publishing. Nothing 400s, nothing throws, the run is green, and the number sits
there being wrong for months.

Measured examples at the time of writing (2026-08-03), all found by hand rather
than by any alarm:

* the eight ``food_*`` retail prices — byte-identical for **93 days**, all from
  one ``market.dam.gov.bd`` page
* ``interbank_repo_data`` — 33 days at 5591.93
* the BB policy corridor — 65 days at the pre-cut 10.00%, which The Brief
  printed for four days after BB actually cut

This module closes that gap for indicators whose values are expected to vary.

Deliberately NOT covered
------------------------
**Standing rates** (``_STANDING_VALUE_IDS``). The policy corridor sat at 10.00%
for six years *legitimately*. There is no signal in the flatness of a policy
rate that distinguishes "BB has not moved it" from "our parser is stuck", so any
budget tight enough to have caught the 65-day freeze would have cried wolf for
the six years before it. That failure is addressed where it is actually
detectable — at the source (the BB-homepage corridor parser) and at the consumer
(The Brief now ages event-cadence metrics off the restamp date).

**Metrics outside the v3 registry.** The alarm iterates
``config/sources-v3.json`` because a registry entry is what declares a live
fetch+parse contract and a cadence to budget against. The other ~80 keys in the
flat ``data`` dict are aliases, derived ratios, retired legacy ids (landmine 4)
and the writer-less ``*_monthly`` archive series — replaying the rule over them
produced **59 alerts on day one**, all restating already-known dead writers. A
metric with no live writer is a missing-writer problem, not a stillness problem.

Detect-and-alert only. It never rejects a run or mutates ``data``: by the time
this runs the values have already landed, and a frozen number is a reason to go
look at a source, not a reason to throw away everything else in the bundle.

Warm-up
-------
The tracker starts empty and learns by observation, so ``unchanged_since`` is
seeded on first sight. Nothing can alert until a metric has been held at the
same value for its full budget *since the tracker was deployed* — 14 days for a
daily indicator, 75 for a monthly one. The already-known freezes above will
therefore re-surface on their own schedule rather than on the first run. That is
the intended trade: the alarm earns its alerts from evidence it collected
itself, instead of inheriting a backfill whose provenance it cannot check.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from utils.notifier import notify

logger = logging.getLogger(__name__)

# How long a value may sit unchanged before it stops looking like real life.
# Each budget is several publication periods wide, because repeating the same
# print is NORMAL between releases: a quarterly NPL is re-stamped daily at the
# same value for ~90 days by design. The budget has to clear that, or the alarm
# fires on healthy metrics every quarter.
#
# `daily` is 14 rather than 10 because several nominally-daily ids are really
# weekly auction results (bill_bond_rates, tbill_182d_yield, tbill_364d_yield)
# that genuinely hold their value between auctions — measured at 6 days
# unchanged on a healthy day, so 10 left almost no room for a skipped auction.
_UNCHANGED_BUDGET_DAYS: dict[str, int] = {
    "daily": 14,
    "weekly": 35,
    "monthly": 75,
    "quarterly": 200,
    "fiscal_year": 400,
}

# Standing values — see the module docstring. Flatness here is the normal state,
# not a symptom.
_STANDING_VALUE_IDS: frozenset[str] = frozenset({
    "policy_rate_repo",
    "policy_rate_sdf",
    "policy_rate_slf",
})

# Once a metric has been reported, stay quiet about it for this long. A frozen
# source usually stays frozen for weeks; re-reporting all eight food prices
# every morning would train the reader to skim past the channel.
_REALERT_AFTER_DAYS = 7

# Below this many observed runs a metric cannot be judged: `unchanged_since` is
# seeded on first sight, so a fresh state file would otherwise let every metric
# age toward its budget from the deploy date without any evidence it is stuck.
_MIN_RUNS_BEFORE_ALERTING = 3


@dataclass(frozen=True)
class StaleMetric:
    """One indicator whose value has not changed for longer than its budget."""

    indicator_id: str
    cadence: str
    value: Any
    unchanged_since: date
    days_unchanged: int
    budget_days: int


def _load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Read the tracker state, treating any damage as "start over".

    The state file is a convenience, never a source of truth — the worst case
    for losing it is that the alarm re-warms for a few runs. That is strictly
    better than an aggregate run dying on a malformed cache.
    """
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("staleness state unreadable (%s) — restarting tracker", e)
        return {}
    metrics = blob.get("metrics")
    if not isinstance(metrics, dict):
        logger.warning("staleness state malformed — restarting tracker")
        return {}
    return metrics


def _write_state(path: Path, metrics: dict[str, dict[str, Any]], today: date) -> None:
    """Persist state via a temp file + atomic replace.

    aggregate runs under systemd and can be killed mid-write; a half-written
    JSON file would be silently discarded by _load_state on the next run,
    quietly resetting every counter.
    """
    payload = {"generated_at": today.isoformat(), "metrics": metrics}
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("staleness state write failed: %s", e)


def _parse_day(raw: Any, *, today: date) -> date | None:
    """Parse a stored ISO date, rejecting anything in the future.

    A future date is not hypothetical: `debt_gdp_ratio` currently carries an
    as_of of 2031-12-31 in metric_history from a mis-parse. Treating one as
    valid would make a metric permanently un-alertable (negative age).
    """
    if not isinstance(raw, str):
        return None
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None
    return None if d > today else d


def check_value_staleness(
    data: dict[str, Any],
    registry: list[dict],
    *,
    today: date,
    state_path: Path,
    notifier: Callable[..., Any] = notify,
) -> list[StaleMetric]:
    """Track how long each registry indicator has held the same value; alert on
    the ones that have held it too long.

    Args:
        data: The flat metric_id -> value dict aggregate_latest assembles.
        registry: ``config/sources-v3.json``'s ``indicators`` list — supplies
            the id set and the cadence each budget is derived from.
        today: Run date (UTC), injected so tests are not clock-dependent.
        state_path: Where the per-metric tracker lives across runs.
        notifier: Seam for tests; defaults to the real Discord notifier.

    Returns:
        The metrics that breached their budget on THIS run, whether or not an
        alert was actually sent (a metric inside its re-alert quiet period is
        still returned — the caller may want to log it).
    """
    state = _load_state(state_path)
    fresh_state: dict[str, dict[str, Any]] = {}
    breached: list[StaleMetric] = []
    to_report: list[StaleMetric] = []

    for ind in registry:
        indicator_id = ind.get("id")
        cadence = ind.get("cadence")
        if not indicator_id or indicator_id in _STANDING_VALUE_IDS:
            continue

        value = data.get(indicator_id)
        prior = state.get(indicator_id) or {}
        # bool is an int subclass; no v3 indicator is boolean, but an accidental
        # True would otherwise compare equal to 1.0 forever.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            # Missing/non-numeric THIS RUN (a transient parse hiccup, a fetch
            # failure, an Opus quarantine of just this field) -- carry the
            # prior entry forward UNCHANGED rather than dropping it. Dropping
            # it here (the old behaviour) let a metric's `unchanged_since`
            # clock silently reset to today the moment it reappeared, even
            # with the exact same stale value it had before the gap -- this
            # is how a genuinely 63-day-frozen metric was once reported as
            # merely "15d" frozen (one missing run zeroed the counter, and it
            # had re-accumulated only 15 more days by the time anyone looked).
            # A brand-new id (never seen before) has no prior entry and stays
            # correctly absent from fresh_state -- only an id THIS RUN found
            # missing but that HAD state carries it forward.
            if prior:
                fresh_state[indicator_id] = prior
            continue
        prior_value = prior.get("value")
        unchanged_since = _parse_day(prior.get("unchanged_since"), today=today)
        runs = prior.get("runs")
        runs = runs + 1 if isinstance(runs, int) and runs > 0 else 1

        moved = prior_value != value or unchanged_since is None
        if moved:
            unchanged_since = today
            runs = 1

        entry: dict[str, Any] = {
            "value": value,
            "unchanged_since": unchanged_since.isoformat(),
            "runs": runs,
        }

        budget = _UNCHANGED_BUDGET_DAYS.get(cadence)
        days_unchanged = (today - unchanged_since).days
        if budget is not None and days_unchanged >= budget and runs >= _MIN_RUNS_BEFORE_ALERTING:
            stale = StaleMetric(
                indicator_id=indicator_id,
                cadence=cadence,
                value=value,
                unchanged_since=unchanged_since,
                days_unchanged=days_unchanged,
                budget_days=budget,
            )
            breached.append(stale)
            last_alerted = _parse_day(prior.get("last_alerted"), today=today)
            quiet_until = (
                last_alerted + timedelta(days=_REALERT_AFTER_DAYS)
                if last_alerted is not None
                else None
            )
            if quiet_until is None or today >= quiet_until:
                to_report.append(stale)
                entry["last_alerted"] = today.isoformat()
            else:
                entry["last_alerted"] = last_alerted.isoformat()
        elif isinstance(prior.get("last_alerted"), str) and not moved:
            entry["last_alerted"] = prior["last_alerted"]

        fresh_state[indicator_id] = entry

    # fresh_state is rebuilt from the registry each run, so ids that were
    # removed or renamed drop out instead of accumulating forever.
    _write_state(state_path, fresh_state, today)

    if breached:
        logger.warning(
            "%d indicator(s) unchanged beyond budget: %s",
            len(breached),
            ", ".join(f"{s.indicator_id}({s.days_unchanged}d)" for s in breached),
        )

    if to_report:
        _send_report(to_report, notifier=notifier)

    return breached


def _send_report(stale: list[StaleMetric], *, notifier: Callable[..., Any]) -> None:
    """One batched alert for the whole run.

    Never one notify() per metric: the food-price freeze alone would have sent
    eight identical-looking alerts every morning for three months.
    """
    ordered = sorted(stale, key=lambda s: -s.days_unchanged)
    lines = [
        f"- {s.indicator_id} ({s.cadence}): {s.value} unchanged since "
        f"{s.unchanged_since.isoformat()} — {s.days_unchanged}d, budget {s.budget_days}d"
        for s in ordered
    ]
    worst = ordered[0]
    notifier(
        "warning",
        f"{len(ordered)} indicator(s) frozen",
        (
            "These values have not changed in longer than their cadence allows. "
            "A frozen value usually means the fetch still returns HTTP 200 while "
            "the source stopped updating, or the parser is locked onto a table "
            "that no longer moves — check the source page before trusting them.\n\n"
            + "\n".join(lines)
        ),
        {
            "worst": f"{worst.indicator_id} ({worst.days_unchanged}d)",
            "count": str(len(ordered)),
        },
    )


# ---------------------------------------------------------------------------
# Watchlist staleness — as_of/ingest-aware predicates for load-bearing ids
# ---------------------------------------------------------------------------
#
# check_value_staleness above answers one question — "has this VALUE stopped
# changing?" — for every v3-registry indicator, budgeted purely by cadence.
# That is deliberately coarse: it can't reach a few financially load-bearing
# ids at all (``gross_reserves_usd_bn`` is a Tier-1/bb_forex flatten_data key,
# never a v3 registry id; ``nbr_fytd_collected_cr`` is a BRIEF_ALIASES COPY of
# ``tax_revenue`` — the registry watches the source key, never the alias The
# Brief actually reads, the same "verify the key the consumer reads" class of
# gap as AGENTS.md landmine 47), and for the ids it DOES reach, it can only
# tell "frozen" from "moving" by raw value equality — a monthly rate that
# happens to print the same number twice in a row (9.2% inflation two months
# running) looks identical to a genuinely stuck parser.
#
# This section adds a SHARPER, THREE-PREDICATE test for a small, explicit
# watchlist of ids, using each metric's own ``as_of`` (the source's reporting
# period, already computed in-process by aggregate_latest's
# `_build_tier1_source_as_of_map` / `_build_source_as_of_map` before the
# Supabase write) alongside its value:
#
#   (a) value frozen while as_of advances  — the source claims a NEW
#       reporting period arrived, but the number is bit-for-bit identical to
#       the last one. For a genuinely live monthly flow/rate this is
#       vanishingly unlikely twice running — it is the signature of as_of
#       FORGERY (a parser re-stamping a stale read with a fresh-looking date,
#       landmine 26/47's failure class) more than of real stability.
#   (b) as_of frozen while ingested_at advances — the pipeline keeps running
#       and writing every day, but the reporting period itself has not moved
#       in far longer than this id's normal publish cycle. Distinct from (a):
#       this is the classic "the document itself never got a newer edition"
#       bug (landmine 34), not a forged date.
#   (c) no ingest at all for N days — the id is missing from `data` entirely,
#       a stronger signal than "present but unchanged".
#
# ``ingested_at`` is never re-read from Supabase here (this module has no DB
# access and none is needed): a watchlist id present in `data` this run WILL
# be posted with ``ingested_at=now()`` by upsert_metric_history's per-row
# stamping (fixed for exactly this reason by the 2026-07-09 "22 indicators
# frozen" incident) — so "present in `data` this run" IS "ingested_at
# advanced this run", and no live read is needed to know it. The one edge
# this simplification doesn't cover: an Opus hard-reject discards the WHOLE
# run's write after this check has already run (see aggregate_latest.py) —
# that failure mode has its own, separate Discord alert already, so it is
# out of scope here rather than double-covered.


@dataclass(frozen=True)
class _WatchlistBudget:
    """Per-id-class thresholds for check_watchlist_staleness."""

    as_of_frozen_days: int
    no_ingest_days: int


# All nine ids below share one real-world class — "monthly cadence, ~30-day
# publish cycle" (BB reserves, BB credit-growth print, EPB/BB trade & FYTD
# flashes, the CPI trio) — so one budget set covers them for now. A future
# addition with a materially different cadence should get its OWN
# _WatchlistBudget, not reuse this default blindly.
#
# as_of_frozen_days=60 is double the ~30-day publish cycle (one full missed
# release before alerting, mirroring check_value_staleness's "budget clears
# several publication periods" discipline) so a single late release never
# trips it; no_ingest_days=10 is short because the DAILY aggregate pipeline
# touches every one of these ids every run regardless of whether the
# underlying source has moved — a stretch with literally nothing written
# means the scrape/parse step itself broke, not that the source is merely
# slow.
_MONTHLY_WATCHLIST_BUDGET = _WatchlistBudget(as_of_frozen_days=60, no_ingest_days=10)

# indicator_id -> budget. Extend this dict (never config/sources-v3.json —
# several of these ids are not even v3 registry entries) to widen the
# watchlist.
WATCHLIST_IDS: dict[str, _WatchlistBudget] = {
    "gross_reserves_usd_bn": _MONTHLY_WATCHLIST_BUDGET,
    "private_sector_credit_yoy_pct": _MONTHLY_WATCHLIST_BUDGET,
    "monthly_export": _MONTHLY_WATCHLIST_BUDGET,
    "monthly_import": _MONTHLY_WATCHLIST_BUDGET,
    "monthly_remittance": _MONTHLY_WATCHLIST_BUDGET,
    "nbr_fytd_collected_cr": _MONTHLY_WATCHLIST_BUDGET,
    "general_inflation": _MONTHLY_WATCHLIST_BUDGET,
    "food_inflation": _MONTHLY_WATCHLIST_BUDGET,
    "non_food_inflation": _MONTHLY_WATCHLIST_BUDGET,
}

# Predicate (a) requires this many CONSECUTIVE occurrences before alerting —
# guards against a single coincidental repeat (two consecutive genuinely
# identical prints) being mistaken for forgery on its very first sighting.
_MIN_CONSECUTIVE_FROZEN_ADVANCES = 2

# Re-alert throttle, mirroring check_value_staleness's _REALERT_AFTER_DAYS:
# without this, a persistent breach would notify every single day forever,
# since each run is a fresh systemd-launched process and notify()'s own
# (level, title) dedup cannot collapse repeats across process boundaries.
_WATCHLIST_REALERT_AFTER_DAYS = _REALERT_AFTER_DAYS


@dataclass(frozen=True)
class WatchlistBreach:
    """One watchlist id's date-integrity anomaly on this run."""

    indicator_id: str
    predicate: str  # "value_frozen_as_of_advanced" | "as_of_frozen" | "no_ingest"
    detail: str
    value: Any = None
    as_of: date | None = None


def check_watchlist_staleness(
    data: dict[str, Any],
    source_as_of: dict[str, date],
    *,
    today: date,
    state_path: Path,
    notifier: Callable[..., Any] = notify,
    watchlist: dict[str, _WatchlistBudget] | None = None,
) -> list[WatchlistBreach]:
    """Run the three as_of/ingest-aware failure-shape predicates over a small,
    explicit watchlist of load-bearing ids.

    Args:
        data: the flat metric_id -> value dict aggregate_latest assembles
            (post v3-merge and post brief-alias application, so alias ids
            like ``nbr_fytd_collected_cr`` are present under their own name).
        source_as_of: metric_id -> the source's reporting-period date for
            THIS run, if recovered (the merged
            ``_build_tier1_source_as_of_map`` / ``_build_source_as_of_map``
            output aggregate_latest builds before its Supabase write). An id
            absent here means "no as_of recovered this run" — predicates
            (a)/(b) are skipped for it that run, never guessed at.
        today: run date (UTC), injected so tests are not clock-dependent.
        state_path: where this tracker's per-id state lives across runs
            (a SEPARATE file from check_value_staleness's — the two trackers
            have different shapes and must not collide).
        notifier: seam for tests; defaults to the real Discord notifier.
        watchlist: override for tests; defaults to ``WATCHLIST_IDS``.

    Returns:
        Every breach detected on THIS run, whether or not an alert was
        actually sent (an id inside its re-alert quiet period is still
        returned).
    """
    watchlist = WATCHLIST_IDS if watchlist is None else watchlist
    state = _load_state(state_path)
    fresh_state: dict[str, dict[str, Any]] = {}
    breached: list[WatchlistBreach] = []
    to_report: list[WatchlistBreach] = []

    for indicator_id, budget in watchlist.items():
        prior = state.get(indicator_id) or {}
        raw_value = data.get(indicator_id)
        has_value = isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool)
        as_of = source_as_of.get(indicator_id)

        if not has_value:
            entry, breach = _watchlist_missing_run(indicator_id, prior, budget, today=today)
            if entry is not None:
                fresh_state[indicator_id] = entry
            if breach is not None:
                breached.append(breach)
                if _should_report(prior, today=today):
                    to_report.append(breach)
                    # L4 (2026-08-22 round-1 review): _watchlist_missing_run's
                    # current contract only ever returns a non-None breach
                    # alongside a non-None entry (which the `if entry is not
                    # None` above has already stored), but guard the write
                    # explicitly rather than relying on that invariant holding
                    # forever across a future edit to that helper -- a broken
                    # invariant here would otherwise KeyError on the very run
                    # that most needs this alert to land.
                    if indicator_id in fresh_state:
                        fresh_state[indicator_id]["last_alerted"] = today.isoformat()
                    else:
                        logger.warning(
                            "watchlist staleness: %s has a breach but no state "
                            "entry was recorded for it — last_alerted not set",
                            indicator_id,
                        )
            continue

        entry, breach = _watchlist_present_run(
            indicator_id, raw_value, as_of, prior, budget, today=today
        )
        fresh_state[indicator_id] = entry
        if breach is not None:
            breached.append(breach)
            if _should_report(prior, today=today):
                to_report.append(breach)
                entry["last_alerted"] = today.isoformat()
            elif isinstance(prior.get("last_alerted"), str):
                entry["last_alerted"] = prior["last_alerted"]

    _write_state(state_path, fresh_state, today)

    if breached:
        logger.warning(
            "%d watchlist metric(s) show a date-integrity anomaly: %s",
            len(breached),
            ", ".join(f"{b.indicator_id}[{b.predicate}]" for b in breached),
        )

    if to_report:
        _send_watchlist_report(to_report, notifier=notifier)

    return breached


def _should_report(prior: dict[str, Any], *, today: date) -> bool:
    """True if enough time has passed since this id's last alert (or it has
    never alerted) to speak again."""
    last_alerted = _parse_day(prior.get("last_alerted"), today=today)
    if last_alerted is None:
        return True
    return today >= last_alerted + timedelta(days=_WATCHLIST_REALERT_AFTER_DAYS)


def _watchlist_missing_run(
    indicator_id: str, prior: dict[str, Any], budget: _WatchlistBudget, *, today: date,
) -> tuple[dict[str, Any] | None, WatchlistBreach | None]:
    """Predicate (c): the id is absent from `data` entirely this run.

    Carries the prior entry forward UNCHANGED (never resets it — the exact
    counter-reset bug fixed in check_value_staleness above) and separately
    tracks how long it has been since this id last had a real value, which is
    what predicate (c) actually measures.
    """
    if not prior:
        # Never once seen with a value -- nothing to measure a gap against yet.
        return None, None

    entry = dict(prior)
    last_seen = _parse_day(prior.get("last_seen"), today=today)
    if last_seen is None:
        return entry, None

    days_missing = (today - last_seen).days
    if days_missing < budget.no_ingest_days:
        return entry, None

    return entry, WatchlistBreach(
        indicator_id=indicator_id,
        predicate="no_ingest",
        detail=(
            f"no ingest for {days_missing}d (budget {budget.no_ingest_days}d) — "
            f"last real value {entry.get('value')!r} seen {last_seen.isoformat()}"
        ),
        value=entry.get("value"),
        as_of=_parse_day(entry.get("as_of"), today=today),
    )


def _watchlist_present_run(
    indicator_id: str,
    value: Any,
    as_of: date | None,
    prior: dict[str, Any],
    budget: _WatchlistBudget,
    *,
    today: date,
) -> tuple[dict[str, Any], WatchlistBreach | None]:
    """Predicates (a) and (b): the id has a value this run (== ingested this
    run, see the module-level note on why no live ingested_at read is
    needed)."""
    prior_value = prior.get("value")
    prior_as_of = _parse_day(prior.get("as_of"), today=today)
    as_of_unchanged_since = _parse_day(prior.get("as_of_unchanged_since"), today=today)
    frozen_advances = prior.get("frozen_advances", 0)
    frozen_advances = frozen_advances if isinstance(frozen_advances, int) else 0

    value_same = prior_value is not None and value == prior_value
    as_of_moved = as_of is not None and prior_as_of is not None and as_of > prior_as_of
    as_of_same = as_of is not None and prior_as_of is not None and as_of == prior_as_of

    breach: WatchlistBreach | None = None

    # Predicate (a): value frozen while as_of advances. Only touch the
    # counter on a run where as_of ACTUALLY moved -- these ids are monthly,
    # so as_of only advances on roughly 1 run in 30. The bug this replaces
    # reset frozen_advances to 0 on every run where as_of hadn't moved (the
    # `else 0` used to fire unconditionally whenever `as_of_moved` was
    # False), which is ~29 of every 30 daily runs for a monthly id -- the
    # counter died every single day before it could ever reach the
    # consecutive-occurrence threshold below. Simulated at production
    # cadence (365 daily runs against a monthly as_of): the old code never
    # fired once. On a run where as_of did NOT move, frozen_advances is left
    # exactly as it was -- there is nothing new to evaluate yet.
    if as_of_moved:
        frozen_advances = frozen_advances + 1 if value_same else 0
    if frozen_advances >= _MIN_CONSECUTIVE_FROZEN_ADVANCES:
        breach = WatchlistBreach(
            indicator_id=indicator_id,
            predicate="value_frozen_as_of_advanced",
            detail=(
                f"value stuck at {value!r} while as_of advanced "
                f"{prior_as_of.isoformat()} -> {as_of.isoformat()} "
                f"({frozen_advances} consecutive times) — possible as_of forgery"
            ),
            value=value,
            as_of=as_of,
        )

    # Predicate (b): as_of frozen for far longer than this id's publish cycle,
    # while the pipeline keeps ingesting it every run (this branch IS an
    # ingest, by construction). Skipped entirely if the run has no as_of at
    # all for this id (nothing to judge staleness of).
    #
    # `as_of_unchanged_since` tracks the RUN DATE (`today`) on which this
    # as_of value was FIRST observed -- never the as_of value itself. Setting
    # it to `as_of` would compare a reporting-period date against a run date
    # on the very first sighting (e.g. as_of=Jan-31 vs today=May-1 reads as
    # "frozen 90 days" immediately, before the metric has been observed even
    # twice) -- exactly the same "wrong clock" mistake `unchanged_since`
    # in check_value_staleness above deliberately avoids.
    if as_of is None:
        as_of_unchanged_since = None
    elif as_of_same:
        if as_of_unchanged_since is None:
            as_of_unchanged_since = today
    else:
        as_of_unchanged_since = today

    if breach is None and as_of is not None and as_of_unchanged_since is not None:
        days_frozen = (today - as_of_unchanged_since).days
        if days_frozen >= budget.as_of_frozen_days:
            breach = WatchlistBreach(
                indicator_id=indicator_id,
                predicate="as_of_frozen",
                detail=(
                    f"as_of stuck at {as_of.isoformat()} for {days_frozen}d "
                    f"(budget {budget.as_of_frozen_days}d) while the pipeline "
                    "keeps ingesting"
                ),
                value=value,
                as_of=as_of,
            )

    entry: dict[str, Any] = {
        "value": value,
        "as_of": as_of.isoformat() if as_of is not None else None,
        "as_of_unchanged_since": (
            as_of_unchanged_since.isoformat() if as_of_unchanged_since is not None else None
        ),
        "frozen_advances": frozen_advances,
        "last_seen": today.isoformat(),
    }
    return entry, breach


def _send_watchlist_report(
    breaches: list[WatchlistBreach], *, notifier: Callable[..., Any]
) -> None:
    """One batched alert for the whole run — never one notify() per id."""
    lines = [f"- `{b.indicator_id}` [{b.predicate}]: {b.detail}" for b in breaches]
    notifier(
        "warning",
        f"{len(breaches)} watchlist metric(s) show a date-integrity anomaly",
        (
            "These load-bearing metrics show a value/as_of/ingest pattern that "
            "does not fit a healthy source: either the value is frozen while the "
            "reporting period keeps advancing (possible as_of forgery), the "
            "reporting period itself has been stuck far longer than its normal "
            "publish cycle, or the pipeline has stopped ingesting it entirely.\n\n"
            + "\n".join(lines)
        ),
        {"count": str(len(breaches))},
    )
