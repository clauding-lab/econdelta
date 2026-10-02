"""Primary source facts, transformations and bounded manifest construction."""

from pathlib import Path

import pytest

from scripts.history_repair_candidates import source_observations
from scripts.repair_observation_history import RepairConflict

ROOT = Path(__file__).resolve().parents[1]


def test_source_periods_values_and_aligned_ratios():
    facts = source_observations(ROOT)
    assert facts["deposits_of_the_system"]["2026-05-31"]["value"] == 2041692.7
    assert facts["deposits_of_the_system"]["2026-06-30"]["value"] == 2079911.1
    assert "2026-07-31" not in facts["deposits_of_the_system"]
    assert facts["monthly_import_lc_opening"]["2026-06-30"]["value"] == 6198.59
    assert facts["monthly_import_lc_opening"]["2026-07-31"]["value"] == 6901.7
    assert facts["monthly_import_lc_opening"]["2026-05-31"]["value"] == 6212.75
    assert facts["fiscal_bank_borrow_trn"]["2026-06-30"]["value"] == pytest.approx(1.655382)
    assert facts["crr_utilisation_pct"]["2026-06-30"]["value"] == 5.1012
    assert "2026-07-31" not in facts["crr_utilisation_pct"]
    assert facts["gdp_growth_fy_pct"]["2025-06-30"]["value"] == 3.49
    assert facts["gdp_growth_fy_pct"]["2026-06-30"]["release_status"] == "provisional"
    assert "slr_utilisation_pct" not in facts  # numerator period not proven here
    assert not any("cpi" in key or "inflation" in key for key in facts)


def test_missing_or_changed_primary_source_refuses_plan(tmp_path):
    with pytest.raises(RepairConflict):
        source_observations(tmp_path)


@pytest.mark.parametrize(
    "metric_id",
    [
        "broad_money",
        "reserve_money",
        "banking_broad_money",
        "banking_reserve_money",
        "monthly_import_lc_opening",
        "monthly_import_lc_settlement",
    ],
)
def test_unmarked_wsei_july_observations_have_unknown_status(metric_id):
    facts = source_observations(ROOT)
    assert facts[metric_id]["2026-07-31"]["release_status"] == "unknown"
    assert facts["broad_money"]["2026-06-30"]["release_status"] == "provisional"
    assert facts["gdp_growth_fy_pct"]["2025-06-30"]["release_status"] == "revised"
    assert facts["gdp_growth_fy_pct"]["2026-06-30"]["release_status"] == "provisional"


# --- Owner decision (d), 26 Sep 2026 BDT: NBR fiscal-year total daily restamps ---------
# The rows below mirror the real 25 Sep backup export: EconDelta re-stamped the standing
# fiscal-year cumulative NBR collection (tax_revenue x 0.00001) with every capture date,
# 140 rows 2026-05-02..2026-09-24 (six capture days missing), five distinct values,
# provenance null. Owner decision (l), 28 Sep 2026 BDT, extends (d) to the parent
# tax_revenue (BDT crore) and its plain alias nbr_fytd_collected_cr: same 140 dates, each
# with its own reviewed per-day values (the alias's first run differs from the parent's).
# The retired news corroborators (nbr_fytd_collected_dailystar/_tbs, deprecated, alias_of
# tax_revenue) are NOT part of either decision.

NBR_ID = "fiscal_nbr_collected_trn"
NBR_MISSING_DAYS = {
    "2026-06-14",
    "2026-06-15",
    "2026-08-30",
    "2026-08-31",
    "2026-09-01",
    "2026-09-09",
}
NBR_RUNS = (  # (value, first as_of, last as_of) exactly as in the backup export
    (1.19, "2026-05-02", "2026-05-02"),
    (2.88, "2026-05-03", "2026-06-01"),
    (3.27, "2026-06-02", "2026-06-28"),
    (3.61, "2026-06-29", "2026-09-05"),
    (4.15, "2026-09-06", "2026-09-24"),
)
# (value, first as_of, last as_of) exactly as in the hashed 25 Sep backup export (BDT crore;
# every value a JSON float there), read offline for owner decision (l).
PARENT_RUNS = {
    "tax_revenue": (
        (119478.0, "2026-05-02", "2026-05-02"),
        (287862.59, "2026-05-03", "2026-06-01"),
        (326928.16, "2026-06-02", "2026-06-28"),
        (360642.0, "2026-06-29", "2026-09-05"),
        (415473.0, "2026-09-06", "2026-09-24"),
    ),
    "nbr_fytd_collected_cr": (
        (287431.0, "2026-05-02", "2026-05-24"),
        (287862.59, "2026-05-25", "2026-06-01"),
        (326928.16, "2026-06-02", "2026-06-28"),
        (360642.0, "2026-06-29", "2026-09-05"),
        (415473.0, "2026-09-06", "2026-09-24"),
    ),
}
# Deprecated news corroborators (alias_of tax_revenue in metric_definitions): 24 rows each,
# 2026-05-02..2026-05-25, one integer value each, exactly as in the backup export.
RETIRED_CORROBORATORS = {"nbr_fytd_collected_dailystar": 287000, "nbr_fytd_collected_tbs": 287862}


def _corroborator_rows() -> list[dict]:
    from datetime import date, timedelta

    first = date(2026, 5, 2)
    return [
        {
            "metric_id": metric_id,
            "as_of": (first + timedelta(days=n)).isoformat(),
            "value": value,
            "source": "EconDelta",
            "ingested_at": f"{(first + timedelta(days=n)).isoformat()}T13:52:59.108274+00:00",
            "provenance": None,
        }
        for metric_id, value in RETIRED_CORROBORATORS.items()
        for n in range(24)
    ]


def _restamp_rows(metric_id: str, runs: tuple = NBR_RUNS) -> list[dict]:
    from datetime import date, timedelta

    rows = []
    for value, first, last in runs:
        day = date.fromisoformat(first)
        while day <= date.fromisoformat(last):
            if day.isoformat() not in NBR_MISSING_DAYS:
                rows.append(
                    {
                        "metric_id": metric_id,
                        "as_of": day.isoformat(),
                        "value": value,
                        "source": "EconDelta",
                        "ingested_at": f"{day.isoformat()}T21:18:08.437914+00:00",
                        "provenance": None,
                    }
                )
            day += timedelta(days=1)
    return rows


def _write_backup(backup_dir: Path, metric_history: list[dict]) -> Path:
    """A twelve-table export shaped like the verified 25 Sep backup (only rows used here)."""
    import hashlib
    import json

    stamp = "2026-05-04T15:15:36.629102+00:00"
    yields = {
        "tbill_91d_yield_monthly": 10.15,
        "tbill_182d_yield_monthly": 10.4085,
        "tbill_364d_yield_monthly": 10.5,
        "yield_2y_monthly": 10.728,
        "yield_5y_monthly": 10.78,
        "yield_10y_monthly": 10.9099,
        "yield_15y_monthly": 11.0198,
        "yield_20y_monthly": 11.0875,
    }
    tables = {
        "metric_history": (["metric_id", "as_of"], metric_history),
        "metric_history_monthly": (
            ["metric_id", "as_of"],
            [
                {
                    "id": 10000 + i,
                    "metric_id": mid,
                    "as_of": "2026-05-01",
                    "value": value,
                    "source": "bb_auction",
                    "source_as_of": "2026-05-01",
                    "ingested_at": "2026-08-08T17:14:54.723178+00:00",
                    "notes": None,
                }
                for i, (mid, value) in enumerate(yields.items())
            ],
        ),
        "metric_definitions": (
            ["metric_id"],
            [
                {
                    "metric_id": "gdp",
                    "label": "Gdp",
                    "short_label": None,
                    "unit": None,
                    "domain": "macro",
                    "sort_order": 100,
                    "cadence": "quarterly",
                    "format": "comma-2dp",
                    "description": None,
                    "source": None,
                    "source_url": "https://www.bb.org.bd/en/index.php/publication/publictn/5/27",
                    "is_hero": False,
                    "inverted": False,
                    "created_at": stamp,
                    "updated_at": stamp,
                    "grace_days": 165,
                    "deprecated": False,
                    "alias_of": None,
                }
            ],
        ),
        "metric_definitions_monthly": (
            ["metric_id"],
            [
                {
                    "metric_id": "net_reserves_bpm6_usd_bn_monthly",
                    "display_name": "FX reserves (BPM6/net)",
                    "unit": "USD bn",
                    "description": "Foreign exchange reserves per IMF BPM6 methodology.",
                    "grace_days": 45,
                }
            ],
        ),
    }
    for table, key in (
        ("auction_results", ["auction_date", "tenor"]),
        ("media_review", ["id"]),
        ("briefs", ["id"]),
        ("sections", ["id"]),
        ("metrics", ["id"]),
        ("news", ["id"]),
        ("chart_series", ["id"]),
        ("chart_notes", ["id"]),
    ):
        tables[table] = (key, [])
    refs = {}
    for table, (key, rows) in tables.items():
        path = backup_dir / f"{table}.json"
        path.write_text(json.dumps(rows))
        refs[table] = {
            "key": key,
            "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (backup_dir / "manifest.json").write_text(
        json.dumps({"target_project": "ssbliukchgibjcjohibi", "tables": refs})
    )
    return backup_dir


def _candidate(backup_dir: Path) -> dict:
    from scripts.history_repair_candidates import build_candidate

    return build_candidate(
        backup_dir,
        ROOT,
        target="supabase:ssbliukchgibjcjohibi",
        commits={"econdelta": "0" * 40, "brief": "0" * 40},
        generated_at="2026-09-25T20:07:46.196927+00:00",
    )


def _history(nbr_rows: list[dict], parent_rows: dict[str, list[dict]] | None = None) -> list[dict]:
    """Backup history: the child, the parent and alias (reviewed rows unless overridden) and
    the two retired corroborators, all mirroring the real export."""
    parents = {mid: _restamp_rows(mid, runs) for mid, runs in PARENT_RUNS.items()}
    parents = parents | (parent_rows or {})
    return nbr_rows + [row for rows in parents.values() for row in rows] + _corroborator_rows()


def test_owner_decision_d_excludes_every_restamped_nbr_total_with_its_full_backup_image(tmp_path):
    nbr_rows = _restamp_rows(NBR_ID)
    assert len(nbr_rows) == 140 and len({r["value"] for r in nbr_rows}) == 5
    candidate = _candidate(_write_backup(tmp_path, _history(nbr_rows)))
    ops = [op for op in candidate["operations"] if op["key"]["metric_id"] == NBR_ID]
    assert [op["key"] for op in ops] == [
        {"metric_id": NBR_ID, "as_of": r["as_of"]} for r in nbr_rows
    ]
    assert [op["before"] for op in ops] == nbr_rows  # archive keeps the complete original row
    assert all(op["table"] == "metric_history" and op["after"] is None for op in ops)
    assert all(op["requires"] == [] for op in ops)  # parent tax_revenue is not repaired here
    assert all("restamp" in op["reason"] and "decision (d)" in op["reason"] for op in ops)
    for op in ops:
        (ref,) = op["evidence"]
        assert Path(ref["path"]) == (tmp_path / "metric_history.json").resolve()
        assert "no numerical or period corroboration" in ref["locator"]


@pytest.mark.parametrize("metric_id", list(PARENT_RUNS))
def test_owner_decision_l_excludes_every_restamped_parent_and_alias_row_with_its_full_backup_image(
    tmp_path, metric_id
):
    rows = _restamp_rows(metric_id, PARENT_RUNS[metric_id])
    assert [r["as_of"] for r in rows] == [r["as_of"] for r in _restamp_rows(NBR_ID)]
    candidate = _candidate(_write_backup(tmp_path, _history(_restamp_rows(NBR_ID))))
    ops = [op for op in candidate["operations"] if op["key"]["metric_id"] == metric_id]
    assert [op["key"] for op in ops] == [
        {"metric_id": metric_id, "as_of": r["as_of"]} for r in rows
    ]
    assert [op["before"] for op in ops] == rows  # archive keeps the complete original row
    assert all(op["table"] == "metric_history" and op["after"] is None for op in ops)
    assert all(op["requires"] == [] for op in ops)  # nothing here rebuilds an NBR id
    assert all("restamp" in op["reason"] and "decision (l)" in op["reason"] for op in ops)
    for op in ops:
        (ref,) = op["evidence"]
        assert Path(ref["path"]) == (tmp_path / "metric_history.json").resolve()
        assert "no numerical or period corroboration" in ref["locator"]


def test_nbr_restamp_exclusions_cover_exactly_child_parent_and_alias_never_the_corroborators(
    tmp_path,
):
    """Decisions (d) + (l): the three NBR restamp ids, 140 rows each, and nothing else of the
    NBR family; the retired news corroborators keep their history (not in either decision)."""
    candidate = _candidate(_write_backup(tmp_path, _history(_restamp_rows(NBR_ID))))
    touched = [op["key"]["metric_id"] for op in candidate["operations"]]
    nbr_family = {NBR_ID, *PARENT_RUNS, *RETIRED_CORROBORATORS}
    assert {mid: touched.count(mid) for mid in nbr_family if mid in touched} == {
        NBR_ID: 140,
        "tax_revenue": 140,
        "nbr_fytd_collected_cr": 140,
    }
    assert not set(touched) & set(RETIRED_CORROBORATORS)


def test_no_nbr_restamp_id_has_a_verified_source_fact_so_its_exclusions_need_no_prerequisite():
    """Existing rule: an exclusion depends only on verified operations of the same id. None of
    the three NBR ids is rebuilt from a source fact (directly, as alias or as conversion)."""
    assert not {NBR_ID, *PARENT_RUNS} & set(source_observations(ROOT))


def _recaptured_day(rows):
    return rows + [rows[-1] | {"as_of": "2026-09-25"}]


def _reviewed_day_moved_to_an_unreviewed_day(rows):
    """Same 140-row count, but reviewed 13 Jun re-dated to 14 Jun, a capture day not in the backup."""
    assert "2026-06-14" in NBR_MISSING_DAYS
    return [r | {"as_of": "2026-06-14"} if r["as_of"] == "2026-06-13" else r for r in rows]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_recaptured_day, id="later-recapture-adds-a-day"),
        pytest.param(lambda rows: rows[:-1], id="a-reviewed-day-is-missing"),
        pytest.param(
            _reviewed_day_moved_to_an_unreviewed_day,
            id="same-count-but-an-unreviewed-day-replaces-a-reviewed-one",
        ),
        pytest.param(
            lambda rows: rows[:-1] + [rows[-1] | {"source": "Bangladesh Bank"}],
            id="a-row-is-not-an-econdelta-restamp",
        ),
        pytest.param(
            lambda rows: rows[:-1] + [rows[-1] | {"value": 4.16}],
            id="a-value-outside-the-reviewed-five",
        ),
        pytest.param(
            lambda rows: (
                rows[:-1] + [rows[-1] | {"provenance": '{"source_period_end": "2026-08-31"}'}]
            ),
            id="a-row-carries-dated-provenance",
        ),
    ],
)
def test_nbr_restamp_exclusion_refuses_any_backup_other_than_the_reviewed_140_keys(
    tmp_path, mutate
):
    rows = mutate(_restamp_rows(NBR_ID))
    with pytest.raises(RepairConflict, match="fiscal_nbr_collected_trn"):
        _candidate(_write_backup(tmp_path, _history(rows)))


def _reviewed_value_changed(as_of: str, value: float | None):
    def mutate(rows):
        assert any(r["as_of"] == as_of for r in rows)  # a reviewed day of the backup
        return [r | {"value": value} if r["as_of"] == as_of else r for r in rows]

    return mutate


def _reviewed_values_swapped(day_a: str, day_b: str):
    """Swap the values of two reviewed days: same dates, same values with the same counts."""

    def mutate(rows):
        values = {r["as_of"]: r["value"] for r in rows}
        assert values[day_a] != values[day_b]  # a real swap, not a no-op
        swapped = {day_a: values[day_b], day_b: values[day_a]}
        out = [r | {"value": swapped[r["as_of"]]} if r["as_of"] in swapped else r for r in rows]
        assert sorted(r["value"] for r in out) == sorted(r["value"] for r in rows)
        return out

    return mutate


def _reviewed_run_restated(old: float, new: float):
    """Every day of one reviewed run carries a different value: same dates, same run shape."""

    def mutate(rows):
        assert any(r["value"] == old for r in rows)  # a reviewed run of the backup
        return [r | {"value": new} if r["value"] == old else r for r in rows]

    return mutate


def _reviewed_series_rescaled(factor: float):
    """The whole series in another unit: same dates and run shape, every level different."""

    def mutate(rows):
        return [r | {"value": round(r["value"] * factor, 2)} for r in rows]

    return mutate


def _run_order(rows: list[dict]) -> list:
    from itertools import groupby

    return [value for value, _ in groupby(r["value"] for r in rows)]


def _run_boundary_shifted(as_of: str, neighbour_value: float):
    """A run's edge day takes its neighbouring run's value: the NBR update captured one day
    earlier or later. Same dates, same run order; only two run lengths change."""

    def mutate(rows):
        out = _reviewed_value_changed(as_of, neighbour_value)(rows)
        assert out != rows and _run_order(out) == _run_order(rows)  # a pure boundary move
        return out

    return mutate


# Every reviewed day on either side of a boundary between two runs, given the other run's
# value. (2026-05-02 is the whole 1.19 run, so moving it would drop a run, not shift one.)
NBR_BOUNDARY_SHIFTS = [
    (day, value)
    for (value_a, _, last_a), (value_b, first_b, _) in zip(NBR_RUNS, NBR_RUNS[1:])
    for day, value in ((last_a, value_b), (first_b, value_a))
    if day != "2026-05-02"
]


@pytest.mark.parametrize(
    ("as_of", "neighbour_value"),
    NBR_BOUNDARY_SHIFTS,
    ids=[f"{day}-takes-neighbouring-run-value-{value}" for day, value in NBR_BOUNDARY_SHIFTS],
)
def test_nbr_restamp_exclusion_refuses_a_run_boundary_moved_by_one_reviewed_day(
    tmp_path, as_of, neighbour_value
):
    """Controller ruling (R2 fix H1), round-2 review: the reviewed image binds how many days
    each value ran, not only the order of the five values. A backup where one run boundary
    moved by a day (update captured a day early or late) is a different snapshot."""
    rows = _run_boundary_shifted(as_of, neighbour_value)(_restamp_rows(NBR_ID))
    assert [r["as_of"] for r in rows] == [r["as_of"] for r in _restamp_rows(NBR_ID)]
    with pytest.raises(RepairConflict, match="fiscal_nbr_collected_trn"):
        _candidate(_write_backup(tmp_path, _history(rows)))


def test_nbr_boundary_shift_cases_cover_every_edge_day_of_the_reviewed_runs():
    """Guard for the parametrize above: seven edge days (05-02 is a one-day run)."""
    assert sorted(day for day, _ in NBR_BOUNDARY_SHIFTS) == [
        "2026-05-03",
        "2026-06-01",
        "2026-06-02",
        "2026-06-28",
        "2026-06-29",
        "2026-09-05",
        "2026-09-06",
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            _reviewed_value_changed("2026-05-10", 4.15),
            id="a-value-moved-between-reviewed-days-keeps-the-same-five-values",
        ),
        pytest.param(
            _reviewed_values_swapped("2026-06-01", "2026-06-02"),
            id="two-reviewed-days-swap-values-across-a-run-boundary-same-value-counts",
        ),
        pytest.param(
            _reviewed_values_swapped("2026-05-10", "2026-07-15"),
            id="two-mid-run-reviewed-days-swap-values-same-value-counts",
        ),
        pytest.param(
            _reviewed_value_changed("2026-05-02", 2.88),
            id="a-reviewed-value-is-missing-though-every-value-is-one-of-the-five",
        ),
        pytest.param(_reviewed_value_changed("2026-07-15", None), id="a-reviewed-day-has-no-value"),
        pytest.param(
            _reviewed_value_changed("2026-05-02", 1.20),
            id="the-one-day-run-restated-1.19-to-1.20-same-run-shape",
        ),
        pytest.param(
            _reviewed_run_restated(4.15, 4.16),
            id="a-whole-run-restated-4.15-to-4.16-same-run-shape",
        ),
        pytest.param(
            _reviewed_series_rescaled(10),
            id="the-whole-series-in-another-unit-x10-same-run-shape",
        ),
    ],
)
def test_nbr_restamp_exclusion_binds_each_reviewed_day_to_its_exact_reviewed_value(
    tmp_path, mutate
):
    """Controller ruling (R2 fix H1): decision (d) reviewed 140 (as_of, value) pairs, not
    140 dates plus a bag of five values. A backup whose value for any reviewed day differs
    from the reviewed one (or is missing) is a different snapshot and needs a new review."""
    rows = mutate(_restamp_rows(NBR_ID))
    assert [r["as_of"] for r in rows] == [r["as_of"] for r in _restamp_rows(NBR_ID)]
    with pytest.raises(RepairConflict, match="fiscal_nbr_collected_trn"):
        _candidate(_write_backup(tmp_path, _history(rows)))


# --- Owner decision (l), 28 Sep 2026 BDT: the parent tax_revenue and alias nbr_fytd_collected_cr
# Same strictness as decision (d) after R2 fix H1: exactly the 140 reviewed keys, each bound to
# its own reviewed value, all EconDelta, provenance null; anything else is refused.


def _boundary_shifts(runs: tuple) -> list[tuple[str, float]]:
    """Each edge day of a multi-day run, given its neighbouring run's value."""
    one_day = {first for _, first, last in runs if first == last}
    return [
        (day, value)
        for (value_a, _, last_a), (value_b, first_b, _) in zip(runs, runs[1:])
        for day, value in ((last_a, value_b), (first_b, value_a))
        if day not in one_day
    ]


def _parent_refusal_cases(runs: tuple) -> list[tuple[str, object]]:
    (first_value, *_), (second_value, *_), (last_value, *_) = runs[0], runs[1], runs[-1]
    shape = [
        ("later-recapture-adds-a-day", _recaptured_day),
        ("a-reviewed-day-is-missing", lambda rows: rows[:-1]),
        ("the-whole-family-is-absent", lambda rows: []),
        (
            "same-count-but-an-unreviewed-day-replaces-a-reviewed-one",
            _reviewed_day_moved_to_an_unreviewed_day,
        ),
        (
            "a-row-is-not-an-econdelta-restamp",
            lambda rows: rows[:-1] + [rows[-1] | {"source": "Bangladesh Bank"}],
        ),
        (
            "a-row-carries-dated-provenance",
            lambda rows: (
                rows[:-1] + [rows[-1] | {"provenance": '{"source_period_end": "2026-08-31"}'}]
            ),
        ),
    ]
    values = [
        ("a-value-moved-between-reviewed-days", _reviewed_value_changed("2026-05-10", last_value)),
        (
            "two-reviewed-days-swap-values-across-a-run-boundary",
            _reviewed_values_swapped("2026-06-01", "2026-06-02"),
        ),
        (
            "two-mid-run-reviewed-days-swap-values",
            _reviewed_values_swapped("2026-05-10", "2026-07-15"),
        ),
        ("the-first-reviewed-value-is-missing", _reviewed_run_restated(first_value, second_value)),
        ("a-reviewed-day-has-no-value", _reviewed_value_changed("2026-07-15", None)),
        (
            "the-last-run-restated-by-one-crore",
            _reviewed_run_restated(last_value, last_value + 1.0),
        ),
        ("the-whole-series-in-another-unit-x10", _reviewed_series_rescaled(10)),
    ]
    shifts = [
        (f"{day}-takes-neighbouring-run-value-{value}", _run_boundary_shifted(day, value))
        for day, value in _boundary_shifts(runs)
    ]
    return shape + values + shifts


PARENT_REFUSALS = [
    (metric_id, case_id, mutate)
    for metric_id, runs in PARENT_RUNS.items()
    for case_id, mutate in _parent_refusal_cases(runs)
]


@pytest.mark.parametrize(
    ("metric_id", "mutate"),
    [(metric_id, mutate) for metric_id, _, mutate in PARENT_REFUSALS],
    ids=[f"{metric_id}-{case_id}" for metric_id, case_id, _ in PARENT_REFUSALS],
)
def test_parent_and_alias_restamp_exclusion_refuses_any_backup_other_than_the_reviewed_pairs(
    tmp_path, metric_id, mutate
):
    """Decision (l) reviewed 140 (as_of, value) pairs per id, from the hashed 25 Sep backup.
    Any other backup (a recapture, a moved day or value, another source or unit, dated
    provenance) is a different snapshot: the generator refuses and names the id and (l)."""
    rows = mutate(_restamp_rows(metric_id, PARENT_RUNS[metric_id]))
    assert rows != _restamp_rows(metric_id, PARENT_RUNS[metric_id])  # a real change
    backup = _history(_restamp_rows(NBR_ID), {metric_id: rows})
    with pytest.raises(RepairConflict, match=rf"{metric_id} .*decision \(l\)"):
        _candidate(_write_backup(tmp_path, backup))


def test_parent_and_alias_boundary_cases_cover_every_edge_day_of_their_reviewed_runs():
    """Guard for the parametrize above (tax_revenue 05-02 is a one-day run; the alias's
    first run lasts 23 days, so its 05-24/05-25 boundary is an extra edge)."""
    edges = {
        mid: sorted(day for day, _ in _boundary_shifts(runs)) for mid, runs in PARENT_RUNS.items()
    }
    common = ["2026-06-01", "2026-06-02", "2026-06-28", "2026-06-29", "2026-09-05", "2026-09-06"]
    assert edges == {
        "tax_revenue": sorted(["2026-05-03", *common]),
        "nbr_fytd_collected_cr": sorted(["2026-05-24", "2026-05-25", *common]),
    }
