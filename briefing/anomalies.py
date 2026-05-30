"""Deterministic anomaly-candidate computation. Pure functions — no I/O.

Numbers come from here, never from Claude. Each candidate has a stable id so
the model can reference (and only reference) it in featured_anomalies.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

_ZSCORE_MIN_POINTS = 8
_ZSCORE_FLAG = 2.0


@dataclass(frozen=True)
class AnomalyCandidate:
    candidate_id: str
    metric_id: str
    label: str
    stat: str
    value: float
    detail: str
    severity: str  # 'up' | 'down' | 'warn'


def _values_newest_first(rows: list[dict]) -> list[float]:
    out = []
    for r in rows:
        try:
            out.append(float(r["value"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def compute_candidates(series_by_metric: dict[str, list[dict]],
                       thresholds: dict[str, float | None],
                       cadence: dict[str, str],
                       labels: dict[str, str] | None = None) -> list[AnomalyCandidate]:
    labels = labels or {}
    out: list[AnomalyCandidate] = []
    for metric_id, rows in series_by_metric.items():
        vals = _values_newest_first(rows)
        if len(vals) < 2:
            continue
        latest, prev = vals[0], vals[1]
        label = labels.get(metric_id, metric_id)

        # Rule 1: change vs prior reading
        thr = thresholds.get(metric_id)
        if thr is not None:
            delta = latest - prev
            if abs(delta) >= thr:
                out.append(AnomalyCandidate(
                    candidate_id=f"{metric_id}:change",
                    metric_id=metric_id, label=label,
                    stat="change vs prior",
                    value=latest,
                    detail=f"{'+' if delta >= 0 else ''}{delta:.2f} vs prior {prev:.2f} (limit {thr})",
                    severity="up" if delta > 0 else "down",
                ))

        # Rule 2: z-score vs trailing mean (exclude the latest point)
        trailing = vals[1:]
        if len(trailing) >= _ZSCORE_MIN_POINTS:
            mean = statistics.fmean(trailing)
            stdev = statistics.pstdev(trailing)
            if stdev > 0:
                z = (latest - mean) / stdev
                if abs(z) >= _ZSCORE_FLAG:
                    out.append(AnomalyCandidate(
                        candidate_id=f"{metric_id}:zscore",
                        metric_id=metric_id, label=label,
                        stat="σ vs trailing mean",
                        value=latest,
                        detail=f"{z:+.1f}σ vs {len(trailing)}-pt mean {mean:.2f}",
                        severity="warn",
                    ))
    return out
