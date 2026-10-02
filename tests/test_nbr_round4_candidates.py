"""Round 4 (owner decision D2, 2 Oct 2026): exclude the post-24-Sep NBR capture-date restamps.

Business rule: after R1 (decisions (d)+(l)) and the new producer's first night, the old producer's
restamps dated 2026-09-25 onwards are removed by exact key and value, from a human-reviewed list,
and NOTHING else: the producer's period rows (2026-06-30, and 2026-07-31 if written), pre-window
rows and the retired corroborators stay. Any snapshot other than the reviewed one is refused.

The world here is built with the real R1 fixtures and engine and the real producer night on the
shared contract's 25 Sep input (tests/test_r1_nbr_exclusion_release_order.py). Every row dated
after 2026-09-24, its value and its ingested_at stamp are SYNTHETIC, as are the pre-window rows.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

import aggregate_latest as agg
import scripts.nbr_round4_candidates as r4
from scripts.export_history import export_repair_snapshot
from scripts.history_repair_candidates import NBR_RESTAMP_WINDOW
from scripts.nbr_round4_candidates import ROUND4_FIRST_DAY, ROUND4_IDS, Round4Review
from scripts.repair_observation_history import apply_manifest, file_hash
from tests.test_history_repair_candidates import _corroborator_rows
from tests.test_r1_nbr_exclusion_release_order import TARGET as R1_TARGET
from tests.test_r1_nbr_exclusion_release_order import (
    MetricHistory,
    _exclusion_manifest,
    _reviewed_backup,
)
from tests.test_shared_case_contract import BINDING, CASES, RUN, _changed, _produce
from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

Row = dict[str, Any]
CHILD, PARENT, ALIAS = ROUND4_IDS
PROJECT = "ssbliukchgibjcjohibi"
TARGET = "snapshot:round4-nbr"
COMMITS = {"econdelta": "c" * 40, "brief": "d" * 40}
GENERATED_AT = "2026-10-03T07:00:00+00:00"
N1_STARTED = "2026-10-02T20:40:00+00:00"  # Night 1, 02:40 BDT 3 Oct (SYNTHETIC)
R4_STARTED = "2026-10-03T03:20:00+00:00"  # Day 2, 09:20 BDT (SYNTHETIC)
MISSING = frozenset({"2026-09-28"})  # SYNTHETIC: one capture day with no restamp
LAST_DAY = "2026-10-02"
# SYNTHETIC post-window runs: the last reviewed R1 run carried on (as B tests do) ...
ONE_RUN = {
    CHILD: ((4.15, "2026-09-25", LAST_DAY),),
    PARENT: ((415473.0, "2026-09-25", LAST_DAY),),
    ALIAS: ((415473.0, "2026-09-25", LAST_DAY),),
}
# ... or a SYNTHETIC second run from 2026-09-30, to give the runs a boundary.
TWO_RUNS = {
    CHILD: ((4.15, "2026-09-25", "2026-09-29"), (4.32, "2026-09-30", LAST_DAY)),
    PARENT: ((415473.0, "2026-09-25", "2026-09-29"), (432100.0, "2026-09-30", LAST_DAY)),
    ALIAS: ((415473.0, "2026-09-25", "2026-09-29"), (432100.0, "2026-09-30", LAST_DAY)),
}
PRE_WINDOW = {CHILD: 4.02, PARENT: 402000.0, ALIAS: 402000.0}  # SYNTHETIC, dated 2026-04-30


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    """The world's snapshots come from the real exporter, which needs a service key."""
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "synthetic-service-key")


def _days(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def _row(mid: str, day: str, value: float, **fields: Any) -> Row:
    return {
        "metric_id": mid,
        "as_of": day,
        "value": value,
        "source": "EconDelta",
        "ingested_at": f"{day}T21:18:08.437914+00:00",
        "provenance": None,
    } | fields


def _post_rows(runs: dict, quirks: dict[str, float]) -> list[Row]:
    rows = []
    for mid in ROUND4_IDS:
        for value, first, last in runs[mid]:
            for day in _days(first, last):
                if day not in MISSING:
                    alias_value = quirks.get(day) if mid == ALIAS else None
                    rows.append(_row(mid, day, alias_value or value))
    return rows


def _night(store: MetricHistory, name: str) -> None:
    """One real producer night (shared contract run `name`) onto the store."""
    run = next(r for r in CASES["nbr_fytd_period_rows"]["runs"] if r["run"] == name)
    inputs = _changed(BINDING["producer_inputs"], run["input_changes"])
    observations = _produce(inputs, RUN)["observations"]
    values = {mid: obs.value for mid, obs in observations.items() if obs.value is not None}
    to_write, _ = agg._unrecorded_nbr_fytd(values, observations, today=RUN.date(),
                                            reader=store.read_at)
    for row in _rows_from_data(to_write, RUN.date(), _DEFAULT_SOURCE, ingested_at=RUN,
                               observations=observations):
        if row["metric_id"] in ROUND4_IDS:
            store.upsert(row)


def _contract_rows(nights: tuple[str, ...]) -> list[tuple[str, str, float, str]]:
    runs = {r["run"]: r for r in CASES["nbr_fytd_period_rows"]["runs"]}
    return sorted(
        (row["metric_id"], row["as_of"], row["value"], row["source"])
        for name in nights
        for row in runs[name]["rows_sent"].values()
    )


def _export(directory: Path, rows: list[Row], started_at: str, project: str = PROJECT) -> Path:
    from datetime import datetime

    return export_repair_snapshot(
        directory,
        fetcher=lambda table, key: rows,
        url=f"https://{project}.supabase.co",
        now=datetime.fromisoformat(started_at),
    )


@functools.lru_cache(maxsize=1)
def _r1_removed_keys() -> frozenset[tuple[str, str]]:
    """The keys the REAL R1 engine removes from a Night-1 world (run once; ~7 s of fsyncs).

    Every world holds the same 420 reviewed rows, so applying R1 to it removes exactly these
    keys; the worlds below reuse this one real apply instead of repeating it.
    """
    pre = [_row(mid, "2026-04-30", value) for mid, value in PRE_WINDOW.items()]
    n1_rows = _reviewed_backup() + _corroborator_rows() + pre + _post_rows(ONE_RUN, {})
    store = MetricHistory(n1_rows)
    with tempfile.TemporaryDirectory() as tmp:
        r1 = _exclusion_manifest(Path(tmp), _reviewed_backup())
        receipt = apply_manifest(r1, expected_sha256=file_hash(r1), target=R1_TARGET,
                                 store=store, receipts_path=Path(tmp) / "receipts.json")
    assert set(receipt["states"].values()) == {"confirmed"} and len(receipt["states"]) == 420
    removed = frozenset((r["metric_id"], r["as_of"]) for r in n1_rows) - set(store.rows)
    assert len(removed) == 420
    return removed


@dataclasses.dataclass
class World:
    n1: Path
    r4: Path
    review: Round4Review
    n1_rows: list[Row]
    r4_rows: list[Row]


Edit = Callable[[list[Row]], list[Row]]


def _world(
    tmp_path: Path,
    *,
    runs: dict = ONE_RUN,
    nights: tuple[str, ...] = ("first-night",),
    quirks: dict[str, float] | None = None,
    extra_n1: list[Row] | None = None,
    both_edit: Edit | None = None,
    n1_edit: Edit | None = None,
    r4_edit: Edit | None = None,
    n1_started: str = N1_STARTED,
    r4_project: str = PROJECT,
) -> World:
    """Night 1 snapshot -> real R1 apply -> real producer night(s) -> Day-2 recapture."""
    quirks = quirks or {}
    pre = [_row(mid, "2026-04-30", value) for mid, value in PRE_WINDOW.items()]
    n1_rows = (_reviewed_backup() + _corroborator_rows() + pre + _post_rows(runs, quirks)
               + (extra_n1 or []))
    removed = _r1_removed_keys()
    store = MetricHistory([r for r in n1_rows if (r["metric_id"], r["as_of"]) not in removed])
    for name in nights:
        _night(store, name)
    r4_rows = sorted(store.rows.values(), key=lambda r: (r["metric_id"], r["as_of"]))
    if both_edit:
        n1_rows, r4_rows = both_edit(n1_rows), both_edit(r4_rows)
    n1_rows = n1_edit(n1_rows) if n1_edit else n1_rows
    r4_rows = r4_edit(r4_rows) if r4_edit else r4_rows
    n1 = _export(tmp_path / "night1-2026-10-03", n1_rows, n1_started)
    r4_dir = _export(tmp_path / "recapture-2026-10-03", r4_rows, R4_STARTED, r4_project)
    keep: dict[str, tuple] = {mid: () for mid in ROUND4_IDS}
    for mid, day, value, _ in _contract_rows(nights):
        keep[mid] += ((day, value),)
    review = Round4Review(
        last_capture_day=LAST_DAY,
        missing_days=MISSING,
        runs=dict(runs),
        alias_quirks={ALIAS: tuple(sorted(quirks.items()))},
        acknowledged_month_end_days=frozenset({"2026-09-30"}),
        keep_period_rows=keep,
        recapture_sha256=file_hash(r4_dir / "metric_history.json"),
        night1_sha256=file_hash(n1 / "metric_history.json"),
    )
    return World(n1, r4_dir, review, n1_rows, r4_rows)


def _build(world: World, review: Round4Review | None = None, **kwargs: Any) -> dict:
    return r4.build_round4_candidate(
        world.r4, world.n1, review or world.review, target=TARGET, commits=COMMITS,
        generated_at=GENERATED_AT, **kwargs,
    )


def _post_keys(world: World) -> list[tuple[str, str]]:
    days = [d for d in _days(ROUND4_FIRST_DAY, LAST_DAY) if d not in MISSING]
    return [(mid, day) for mid in ROUND4_IDS for day in days]


# --- T1, T2: the committed placeholder and the first day -----------------------------------


def test_the_committed_review_is_a_placeholder_and_build_refuses(tmp_path, capsys):
    world = _world(tmp_path)
    output = tmp_path / "candidate.json"

    code = r4.main([
        "--build", "--recapture-dir", str(world.r4), "--night1-backup-dir", str(world.n1),
        "--target", TARGET, "--econdelta-commit", "c" * 40, "--brief-commit", "d" * 40,
        "--output", str(output),
    ])

    assert r4.REVIEWED is None
    assert code == 1
    assert "round-4 review is a placeholder" in capsys.readouterr().out
    assert not output.exists()


def test_round4_first_day_is_the_day_after_the_reviewed_r1_window():
    assert date.fromisoformat(ROUND4_FIRST_DAY) == NBR_RESTAMP_WINDOW[1] + timedelta(days=1)
    assert ROUND4_IDS == ("fiscal_nbr_collected_trn", "tax_revenue", "nbr_fytd_collected_cr")


# --- T3, T4: the draft proposes, it never decides ------------------------------------------
BANNER = "PROPOSAL ONLY: a human must check and transcribe this; it decides nothing"
LITERAL_HEADER = "# DRAFT: paste into REVIEWED only after review"


def _draft(world: World, capsys) -> tuple[int, list[str]]:
    code = r4.main(["--draft", "--recapture-dir", str(world.r4),
                    "--night1-backup-dir", str(world.n1)])
    return code, capsys.readouterr().out.splitlines()


def _literal(lines: list[str]) -> Round4Review:
    block = lines[lines.index(LITERAL_HEADER) + 1:]
    assert block[0].startswith("REVIEWED = Round4Review(") and block[-1] == ")"
    text = "\n".join(block).removeprefix("REVIEWED = ")
    return eval(text, {"__builtins__": {}, "Round4Review": Round4Review, "frozenset": frozenset})


def _tree(root: Path) -> list[tuple[str, str]]:
    return sorted((str(p.relative_to(root)), file_hash(p) if p.is_file() else "dir")
                  for p in root.rglob("*"))


def test_draft_writes_nothing_and_its_literal_equals_the_synthetic_truth(tmp_path, capsys):
    world = _world(tmp_path)
    before = _tree(tmp_path)

    code, lines = _draft(world, capsys)

    assert code == 0
    assert _tree(tmp_path) == before  # writes nothing, anywhere under the world
    assert lines[0] == BANNER
    assert _literal(lines) == world.review
    checks = [line for line in lines if "CHECK:" in line]
    assert len(checks) == 1 and "month-end capture day 2026-09-30" in checks[0]
    assert any("N1 provenance" in line and line.lstrip().startswith("OK") for line in lines)


def test_draft_flags_month_end_capture_days_alias_drift_rows_missing_from_night1_and_a_non_pre_r1_night1(
    tmp_path, capsys
):
    window = {(r["metric_id"], r["as_of"]) for r in _reviewed_backup()}
    world = _world(
        tmp_path,
        quirks={"2026-09-26": 415000.0},  # SYNTHETIC alias drift
        n1_edit=lambda rows: [r for r in rows if (r["metric_id"], r["as_of"]) not in window]
        + [_row(CHILD, "2026-09-28", 4.15)],  # Night 1 taken after R1, plus a row R4 lacks
    )

    code, lines = _draft(world, capsys)

    checks = "\n".join(line for line in lines if "CHECK:" in line)
    assert code == 0 and lines[0] == BANNER
    assert "N1 provenance" in checks and "not the pre-R1" in checks
    assert "alias != parent" in checks and "2026-09-26" in checks
    assert f"Night-1 rows at or after {ROUND4_FIRST_DAY} missing from the recapture" in checks
    assert f"{CHILD} 2026-09-28" in checks
    assert "month-end capture day 2026-09-30" in checks
    assert _literal(lines).alias_quirks == {ALIAS: (("2026-09-26", 415000.0),)}
