"""The 1 July fiscal-year reset must not be mistaken for a data collapse.

Bangladesh's fiscal year runs July-June, so a fiscal-year-to-date total
restarts near zero every 1 July. In August 2026 the pipeline read that restart
as a fault twice over, and the two readings are separate bugs:

* ``_is_cumulative_regression`` was fed SCRAPE dates. The FY2025-26 closing
  figure and the FY2026-27 opening figure were both downloaded in August, so
  "are these in the same fiscal year?" answered yes and July's legitimate
  restart looked like a parse error.
* the Opus reviewer, which has no concept of a July fiscal year, called the
  same restart a ~90% overnight collapse. Quarantine then substituted the prior
  FY's closing total -- and because quarantine rewrites the ``data`` block that
  becomes tomorrow's history, the next run compared the correct value against
  its own substitution and quarantined it again. ``categorywise_export`` and
  ``remittance_by_country`` were held at last year's totals for eight days,
  twice a day, with no path back: a quarantine is meant to be a one-day patch
  and nothing in the design ended this one.

Both fixes are date-driven, and the second never substitutes a value -- it can
only decline to overwrite fresh data with stale data. See AGENTS.md landmine 56.
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


class TestGuardStandsDownWhenItCannotTell:
    """Undated series inside the reset window: refuse to guess."""

    def _write(self, root, ind_id, rows):
        d = root / ind_id
        d.mkdir()
        for ds, val in rows:
            (d / f"{ds}.json").write_text(
                f'{{"value": {val}, "scraped_at": "{ds}T05:00:00+00:00", '
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

    def test_undated_drop_inside_the_reset_window_is_left_alone(self, tmp_path, monkeypatch):
        """August scrape, no source_as_of on either side. The guard cannot
        distinguish a July restart from a parse error, and clobbering a correct
        restart republishes last year's total as this year's forever."""
        value = self._run(
            tmp_path, monkeypatch,
            [("2026-08-24", EXPORT_FY26_CLOSE), ("2026-08-25", EXPORT_FY27_JULY)],
            date(2026, 8, 25),
        )
        assert value == EXPORT_FY27_JULY

    def test_undated_drop_outside_the_reset_window_still_falls_back(self, tmp_path, monkeypatch):
        """March: no fiscal year starts here, so a falling cumulative total is
        unambiguous and the original guard applies untouched."""
        value = self._run(
            tmp_path, monkeypatch,
            [("2027-03-30", EXPORT_FY26_CLOSE), ("2027-03-31", EXPORT_FY27_JULY)],
            date(2027, 3, 31),
        )
        assert value == EXPORT_FY26_CLOSE

    def test_the_grace_window_is_the_first_four_months_of_the_fy(self):
        from aggregate_latest import FY_RESET_GRACE_MONTHS

        assert FY_RESET_GRACE_MONTHS == frozenset({7, 8, 9, 10})


class TestDropExpectedFyResets:
    """The reviewer-override layer: never substitutes, only declines to."""

    HISTORY = [
        {"data": {"categorywise_export": EXPORT_FY26_CLOSE,
                  "remittance_by_country": REMIT_FY26_CLOSE,
                  "interbank_repo_data": 3390.82}},
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

        return _drop_expected_fy_resets(
            flagged,
            over.get("data", self.TODAY),
            over.get("history", self.HISTORY),
            over.get("as_of", self.AS_OF),
            over.get("cumulative", self.CUMULATIVE),
        )

    def test_the_august_2026_incident_is_fully_excused(self):
        still, excused = self._run(["categorywise_export", "remittance_by_country"])
        assert still == []
        assert sorted(excused) == ["categorywise_export", "remittance_by_country"]

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
            history=[{"data": {"trading_day": True}}],
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
                {"data": {"categorywise_export": EXPORT_FY26_CLOSE}},
                {"data": {"categorywise_export": 1.0}},   # newest: already reset
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

    def test_the_july_reset_is_explained_as_correct_data(self, monkeypatch):
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "1 July to 30 June" in prompt
        assert "CORRECT DATA" in prompt
        # The trap that produced eight days of quarantine: the reset lands as a
        # sudden drop after a run of identical values, which reads as a fault.
        assert "collapsed after N days of stable values" in prompt

    def test_no_declared_ids_still_renders(self, monkeypatch):
        """A registry with the flag removed must not blow up prompt formatting."""
        prompt = self._prompt(monkeypatch)
        assert "(none declared for this run)" in prompt

    def test_the_prompt_template_has_no_unfilled_placeholders(self, monkeypatch):
        prompt = self._prompt(monkeypatch, cumulative_ids={"categorywise_export"})
        assert "{cumulative_block}" not in prompt
