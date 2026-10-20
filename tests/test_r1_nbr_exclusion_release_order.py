"""R1 NBR restamp exclusion (owner decisions d + l) versus the new producer's period rows.

R2 fix R1B round 1 (review finding, 29 Sep 2026 BDT). Business rule: the R1 exclusion must
never delete an NBR row the new producer wrote at its source's own period, and must never
apply half a batch. The reviewed window 2026-05-02..2026-09-24 holds month-end keys
(2026-06-30, 2026-07-31). On its first night the new producer (fix H5 plus source-period
dating) writes the FY26 total to (id, 2026-06-30), the key of a reviewed restamp row. So R1
must be applied BEFORE that first night: afterwards the reviewed before-image no longer
matches, the engine refuses the whole batch and writes nothing, and a recapture is refused.

The night is the real producer on the shared contract's real 25 Sep input (WSEI 415,473 crore
for FY26, dated 2026-06-30). The restamp rows carry the reviewed keys and values of the hashed
25 Sep backup, as bound in scripts/history_repair_candidates.py; their ingested_at stamps are
SYNTHETIC.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import aggregate_latest as agg
from scripts.history_repair_candidates import (
    NBR_PARENT_RESTAMP_REASON,
    NBR_PARENT_RESTAMP_RUNS,
    NBR_RESTAMP_ID,
    NBR_RESTAMP_REASON,
    NBR_RESTAMP_RUNS,
    nbr_restamp_rows,
    reviewed_nbr_restamp_dates,
    reviewed_nbr_restamp_values,
)
from scripts.repair_observation_history import (
    ApplyReceipt,
    RepairConflict,
    apply_manifest,
    file_hash,
    write_json,
)
from tests.test_shared_case_contract import BINDING, CASES, RUN, _changed, _produce
from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

Row = dict[str, Any]
TARGET = "snapshot:r1-nbr-release-order"
# id -> (reviewed runs, owner decision, manifest reason), in the generator's order.
FAMILIES: dict[str, tuple[tuple[tuple[float, str, str], ...], str, str]] = {
    NBR_RESTAMP_ID: (NBR_RESTAMP_RUNS, "(d)", NBR_RESTAMP_REASON),
    **{
        mid: (runs, "(l)", NBR_PARENT_RESTAMP_REASON)
        for mid, runs in NBR_PARENT_RESTAMP_RUNS.items()
    },
}
SOURCE_PERIOD = "2026-06-30"  # WSEI "FY26" in the real 25 Sep input


class MetricHistory:
    """In-memory metric_history keyed by (metric_id, as_of): the repair engine's Store and the
    producer's merge-upsert target (payload columns overwrite; an insert starts provenance null)."""

    target = TARGET

    def __init__(self, rows: list[Row]) -> None:
        self.rows: dict[tuple[str, str], Row] = {
            (r["metric_id"], r["as_of"]): dict(r) for r in rows
        }
        self.repair_writes = 0

    def get(self, table: str, key: dict[str, str]) -> Row | None:
        assert table == "metric_history"
        return copy.deepcopy(self.rows.get((key["metric_id"], key["as_of"])))

    def change(self, op: dict[str, Any]) -> None:
        key = (op["key"]["metric_id"], op["key"]["as_of"])
        if op["after"] is None:
            del self.rows[key]
        else:
            self.rows[key] = copy.deepcopy(op["after"])
        self.repair_writes += 1

    def upsert(self, payload: Row) -> None:
        key = (payload["metric_id"], payload["as_of"])
        self.rows[key] = {"provenance": None, **self.rows.get(key, {}), **payload}

    def read_at(self, keys: list[tuple[str, str]], **_: Any) -> list[Row]:
        return [dict(self.rows[tuple(k)]) for k in keys if tuple(k) in self.rows]

    def family(self, metric_id: str) -> list[tuple[str, float]]:
        return sorted(
            (day, row["value"]) for (mid, day), row in self.rows.items() if mid == metric_id
        )


def _reviewed_backup() -> list[Row]:
    """The 420 reviewed restamp rows (child, parent, alias), accepted by the generator's own check."""
    rows: list[Row] = []
    for mid, (runs, decision, _) in FAMILIES.items():
        family = [
            {
                "metric_id": mid,
                "as_of": day,
                "value": value,
                "source": "EconDelta",
                "provenance": None,
                "ingested_at": f"{day}T00:00:00+00:00",
            }
            for day, value in zip(reviewed_nbr_restamp_dates(), reviewed_nbr_restamp_values(runs))
        ]
        rows.extend(nbr_restamp_rows(family, mid, runs, decision))
    return rows


def _exclusion_manifest(tmp_path: Path, backup: list[Row]) -> Path:
    """One exclusion per reviewed row, shaped like the round-3 candidate's (d)/(l) operations."""
    backup_path = tmp_path / "metric_history.json"
    write_json(backup_path, backup)
    ref = {
        "path": str(backup_path),
        "sha256": file_hash(backup_path),
        "locator": "SYNTHETIC backup of the reviewed NBR restamp rows",
    }
    operations = [
        {
            "operation_id": f"metric_history:{r['metric_id']}:{r['as_of']}",
            "table": "metric_history",
            "key": {"metric_id": r["metric_id"], "as_of": r["as_of"]},
            "before": r,
            "after": None,
            "reason": FAMILIES[r["metric_id"]][2],
            "evidence": [ref],
            "requires": [],
        }
        for r in backup
    ]
    manifest = {
        "version": 1,
        "target_project": "test",
        "target": TARGET,
        "code_commits": {"econdelta": "a" * 40, "brief": "b" * 40},
        "generated_at": "2026-09-29T00:00:00+00:00",
        "backups": [
            {
                "table": "metric_history",
                "path": str(backup_path),
                "sha256": file_hash(backup_path),
                "rows": len(backup),
            }
        ],
        "operations": operations,
    }
    path = tmp_path / "manifest.json"
    write_json(path, manifest)
    return path


def _apply(path: Path, store: MetricHistory, tmp_path: Path) -> ApplyReceipt:
    return apply_manifest(
        path,
        expected_sha256=file_hash(path),
        target=TARGET,
        store=store,
        receipts_path=tmp_path / "receipts.json",
    )


def _first_night(store: MetricHistory) -> list[Row]:
    """The new producer's first aggregate night on the real 25 Sep input; the NBR rows it wrote."""
    run = next(r for r in CASES["nbr_fytd_period_rows"]["runs"] if r["run"] == "first-night")
    observations = _produce(_changed(BINDING["producer_inputs"], run["input_changes"]), RUN)[
        "observations"
    ]
    values = {mid: obs.value for mid, obs in observations.items() if obs.value is not None}
    to_write, _ = agg._unrecorded_nbr_fytd(
        values, observations, today=RUN.date(), reader=store.read_at
    )
    rows = [
        row
        for row in _rows_from_data(
            to_write, RUN.date(), _DEFAULT_SOURCE, ingested_at=RUN, observations=observations
        )
        if row["metric_id"] in FAMILIES
    ]
    for row in rows:
        store.upsert(row)
    return rows


def test_the_new_producers_first_night_writes_each_nbr_id_onto_a_reviewed_restamp_key():
    """The release-order premise: the properly dated FY26 row collides with a reviewed key."""
    store = MetricHistory(_reviewed_backup())
    reviewed = {(r["metric_id"], r["as_of"]): r["value"] for r in _reviewed_backup()}

    sent = _first_night(store)

    assert sorted((r["metric_id"], r["as_of"]) for r in sent) == sorted(
        (mid, SOURCE_PERIOD) for mid in FAMILIES
    )
    assert SOURCE_PERIOD in reviewed_nbr_restamp_dates()
    assert all(r["value"] != reviewed[(r["metric_id"], r["as_of"])] for r in sent)
    assert {r["metric_id"]: r["value"] for r in sent if r["metric_id"] != NBR_RESTAMP_ID} == {
        "tax_revenue": 415473.0,
        "nbr_fytd_collected_cr": 415473.0,
    }


def test_r1_applied_after_the_new_producers_first_night_refuses_whole_and_keeps_its_period_rows(
    tmp_path,
):
    backup = _reviewed_backup()
    store = MetricHistory(backup)
    manifest = _exclusion_manifest(tmp_path, backup)
    sent = _first_night(store)
    after_night = copy.deepcopy(store.rows)

    with pytest.raises(RepairConflict, match=f"{NBR_RESTAMP_ID}:{SOURCE_PERIOD}"):
        _apply(manifest, store, tmp_path)

    assert store.repair_writes == 0
    assert store.rows == after_night
    assert all(store.rows[(r["metric_id"], r["as_of"])]["value"] == r["value"] for r in sent)


def test_a_recapture_after_the_new_producers_first_night_is_refused_for_every_nbr_id():
    """Regenerating (d)+(l) from a post-night snapshot must not silently absorb the producer's row."""
    store = MetricHistory(_reviewed_backup())
    _first_night(store)
    recaptured = list(store.rows.values())

    for mid, (runs, decision, _) in FAMILIES.items():
        with pytest.raises(RepairConflict, match=f"{mid} backup differs"):
            nbr_restamp_rows(recaptured, mid, runs, decision)


def test_r1_applied_before_the_new_producers_first_night_leaves_exactly_one_period_row_per_id(
    tmp_path,
):
    backup = _reviewed_backup()
    store = MetricHistory(backup)

    receipt = _apply(_exclusion_manifest(tmp_path, backup), store, tmp_path)
    assert list(receipt["states"].values()) == ["confirmed"] * len(backup) == ["confirmed"] * 420
    assert store.rows == {}
    sent = _first_night(store)

    assert {mid: store.family(mid) for mid in FAMILIES} == {
        r["metric_id"]: [(SOURCE_PERIOD, r["value"])] for r in sent
    }
    assert sorted(r["metric_id"] for r in sent) == sorted(FAMILIES)
