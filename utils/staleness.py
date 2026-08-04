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
        # bool is an int subclass; no v3 indicator is boolean, but an accidental
        # True would otherwise compare equal to 1.0 forever.
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue

        prior = state.get(indicator_id) or {}
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
