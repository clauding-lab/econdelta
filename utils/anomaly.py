"""Per-metric threshold checks — prevent bad data from corrupting latest.json."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 0.10  # 10% default if metric not in thresholds.json


def check_threshold(
    metric_name: str,
    new_value: float,
    prev_value: float | None,
    thresholds: dict[str, float],
) -> tuple[bool, float]:
    """Check whether a new metric value is within the allowed daily change.

    Args:
        metric_name: Key used to look up the threshold (e.g. "dsex", "usd_bdt_mid").
        new_value: The freshly scraped value.
        prev_value: The previously accepted value, or None on first run.
        thresholds: Dict of metric -> fractional threshold from thresholds.json.

    Returns:
        Tuple of (ok, pct_change) where:
          ok         -- True if the change is within threshold, False if anomalous.
          pct_change -- Absolute fractional change (0.05 means 5%). 0.0 on first run.
    """
    if prev_value is None or prev_value == 0:
        # First run or division-by-zero guard: accept unconditionally.
        return (True, 0.0)

    pct_change = abs(new_value - prev_value) / abs(prev_value)
    threshold = thresholds.get(metric_name, _DEFAULT_THRESHOLD)
    ok = pct_change <= threshold

    if not ok:
        logger.warning(
            "Anomaly detected for %r: %.4f -> %.4f (%.2f%% change, threshold %.2f%%)",
            metric_name,
            prev_value,
            new_value,
            pct_change * 100,
            threshold * 100,
        )

    return (ok, pct_change)


def load_thresholds(path: str | Path) -> dict[str, float]:
    """Load per-metric anomaly thresholds from a JSON file.

    Args:
        path: Path to thresholds.json (e.g. "config/thresholds.json").

    Returns:
        Dict mapping metric names to fractional thresholds.
        The "_meta" key is stripped automatically.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file contains invalid JSON or non-numeric thresholds.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw: dict = json.load(fh)

    return {k: float(v) for k, v in raw.items() if k != "_meta"}
