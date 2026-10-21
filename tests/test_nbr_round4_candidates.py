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

import copy
import dataclasses
import functools
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

import scripts.nbr_round4_candidates as r4
from scripts.export_history import export_repair_snapshot
from scripts.history_repair_candidates import NBR_RESTAMP_WINDOW, nbr_restamp_rows
from scripts.nbr_round4_candidates import ROUND4_FIRST_DAY, ROUND4_IDS, Round4Review
from scripts.repair_observation_history import (
    apply_manifest,
    file_hash,
    load_manifest,
    restore_receipts,
    same,
    write_json,
)
from tests.test_history_repair_candidates import _corroborator_rows
from tests.test_r1_nbr_exclusion_release_order import FAMILIES as R1_FAMILIES
from tests.test_r1_nbr_exclusion_release_order import TARGET as R1_TARGET
from tests.test_r1_nbr_exclusion_release_order import (
    MetricHistory,
    _exclusion_manifest,
    _first_night,
    _reviewed_backup,
)
from tests.test_shared_case_contract import CASES

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
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-service-key")


def _days(first: str, last: str) -> list[str]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def _row(mid: str, day: str, value: float, **fields: Any) -> Row:
    return {"metric_id": mid, "as_of": day, "value": value, "source": "EconDelta",
            "ingested_at": f"{day}T21:18:08.437914+00:00", "provenance": None} | fields


def _post_rows(runs: dict, quirks: dict[str, float]) -> list[Row]:
    rows = []
    for mid in ROUND4_IDS:
        for value, first, last in runs[mid]:
            for day in _days(first, last):
                if day not in MISSING:
                    alias_value = quirks.get(day) if mid == ALIAS else None
                    rows.append(_row(mid, day, alias_value or value))
    return rows


def _contract_rows(nights: tuple[str, ...]) -> list[tuple[str, str, float, str]]:
    runs = {r["run"]: r for r in CASES["nbr_fytd_period_rows"]["runs"]}
    return sorted(
        (row["metric_id"], row["as_of"], row["value"], row["source"])
        for name in nights
        for row in runs[name]["rows_sent"].values()
    )


def _export(directory: Path, rows: list[Row], started_at: str, project: str = PROJECT) -> Path:
    return export_repair_snapshot(directory, fetcher=lambda table, key: rows,
                                  url=f"https://{project}.supabase.co",
                                  now=datetime.fromisoformat(started_at))


def _night1_rows(runs: dict, quirks: dict[str, float], extra: list[Row]) -> list[Row]:
    pre = [_row(mid, "2026-04-30", value) for mid, value in PRE_WINDOW.items()]
    return _reviewed_backup() + _corroborator_rows() + pre + _post_rows(runs, quirks) + extra


@functools.lru_cache(maxsize=1)
def _r1_removed_keys() -> frozenset[tuple[str, str]]:
    """The keys the REAL R1 engine removes from a Night-1 world (run once; ~7 s of fsyncs).

    Every world holds the same 420 reviewed rows, so applying R1 to it removes exactly these
    keys; the worlds below reuse this one real apply instead of repeating it.
    """
    n1_rows = _night1_rows(ONE_RUN, {}, [])
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
    n1_rows = _night1_rows(runs, quirks, extra_n1 or [])
    removed = _r1_removed_keys()
    store = MetricHistory([r for r in n1_rows if (r["metric_id"], r["as_of"]) not in removed])
    for name in nights:
        _first_night(store, name)  # the real producer night, shared R1 fixture
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
        recapture_manifest_sha256=file_hash(r4_dir / "manifest.json"),
        night1_manifest_sha256=file_hash(n1 / "manifest.json"),
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


HEAD = "e" * 40  # the commit --build names (T18 in tests/test_nbr_round4_guard.py)


def _main_build(world: World, git: Callable[[list[str]], tuple[int, str]] | None, *extra: str,
                output: Path | None = None) -> int:
    argv = ["--build", "--recapture-dir", str(world.r4), "--night1-backup-dir", str(world.n1),
            "--target", TARGET, "--econdelta-commit", HEAD, "--brief-commit", "d" * 40]
    argv += ["--output", str(output)] if output else []
    return r4.main(argv + list(extra), git=git)


# --- T1, T2: the committed placeholder and the first day -----------------------------------


def test_the_committed_review_is_a_placeholder_and_build_refuses(tmp_path, capsys):
    world = _world(tmp_path)
    output = tmp_path / "candidate.json"

    code = _main_build(world, None, output=output)  # the real local git is never reached

    assert r4.REVIEWED is None
    assert code == 1
    assert "round-4 review is a placeholder" in capsys.readouterr().out
    assert not output.exists()


def test_round4_first_day_is_the_day_after_the_reviewed_r1_window():
    assert date.fromisoformat(ROUND4_FIRST_DAY) == NBR_RESTAMP_WINDOW[1] + timedelta(days=1)
    assert ROUND4_IDS == ("fiscal_nbr_collected_trn", "tax_revenue", "nbr_fytd_collected_cr")


# --- T3, T4: the draft proposes, it never decides ------------------------------------------
BANNER = "PROPOSAL ONLY: a human must check and transcribe this; it decides nothing"
LITERAL_HEADER = r4.LITERAL_HEADER


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


# --- T5-T11: what the build excludes, and what it never touches ----------------------------


def _ops_by_key(candidate: dict) -> dict[tuple[str, str], dict]:
    return {(op["key"]["metric_id"], op["key"]["as_of"]): op for op in candidate["operations"]}


def test_round4_excludes_exactly_the_reviewed_post_window_restamps_with_full_before_images(
    tmp_path,
):
    world = _world(tmp_path)
    r4_index = {(r["metric_id"], r["as_of"]): r for r in world.r4_rows}
    r4_path, n1_path = (str((d / "metric_history.json").resolve()) for d in (world.r4, world.n1))

    candidate = _build(world)

    ops = candidate["operations"]
    assert [(op["key"]["metric_id"], op["key"]["as_of"]) for op in ops] == _post_keys(world)
    assert len(ops) == 3 * 7  # three ids x seven SYNTHETIC capture days (09-28 missing)
    for op in ops:
        key = (op["key"]["metric_id"], op["key"]["as_of"])
        assert op["operation_id"] == f"metric_history:{key[0]}:{key[1]}"
        assert op["table"] == "metric_history"
        assert op["before"] == r4_index[key] and op["after"] is None and op["requires"] == []
        assert op["reason"] == r4.ROUND4_REASON
        recapture, night1 = op["evidence"]
        assert (recapture["path"], recapture["sha256"]) == (r4_path, world.review.recapture_sha256)
        assert "archive/exclusion only, no period corroboration" in recapture["locator"]
        assert (night1["path"], night1["sha256"]) == (n1_path, world.review.night1_sha256)
        assert "pre-R1 Night-1 snapshot" in night1["locator"]
    assert candidate["backups"] == [{"table": "metric_history", "path": r4_path, "sha256":
                                     world.review.recapture_sha256, "rows": len(world.r4_rows)}]
    assert candidate["backup_manifest_sha256"] == file_hash(world.r4 / "manifest.json")
    assert (candidate["version"], candidate["target"], candidate["target_project"]) == (
        1, TARGET, PROJECT)
    assert candidate["code_commits"] == COMMITS and candidate["generated_at"] == GENERATED_AT


def test_round4_keeps_the_producers_2026_06_30_period_row_for_every_id(tmp_path):
    world = _world(tmp_path)
    touched = _ops_by_key(_build(world))
    for mid in ROUND4_IDS:
        assert (mid, "2026-06-30") not in touched
    assert sorted((r["metric_id"], r["as_of"], r["value"], r["source"]) for r in world.r4_rows
                  if r["metric_id"] in ROUND4_IDS and "2026-05-02" <= r["as_of"] <= "2026-09-24"
                  ) == _contract_rows(("first-night",))


def test_round4_keeps_06_30_and_07_31_when_the_source_moved_to_july_fy27(tmp_path):
    world = _world(tmp_path, nights=("first-night", "new-month"))
    touched = _ops_by_key(_build(world))
    assert world.review.keep_period_rows[CHILD] == (("2026-06-30", 4.15), ("2026-07-31", 0.31))
    assert not {(mid, day) for mid in ROUND4_IDS for day in ("2026-06-30", "2026-07-31")} & set(
        touched)
    assert sorted(touched) == sorted(_post_keys(world))


def test_round4_keeps_only_07_31_when_night1_first_wrote_july(tmp_path):
    world = _world(tmp_path, nights=("new-month",))
    touched = _ops_by_key(_build(world))
    assert world.review.keep_period_rows == {
        mid: (("2026-07-31", v),) for mid, v in ((CHILD, 0.31), (PARENT, 30512.4), (ALIAS, 30512.4))}
    assert sorted(touched) == sorted(_post_keys(world))
    with pytest.raises(r4.RepairConflict, match="R1-window keys differ"):  # 06-30 is not kept
        _build(world, dataclasses.replace(world.review, keep_period_rows={
            mid: (("2026-06-30", 4.15 if mid == CHILD else 415473.0),) + rows
            for mid, rows in world.review.keep_period_rows.items()}))


QUIRK_DAY = "2026-09-26"


@pytest.mark.parametrize(
    ("world_quirk", "listed", "refusal"),
    [
        pytest.param(415000.0, (), "alias != parent on 2026-09-26", id="untranscribed-quirk"),
        pytest.param(415000.0, ((QUIRK_DAY, 415000.0),), None, id="transcribed-quirk-passes"),
        pytest.param(None, ((QUIRK_DAY, 415473.0),), "equals the parent",
                     id="transcribed-quirk-equals-parent"),
        pytest.param(415000.0, ((QUIRK_DAY, 414999.0),), "differs from the listed",
                     id="transcribed-quirk-other-value"),
    ],
)
def test_alias_quirk_rule_is_exact_in_both_directions(tmp_path, world_quirk, listed, refusal):
    quirks = {QUIRK_DAY: world_quirk} if world_quirk else {}
    world = _world(tmp_path, quirks=quirks)
    alias_runs = (
        (415473.0, "2026-09-25", "2026-09-25"),
        (world_quirk or 415473.0, "2026-09-26", "2026-09-26"),
        (415473.0, "2026-09-27", LAST_DAY),
    ) if world_quirk else ONE_RUN[ALIAS]
    review = dataclasses.replace(world.review, alias_quirks={ALIAS: listed},
                                 runs=world.review.runs | {ALIAS: alias_runs})
    if refusal is None:
        assert sorted(_ops_by_key(_build(world, review))) == sorted(_post_keys(world))
    else:
        with pytest.raises(r4.RepairConflict, match=refusal):
            _build(world, review)


def test_retired_corroborators_are_never_touched_even_with_post_window_rows(tmp_path):
    extra = [_row(mid, "2026-09-26", value) for mid, value in
             (("nbr_fytd_collected_dailystar", 415000), ("nbr_fytd_collected_tbs", 415473))]
    world = _world(tmp_path, extra_n1=extra)  # SYNTHETIC post-window corroborator rows
    touched = {mid for mid, _ in _ops_by_key(_build(world))}
    assert touched == set(ROUND4_IDS)
    assert all(row in world.r4_rows for row in extra)


def _edit(mid: str, day: str, **fields: Any) -> Edit:
    return lambda rows: [r | fields if (r["metric_id"], r["as_of"]) == (mid, day) else r
                         for r in rows]


def _drop(*keys: tuple[str, str]) -> Edit:
    return lambda rows: [r for r in rows if (r["metric_id"], r["as_of"]) not in keys]


def _add(*new: Row) -> Edit:
    return lambda rows: rows + list(new)


def test_pre_window_rows_are_never_touched_and_must_equal_the_night1_backup(tmp_path):
    world = _world(tmp_path / "ok")
    assert not [key for key in _ops_by_key(_build(world)) if key[1] < "2026-05-02"]
    for name, kwargs in {
        "changed": {"r4_edit": _edit(PARENT, "2026-04-30", value=402001.0)},
        "deleted": {"r4_edit": _drop((PARENT, "2026-04-30"))},
        "added": {"r4_edit": _add(_row(PARENT, "2026-04-29", 401000.0))},
    }.items():
        bad = _world(tmp_path / name, **kwargs)
        with pytest.raises(r4.RepairConflict, match=f"{PARENT} pre-window rows differ"):
            _build(bad)


# --- T12: any recapture other than the reviewed one is refused -----------------------------
ALL_POST = {CHILD: 4.15, PARENT: 415473.0, ALIAS: 415473.0}
_R1_WINDOW_KEYS = {(r["metric_id"], r["as_of"]) for r in _reviewed_backup()}


def _move(day_from: str, day_to: str) -> Edit:
    return lambda rows: [r | {"as_of": day_to} if r["metric_id"] in ROUND4_IDS
                         and r["as_of"] == day_from else r for r in rows]


def _review(**changes: Any) -> Callable[[Round4Review], Round4Review]:
    def change(review: Round4Review) -> Round4Review:
        resolved = {k: v(review) if callable(v) else v for k, v in changes.items()}
        return dataclasses.replace(review, **resolved)
    return change


def _boundary_cases() -> list[tuple[str, dict, Any, str]]:
    """Each edge day of a SYNTHETIC two-run world takes its neighbour's value (per id)."""
    cases = []
    for mid in ROUND4_IDS:
        (value_a, _, last_a), (value_b, first_b, _) = TWO_RUNS[mid]
        for day, value in ((last_a, value_b), (first_b, value_a)):
            cases.append((f"run-boundary-shifted-by-one-day-{mid}-{day}",
                          {"runs": TWO_RUNS, "both_edit": _edit(mid, day, value=value)}, None,
                          f"{mid} post-window values differ from the reviewed runs"))
    return cases


REFUSALS: list[tuple[str, dict, Any, str]] = [
    ("later-day-added",
     {"both_edit": _add(*(_row(m, "2026-10-03", v) for m, v in ALL_POST.items()))}, None,
     f"{CHILD} has rows after the reviewed last capture day"),
    ("reviewed-day-missing", {"r4_edit": _drop(*((m, "2026-10-01") for m in ROUND4_IDS))}, None,
     f"{CHILD} post-window capture days differ from the review"),
    ("day-moved-onto-a-missing-day", {"r4_edit": _move("2026-09-27", "2026-09-28")}, None,
     f"{CHILD} post-window capture days differ from the review"),
    ("one-value-changed", {"both_edit": _edit(CHILD, "2026-09-29", value=4.16)}, None,
     f"{CHILD} post-window values differ from the reviewed runs"),
    *_boundary_cases(),
    ("source-not-econdelta", {"both_edit": _edit(CHILD, "2026-09-29", source="Bangladesh Bank")},
     None, f"{CHILD} 2026-09-29 is not an EconDelta restamp with null provenance"),
    ("media-approved-row-in-range",
     {"both_edit": _edit(PARENT, "2026-09-29", source="media-approved:thedailystar")}, None,
     f"{PARENT} 2026-09-29 is not an EconDelta restamp with null provenance"),
    ("dated-provenance",
     {"both_edit": _edit(ALIAS, "2026-09-29", provenance='{"source_period_end": "2026-09-30"}')},
     None, f"{ALIAS} 2026-09-29 is not an EconDelta restamp with null provenance"),
    ("ids-date-sets-differ", {"r4_edit": _drop((ALIAS, "2026-10-01"))}, None,
     "the three ids have different post-window date sets"),
    ("restamp-differs-from-night1",
     {"n1_edit": _edit(CHILD, "2026-09-29", ingested_at="2026-09-30T00:00:00+00:00")}, None,
     f"{CHILD} 2026-09-29 is not identical to its Night-1 row"),
    ("restamp-absent-from-night1", {"n1_edit": _drop((PARENT, "2026-09-29"))}, None,
     f"{PARENT} 2026-09-29 is absent from the Night-1 snapshot"),
    ("night1-has-a-restamp-the-recapture-lacks",
     {"n1_edit": _add(_row(CHILD, "2026-09-28", 4.15))}, None,
     f"Night-1 snapshot holds {CHILD} rows at or after 2026-09-25 missing from the recapture"),
    ("row-after-last-capture-day", {"n1_edit": _add(_row(ALIAS, "2026-10-03", 415473.0))}, None,
     f"Night-1 snapshot holds {ALIAS} rows after the reviewed last capture day"),
    ("period-row-missing", {"r4_edit": _drop((CHILD, "2026-06-30"))}, None,
     f"{CHILD} R1-window keys differ from the reviewed kept period rows"),
    ("period-row-wrong-value", {"r4_edit": _edit(PARENT, "2026-06-30", value=415474.0)}, None,
     f"{PARENT} kept period row 2026-06-30 value differs from the review"),
    ("period-row-wrong-source", {"r4_edit": _edit(ALIAS, "2026-06-30", source="Bangladesh Bank")},
     None, f"{ALIAS} kept period row 2026-06-30 is not an EconDelta month-end row"),
    ("extra-row-in-r1-window", {"r4_edit": _add(_row(CHILD, "2026-07-15", 3.61))}, None,
     f"{CHILD} R1-window keys differ from the reviewed kept period rows"),
    ("keep-key-inside-capture-range", {},
     _review(keep_period_rows=lambda rv: rv.keep_period_rows | {
         CHILD: rv.keep_period_rows[CHILD] + (("2026-09-30", 4.15),)}),
     f"kept period row {CHILD} 2026-09-30 is inside the round-4 capture range"),
    ("empty-keep", {}, _review(keep_period_rows=lambda rv: rv.keep_period_rows | {PARENT: ()}),
     f"keep_period_rows is empty for {PARENT}"),
    ("night1-taken-after-r1",
     {"n1_edit": lambda rows: [r for r in rows if (r["metric_id"], r["as_of"]) not in
                               _R1_WINDOW_KEYS]}, None,
     "Night-1 snapshot is not the pre-R1 backup"),
    ("night1-taken-after-the-new-producer",
     {"n1_edit": _edit(PARENT, "2026-06-30", value=415473.0)}, None,
     "Night-1 snapshot is not the pre-R1 backup: tax_revenue backup differs"),
    ("night1-started-after-recapture", {"n1_started": "2026-10-03T04:00:00+00:00"}, None,
     "Night-1 snapshot did not start before the recapture"),
    ("month-end-capture-day-not-acknowledged", {},
     _review(acknowledged_month_end_days=frozenset()),
     "month-end capture day 2026-09-30 is not acknowledged"),
    ("acknowledged-day-not-a-capture-day", {},
     _review(acknowledged_month_end_days=frozenset({"2026-09-30", "2026-10-31"})),
     "acknowledged day 2026-10-31 is not a month-end capture day"),
    ("recapture-hash-not-reviewed", {}, _review(recapture_sha256="0" * 64),
     "recapture metric_history.json hash differs from the reviewed hash"),
    ("night1-hash-not-reviewed", {}, _review(night1_sha256="0" * 64),
     "Night-1 metric_history.json hash differs from the reviewed hash"),
    ("wrong-target-project", {"r4_project": "abcdefghijklmnopqrst"}, None,
     "recapture R4 snapshot target project 'abcdefghijklmnopqrst'"),
    ("duplicate-key", {"r4_edit": lambda rows: rows + [rows[-1]]}, None, "duplicate key"),
    ("child-not-parent-times-factor",
     {"runs": ONE_RUN | {CHILD: ((4.16, "2026-09-25", LAST_DAY),)}}, None,
     "child != round"),
]


@pytest.mark.parametrize(
    ("world_kwargs", "review_change", "refusal"),
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_round4_refuses_any_recapture_other_than_the_reviewed_one(
    tmp_path, world_kwargs, review_change, refusal
):
    world = _world(tmp_path, **world_kwargs)
    review = review_change(world.review) if review_change else world.review
    with pytest.raises(r4.RepairConflict, match=refusal):
        _build(world, review)


def test_the_refusal_cases_cover_every_run_edge_and_every_design_case():
    ids = [case[0] for case in REFUSALS]
    assert len([i for i in ids if i.startswith("run-boundary-shifted")]) == 6
    assert len(ids) == len(set(ids)) == 29 - 1 + 6  # 29 named cases, the boundary one per edge


# --- T13-T17: through the unchanged engine, drift receipts, and the contract residual ------


class Round4History(MetricHistory):
    target = TARGET


def _candidate_file(world: World, tmp_path: Path) -> Path:
    path = tmp_path / "candidate" / "candidate.json"
    write_json(path, _build(world))
    return path


def _apply4(candidate: Path, store: MetricHistory, receipts: Path) -> dict:
    return apply_manifest(candidate, expected_sha256=file_hash(candidate), target=TARGET,
                          store=store, receipts_path=receipts)


def _rows_equal(store: MetricHistory, rows: list[Row]) -> bool:
    expected = {(r["metric_id"], r["as_of"]): r for r in rows}
    return set(store.rows) == set(expected) and all(
        same(store.rows[key], row) for key, row in expected.items())


def test_round4_manifest_plans_applies_reapplies_and_restores_exactly(tmp_path):
    world = _world(tmp_path)
    candidate = _candidate_file(world, tmp_path)
    plan = load_manifest(candidate, expected_sha256=file_hash(candidate), target=TARGET)
    keys = {(op["key"]["metric_id"], op["key"]["as_of"]) for op in plan["operations"]}
    store = Round4History(world.r4_rows)
    receipts = tmp_path / "receipts" / "receipts.json"

    receipt = _apply4(candidate, store, receipts)

    assert list(receipt["states"].values()) == ["confirmed"] * 21
    assert _rows_equal(store, [r for r in world.r4_rows if (r["metric_id"], r["as_of"]) not in keys])
    writes, receipt_bytes = store.repair_writes, receipts.read_bytes()
    _apply4(candidate, store, receipts)  # idempotent re-apply: checked, not rewritten
    assert store.repair_writes == writes == 21 and receipts.read_bytes() == receipt_bytes
    reverse = restore_receipts(receipts, expected_sha256=file_hash(receipts), target=TARGET,
                               store=store)
    assert list(reverse["states"].values()) == ["confirmed"] * 21
    assert _rows_equal(store, world.r4_rows)


def test_round4_whole_batch_refuses_if_a_listed_restamp_changed_after_review(tmp_path):
    world = _world(tmp_path)
    candidate = _candidate_file(world, tmp_path)
    store = Round4History(world.r4_rows)
    store.rows[(ALIAS, "2026-10-01")]["ingested_at"] = "2026-10-03T21:18:08+00:00"  # SYNTHETIC
    after_review = copy.deepcopy(store.rows)

    with pytest.raises(r4.RepairConflict, match=f"{ALIAS}:2026-10-01"):
        _apply4(candidate, store, tmp_path / "receipts" / "receipts.json")

    assert store.repair_writes == 0
    assert store.rows == after_review


def test_r1_generator_still_refuses_the_post_night1_recapture(tmp_path):
    world = _world(tmp_path)
    for mid, (runs, decision, _) in R1_FAMILIES.items():
        with pytest.raises(r4.RepairConflict, match=f"{mid} backup differs"):
            nbr_restamp_rows(world.r4_rows, mid, runs, decision)
        window = [r for r in world.r4_rows if "2026-05-02" <= r["as_of"] <= "2026-09-24"]
        with pytest.raises(r4.RepairConflict, match=f"{mid} backup differs"):
            nbr_restamp_rows(window, mid, runs, decision)


def _verify(world: World, fresh: Path, out: Path, *extra: str) -> int:
    return r4.main(["--verify-unchanged", "--recapture-dir", str(world.r4), "--fresh-dir",
                    str(fresh), "--out", str(out), *extra])


def test_verify_unchanged_writes_a_receipt_and_refuses_a_new_nbr_row_written_after_the_review(
    tmp_path, capsys
):
    world = _world(tmp_path)
    n2 = _export(tmp_path / "n2-2026-10-03", world.r4_rows, "2026-10-03T15:50:00+00:00")
    out = tmp_path / "receipts" / "verify-n2.json"

    assert _verify(world, n2, out) == 0
    receipt = json.loads(out.read_text())
    assert receipt == {
        "result": "unchanged",
        "differing_keys": [],
        "recapture_manifest_sha256": file_hash(world.r4 / "manifest.json"),
        "fresh_manifest_sha256": file_hash(n2 / "manifest.json"),
        "fresh_started_at": "2026-10-03T15:50:00+00:00",
        "expect_applied_manifest_sha256": None,
    }
    assert f"unchanged; receipt sha256={file_hash(out)}" in capsys.readouterr().out
    receipt_bytes = out.read_bytes()
    assert _verify(world, n2, out) == 1 and out.read_bytes() == receipt_bytes  # never overwrites

    # SYNTHETIC: the producer writes an August period row after the review.
    august = [_row(mid, "2026-08-31", value) for mid, value in
              ((CHILD, 0.31), (PARENT, 30512.4), (ALIAS, 30512.4))]
    n2b = _export(tmp_path / "n2b-2026-10-03", world.r4_rows + august,
                  "2026-10-03T15:55:00+00:00")
    assert _verify(world, n2b, tmp_path / "receipts" / "verify-n2b.json") == 1
    changed = json.loads((tmp_path / "receipts" / "verify-n2b.json").read_text())
    assert changed["result"] == "changed"
    assert changed["differing_keys"] == [[mid, "2026-08-31"] for mid in sorted(ROUND4_IDS)]

    # A retired corroborator edit is drift too.
    n2c = _export(tmp_path / "n2c-2026-10-03",
                  _edit("nbr_fytd_collected_tbs", "2026-05-02", value=1)(world.r4_rows),
                  "2026-10-03T15:56:00+00:00")
    assert _verify(world, n2c, tmp_path / "receipts" / "verify-n2c.json") == 1

    # After the apply, the read-back must be R4 minus exactly the manifest keys.
    candidate = _candidate_file(world, tmp_path)
    store = Round4History(world.r4_rows)
    _apply4(candidate, store, tmp_path / "receipts" / "receipts.json")
    n3 = _export(tmp_path / "n3-2026-10-03", list(store.rows.values()),
                 "2026-10-03T16:40:00+00:00")
    out3 = tmp_path / "receipts" / "verify-n3.json"
    assert _verify(world, n3, out3, "--expect-applied", str(candidate)) == 0
    applied = json.loads(out3.read_text())
    assert applied["result"] == "applied-as-reviewed"
    assert applied["expect_applied_manifest_sha256"] == file_hash(candidate)
    assert _verify(world, n2, tmp_path / "receipts" / "not-applied.json", "--expect-applied",
                   str(candidate)) == 1  # nothing applied yet: 21 keys still present


@pytest.mark.parametrize("nights", [("first-night",), ("first-night", "new-month")])
def test_after_round4_the_nbr_family_equals_the_contract_rows_the_brief_publishes(
    tmp_path, nights
):
    """EconDelta half of the cross-repo proof (fallback, design OQ1): the residual equals the
    shared contract's rows_sent, which the Brief's tests prove do not HOLD (B:82-91)."""
    world = _world(tmp_path, nights=nights)
    store = Round4History(world.r4_rows)
    _apply4(_candidate_file(world, tmp_path), store, tmp_path / "receipts" / "receipts.json")

    residual = sorted((r["metric_id"], r["as_of"], r["value"], r["source"])
                      for r in store.rows.values()
                      if r["metric_id"] in ROUND4_IDS and r["as_of"] >= "2026-05-02")
    assert residual == _contract_rows(nights)
