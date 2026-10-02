"""Round 4 (owner decision D2, 2 Oct 2026): the refusals the first review round found untested.

Business rule (same as tests/test_nbr_round4_candidates.py, whose world and helpers this file
imports; it is a separate file only because of the 800-line cap): the build, the draft, the drift
receipt and the commit guard each refuse anything but the reviewed inputs, and each refusal below
fails here if its check is removed. Every row dated after 2026-09-24 is SYNTHETIC.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.nbr_round4_candidates as r4
from scripts.repair_observation_history import write_json
from tests.test_nbr_round4_candidates import (
    ALIAS,
    CHILD,
    HEAD,
    LAST_DAY,
    ONE_RUN,
    PARENT,
    R4_STARTED,
    TARGET,
    FakeGit,
    Round4History,
    World,
    _add,
    _apply4,
    _build,
    _candidate_file,
    _drop,
    _edit,
    _export,
    _review,
    _row,
    _verify,
    _world,
)
from tests.test_r1_nbr_exclusion_release_order import _reviewed_backup

ROUND4_IDS = (CHILD, PARENT, ALIAS)
_DELETE = object()


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    """The world's snapshots come from the real exporter, which needs a service key."""
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-service-key")


def _window_of(mid: str) -> set[tuple[str, str]]:
    return {(r["metric_id"], r["as_of"]) for r in _reviewed_backup() if r["metric_id"] == mid}


def _drop_window(mid: str):
    keys = _window_of(mid)
    return lambda rows: [r for r in rows if (r["metric_id"], r["as_of"]) not in keys]


# --- build refusals: N1 provenance per id, R1-window month-end, review transcription -------
REFUSALS: list[tuple[str, dict, Any, str]] = [
    *[(f"night1-{mid}-window-missing", {"n1_edit": _drop_window(mid)}, None,
       f"Night-1 snapshot is not the pre-R1 backup: {mid} backup differs") for mid in ROUND4_IDS],
    ("period-row-not-month-end", {"r4_edit": _add(_row(CHILD, "2026-07-15", 3.61))},
     _review(keep_period_rows=lambda rv: rv.keep_period_rows | {
         CHILD: rv.keep_period_rows[CHILD] + (("2026-07-15", 3.61),)}),
     f"{CHILD} kept period row 2026-07-15 is not an EconDelta month-end row"),
    ("night1-started-at-the-same-instant", {"n1_started": R4_STARTED}, None,
     "Night-1 snapshot did not start before the recapture"),
    ("run-outside-range", {},
     _review(runs={mid: ((value, "2026-09-01", "2026-12-31"),)
                   for mid, ((value, _, _),) in ONE_RUN.items()}),
     f"{CHILD} runs overlap or leave gaps over the reviewed capture days"),
    ("missing-day-outside-range", {},
     _review(missing_days=lambda rv: rv.missing_days | {"2026-10-20"}),
     "missing day 2026-10-20 is outside the capture range"),
    ("keep-names-extra-id", {},
     _review(keep_period_rows=lambda rv: rv.keep_period_rows | {
         "nbr_fytd_collected_tbs": (("2026-06-30", 415473.0),)}),
     "keep_period_rows must name exactly the three round-4 ids"),
    ("quirk-day-not-a-capture-day", {},
     _review(alias_quirks={ALIAS: (("2026-09-28", 415000.0),)}),
     "alias quirk day 2026-09-28 is not a reviewed capture day"),
    ("last-capture-day-before-first-day", {}, _review(last_capture_day="2026-09-24"),
     "last_capture_day 2026-09-24 is before 2026-09-25"),
    ("keep-row-before-window", {},
     _review(keep_period_rows=lambda rv: rv.keep_period_rows | {
         CHILD: (("2026-04-30", 4.02),) + rv.keep_period_rows[CHILD]}),
     f"kept period row {CHILD} 2026-04-30 is before the R1 window"),
    ("recapture-manifest-hash-not-reviewed", {}, _review(recapture_manifest_sha256="0" * 64),
     "recapture manifest.json hash differs from the reviewed hash"),
    ("night1-manifest-hash-not-reviewed", {}, _review(night1_manifest_sha256="0" * 64),
     "Night-1 manifest.json hash differs from the reviewed hash"),
]


@pytest.mark.parametrize(
    ("world_kwargs", "review_change", "refusal"),
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_round4_refuses_each_unreviewed_input_by_its_own_rule(
    tmp_path, world_kwargs, review_change, refusal
):
    world = _world(tmp_path, **world_kwargs)
    review = review_change(world.review) if review_change else world.review
    with pytest.raises(r4.RepairConflict, match=refusal):
        _build(world, review)


def test_the_review_binds_both_manifests_so_an_edited_started_at_is_refused(tmp_path):
    """started_at lives in manifest.json; editing it there (data bytes untouched) is refused."""
    world = _world(tmp_path)
    _tamper(world.n1, started_at="2026-10-03T03:00:00+00:00")
    with pytest.raises(r4.RepairConflict, match="Night-1 manifest.json hash differs"):
        _build(world)


# --- the snapshot must verify against its own manifest (build AND draft) ---------------------


def _tamper(directory: Path, table: dict | None = None, **top: Any) -> None:
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    for name, value in top.items():
        manifest.pop(name) if value is _DELETE else manifest.__setitem__(name, value)
    manifest["tables"]["metric_history"] |= table or {}
    path.write_text(json.dumps(manifest, indent=2))


MANIFEST_CASES = [
    ("manifest-hash-mismatch", "r4", {"table": {"sha256": "0" * 64}},
     "recapture R4 metric_history.json hash differs from its manifest"),
    ("manifest-row-count-mismatch", "r4", {"table": {"rows": 76}},
     "recapture R4 metric_history row count differs from its manifest"),
    ("manifest-key-wrong", "r4", {"table": {"key": ["as_of", "metric_id"]}},
     "recapture R4 manifest has no metric_history entry keyed metric_id, as_of"),
    ("manifest-key-role-anon", "r4", {"key_role": "anon"},
     "recapture R4 manifest key_role is 'anon', not 'service'"),
    ("night1-manifest-key-role-missing", "n1", {"key_role": _DELETE},
     "Night-1 N1 manifest key_role is None, not 'service'"),
    ("manifest-not-non-transactional", "r4", {"non_transactional": _DELETE},
     "recapture R4 manifest does not record non_transactional: true"),
]


@pytest.mark.parametrize(
    ("which", "changes", "refusal"),
    [case[1:] for case in MANIFEST_CASES],
    ids=[case[0] for case in MANIFEST_CASES],
)
def test_build_and_draft_refuse_a_snapshot_that_does_not_verify_against_its_manifest(
    tmp_path, capsys, which, changes, refusal
):
    world = _world(tmp_path)
    _tamper(getattr(world, which), **changes)

    with pytest.raises(r4.RepairConflict, match=refusal):
        _build(world)
    code = r4.main(["--draft", "--recapture-dir", str(world.r4),
                    "--night1-backup-dir", str(world.n1)])
    out = capsys.readouterr().out
    assert code == 1 and refusal in out and r4.BANNER not in out


# --- every draft cross-check line can say CHECK ----------------------------------------------
DRAFT_CASES = [
    ("post-window-row-not-identical-in-n1",
     {"n1_edit": _edit(CHILD, "2026-09-29", ingested_at="2026-09-30T00:00:00+00:00")},
     f"CHECK: post-window rows not identical in N1: {CHILD} 2026-09-29"),
    ("child-not-parent-times-factor", {"runs": ONE_RUN | {CHILD: ((4.16, "2026-09-25", LAST_DAY),)}},
     "CHECK: child != round(parent x 1e-05, 2) on: 2026-09-25, 2026-09-26"),
    ("date-sets-differ", {"r4_edit": _drop((ALIAS, "2026-10-01"))},
     "CHECK: post-window date sets differ"),
    ("night1-row-after-last-capture-day", {"n1_edit": _add(_row(ALIAS, "2026-10-03", 415473.0))},
     f"CHECK: Night-1 rows after the last capture day: {ALIAS} 2026-10-03"),
]


@pytest.mark.parametrize(("world_kwargs", "check"), [c[1:] for c in DRAFT_CASES],
                         ids=[c[0] for c in DRAFT_CASES])
def test_each_draft_cross_check_prints_its_own_check_line(tmp_path, capsys, world_kwargs, check):
    world = _world(tmp_path, **world_kwargs)
    code = r4.main(["--draft", "--recapture-dir", str(world.r4),
                    "--night1-backup-dir", str(world.n1)])
    lines = capsys.readouterr().out.splitlines()
    assert code == 0 and lines[0] == r4.BANNER
    assert any(line.strip().startswith(check) for line in lines), check


# --- the drift receipt: a fresh, newer snapshot, and a manifest from this recapture ----------


def test_verify_unchanged_refuses_the_recapture_itself_or_an_older_snapshot_as_fresh(
    tmp_path, capsys
):
    world = _world(tmp_path)
    older = _export(tmp_path / "older", world.r4_rows, "2026-10-01T00:00:00+00:00")
    for fresh, refusal in (
        (world.r4, "fresh snapshot is the recapture itself"),
        (older, "fresh snapshot did not start after the recapture"),
    ):
        out = tmp_path / "receipts" / f"verify-{fresh.name}.json"
        assert _verify(world, fresh, out) == 1
        assert refusal in capsys.readouterr().out and not out.exists()


def test_expect_applied_refuses_a_manifest_built_from_another_recapture(tmp_path, capsys):
    world = _world(tmp_path)
    candidate = _candidate_file(world, tmp_path)
    store = Round4History(world.r4_rows)
    _apply4(candidate, store, tmp_path / "receipts" / "receipts.json")
    n3 = _export(tmp_path / "n3-2026-10-03", list(store.rows.values()), "2026-10-03T16:40:00+00:00")
    other = json.loads(candidate.read_text())
    other["backups"][0]["sha256"] = "0" * 64
    other_path = tmp_path / "other" / "candidate.json"
    write_json(other_path, other)
    out = tmp_path / "receipts" / "verify-n3.json"

    assert _verify(world, n3, out, "--expect-applied", str(other_path)) == 1

    assert "--expect-applied manifest was not built from this recapture" in capsys.readouterr().out
    assert not out.exists()


# --- the commit guard: exact commits, git failures, a real ruling -----------------------------


class FailingGit(FakeGit):
    """FakeGit whose `fail` command exits non-zero while printing `stdout`."""

    def __init__(self, fail: list[str], stdout: str) -> None:
        super().__init__()
        self.fail, self.stdout = fail, stdout

    def __call__(self, args: list[str]) -> tuple[int, str]:
        return (128, self.stdout) if args == self.fail else super().__call__(args)


def _argv(world: World, output: Path, brief: str = "d" * 40) -> list[str]:
    return ["--build", "--recapture-dir", str(world.r4), "--night1-backup-dir", str(world.n1),
            "--target", TARGET, "--econdelta-commit", HEAD, "--brief-commit", brief,
            "--output", str(output)]


def test_build_refuses_a_short_commit_a_failed_git_call_and_an_empty_ruling(
    tmp_path, monkeypatch, capsys
):
    world = _world(tmp_path)
    monkeypatch.setattr(r4, "REVIEWED", world.review)
    output = tmp_path / "candidate" / "candidate.json"
    for argv, git, refusal in (
        (_argv(world, output, brief="edd67d9"), FakeGit(), "both exact 40-hex code commits"),
        (_argv(world, output), FailingGit(["rev-parse", "HEAD"], HEAD + "\n"),
         f"is not --econdelta-commit {HEAD}"),
        (_argv(world, output), FailingGit(["status", "--porcelain"], ""), "checkout is not clean"),
        (_argv(world, output) + ["--owner-ruled-unmerged", ""], FakeGit(on_main=False),
         "--owner-ruled-unmerged needs the ruling text"),
        (_argv(world, output) + ["--owner-ruled-unmerged", "   "], FakeGit(on_main=False),
         "--owner-ruled-unmerged needs the ruling text"),
    ):
        assert r4.main(argv, git=git) == 1
        assert refusal in capsys.readouterr().out and not output.exists()
