"""Tests never write the real, git-ignored data/ directory.

aggregate_latest.main() keeps state between runs in data/ (latest.json, archive/latest_<date>.json,
staleness and alert de-dup files). Tests that ran main() without redirecting every one of those
paths used to overwrite the real files, and a leftover alert de-dup file could silence a later
test's alert. tests/conftest.py (isolated_aggregate_state, isolated_monthly_evidence) points them
at a per-test temporary directory; this test fails if any module-level path in those modules
still points into the real data/ while a test runs — including a path constant added later and
not yet redirected.
"""
from __future__ import annotations

from pathlib import Path

import aggregate_latest
import utils.monthly_evidence
import utils.supabase_writer

REAL_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _paths_inside_real_data(module) -> list[str]:
    return sorted(
        f"{module.__name__}.{name} = {value}"
        for name, value in vars(module).items()
        if isinstance(value, Path) and value.resolve().is_relative_to(REAL_DATA_DIR)
    )


def test_no_aggregate_state_path_points_at_the_real_data_dir_during_tests() -> None:
    leaks = [
        leak
        for module in (aggregate_latest, utils.supabase_writer, utils.monthly_evidence)
        for leak in _paths_inside_real_data(module)
    ]
    assert leaks == [], f"tests would write the real data/ directory through: {leaks}"
