"""The 1 July fiscal-year reset must not be mistaken for a data collapse.

Bangladesh's fiscal year runs July-June, so a fiscal-year-to-date total
restarts near zero every 1 July. In August 2026 the pipeline read that restart
as a fault and held ``categorywise_export`` and ``remittance_by_country`` at
last year's closing totals for eight days, twice a day.

The cause was the Opus reviewer, which has no concept of a July fiscal year,
plus the quarantine that acts on its verdict: quarantine substituted the prior
FY's closing total, and because it rewrites the ``data`` block that becomes
tomorrow's history, the next run compared the correct value against **its own
substitution** and quarantined it again. A quarantine is meant to be a one-day
patch; nothing in the design ended this one.

``_is_cumulative_regression`` reading SCRAPE dates instead of publication dates
is a real bug of the same family and is fixed here too -- but it did NOT cause
this incident. That guard only runs on ids the registry marks ``cumulative``,
and neither id carried the flag until the flag and the date fix shipped
together. It was inert for both ids for the whole eight days. (AGENTS.md
landmine 56 originally claimed otherwise; see its CORRECTIONS block.)

The override that discards a reviewer verdict carries its own evidence -- an
observed fiscal-year boundary on both sides and a plausible reset magnitude --
because "the calendar says a reset is due" is equally true of a parser landing
on the wrong row. See landmine 57.
"""
from __future__ import annotations

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def skip_supabase(monkeypatch):
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
    yield


# The real August 2026 numbers, kept as the fixture for every test below so the
# regression is expressed in the values that actually broke.
EXPORT_FY26_CLOSE = 48.38       # FY2025-26 cumulative, published as of 2026-06-30
EXPORT_FY27_JULY = 4.72897      # FY2026-27 July alone, published as of 2026-07-31
REMIT_FY26_CLOSE = 5.28274
REMIT_FY27_JULY = 0.58729


class TestCumulativeGuardDates:
    """Which clock the cumulative guard reads is the whole bug."""

    def test_publication_dates_win_when_both_sides_have_them(self):
        from aggregate_latest import _cumulative_guard_dates

        today, prior, dated = _cumulative_guard_dates(
            {"source_as_of": "2026-07-31"},
            {"source_as_of": "2026-06-30"},
            date(2026, 8, 25),
            date(2026, 8, 24),
        )
        assert (today, prior, dated) == (date(2026, 7, 31), date(2026, 6, 30), True)

    def test_falls_back_to_scrape_dates_when_today_is_undated(self):
        from aggregate_latest import _cumulative_guard_dates

        today, prior, dated = _cumulative_guard_dates(
            {}, {"source_as_of": "2026-06-30"}, date(2026, 8, 25), date(2026, 8, 24)
        )
        assert (today, prior, dated) == (date(2026, 8, 25), date(2026, 8, 24), False)

    def test_falls_back_when_the_prior_side_is_undated(self):
        from aggregate_latest import _cumulative_guard_dates

        _, _, dated = _cumulative_guard_dates(
            {"source_as_of": "2026-07-31"}, {}, date(2026, 8, 25), date(2026, 8, 24)
        )
        assert dated is False

    def test_unparseable_dates_are_treated_as_absent(self):
        from aggregate_latest import _cumulative_guard_dates

        today, prior, dated = _cumulative_guard_dates(
            {"source_as_of": "not a date"},
            {"source_as_of": "2026-06-30"},
            date(2026, 8, 25),
            date(2026, 8, 24),
        )
        assert (today, prior, dated) == (date(2026, 8, 25), date(2026, 8, 24), False)

    def test_a_missing_prior_scrape_date_survives_the_fallback(self):
        """`prior_scraped` is None when the prior snapshot has no parseable
        scraped_at. The helper must pass that through rather than invent a
        date; the caller checks for None and skips the guard."""
        from aggregate_latest import _cumulative_guard_dates

        today, prior, dated = _cumulative_guard_dates({}, {}, date(2026, 8, 25), None)
        assert (today, prior, dated) == (date(2026, 8, 25), None, False)


class TestTheAugust2026Regression:
    """The exact shape that broke, asserted end to end through both helpers."""

    def test_scrape_dates_would_have_called_the_reset_a_regression(self):
        """Documents the bug rather than the fix -- if this ever stops holding,
        the guard's inputs have changed and the test below needs re-reading."""
        from aggregate_latest import _is_cumulative_regression

        assert _is_cumulative_regression(
            EXPORT_FY27_JULY, EXPORT_FY26_CLOSE,
            date(2026, 8, 25),   # both scraped in August ...
            date(2026, 8, 24),   # ... so both land in FY2026-27
        ) is True

    def test_publication_dates_recognise_it_as_the_annual_reset(self):
        from aggregate_latest import _cumulative_guard_dates, _is_cumulative_regression

        today, prior, dated = _cumulative_guard_dates(
            {"value": EXPORT_FY27_JULY, "source_as_of": "2026-07-31"},
            {"value": EXPORT_FY26_CLOSE, "source_as_of": "2026-06-30"},
            date(2026, 8, 25),
            date(2026, 8, 24),
        )
        assert dated is True
        assert _is_cumulative_regression(
            EXPORT_FY27_JULY, EXPORT_FY26_CLOSE, today, prior
        ) is False

    def test_a_genuine_within_fy_collapse_is_still_caught(self):
        """The fix must not blunt the guard: two figures reporting the SAME
        fiscal year that fall 90% are still a parse error."""
        from aggregate_latest import _is_cumulative_regression

        assert _is_cumulative_regression(
            4.72897, 48.38, date(2027, 3, 31), date(2027, 2, 28)
        ) is True


class TestTheGuardRunsOnEveryDateAxisItHas:
    """There is deliberately NO "stand down when undated" branch (landmine 57).

    PR #134 added one, reasoning that clobbering a correct reset "never
    self-heals". It does: ``_prior_good_snapshot`` reads ``data/<id>/<date>.json``
    written by the FETCH stage, and the aggregate never writes those files, so a
    stale-fallback changes ONE day's bundle and the next run reads the fresh
    snapshot again. Standing down instead disabled the only within-FY guard for
    four months a year on the 31-of-59 indicators that carry no ``source_as_of``.
    """

    def _write(self, root, ind_id, rows):
        d = root / ind_id
        d.mkdir()
        for row in rows:
            ds, val = row[0], row[1]
            as_of = row[2] if len(row) > 2 else None
            as_of_json = f'"{as_of}"' if as_of else "null"
            (d / f"{ds}.json").write_text(
                f'{{"value": {val}, "scraped_at": "{ds}T05:00:00+00:00", '
                f'"source_as_of": {as_of_json}, '
                f'"_provenance": "llm_extracted", "change_pct": null}}'
            )

    def _run(self, tmp_path, monkeypatch, rows, today):
        from datetime import datetime, timezone

        import aggregate_latest

        self._write(tmp_path, "fy_thing", rows)
        monkeypatch.setattr(aggregate_latest, "DATA_DIR", tmp_path)
        monkeypatch.setattr(
            aggregate_latest, "_load_v3_registry",
            lambda: [{"id": "fy_thing", "domain": "external_sector",
                      "cadence": "monthly", "cumulative": True}],
        )
        data_additions, _, _, _ = aggregate_latest._build_v3_blocks(
            datetime(today.year, today.month, today.day, 6, 0, tzinfo=timezone.utc)
        )
        return data_additions["fy_thing"]

    def test_a_dated_july_reset_survives_the_guard_end_to_end(self, tmp_path, monkeypatch):
        """THE headline fix, pinned at the call site.

        Both sides scraped in August (same FY on the scrape axis) but reporting
        June and July (different FY on the publication axis). Reading the wrong
        clock republishes FY26's close as FY27's July -- which is the row that
        is currently wrong in `metric_history`. Revert `_cumulative_guard_dates`
        at its call site and this test, and only this test, goes red.
        """
        value = self._run(
            tmp_path, monkeypatch,
            [("2026-08-24", EXPORT_FY26_CLOSE, "2026-06-30"),
             ("2026-08-25", EXPORT_FY27_JULY, "2026-07-31")],
            date(2026, 8, 25),
        )
        assert value == EXPORT_FY27_JULY

    def test_a_dated_within_fy_collapse_is_still_clobbered(self, tmp_path, monkeypatch):
        """The mirror image: same fix, opposite verdict. Two figures both
        reporting FY2026-27 that fall 90% are a parse error, and the fresh
        reading must NOT be published."""
        value = self._run(
            tmp_path, monkeypatch,
            [("2027-03-30", EXPORT_FY26_CLOSE, "2027-01-31"),
             ("2027-03-31", EXPORT_FY27_JULY, "2027-02-28")],
            date(2027, 3, 31),
        )
        assert value == EXPORT_FY26_CLOSE

    def test_an_undated_drop_inside_the_window_is_still_guarded(self, tmp_path, monkeypatch):
        """No stand-down. Scrape dates are all we have, they read as same-FY,
        and the guard falls back -- for one day, self-healing on the next run."""
        value = self._run(
            tmp_path, monkeypatch,
            [("2026-08-24", EXPORT_FY26_CLOSE), ("2026-08-25", EXPORT_FY27_JULY)],
            date(2026, 8, 25),
        )
        assert value == EXPORT_FY26_CLOSE

    def test_undated_drop_outside_the_reset_window_still_falls_back(self, tmp_path, monkeypatch):
        value = self._run(
            tmp_path, monkeypatch,
            [("2027-03-30", EXPORT_FY26_CLOSE), ("2027-03-31", EXPORT_FY27_JULY)],
            date(2027, 3, 31),
        )
        assert value == EXPORT_FY26_CLOSE

    def test_the_grace_window_is_the_first_four_months_of_the_fy(self):
        from aggregate_latest import FY_RESET_GRACE_MONTHS

        assert FY_RESET_GRACE_MONTHS == frozenset({7, 8, 9, 10})

    def test_no_stand_down_branch_survives_in_the_source(self):
        """Structural: the removed branch was a `pass` guarded on either date
        being None inside the FY window. If someone re-adds a stand-down, the
        undated tests above would go green again silently -- so assert the
        shape, not just the behaviour."""
        import ast
        import inspect

        import aggregate_latest

        src = inspect.getsource(aggregate_latest._build_v3_blocks)
        module = ast.parse(inspect.getsource(aggregate_latest))
        blocks_fn = next(
            n for n in module.body
            if isinstance(n, ast.FunctionDef) and n.name == "_build_v3_blocks"
        )
        # `_is_cumulative_regression` must be reachable whenever both dates
        # exist -- i.e. it is called in this function and not inside any branch
        # testing FY_RESET_GRACE_MONTHS membership.
        calls = [
            n for n in ast.walk(blocks_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_is_cumulative_regression"
        ]
        assert calls, "_build_v3_blocks no longer runs the cumulative guard"
        assert "FY_RESET_GRACE_MONTHS" not in src, (
            "the cumulative guard is gated on the calendar again — landmine 57"
        )


class TestDropExpectedFyResets:
    """The reviewer-override layer: it must carry its own evidence."""

    # Value and date come from the SAME archived snapshot -- read off different
    # days, they can disagree about which fiscal year the comparison spans.
    HISTORY = [
        {"data": {"categorywise_export": EXPORT_FY26_CLOSE,
                  "remittance_by_country": REMIT_FY26_CLOSE,
                  "interbank_repo_data": 3390.82},
         "source_as_of": {"categorywise_export": "2026-06-30",
                          "remittance_by_country": "2026-06-30"}},
    ]
    TODAY = {"categorywise_export": EXPORT_FY27_JULY,
             "remittance_by_country": REMIT_FY27_JULY,
             "interbank_repo_data": 9400.0}
    AS_OF = {"categorywise_export": date(2026, 7, 31),
             "remittance_by_country": date(2026, 7, 31),
             "interbank_repo_data": date(2026, 8, 25)}
    CUMULATIVE = {"categorywise_export", "remittance_by_country"}

    def _run(self, flagged, **over):
        from aggregate_latest import _drop_expected_fy_resets

        still, excused, evidence = _drop_expected_fy_resets(
            flagged,
            over.get("data", self.TODAY),
            over.get("history", self.HISTORY),
            over.get("as_of", self.AS_OF),
            over.get("cumulative", self.CUMULATIVE),
            over.get("parent_of"),
        )
        self.last_evidence = evidence
        return still, excused

    def test_the_august_2026_incident_is_fully_excused(self):
        still, excused = self._run(["categorywise_export", "remittance_by_country"])
        assert still == []
        assert sorted(excused) == ["categorywise_export", "remittance_by_country"]

    def test_every_excuse_states_both_values_and_both_dates(self):
        """An override that silently discards a safety verdict is
        indistinguishable from the safety net not running."""
        self._run(["categorywise_export"])
        line = self.last_evidence[0]
        assert "categorywise_export" in line
        assert str(EXPORT_FY27_JULY) in line
        assert str(EXPORT_FY26_CLOSE) in line
        assert "2026-07-31" in line and "2026-06-30" in line
        assert "FY2026" in line and "FY2025" in line
        assert "9.8%" in line          # the ratio, so the band is auditable

    def test_a_non_cumulative_flag_is_never_excused(self):
        """interbank_repo_data's +177% spike shared the same verdicts and must
        survive the filter untouched."""
        still, excused = self._run(
            ["categorywise_export", "interbank_repo_data"]
        )
        assert still == ["interbank_repo_data"]
        assert excused == ["categorywise_export"]

    def test_a_rise_is_never_a_reset(self):
        """A cumulative total that jumps is not a July restart, whatever the
        month -- only a drop can be."""
        still, excused = self._run(
            ["categorywise_export"],
            data={**self.TODAY, "categorywise_export": 500.0},
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_outside_the_grace_window_nothing_is_excused(self):
        still, excused = self._run(
            ["categorywise_export"],
            as_of={**self.AS_OF, "categorywise_export": date(2027, 3, 31)},
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_an_undated_field_is_never_excused(self):
        """No source_as_of means we cannot show the figure reports an early
        FY month, so the reviewer's verdict stands."""
        still, excused = self._run(["categorywise_export"], as_of={})
        assert still == ["categorywise_export"]
        assert excused == []

    def test_an_undated_PRIOR_is_never_excused(self):
        """The boundary must be observed on BOTH sides. Without a dated prior
        figure there is nothing to show a fiscal year actually turned over --
        and 31 of 59 indicators genuinely carry no source_as_of."""
        still, excused = self._run(
            ["categorywise_export"],
            history=[{"data": {"categorywise_export": EXPORT_FY26_CLOSE}}],
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_a_prior_in_the_SAME_fiscal_year_is_never_excused(self):
        """August 2026 against a prior reporting July 2026: both FY2026-27, so
        a fall is a regression however early in the FY it is published."""
        still, excused = self._run(
            ["categorywise_export"],
            as_of={**self.AS_OF, "categorywise_export": date(2026, 8, 31)},
            history=[{"data": {"categorywise_export": EXPORT_FY26_CLOSE},
                      "source_as_of": {"categorywise_export": "2026-07-31"}}],
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_the_date_is_taken_from_the_SAME_snapshot_as_the_value(self):
        """The trap this shape avoids. During the August lock-in the archived
        `.data` held the quarantined 48.38 while the per-indicator snapshot on
        disk held the true 4.72897 dated 2026-07-31 -- reading the value from
        one and the date from the other compares FY2026 against FY2026 and
        refuses to excuse the very case this exists for.

        Here the newest snapshot has no value, so both the value and the date
        must come from the older one that does.
        """
        still, excused = self._run(
            ["categorywise_export"],
            history=[
                {"data": {"categorywise_export": EXPORT_FY26_CLOSE},
                 "source_as_of": {"categorywise_export": "2026-06-30"}},
                {"data": {}, "source_as_of": {"categorywise_export": "2026-07-31"}},
            ],
        )
        assert still == []
        assert excused == ["categorywise_export"]

    def test_a_drop_far_too_deep_to_be_a_reset_is_not_excused(self):
        """THE hole this band closes: a parser landing on the wrong row in
        July returns 0.09 against last FY's 48.38. Cumulative, early-FY month,
        across the boundary, and it fell -- it satisfies every OTHER condition,
        and it is 0.19% of the prior year, which no one month of twelve is."""
        still, excused = self._run(
            ["categorywise_export"],
            data={**self.TODAY, "categorywise_export": 0.09},
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_a_drop_too_shallow_to_be_a_reset_is_not_excused(self):
        """A 20% dip is a bad month or a bad parse, not twelve months becoming
        one -- the band has a ceiling as well as a floor."""
        still, excused = self._run(
            ["categorywise_export"],
            data={**self.TODAY, "categorywise_export": EXPORT_FY26_CLOSE * 0.8},
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_the_band_brackets_the_real_resets_with_room_to_spare(self):
        """Both real ratios sit near 10%, well inside (2%, 45%) -- the band is
        sized to reject the impossible, not to validate the figure."""
        from aggregate_latest import RESET_PLAUSIBILITY_BAND

        lo, hi = RESET_PLAUSIBILITY_BAND
        assert lo < EXPORT_FY27_JULY / EXPORT_FY26_CLOSE < hi
        assert lo < REMIT_FY27_JULY / REMIT_FY26_CLOSE < hi
        assert not lo <= 0.09 / EXPORT_FY26_CLOSE <= hi

    def test_a_field_with_no_numeric_history_is_not_excused(self):
        still, excused = self._run(["categorywise_export"], history=[{"data": {}}])
        assert still == ["categorywise_export"]
        assert excused == []

    def test_a_missing_field_is_not_excused(self):
        """A missing indicator is absent from `data` entirely (this is what the
        dead treasury source produced). It must reach _quarantine_flagged so the
        untrustworthy-verdict hard reject still fires."""
        still, excused = self._run(
            ["treasury_bill_outstanding"],
            cumulative=self.CUMULATIVE | {"treasury_bill_outstanding"},
        )
        assert still == ["treasury_bill_outstanding"]
        assert excused == []

    def test_a_non_numeric_value_is_not_excused(self):
        still, excused = self._run(
            ["categorywise_export"],
            data={**self.TODAY, "categorywise_export": "4.72897"},
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_booleans_are_not_treated_as_numbers(self):
        """`trading_day` was flagged in one of the August verdicts; bool is an
        int subclass, so it needs an explicit exclusion."""
        still, excused = self._run(
            ["trading_day"],
            data={"trading_day": False},
            history=[{"data": {"trading_day": True},
                      "source_as_of": {"trading_day": "2026-06-30"}}],
            as_of={"trading_day": date(2026, 7, 31)},
            cumulative={"trading_day"},
        )
        assert still == ["trading_day"]
        assert excused == []

    def test_the_newest_history_entry_wins(self):
        """History is newest-last. Comparing against the OLDEST entry would
        excuse a drop that already happened days ago."""
        still, excused = self._run(
            ["categorywise_export"],
            history=[
                {"data": {"categorywise_export": EXPORT_FY26_CLOSE},
                 "source_as_of": {"categorywise_export": "2026-06-30"}},
                {"data": {"categorywise_export": 1.0},    # newest: already reset
                 "source_as_of": {"categorywise_export": "2026-07-31"}},
            ],
        )
        assert still == ["categorywise_export"]
        assert excused == []

    def test_ordering_of_the_survivors_is_preserved(self):
        still, _ = self._run(
            ["interbank_repo_data", "categorywise_export", "trading_day"],
            data={**self.TODAY, "trading_day": False},
        )
        assert still == ["interbank_repo_data", "trading_day"]

    def test_an_alias_child_is_excused_through_its_parents_dates(self):
        """`nbr_fytd_collected_cr` IS `tax_revenue`, and it is the key The
        Brief's fiscal builder reads. It carries no dates of its own, so both
        lookups fall back to the registry id behind it."""
        still, excused = self._run(
            ["nbr_fytd_collected_cr"],
            data={"nbr_fytd_collected_cr": 4.72897},
            history=[{"data": {"nbr_fytd_collected_cr": 48.38},
                      "source_as_of": {"tax_revenue": "2026-06-30"}}],
            as_of={"tax_revenue": date(2026, 7, 31)},
            cumulative={"tax_revenue", "nbr_fytd_collected_cr"},
            parent_of={"nbr_fytd_collected_cr": "tax_revenue"},
        )
        assert still == []
        assert excused == ["nbr_fytd_collected_cr"]


class TestTheCumulativeFlagIsInheritedByAliases:
    """The lock-in was live on the keys The Brief actually reads (landmine 57)."""

    def test_plain_aliases_and_unit_conversions_are_both_covered(self):
        from aggregate_latest import _cumulative_alias_parents

        parents = _cumulative_alias_parents(
            {"tax_revenue", "nbr_vat_collected_cr", "nbr_it_collected_cr",
             "nbr_customs_collected_cr"}
        )
        assert parents["nbr_fytd_collected_cr"] == "tax_revenue"       # alias
        assert parents["fiscal_nbr_collected_trn"] == "tax_revenue"    # ×0.00001
        assert parents["nbr_vat_bn"] == "nbr_vat_collected_cr"         # ×0.01
        assert parents["nbr_it_bn"] == "nbr_it_collected_cr"
        assert parents["nbr_customs_bn"] == "nbr_customs_collected_cr"

    def test_children_of_non_cumulative_ids_are_not_swept_in(self):
        """Derived, not hand-listed — so it must not over-reach either.
        `remit_fy_mn` converts `fy_remittance`, which is a COMPLETED-fiscal-year
        total (its fetch task reads "latest complete fiscal year column"), not a
        year-to-date running total. It never resets."""
        from aggregate_latest import _cumulative_alias_parents

        assert _cumulative_alias_parents({"tax_revenue"}).get("remit_fy_mn") is None
        assert _cumulative_alias_parents(set()) == {}

    def test_the_real_registry_flag_reaches_the_brief_facing_keys(self):
        from aggregate_latest import (
            _cumulative_alias_parents,
            _cumulative_indicator_ids,
        )

        children = set(_cumulative_alias_parents(_cumulative_indicator_ids()))
        assert {"nbr_fytd_collected_cr", "fiscal_nbr_collected_trn"} <= children


class TestRegistryDeclaresTheFyCumulativeSeries:
    """The prompt and the deterministic guards both read this one flag."""

    def _registry(self):
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return json.loads((root / "config" / "sources-v3.json").read_text())["indicators"]

    @pytest.mark.parametrize(
        "indicator_id", ["categorywise_export", "remittance_by_country"]
    )
    def test_the_two_external_sector_series_are_marked_cumulative(self, indicator_id):
        by_id = {i["id"]: i for i in self._registry()}
        assert by_id[indicator_id].get("cumulative") is True

    def test_cumulative_indicator_ids_reads_the_registry_flag(self, monkeypatch):
        import aggregate_latest

        monkeypatch.setattr(
            aggregate_latest, "_load_v3_registry",
            lambda: [{"id": "a", "cumulative": True}, {"id": "b"},
                     {"id": "c", "cumulative": False}],
        )
        assert aggregate_latest._cumulative_indicator_ids() == {"a"}


class TestReviewPromptCalibration:
    """The reviewer is told which series reset, by name, from the same flag."""

    def _prompt(self, monkeypatch, **kwargs) -> str:
        from utils.opus_review import review_data

        captured: dict = {}

        class _Done:
            returncode = 0
            stdout = '{"status": "ok", "reason": "fine"}'
            stderr = ""

        def _fake_run(argv, **kw):
            captured["prompt"] = kw["input"]
            return _Done()

        monkeypatch.setattr("utils.opus_review.subprocess.run", _fake_run)
        review_data(
            {"categorywise_export": EXPORT_FY27_JULY},
            [{"data": {"categorywise_export": EXPORT_FY26_CLOSE}}],
            binary="claude",
            **kwargs,
        )
        return captured["prompt"]

    def test_the_cumulative_ids_are_listed_by_name(self, monkeypatch):
        """Named, not described -- the reviewer must never have to infer
        "is this one cumulative?" from an indicator id."""
        prompt = self._prompt(
            monkeypatch,
            cumulative_ids={"remittance_by_country", "categorywise_export"},
        )
        assert "FISCAL-YEAR-TO-DATE SERIES" in prompt
        assert "- categorywise_export" in prompt
        assert "- remittance_by_country" in prompt

    def test_the_july_reset_is_explained_as_the_annual_reset(self, monkeypatch):
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "1 July to 30 June" in prompt
        assert "the annual reset, not an anomaly" in prompt
        # The trap that produced eight days of quarantine: the reset lands as a
        # sudden drop after a run of identical values, which reads as a fault.
        assert "collapsed after N days of stable values" in prompt

    def test_the_prompt_never_tells_the_reviewer_to_omit_an_id(self, monkeypatch):
        """PR #134 said a reset "must NOT be reported as an anomaly or as
        missing" -- but an id absent from `data` is exactly what makes
        `_quarantine_flagged` hard-reject, so that asked the reviewer to hide
        the trigger for the safety mechanism. Suppress the REACTION in code;
        never ask the model to suppress the REPORT."""
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "or as missing" not in prompt
        assert "Report a reset in the normal way" in prompt

    def test_each_cumulative_id_is_listed_with_its_publication_date(self, monkeypatch):
        """The month qualifier is unevaluable without this: the reviewer sees
        `.data`, which is bare id->number. `source_as_of` lives in `.domains`
        and load_history() strips it."""
        prompt = self._prompt(
            monkeypatch,
            cumulative_ids={"categorywise_export"},
            source_as_of_map={"categorywise_export": date(2026, 7, 31)},
        )
        assert "- categorywise_export — today's figure reports 2026-07-31" in prompt

    def test_an_undated_id_still_renders_without_a_date_clause(self, monkeypatch):
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "- categorywise_export\n" in prompt

    def test_the_prompt_month_list_matches_the_code_window(self, monkeypatch):
        """FY_RESET_GRACE_MONTHS = {7,8,9,10}. A prompt that stops at September
        trains the reviewer to flag exactly the October figures the code is
        prepared to excuse."""
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "July, August, September or October" in prompt

    def test_no_declared_ids_still_renders(self, monkeypatch):
        """A registry with the flag removed must not blow up prompt formatting."""
        prompt = self._prompt(monkeypatch)
        assert "(none declared for this run)" in prompt

    def test_the_prompt_template_has_no_unfilled_placeholders(self, monkeypatch):
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "{cumulative_block}" not in prompt


class TestExcusingCannotUnlockQuarantine:
    """Finding 1 of the PR #134 review: the override could not write a number,
    but it could hand the writer a green light (landmine 57).

    `_quarantine_flagged` hard-rejects when more than MAX_QUARANTINE_FIELDS ids
    are flagged. Excusing shortens that list -- so excusing two of seven dropped
    the count to five, turned a refusal-to-publish into a publish, and let
    quarantine substitute stale values into the five that remained.
    """

    HISTORY = [{"data": {f"id{i}": 100.0 for i in range(9)}}]
    DATA = {f"id{i}": 1.0 for i in range(9)}

    def test_breadth_is_judged_on_what_the_reviewer_flagged(self):
        from aggregate_latest import _quarantine_flagged

        survivors = [f"id{i}" for i in range(5)]      # 5 <= MAX_QUARANTINE_FIELDS
        _, quarantined, hard_reject = _quarantine_flagged(
            self.DATA, survivors, self.HISTORY, breadth_count=7,
        )
        assert hard_reject is True
        assert quarantined == []

    def test_a_fully_excused_run_publishes_clean(self):
        """The real 1 July shape: every cumulative series resets the same night.
        Nothing is left to quarantine, so there is nothing to substitute -- the
        breadth gate must not turn a clean publish into a rejection."""
        from aggregate_latest import _quarantine_flagged

        cleaned, quarantined, hard_reject = _quarantine_flagged(
            self.DATA, [], self.HISTORY, breadth_count=0,
        )
        assert hard_reject is False
        assert quarantined == []
        assert cleaned == self.DATA

    def test_the_default_is_the_pre_existing_behaviour(self):
        from aggregate_latest import _quarantine_flagged

        _, _, hard_reject = _quarantine_flagged(
            self.DATA, [f"id{i}" for i in range(5)], self.HISTORY,
        )
        assert hard_reject is False
        _, _, hard_reject = _quarantine_flagged(
            self.DATA, [f"id{i}" for i in range(6)], self.HISTORY,
        )
        assert hard_reject is True

    def test_an_unmappable_id_still_hard_rejects_whatever_the_breadth(self):
        """The other gate is untouched: a flagged id absent from `data` means
        the verdict cannot be trusted, and that is true of a 1-field verdict."""
        from aggregate_latest import _quarantine_flagged

        _, _, hard_reject = _quarantine_flagged(
            self.DATA, ["treasury_bill_outstanding"], self.HISTORY, breadth_count=1,
        )
        assert hard_reject is True

    def test_main_passes_the_raw_count_not_the_survivors(self):
        """Structural, against the shipped source: the wiring IS the fix, and a
        behavioural test of it would have to drive a whole aggregate run."""
        import ast
        import inspect

        import aggregate_latest

        src = inspect.getsource(aggregate_latest.main)
        assert "raw_flagged_count = len(flagged)" in src
        assert "breadth_count=raw_flagged_count if flagged else 0" in src
        # and the raw count must be taken BEFORE the override shortens the list
        module = ast.parse(inspect.getsource(aggregate_latest))
        main_fn = next(
            n for n in module.body
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        lines = {}
        for node in ast.walk(main_fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "raw_flagged_count":
                        lines["raw"] = node.lineno
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_drop_expected_fy_resets"):
                lines["drop"] = node.lineno
        assert lines["raw"] < lines["drop"]

    def test_the_flagged_list_is_deterministically_ordered(self):
        """It is logged, notified and used to decide breadth; `list({...})` made
        two identical runs alert differently."""
        import inspect

        import aggregate_latest

        src = inspect.getsource(aggregate_latest.main)
        assert "flagged = sorted({*flagged, *missing})" in src


class TestHistoryCarriesItsPublicationDates:
    """`load_history` used to reduce each archive to `{updated_at, data}`.

    That dropped `.domains`, which is the only place `source_as_of` lives — so
    every consumer downstream, the Opus reviewer included, saw a wall of bare
    id->number with no way to tell which PERIOD any figure described. It is why
    the reviewer could not distinguish a 1 July restart from a collapse, and
    why the calibration note asking it about "a value reporting July" was
    unevaluable. See landmine 57.
    """

    def _archive(self, tmp_path, name, data, domains):
        import json

        (tmp_path / name).write_text(json.dumps({
            "updated_at": "2026-08-29T21:00:00+00:00",
            "data": data,
            "domains": domains,
        }))

    def test_source_as_of_is_flattened_alongside_the_values(self, tmp_path):
        from utils.opus_review import load_history

        self._archive(
            tmp_path, "latest_2026-08-29.json",
            {"categorywise_export": EXPORT_FY27_JULY, "usd_bdt_mid": 122.0},
            {"external_sector": {
                "categorywise_export": {"value": EXPORT_FY27_JULY,
                                        "source_as_of": "2026-07-31"},
                "usd_bdt_mid": {"value": 122.0, "source_as_of": None},
            }},
        )
        history = load_history(tmp_path, days=5)

        assert history[0]["data"]["categorywise_export"] == EXPORT_FY27_JULY
        assert history[0]["source_as_of"]["categorywise_export"] == "2026-07-31"
        # an undated indicator is simply absent, never a null placeholder
        assert "usd_bdt_mid" not in history[0]["source_as_of"]

    def test_an_archive_with_no_domains_block_still_loads(self, tmp_path):
        """Older archives predate the domains block entirely."""
        import json

        from utils.opus_review import load_history

        (tmp_path / "latest_2026-08-28.json").write_text(
            json.dumps({"updated_at": "x", "data": {"a": 1.0}})
        )
        history = load_history(tmp_path, days=5)

        assert history[0]["data"] == {"a": 1.0}
        assert history[0]["source_as_of"] == {}

    def test_a_non_dict_domain_block_is_skipped_not_fatal(self, tmp_path):
        from utils.opus_review import load_history

        self._archive(
            tmp_path, "latest_2026-08-27.json", {"a": 1.0},
            {"external_sector": ["not", "a", "dict"]},
        )
        assert load_history(tmp_path, days=5)[0]["source_as_of"] == {}

    def test_the_reviewer_sees_the_dates_in_its_prompt(self, monkeypatch, tmp_path):
        """The whole point: the history block it reasons over now says which
        period each historical figure covers."""
        from utils.opus_review import load_history, review_data

        self._archive(
            tmp_path, "latest_2026-08-29.json",
            {"categorywise_export": EXPORT_FY26_CLOSE},
            {"external_sector": {"categorywise_export": {
                "value": EXPORT_FY26_CLOSE, "source_as_of": "2026-06-30"}}},
        )
        captured: dict = {}

        class _Done:
            returncode = 0
            stdout = '{"status": "ok", "reason": "fine"}'
            stderr = ""

        def _fake_run(argv, **kw):
            captured["prompt"] = kw["input"]
            return _Done()

        monkeypatch.setattr("utils.opus_review.subprocess.run", _fake_run)
        review_data(
            {"categorywise_export": EXPORT_FY27_JULY},
            load_history(tmp_path, days=5),
            binary="claude",
        )
        assert "2026-06-30" in captured["prompt"]
