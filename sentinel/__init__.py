"""Freshness sentinel — the daily dead-man's-switch for data staleness.

Every one of EconDelta's 16 timers fires on schedule and logs OK while the
data underneath can silently freeze (E1.1 inflation family, E1.2 DSE, E1.5
pink sheet). Run-health is not data-freshness. This package is the one place
that asks the only question that would have caught every silent freeze:

    for each metric_id, is its latest reporting vintage (as_of) still within
    the grace window its cadence allows?

It reads BOTH history tables, joins each metric to its cadence, flags breaches,
and posts ONE Discord digest to #econdelta-alerts. It writes run_logs under
``source='freshness_sentinel'`` so The Brief's off-box heartbeat can tell when
the sentinel itself goes quiet. (Phase E2.1.)
"""
