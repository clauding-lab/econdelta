"""Round 4 (owner decision D2, 2 Oct 2026): each design rule is exact, not merely "close enough".

Business rule (same as tests/test_nbr_round4_candidates.py, whose world and helpers this file
imports; a separate file only because of the 800-line cap): the draft reads data and never the
pasted review; the alias rule is per day; a month-end is the last day of its month (not any 30th);
pre-window rows and the drift receipt compare FULL rows (source, provenance, ingested_at), not just
values; a kept period row can never be an R1 restamp that R1 failed to remove; and the commit
guard sees files that local git settings would hide. Each test below fails if its rule is weakened
(second review round, mutation evidence). Every row dated after 2026-09-24 is SYNTHETIC.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

import scripts.nbr_round4_candidates as r4
from tests.test_nbr_round4_candidates import (
    ALIAS,
    CHILD,
    LAST_DAY,
    ONE_RUN,
    PARENT,
    R4_STARTED,
    _build,
    _draft,
    _edit,
    _export,
    _literal,
    _row,
    _verify,
    _world,
)
from tests.test_r1_nbr_exclusion_release_order import _reviewed_backup

ROUND4_IDS = (CHILD, PARENT, ALIAS)


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    """The world's snapshots come from the real exporter, which needs a service key."""
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_synthetic-service-key")


# --- the draft reads data only, never the pasted REVIEWED literal --------------------------


def test_draft_never_reads_the_pasted_review_even_when_one_is_committed(
    tmp_path, monkeypatch, capsys
):
    """A re-review (e.g. a moved Night 2) must print what the data says, not the old literal."""
    world = _world(tmp_path)
    stale = dataclasses.replace(world.review, last_capture_day="2026-10-01",
                                recapture_sha256="0" * 64)
    monkeypatch.setattr(r4, "REVIEWED", stale)

    code, lines = _draft(world, capsys)

    assert code == 0
    assert _literal(lines) == world.review != stale


def test_draft_last_capture_day_comes_from_the_data_not_the_calendar(tmp_path, capsys):
    runs = {mid: ((value, "2026-09-25", "2026-10-01"),) for mid, ((value, _, _),) in ONE_RUN.items()}
    world = _world(tmp_path, runs=runs)  # SYNTHETIC: the old producer last ran on 2026-10-01

    code, lines = _draft(world, capsys)

    assert code == 0
    assert _literal(lines) == dataclasses.replace(world.review, last_capture_day="2026-10-01")


# --- the alias rule is exact per day -------------------------------------------------------


def test_a_second_untranscribed_alias_drift_day_with_the_same_value_is_refused(tmp_path):
    drift = {"2026-09-26": 415000.0, "2026-09-27": 415000.0}  # SYNTHETIC alias drift, two days
    world = _world(tmp_path, quirks=drift)
    alias_runs = ((415473.0, "2026-09-25", "2026-09-25"), (415000.0, "2026-09-26", "2026-09-27"),
                  (415473.0, "2026-09-29", LAST_DAY))
    review = dataclasses.replace(world.review, runs=world.review.runs | {ALIAS: alias_runs},
                                 alias_quirks={ALIAS: (("2026-09-26", 415000.0),)})

    with pytest.raises(r4.RepairConflict, match="alias != parent on 2026-09-27"):
        _build(world, review)
    both = dataclasses.replace(review, alias_quirks={ALIAS: tuple(sorted(drift.items()))})
    assert len(_build(world, both)["operations"]) == 21  # both transcribed: accepted


# --- pre-window rows: full-row identity with Night 1, not value only ------------------------


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"source": "Bangladesh Bank"}, id="source-changed"),
        pytest.param({"ingested_at": "2026-10-03T03:00:00+00:00"}, id="ingested-at-changed"),
        pytest.param({"provenance": '{"source_period_end": "2026-04-30"}'}, id="provenance-set"),
    ],
)
def test_a_pre_window_row_with_the_same_value_but_another_field_changed_is_refused(
    tmp_path, fields
):
    world = _world(tmp_path, r4_edit=_edit(PARENT, "2026-04-30", **fields))
    with pytest.raises(r4.RepairConflict, match=f"{PARENT} pre-window rows differ"):
        _build(world)


# --- a kept period row is never an R1 restamp that R1 failed to remove ----------------------


def _unremoved_restamps(day: str) -> list[dict]:
    return [r for r in _reviewed_backup() if r["as_of"] == day]


def _keep_restamps_too(day: str):
    rows = {r["metric_id"]: (day, r["value"]) for r in _unremoved_restamps(day)}
    return lambda rv: dataclasses.replace(rv, keep_period_rows={
        mid: tuple(sorted(rv.keep_period_rows[mid] + (rows[mid],))) for mid in ROUND4_IDS})


def test_a_kept_month_end_row_identical_to_its_night1_restamp_is_refused(tmp_path):
    """R1 removes every Night-1 window row; the producer writes after Night 1. So a "period row"
    identical to its Night-1 row is a restamp R1 did not remove, not a producer row."""
    world = _world(tmp_path, r4_edit=lambda rows: rows + _unremoved_restamps("2026-05-31"))
    review = _keep_restamps_too("2026-05-31")(world.review)

    with pytest.raises(r4.RepairConflict,
                       match=f"{CHILD} kept period row 2026-05-31 is identical to its Night-1 row"):
        _build(world, review)


def test_draft_says_check_for_an_r1_window_row_identical_to_night1(tmp_path, capsys):
    world = _world(tmp_path, r4_edit=lambda rows: rows + _unremoved_restamps("2026-05-31"))

    code, lines = _draft(world, capsys)

    assert code == 0
    assert any("2026-05-31" in line and "NOT-IN-NIGHT1-AS-NEW" in line for line in lines)
    checks = [line.strip() for line in lines if "CHECK:" in line]
    assert (f"CHECK: R1-window rows identical to their Night-1 row (an R1 restamp R1 did not "
            f"remove): {CHILD} 2026-05-31, {PARENT} 2026-05-31, {ALIAS} 2026-05-31") in checks


# --- a month-end is the last day of its month, not any 30th ---------------------------------


def test_a_kept_row_on_a_30th_that_is_not_a_month_end_is_refused(tmp_path):
    world = _world(tmp_path, r4_edit=lambda rows: rows + [_row(CHILD, "2026-07-30", 3.61)])
    review = dataclasses.replace(world.review, keep_period_rows=world.review.keep_period_rows | {
        CHILD: world.review.keep_period_rows[CHILD] + (("2026-07-30", 3.61),)})

    with pytest.raises(r4.RepairConflict,
                       match=f"{CHILD} kept period row 2026-07-30 is not an EconDelta month-end"):
        _build(world, review)


# --- the draft's zone flags are the human review aid; each one must still fire -------------


def test_draft_flags_an_r1_window_row_that_is_not_month_end_and_not_econdelta(tmp_path, capsys):
    world = _world(tmp_path, r4_edit=lambda rows: rows + [
        _row(CHILD, "2026-07-15", 3.61, source="Bangladesh Bank")])

    code, lines = _draft(world, capsys)

    flagged = [line for line in lines if line.strip().startswith("2026-07-15 3.61")]
    assert code == 0 and len(flagged) == 1
    assert "NOT-MONTH-END" in flagged[0] and "NOT-ECONDELTA" in flagged[0]
    assert "NOT-IN-NIGHT1-AS-NEW" not in flagged[0]


def test_draft_says_no_when_a_pre_window_row_differs_from_night1(tmp_path, capsys):
    world = _world(tmp_path, r4_edit=_edit(PARENT, "2026-04-30", value=402001.0))

    code, lines = _draft(world, capsys)

    assert code == 0
    assert "    pre-window: 1 rows 2026-04-30..2026-04-30; identical to N1: NO (2026-04-30)" in lines
    assert lines.count("    pre-window: 1 rows 2026-04-30..2026-04-30; identical to N1: yes") == 2


# --- the review transcription: id sets and run shape, each by its own message ---------------
SHAPE = "runs overlap or leave gaps over the reviewed capture days"
TRANSCRIPTION_CASES = [
    ("runs-name-an-extra-id", lambda rv: {"runs": rv.runs | {
        "nbr_fytd_collected_tbs": ((415473.0, "2026-09-25", LAST_DAY),)}},
     "runs must name exactly the three round-4 ids and alias_quirks only the alias"),
    ("quirks-name-a-non-alias-id", lambda rv: {"alias_quirks": rv.alias_quirks | {PARENT: ()}},
     "runs must name exactly the three round-4 ids and alias_quirks only the alias"),
    ("runs-leave-a-gap", lambda rv: {"runs": rv.runs | {CHILD: (
        (4.15, "2026-09-25", "2026-09-26"), (4.15, "2026-09-29", LAST_DAY))}}, f"{CHILD} {SHAPE}"),
    ("runs-overlap", lambda rv: {"runs": rv.runs | {CHILD: (
        (4.15, "2026-09-25", "2026-09-29"), (4.15, "2026-09-27", LAST_DAY))}}, f"{CHILD} {SHAPE}"),
    ("runs-start-on-the-wrong-first-day", lambda rv: {"runs": rv.runs | {CHILD: (
        (4.15, "2026-09-26", LAST_DAY),)}}, f"{CHILD} {SHAPE}"),
    ("runs-end-before-the-last-day", lambda rv: {"runs": rv.runs | {CHILD: (
        (4.15, "2026-09-25", "2026-10-01"),)}}, f"{CHILD} {SHAPE}"),
    ("run-edge-on-a-missing-day", lambda rv: {"runs": rv.runs | {CHILD: (
        (4.15, "2026-09-25", "2026-09-28"), (4.15, "2026-09-29", LAST_DAY))}}, f"{CHILD} {SHAPE}"),
    ("run-ends-before-it-starts", lambda rv: {"runs": rv.runs | {CHILD: (
        (4.15, "2026-09-25", "2026-09-29"), (4.15, "2026-09-30", "2026-09-29"),
        (4.15, "2026-09-30", LAST_DAY))}}, f"{CHILD} {SHAPE}"),
]


@pytest.mark.parametrize(("change", "refusal"), [c[1:] for c in TRANSCRIPTION_CASES],
                         ids=[c[0] for c in TRANSCRIPTION_CASES])
def test_each_review_transcription_problem_is_refused_by_its_own_message(
    tmp_path, change, refusal
):
    world = _world(tmp_path)
    review = dataclasses.replace(world.review, **change(world.review))
    with pytest.raises(r4.RepairConflict, match=refusal):
        _build(world, review)


# --- the candidate's unresolved list is reviewed bytes --------------------------------------


def test_the_candidate_unresolved_list_names_the_kept_rows_and_the_untouched_families(tmp_path):
    world = _world(tmp_path, nights=("first-night", "new-month"))

    unresolved = _build(world)["unresolved"]

    assert unresolved == [
        "Producer period rows kept, never excluded: "
        f"{CHILD} 2026-06-30=4.15; {CHILD} 2026-07-31=0.31; "
        f"{PARENT} 2026-06-30=415473.0; {PARENT} 2026-07-31=30512.4; "
        f"{ALIAS} 2026-06-30=415473.0; {ALIAS} 2026-07-31=30512.4",
        "Pre-window rows of the three ids untouched (identical to the Night-1 snapshot)",
        "Retired corroborators nbr_fytd_collected_dailystar/_tbs untouched",
        "R1 window 2026-05-02..2026-09-24 owned by the reviewed R1 candidate 12abc596...9aa3; "
        "nothing there is touched here",
    ]


# --- the drift receipt compares full rows and needs a strictly newer snapshot ---------------


@pytest.mark.parametrize(
    ("key", "fields"),
    [
        pytest.param((CHILD, "2026-09-29"), {"ingested_at": "2026-10-03T15:00:00+00:00"},
                     id="restamp-ingested-at-only"),
        pytest.param((PARENT, "2026-06-30"), {"source": "Bangladesh Bank"},
                     id="kept-period-row-source-only"),
        pytest.param(("nbr_fytd_collected_tbs", "2026-05-02"),
                     {"provenance": '{"source_period_end": "2026-05-31"}'},
                     id="corroborator-provenance-only"),
    ],
)
def test_verify_unchanged_sees_a_change_that_keeps_the_value(tmp_path, capsys, key, fields):
    world = _world(tmp_path)
    n2 = _export(tmp_path / "n2-2026-10-03", _edit(*key, **fields)(world.r4_rows),
                 "2026-10-03T15:50:00+00:00")
    out = tmp_path / "receipts" / "verify-n2.json"

    assert _verify(world, n2, out) == 1

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "changed" and receipt["differing_keys"] == [list(key)]
    assert f"differs: {key[0]} {key[1]}" in capsys.readouterr().out


@pytest.mark.parametrize("corroborator", ["nbr_fytd_collected_dailystar", "nbr_fytd_collected_tbs"])
def test_verify_unchanged_watches_each_retired_corroborator(tmp_path, capsys, corroborator):
    """Both retired corroborators are watched: dropping either from the drift receipt is a hole."""
    world = _world(tmp_path)
    key = (corroborator, "2026-05-02")
    n2 = _export(tmp_path / "n2-2026-10-03", _edit(*key, value=1)(world.r4_rows),
                 "2026-10-03T15:50:00+00:00")
    out = tmp_path / "receipts" / "verify-n2.json"

    assert _verify(world, n2, out) == 1

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "changed" and receipt["differing_keys"] == [list(key)]
    assert f"differs: {key[0]} {key[1]}" in capsys.readouterr().out


def test_verify_unchanged_refuses_a_fresh_snapshot_started_at_the_same_instant(tmp_path, capsys):
    world = _world(tmp_path)
    same_instant = _export(tmp_path / "n2-same-instant", world.r4_rows[1:], R4_STARTED)
    out = tmp_path / "receipts" / "verify-n2.json"

    assert _verify(world, same_instant, out) == 1

    assert "fresh snapshot did not start after the recapture" in capsys.readouterr().out
    assert not out.exists()


# --- the commit guard, against a REAL local git repo (offline) ------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=round4", "-c", "user.email=r4@example.invalid",
                           *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "reviewed.py").write_text("REVIEWED = None\n")
    _git(repo, "add", "reviewed.py")
    _git(repo, "commit", "-q", "-m", "reviewed")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _runner(repo: Path):
    def run(args: list[str]) -> tuple[int, str]:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        return done.returncode, done.stdout
    return run


@pytest.mark.parametrize(
    "hide",
    [
        pytest.param("untracked-with-showUntrackedFiles-no", id="untracked-hidden-by-config"),
        pytest.param("skip-worktree", id="edit-hidden-by-skip-worktree"),
        pytest.param("assume-unchanged", id="edit-hidden-by-assume-unchanged"),
    ],
)
def test_commit_guard_sees_changes_that_local_git_settings_hide(tmp_path, hide):
    repo, head = _repo(tmp_path)
    assert r4.commit_guard(_runner(repo), head, check_main=False) == head  # clean: passes
    if hide == "untracked-with-showUntrackedFiles-no":
        _git(repo, "config", "status.showUntrackedFiles", "no")
        (repo / "stray.py").write_text("x = 1\n")
    else:
        _git(repo, "update-index", f"--{hide}", "reviewed.py")
        (repo / "reviewed.py").write_text("REVIEWED = 'hand-edited'\n")
    assert _git(repo, "status", "--porcelain") == ""  # plain status is blind here

    with pytest.raises(r4.RepairConflict, match="checkout is not clean"):
        r4.commit_guard(_runner(repo), head, check_main=False)
