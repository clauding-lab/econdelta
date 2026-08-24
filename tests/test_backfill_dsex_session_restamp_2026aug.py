"""Tests for scripts/backfill_dsex_session_restamp_2026aug.py.

Two layers:
  1. Pure plan-computation tests (compute_plan / cross_check_plan /
     build_insert_rows / group_cohorts / match_true_session) — no network,
     built from the REAL delete/restamp/insert cohort table the module's
     own docstring describes (not an idealized toy shape), reverse-derived
     from the module's own EXPECTED_DELETES/EXPECTED_RESTAMPS/
     EXPECTED_INSERT_DATES tripwire constants so a change to the algorithm
     (not the constants) is what these tests actually exercise.
  2. I/O tests with a mocked requests.Session (mirrors
     tests/test_supabase_reader.py's MagicMock(spec=requests.Session)
     style) — no real Supabase call goes out.

NEVER exercises the real --write path against a live database — this is a
one-time production backfill, not run in CI or by any agent unattended (see
its module docstring). run()'s --dry-run path itself performs a REAL read
(the plan has to be computed and cross-checked against something), which is
why this file does not add a network-mocked run() CLI test: the module's
required test surface — plan computation, aborts, idempotency, insert
arithmetic — lives entirely in the pure functions below, matching the
approach tests/test_backfill_cpi_july_2026.py already established for a
sibling one-time backfill script's dry-run-vs-network split.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from scripts.backfill_dsex_session_restamp_2026aug import (
    DSE_METRIC_IDS,
    EXPECTED_DELETES,
    EXPECTED_INSERT_DATES,
    EXPECTED_RESTAMPS,
    OFFICIAL_SESSIONS_RAW,
    Cohort,
    DeleteAction,
    HistoryRow,
    InsertAction,
    OfficialSession,
    Plan,
    PlanError,
    RestampAction,
    build_insert_rows,
    compute_plan,
    cross_check_plan,
    execute_delete,
    execute_insert,
    execute_restamp,
    fetch_dse_history_rows,
    group_cohorts,
    match_true_session,
    parse_official_sessions,
    verify_post_write,
)
from utils.supabase_writer import SupabaseWriteError


def _make_session(status: int = 200, payload: object = None) -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    resp = MagicMock()
    resp.status_code = status
    resp.text = "[]" if payload is None else "x"
    resp.json.return_value = payload if payload is not None else []
    sess.get.return_value = resp
    sess.post.return_value = resp
    sess.patch.return_value = resp
    sess.delete.return_value = resp
    return sess


def _row(metric_id: str, as_of: str, value: float, ingested_at: str) -> HistoryRow:
    return HistoryRow(metric_id=metric_id, as_of=date.fromisoformat(as_of), value=value, ingested_at=ingested_at)


def _cohort(as_of: str, ingested_at: str, dsex: float, ids: tuple[str, ...] = DSE_METRIC_IDS) -> Cohort:
    rows = tuple(HistoryRow(metric_id=m, as_of=date.fromisoformat(as_of), value=dsex, ingested_at=ingested_at) for m in ids)
    return Cohort(as_of=date.fromisoformat(as_of), ingested_at=ingested_at, rows=rows)


# ---------------------------------------------------------------------------
# parse_official_sessions
# ---------------------------------------------------------------------------


class TestOfficialSessions:
    def test_excludes_2026_08_24(self):
        assert "2026-08-24" not in OFFICIAL_SESSIONS_RAW

    def test_covers_exactly_36_sessions_2026_07_02_through_2026_08_23(self):
        sessions = parse_official_sessions()
        assert len(sessions) == 36
        assert min(sessions) == date(2026, 7, 2)
        assert max(sessions) == date(2026, 8, 23)

    def test_dses_is_always_none(self):
        """Ground truth for dses is null everywhere in this archive capture
        -- the reason it's excluded from INSERTABLE_IDS (honest gap)."""
        sessions = parse_official_sessions()
        assert all(s.dses is None for s in sessions.values())


# ---------------------------------------------------------------------------
# group_cohorts / match_true_session
# ---------------------------------------------------------------------------


class TestGroupCohorts:
    def test_groups_by_as_of_and_ingested_at(self):
        rows = [
            _row("dsex", "2026-07-12", 5849.21831, "2026-07-13T08:01:00Z"),
            _row("ds30", "2026-07-12", 2200.74042, "2026-07-13T08:01:00Z"),
            _row("dsex", "2026-07-13", 5866.54957, "2026-07-14T08:01:00Z"),
        ]
        cohorts = group_cohorts(rows)
        assert len(cohorts) == 2
        assert cohorts[0].as_of == date(2026, 7, 12)
        assert cohorts[0].dsex == pytest.approx(5849.21831)
        assert set(cohorts[0].metric_ids) == {"dsex", "ds30"}

    def test_sorted_ascending_by_as_of_then_ingested_at(self):
        rows = [
            _row("dsex", "2026-07-14", 1.0, "x"),
            _row("dsex", "2026-07-12", 1.0, "x"),
            _row("dsex", "2026-07-13", 1.0, "x"),
        ]
        cohorts = group_cohorts(rows)
        assert [c.as_of for c in cohorts] == [date(2026, 7, 12), date(2026, 7, 13), date(2026, 7, 14)]


class TestMatchTrueSession:
    def test_matches_the_one_official_session_within_tolerance(self):
        official = parse_official_sessions()
        c = _cohort("2026-07-13", "ing", dsex=5849.21831)  # true 07-12's close
        assert match_true_session(c, official) == date(2026, 7, 12)

    def test_no_dsex_row_raises(self):
        official = parse_official_sessions()
        c = Cohort(as_of=date(2026, 7, 12), ingested_at="x", rows=())
        with pytest.raises(PlanError, match="no 'dsex' row"):
            match_true_session(c, official)

    def test_ambiguous_match_aborts(self):
        """Widening the tolerance band around a real dsex value (5849.21831,
        2026-07-12's true close) until it ALSO covers 2026-08-10's close
        (5844.87198, 4.35 points away) reproduces a genuine two-way
        ambiguity -- must abort, never guess which session is meant."""
        official = parse_official_sessions()
        c = _cohort("2026-07-20", "ing", dsex=5849.21831)
        with pytest.raises(PlanError, match="matched 2 official session"):
            match_true_session(c, official, tolerance=5.0)

    def test_zero_matches_aborts(self):
        official = parse_official_sessions()
        c = _cohort("2026-07-20", "ing", dsex=1.0)
        with pytest.raises(PlanError, match="matched 0 official session"):
            match_true_session(c, official)


# ---------------------------------------------------------------------------
# compute_plan — the flagship reproduction test, built from the REAL cohort
# table (reverse-derived from the module's own tripwire constants, not a
# toy shape).
# ---------------------------------------------------------------------------


def _build_real_cohorts() -> list[Cohort]:
    """Reconstruct the 37 real stored cohorts (as of the incident) from the
    module's own EXPECTED_DELETES / EXPECTED_RESTAMPS / EXPECTED_INSERT_DATES
    + OFFICIAL_SESSIONS_RAW. Every official date EXCEPT the 3 insert targets
    had a stored cohort; PLUS the 4 delete-source dates and 23 restamp-source
    dates (some of which coincide with official dates already counted).
    This mirrors exactly how the real table was reconstructed and cross-
    verified offline before this script was written.
    """
    official = parse_official_sessions()
    # duplicate_of per delete (the true session each duplicate holds) —
    # derivable from context: each delete's stored cohort must dsex-match
    # a distinct, ALREADY-CORRECT official session. We look this up from the
    # real incident record directly (hardcoded here ONLY as fixture truth,
    # exactly mirroring the module docstring's own DELETE list).
    delete_true = {
        "2026-07-13": "2026-07-12",
        "2026-07-15": "2026-07-14",
        "2026-08-06": "2026-08-04",
        "2026-08-13": "2026-08-12",
    }
    restamp_true = {src: tgt for src, tgt in EXPECTED_RESTAMPS}
    insert_dates = EXPECTED_INSERT_DATES

    stored_dates = {d.isoformat() for d in official} - insert_dates
    stored_dates |= set(delete_true)
    stored_dates |= set(restamp_true)

    true_session_for = {}
    for d in stored_dates:
        if d in delete_true:
            true_session_for[d] = delete_true[d]
        elif d in restamp_true:
            true_session_for[d] = restamp_true[d]
        else:
            true_session_for[d] = d

    ingested_at_for = dict(EXPECTED_DELETES)  # (as_of, ingested_at) pairs for deletes

    cohorts = []
    for stored_iso in stored_dates:
        true_iso = true_session_for[stored_iso]
        dsex_value = official[date.fromisoformat(true_iso)].dsex
        ingested_at = ingested_at_for.get(stored_iso, f"{stored_iso}T08:01:00Z")
        cohorts.append(_cohort(stored_iso, ingested_at, dsex_value))
    return cohorts


class TestComputePlanReproducesRealIncident:
    def test_deletes_match_expected_exactly(self):
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        plan = compute_plan(cohorts, official)
        computed = {(a.as_of.isoformat(), a.ingested_at) for a in plan.deletes}
        assert computed == set(EXPECTED_DELETES)

    def test_restamps_match_expected_exactly_including_order(self):
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        plan = compute_plan(cohorts, official)
        computed = tuple((a.old_as_of.isoformat(), a.new_as_of.isoformat()) for a in plan.restamps)
        assert computed == EXPECTED_RESTAMPS

    def test_insert_dates_match_expected_exactly(self):
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        plan = compute_plan(cohorts, official)
        assert {d.isoformat() for d in plan.insert_dates} == EXPECTED_INSERT_DATES

    def test_no_action_count_is_the_rest(self):
        """37 stored cohorts - 4 deletes - 23 restamps = 10 no-ops (the
        sessions that were always correctly dated)."""
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        plan = compute_plan(cohorts, official)
        assert len(cohorts) == 37
        assert len(plan.no_actions) == 37 - 4 - 23

    def test_cross_check_passes_on_the_real_plan(self):
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        plan = compute_plan(cohorts, official)
        cross_check_plan(plan)  # must not raise


class TestComputePlanAborts:
    def test_occupied_unresolved_target_aborts(self):
        """Forward-shift fixture: three official sessions where every
        cohort's own stored as_of is EARLIER than its true session, so each
        mover's target hasn't been resolved yet by ascending-order
        processing. This is the only reachable way the 'occupied-slot'
        integrity check fires as an ABORT rather than a clean delete/restamp
        -- the real (backward-shift) data never hits it."""
        d1, d2, d3 = date(2026, 7, 2), date(2026, 7, 5), date(2026, 7, 6)
        official = parse_official_sessions()
        v1 = official[d1].dsex
        v2 = official[d2].dsex
        v3 = official[d3].dsex
        cohorts = [
            _cohort(d1.isoformat(), "ing1", dsex=v2),  # wants to move FORWARD to d2
            _cohort(d2.isoformat(), "ing2", dsex=v3),  # wants to move FORWARD to d3
            _cohort(d3.isoformat(), "ing3", dsex=v1),  # wants to move BACKWARD to d1
        ]
        with pytest.raises(PlanError, match="not yet resolved"):
            compute_plan(cohorts, official)

    def test_occupied_permanent_slot_with_mismatched_dsex_aborts(self):
        """Both the permanent occupant and the incoming mover independently
        matched the SAME target within tolerance of the target's own
        official close, but are more than `tolerance` apart from EACH
        OTHER (each sits on an opposite edge of the tolerance band) — a
        real, reachable edge case (not merely hypothetical), since the
        tolerance check in match_true_session is against the OFFICIAL
        value, not between cohorts. The occupied-slot integrity check
        inside compute_plan must catch this and abort rather than silently
        treating the mover as a duplicate."""
        d1, d2 = date(2026, 7, 2), date(2026, 7, 5)
        official = {
            d1: OfficialSession(as_of=d1, dsex=100.00, dsex_chg=0.0, ds30=0.0,
                                 ds30_chg=0.0, dses=None, value_mn=0.0, trades=0.0),
            d2: OfficialSession(as_of=d2, dsex=500.00, dsex_chg=0.0, ds30=0.0,
                                 ds30_chg=0.0, dses=None, value_mn=0.0, trades=0.0),
        }
        occupant = _cohort(d1.isoformat(), "ing1", dsex=100.009)  # true_session=d1, stays
        mover = _cohort(d2.isoformat(), "ing2", dsex=99.994)  # true_session=d1 too (within tol of 100.00)
        with pytest.raises(PlanError, match="occupied-slot integrity check failed"):
            compute_plan([occupant, mover], official)


class TestComputePlanSameAsOfCollision:
    """A resumed run after a mid-sequence crash can legitimately fetch TWO
    cohorts sharing the exact same as_of at once: an unexecuted duplicate
    still sitting at its stored (wrong) as_of, plus an already-restamped
    mover that landed on that SAME date via an earlier action in the SAME
    crashed run. compute_plan() must resolve each cohort against its OWN
    match, not whichever same-as_of cohort happened to be matched last --
    a per-as_of (rather than per-cohort-identity) lookup table silently
    corrupts this by letting the second cohort's match overwrite the
    first's before the first is ever read back."""

    def test_duplicate_and_already_correct_cohort_sharing_as_of_both_resolve_correctly(self):
        official = parse_official_sessions()
        d1, d2 = date(2026, 7, 2), date(2026, 7, 5)  # adjacent real sessions
        no_op_at_d1 = _cohort(d1.isoformat(), f"{d1.isoformat()}T08:01:00Z", dsex=official[d1].dsex)
        # A duplicate of d1's data, mislabeled at d2 -- sorts FIRST at as_of=d2
        # by ingested_at.
        duplicate_at_d2 = _cohort(d2.isoformat(), "2026-07-05T01:00:00Z", dsex=official[d1].dsex)
        # The genuinely correct d2 cohort, ALSO at as_of=d2 -- sorts SECOND.
        correct_at_d2 = _cohort(d2.isoformat(), "2026-07-05T09:00:00Z", dsex=official[d2].dsex)

        plan = compute_plan([no_op_at_d1, duplicate_at_d2, correct_at_d2], official)

        assert plan.restamps == ()
        assert len(plan.deletes) == 1
        [delete] = plan.deletes
        assert delete.as_of == d2
        assert delete.ingested_at == "2026-07-05T01:00:00Z"
        assert delete.duplicate_of == d1
        assert {c.ingested_at for c in plan.no_actions} == {
            no_op_at_d1.ingested_at, correct_at_d2.ingested_at,
        }


class TestComputePlanIdempotency:
    def test_already_healed_table_yields_zero_actions(self):
        """Every official session has exactly one correctly-dated cohort —
        the state AFTER a successful --write run. Re-running compute_plan
        must find nothing to do."""
        official = parse_official_sessions()
        cohorts = [
            _cohort(d.isoformat(), f"{d.isoformat()}T08:01:00Z", dsex=s.dsex)
            for d, s in official.items()
        ]
        plan = compute_plan(cohorts, official)
        assert plan.deletes == ()
        assert plan.restamps == ()
        assert plan.insert_dates == ()
        assert len(plan.no_actions) == len(official)

    def test_cross_check_passes_on_the_healed_table_zero_actions_exit_0(self):
        """Spec requirement: 'idempotent (re-run after success = zero
        actions, exit 0)'. The incident-specific EXPECTED_* tripwire checks
        that every COMPUTED action is a member of the hand-verified
        superset -- an EMPTY computed plan is trivially a subset of
        anything, so cross_check_plan must NOT raise here. (Earlier draft
        of this test asserted the opposite -- that was itself the bug: it
        locked in an abort on the healed table, which made run()'s own
        'No actions needed — DB already matches...' success message
        unreachable dead code, since cross_check_plan() was called BEFORE
        that print and always raised first on a fully healed table.)"""
        official = parse_official_sessions()
        cohorts = [
            _cohort(d.isoformat(), f"{d.isoformat()}T08:01:00Z", dsex=s.dsex)
            for d, s in official.items()
        ]
        plan = compute_plan(cohorts, official)
        cross_check_plan(plan)  # must not raise


class TestPartialFailureReRunConvergence:
    """Spec requirement: 'mid-sequence-crash convergent on re-run'. Simulate
    a --write run that crashed after committing only a PREFIX of the full
    incident plan's actions (some deletes done, some restamps done, no
    inserts yet), then prove a fresh compute_plan() on the resulting
    (partially-healed) cohort set: (a) still passes cross_check_plan without
    a human touching EXPECTED_*, (b) computes exactly the REMAINING actions
    (not a re-do of what's already done, not a mis-derived plan), and (c)
    that remaining plan, once conceptually applied on top of what the
    crashed run already committed, reconstructs the exact same total set of
    actions the untouched incident table's full first-run plan produced --
    i.e. the two-stage (crash + resume) execution is equivalent to one
    clean run, never double-applies, and never leaves a gap.
    """

    def _apply_ordered_prefix(self, cohorts, ordered_actions_prefix):
        """Mutate a cohort list the way a real partial --write would leave
        the DB after committing exactly this PREFIX of ``plan.ordered_actions``,
        IN ORDER -- mirrors what execute_delete/execute_restamp actually do
        to the underlying rows, at the pure-data level this test operates at
        (no network).

        Deliberately walks a genuine prefix of the SAME ``ordered_actions``
        sequence ``run()`` executes (rather than independently slicing
        ``.deletes`` and ``.restamps``): ``run()``'s real execution loop
        processes actions strictly in that interleaved order, so a restamp
        that lands INTO a slot is only ever reached after whatever action
        vacates that slot has already run. An independently-sliced "N
        restamps but fewer deletes" prefix does not correspond to any
        reachable crash point of the real loop, and can construct a same-
        as_of collision (an un-executed duplicate still sitting at its
        stored as_of, plus an already-restamped mover landing on that same
        date) that a real sequential crash can never produce -- ascending
        stored-as_of order guarantees whatever vacates a target slot is
        always ordered, and therefore executed, before the restamp that
        moves into it."""
        by_key = {(c.as_of, c.ingested_at): c for c in cohorts}
        for action in ordered_actions_prefix:
            if isinstance(action, DeleteAction):
                del by_key[(action.as_of, action.ingested_at)]
            else:
                c = by_key.pop((action.old_as_of, action.ingested_at))
                moved_rows = tuple(
                    HistoryRow(metric_id=row.metric_id, as_of=action.new_as_of, value=row.value, ingested_at=row.ingested_at, source=row.source)
                    for row in c.rows
                )
                moved = Cohort(as_of=action.new_as_of, ingested_at=action.ingested_at, rows=moved_rows)
                by_key[(moved.as_of, moved.ingested_at)] = moved
        return list(by_key.values())

    def test_resumed_plan_is_exactly_the_remainder_and_cross_checks_clean(self):
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        full_plan = compute_plan(cohorts, official)
        cross_check_plan(full_plan)  # sanity: the untouched first-run plan is still clean
        assert len(full_plan.ordered_actions) == 4 + 23  # sanity on the fixture itself

        # "Crash" partway through the interleaved delete/restamp sequence.
        prefix = full_plan.ordered_actions[:6]
        partial_cohorts = self._apply_ordered_prefix(cohorts, prefix)

        resumed_plan = compute_plan(partial_cohorts, official)
        cross_check_plan(resumed_plan)  # must NOT raise -- no human edits EXPECTED_* mid-incident

        # Exactly the remainder: nothing re-done, nothing skipped, same order.
        assert resumed_plan.ordered_actions == full_plan.ordered_actions[6:]
        assert set(resumed_plan.insert_dates) == set(full_plan.insert_dates)

    def test_two_stage_execution_reconstructs_the_full_plan(self):
        """(what the crashed run committed) followed by (what the resumed
        run computes) == the original untouched-table full plan, action for
        action, in order -- proves the crash/resume split is equivalent to
        one clean run, not a lossy or duplicating one."""
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        full_plan = compute_plan(cohorts, official)

        prefix = full_plan.ordered_actions[:15]
        partial_cohorts = self._apply_ordered_prefix(cohorts, prefix)
        resumed_plan = compute_plan(partial_cohorts, official)
        cross_check_plan(resumed_plan)

        assert prefix + resumed_plan.ordered_actions == full_plan.ordered_actions
        assert set(resumed_plan.insert_dates) == set(full_plan.insert_dates)

    def test_crash_after_everything_but_inserts_leaves_only_inserts(self):
        official = parse_official_sessions()
        cohorts = _build_real_cohorts()
        full_plan = compute_plan(cohorts, official)
        partial_cohorts = self._apply_ordered_prefix(cohorts, full_plan.ordered_actions)

        resumed_plan = compute_plan(partial_cohorts, official)
        cross_check_plan(resumed_plan)
        assert resumed_plan.deletes == ()
        assert resumed_plan.restamps == ()
        assert set(resumed_plan.insert_dates) == set(full_plan.insert_dates)


class TestCrossCheckPlanMismatches:
    def test_missing_restamp_does_not_abort(self):
        """A restamp present in EXPECTED_RESTAMPS but absent from the
        computed plan is the NORMAL shape of a partially-resumed run (that
        cohort's correction already landed in an earlier partial --write) --
        cross_check_plan must not treat 'fewer than the full expected set'
        as an error. (Superseded the old 'missing restamp aborts'
        expectation -- that behavior was the confirmed bug: it made a
        legitimate crash/resume cycle unrecoverable without a human hand-
        editing EXPECTED_RESTAMPS.)"""
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        truncated = Plan(
            ordered_actions=tuple(a for a in real_plan.ordered_actions if not (isinstance(a, RestampAction) and a.old_as_of == date(2026, 7, 16))),
            insert_dates=real_plan.insert_dates,
            no_actions=real_plan.no_actions,
        )
        cross_check_plan(truncated)  # must not raise

    def test_missing_insert_does_not_abort(self):
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        truncated = Plan(
            ordered_actions=real_plan.ordered_actions,
            insert_dates=tuple(d for d in real_plan.insert_dates if d != date(2026, 8, 20)),
            no_actions=real_plan.no_actions,
        )
        cross_check_plan(truncated)  # must not raise

    def test_missing_delete_does_not_abort(self):
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        truncated = Plan(
            ordered_actions=tuple(a for a in real_plan.ordered_actions if not (isinstance(a, DeleteAction) and a.as_of == date(2026, 7, 13))),
            insert_dates=real_plan.insert_dates,
            no_actions=real_plan.no_actions,
        )
        cross_check_plan(truncated)  # must not raise

    def test_extra_delete_aborts(self):
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        extra = Plan(
            ordered_actions=real_plan.ordered_actions + (
                DeleteAction(as_of=date(2026, 7, 2), ingested_at="2026-07-02T08:01:00Z",
                             metric_ids=("dsex",), duplicate_of=date(2026, 7, 2)),
            ),
            insert_dates=real_plan.insert_dates,
            no_actions=real_plan.no_actions,
        )
        with pytest.raises(PlanError, match="DELETE plan mismatch"):
            cross_check_plan(extra)

    def test_extra_restamp_aborts(self):
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        extra = Plan(
            ordered_actions=real_plan.ordered_actions + (
                RestampAction(old_as_of=date(2026, 7, 2), new_as_of=date(2026, 7, 3),
                               ingested_at="2026-07-02T08:01:00Z", metric_ids=("dsex",)),
            ),
            insert_dates=real_plan.insert_dates,
            no_actions=real_plan.no_actions,
        )
        with pytest.raises(PlanError, match="RESTAMP plan mismatch"):
            cross_check_plan(extra)

    def test_extra_insert_aborts(self):
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        extra = Plan(
            ordered_actions=real_plan.ordered_actions,
            insert_dates=real_plan.insert_dates + (date(2026, 7, 3),),
            no_actions=real_plan.no_actions,
        )
        with pytest.raises(PlanError, match="INSERT plan mismatch"):
            cross_check_plan(extra)

    def test_out_of_order_restamps_abort(self):
        """Two genuine EXPECTED_RESTAMPS entries reordered relative to the
        hand-verified backward-shift chain -- must abort even though both
        are individually members of the expected set."""
        real_plan = compute_plan(_build_real_cohorts(), parse_official_sessions())
        restamps = list(real_plan.restamps)
        # Swap two adjacent restamps to violate the expected ascending order.
        restamps[0], restamps[1] = restamps[1], restamps[0]
        non_restamp_actions = tuple(a for a in real_plan.ordered_actions if isinstance(a, DeleteAction))
        reordered = Plan(
            ordered_actions=non_restamp_actions + tuple(restamps),
            insert_dates=real_plan.insert_dates,
            no_actions=real_plan.no_actions,
        )
        with pytest.raises(PlanError, match="out of the .* order"):
            cross_check_plan(reordered)


# ---------------------------------------------------------------------------
# build_insert_rows — arithmetic
# ---------------------------------------------------------------------------


class TestBuildInsertRows:
    def test_dsex_change_pct_for_2026_08_20(self):
        """dsex_change_pct = +16.36974 / 5769.7108 * 100, rounded to 5dp."""
        official = parse_official_sessions()
        [action] = build_insert_rows((date(2026, 8, 20),), official)
        expected_pct = round(16.36974 / 5769.7108 * 100, 5)
        assert action.values["dsex_change_pct"] == pytest.approx(expected_pct, abs=1e-9)
        assert action.values["dsex_change_pct"] == pytest.approx(0.28372, abs=1e-5)

    def test_turnover_crore_for_2026_08_20(self):
        official = parse_official_sessions()
        [action] = build_insert_rows((date(2026, 8, 20),), official)
        assert action.values["turnover_crore"] == pytest.approx(671.9891)

    def test_values_for_2026_07_13_and_2026_08_11(self):
        official = parse_official_sessions()
        actions = {a.as_of: a for a in build_insert_rows((date(2026, 7, 13), date(2026, 8, 11)), official)}
        a13 = actions[date(2026, 7, 13)]
        assert a13.values["dsex"] == pytest.approx(5866.54957)
        assert a13.values["dsex_change"] == pytest.approx(17.33126)
        assert a13.values["dsex_change_pct"] == pytest.approx(round(17.33126 / 5849.21831 * 100, 5))
        a11 = actions[date(2026, 8, 11)]
        assert a11.values["dsex"] == pytest.approx(5903.53106)
        assert a11.values["dsex_change_pct"] == pytest.approx(round(58.65908 / 5844.87198 * 100, 5))

    def test_omits_advancing_declining_unchanged_and_dses(self):
        official = parse_official_sessions()
        [action] = build_insert_rows((date(2026, 8, 20),), official)
        for missing_id in ("advancing", "declining", "unchanged", "dses"):
            assert missing_id not in action.values

    def test_source_label(self):
        official = parse_official_sessions()
        [action] = build_insert_rows((date(2026, 8, 20),), official)
        assert action.source == "dsebd_market_summary_archive"

    def test_no_prior_session_raises(self):
        official = parse_official_sessions()
        with pytest.raises(PlanError, match="no prior session"):
            build_insert_rows((date(2026, 7, 2),), official)  # earliest session, no prior


# ---------------------------------------------------------------------------
# I/O — mocked requests.Session
# ---------------------------------------------------------------------------


class TestFetchDseHistoryRows:
    def test_hits_metric_history_with_expected_filters(self):
        sess = _make_session(payload=[
            {"metric_id": "dsex", "as_of": "2026-07-12", "value": 5849.21831, "ingested_at": "2026-07-12T08:01:00Z", "source": "EconDelta"},
        ])
        rows = fetch_dse_history_rows(url="https://example.supabase.co", key="sk_test", session=sess)
        assert len(rows) == 1
        assert rows[0].metric_id == "dsex"
        assert rows[0].as_of == date(2026, 7, 12)
        args, kwargs = sess.get.call_args
        assert args[0] == "https://example.supabase.co/rest/v1/metric_history"
        assert kwargs["params"]["metric_id"] == "in.(" + ",".join(DSE_METRIC_IDS) + ")"
        assert kwargs["headers"]["apikey"] == "sk_test"

    def test_pages_until_short_page(self):
        page1 = [{"metric_id": "dsex", "as_of": "2026-07-02", "value": 1.0, "ingested_at": "x", "source": "s"}] * 1000
        page2 = [{"metric_id": "dsex", "as_of": "2026-07-03", "value": 2.0, "ingested_at": "x", "source": "s"}]
        sess = MagicMock(spec=requests.Session)
        resp1, resp2 = MagicMock(), MagicMock()
        resp1.status_code, resp2.status_code = 200, 200
        resp1.json.return_value, resp2.json.return_value = page1, page2
        sess.get.side_effect = [resp1, resp2]
        rows = fetch_dse_history_rows(url="https://example.supabase.co", key="sk_test", session=sess)
        assert len(rows) == 1001
        assert sess.get.call_count == 2

    def test_raises_on_non_2xx(self):
        sess = _make_session(status=500)
        with pytest.raises(SupabaseWriteError):
            fetch_dse_history_rows(url="https://example.supabase.co", key="sk_test", session=sess)

    def test_raises_on_missing_credentials(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        with pytest.raises(SupabaseWriteError):
            fetch_dse_history_rows()


class TestExecuteDelete:
    def test_deletes_and_returns_row_count(self):
        action = DeleteAction(as_of=date(2026, 7, 13), ingested_at="2026-07-13T08:01:17Z",
                               metric_ids=("dsex", "ds30"), duplicate_of=date(2026, 7, 12))
        sess = _make_session(payload=[{"metric_id": "dsex"}, {"metric_id": "ds30"}])
        n = execute_delete(action, url="https://example.supabase.co", key="sk_test", session=sess)
        assert n == 2
        args, kwargs = sess.delete.call_args
        assert kwargs["params"]["as_of"] == "eq.2026-07-13"
        assert kwargs["params"]["ingested_at"] == "eq.2026-07-13T08:01:17Z"

    def test_row_count_mismatch_aborts(self):
        action = DeleteAction(as_of=date(2026, 7, 13), ingested_at="x",
                               metric_ids=("dsex", "ds30", "dses"), duplicate_of=date(2026, 7, 12))
        sess = _make_session(payload=[{"metric_id": "dsex"}])  # only 1, expected 3
        with pytest.raises(SupabaseWriteError, match="expected 3"):
            execute_delete(action, url="https://example.supabase.co", key="sk_test", session=sess)


class TestExecuteRestamp:
    def test_restamps_when_target_is_empty(self):
        action = RestampAction(old_as_of=date(2026, 7, 16), new_as_of=date(2026, 7, 15),
                                ingested_at="ing", metric_ids=("dsex", "ds30"))
        sess = MagicMock(spec=requests.Session)
        check_resp = MagicMock(status_code=200)
        check_resp.json.return_value = []  # target empty
        patch_resp = MagicMock(status_code=200, text="x")
        patch_resp.json.return_value = [{"metric_id": "dsex"}, {"metric_id": "ds30"}]
        sess.get.return_value = check_resp
        sess.patch.return_value = patch_resp
        n = execute_restamp(action, url="https://example.supabase.co", key="sk_test", session=sess)
        assert n == 2
        patch_args, patch_kwargs = sess.patch.call_args
        assert patch_kwargs["json"] == {"as_of": "2026-07-15"}

    def test_occupied_target_at_execution_time_aborts(self):
        """The write-time re-check tripwire: if the target slot is NOT
        empty right before the PATCH, abort rather than let a PK violation
        surface as an opaque Postgres error."""
        action = RestampAction(old_as_of=date(2026, 7, 16), new_as_of=date(2026, 7, 15),
                                ingested_at="ing", metric_ids=("dsex",))
        sess = MagicMock(spec=requests.Session)
        check_resp = MagicMock(status_code=200)
        check_resp.json.return_value = [{"metric_id": "dsex"}]  # target NOT empty
        sess.get.return_value = check_resp
        with pytest.raises(SupabaseWriteError, match="occupied at execution time"):
            execute_restamp(action, url="https://example.supabase.co", key="sk_test", session=sess)
        sess.patch.assert_not_called()


class TestExecuteInsert:
    def test_delegates_to_upsert_metric_history_with_correct_shape(self):
        action = InsertAction(as_of=date(2026, 8, 20), values={"dsex": 5786.08054, "dsex_change_pct": 0.28372})
        sess = _make_session()
        n = execute_insert(action, url="https://example.supabase.co", key="sk_test", session=sess)
        assert n == 2
        post_args, post_kwargs = sess.post.call_args
        body = post_kwargs["json"]
        as_ofs = {row["as_of"] for row in body}
        sources = {row["source"] for row in body}
        assert as_ofs == {"2026-08-20"}
        assert sources == {"dsebd_market_summary_archive"}


class TestVerifyPostWrite:
    def _official_slice(self):
        return {date(2026, 7, 2): parse_official_sessions()[date(2026, 7, 2)]}

    def test_pass_when_everything_matches(self):
        official = self._official_slice()
        sess = _make_session(payload=[
            {"metric_id": "dsex", "as_of": "2026-07-02", "value": 5743.85884, "ingested_at": "x", "source": "s"},
        ])
        result = verify_post_write(official, frozenset(), url="https://example.supabase.co", key="sk_test", session=sess)
        assert result.ok
        assert result.problems == ()

    def test_fail_on_value_mismatch(self):
        official = self._official_slice()
        sess = _make_session(payload=[
            {"metric_id": "dsex", "as_of": "2026-07-02", "value": 9999.0, "ingested_at": "x", "source": "s"},
        ])
        result = verify_post_write(official, frozenset(), url="https://example.supabase.co", key="sk_test", session=sess)
        assert not result.ok
        assert any("!= official" in p for p in result.problems)

    def test_fail_on_duplicate_as_of(self):
        official = self._official_slice()
        sess = _make_session(payload=[
            {"metric_id": "dsex", "as_of": "2026-07-02", "value": 5743.85884, "ingested_at": "a", "source": "s"},
            {"metric_id": "dsex", "as_of": "2026-07-02", "value": 5743.85884, "ingested_at": "b", "source": "s"},
        ])
        result = verify_post_write(official, frozenset(), url="https://example.supabase.co", key="sk_test", session=sess)
        assert not result.ok
        assert any("duplicate" in p for p in result.problems)

    def test_fail_when_deleted_cohort_still_present(self):
        official = self._official_slice()
        sess = _make_session(payload=[
            {"metric_id": "dsex", "as_of": "2026-07-02", "value": 5743.85884, "ingested_at": "2026-07-13T08:01:17Z", "source": "s"},
        ])
        deleted = frozenset({("2026-07-02", "2026-07-13T08:01:17Z")})
        result = verify_post_write(official, deleted, url="https://example.supabase.co", key="sk_test", session=sess)
        assert not result.ok
        assert any("still present" in p for p in result.problems)

    def test_fail_when_session_missing_entirely(self):
        official = self._official_slice()
        sess = _make_session(payload=[])
        result = verify_post_write(official, frozenset(), url="https://example.supabase.co", key="sk_test", session=sess)
        assert not result.ok
        assert any("no dsex row found" in p for p in result.problems)


class TestExpectedDeletesCarryFullPrecision:
    """Regression: the 2026-08-25 dry-run aborted because EXPECTED_DELETES
    held second-precision stamps while production PostgREST returns
    microseconds — the four CORRECT deletes looked 'unexpected' and the
    tripwire refused the whole (correct) plan. The constants must carry the
    rows' exact microsecond ingested_at, not a human-truncated rendering."""

    def test_every_expected_delete_stamp_has_sub_second_precision(self):
        from scripts.backfill_dsex_session_restamp_2026aug import (
            EXPECTED_DELETES,
            _normalize_iso,
        )
        for as_of, ingested_at in sorted(EXPECTED_DELETES):
            parsed = _normalize_iso(ingested_at)
            assert parsed.microsecond != 0, (
                f"EXPECTED_DELETES stamp for {as_of} ({ingested_at}) has no "
                "sub-second precision — production ingested_at values carry "
                "microseconds, and a truncated stamp can never match one"
            )
