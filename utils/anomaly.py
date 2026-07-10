"""Per-metric threshold checks — prevent bad data from corrupting latest.json."""

import json
import logging
from pathlib import Path

from utils.notifier import notify

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 0.10  # 10% default if metric not in thresholds.json

# Bangladesh Bank policy corridor. The Standing Deposit Facility (SDF) is the
# floor, the policy repo rate sits in the middle, and the Standing Lending
# Facility (SLF) is the ceiling. A healthy corridor always satisfies
# SDF <= repo <= SLF. The three legs are parsed independently (one
# ``pdf_table_column_latest`` sources-v3 entry each), so no single parser ever
# sees all three — the coherence check below runs at aggregate time instead.
CORRIDOR_SDF_ID = "policy_rate_sdf"
CORRIDOR_REPO_ID = "policy_rate_repo"
CORRIDOR_SLF_ID = "policy_rate_slf"


def check_corridor_coherence(data: dict) -> bool:
    """Verify the BB policy corridor invariant SDF <= repo <= SLF.

    Detect-and-alert only. On a genuine ordering violation among three present
    numeric legs it fires a loud ``notify("error", ...)`` and returns False.
    It does NOT reject the run: the three values already landed in
    metric_history at parse time, so there is nothing to reject — this is a
    cross-metric health check that surfaces a mis-ordered corridor loudly
    instead of letting it pass silently.

    Args:
        data: The assembled flat latest-values dict (metric_id -> value) that
            aggregate_latest builds; the corridor legs are read by their ids.

    Returns:
        True when the corridor is coherent OR when any leg is missing/
        non-numeric (absent data must never false-alarm). False only on a
        real ordering violation among three present numeric legs.
    """
    sdf = data.get(CORRIDOR_SDF_ID)
    repo = data.get(CORRIDOR_REPO_ID)
    slf = data.get(CORRIDOR_SLF_ID)

    if not all(isinstance(v, (int, float)) for v in (sdf, repo, slf)):
        # A missing or non-numeric leg — skip silently, never false-alarm.
        return True

    if sdf <= repo <= slf:
        return True

    logger.error(
        "policy corridor incoherent: %s=%s, %s=%s, %s=%s (expected SDF <= repo <= SLF)",
        CORRIDOR_SDF_ID, sdf, CORRIDOR_REPO_ID, repo, CORRIDOR_SLF_ID, slf,
    )
    notify(
        "error",
        "policy corridor incoherent",
        (
            f"Bangladesh Bank policy corridor violates SDF <= repo <= SLF: "
            f"{CORRIDOR_SDF_ID}={sdf}, {CORRIDOR_REPO_ID}={repo}, "
            f"{CORRIDOR_SLF_ID}={slf}. A leg is likely mis-parsed — verify "
            f"against BB's current MPS before trusting these rates."
        ),
    )
    return False


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
