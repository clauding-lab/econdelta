"""R2 fix H5 (owner decision m, 28 Sep 2026): the NBR fiscal-year-to-date total is dated by the
source's own stated period and written only when that (period, value) is new.

`tax_revenue` (BB WSEI "Tax Revenue (NBR)"), its plain alias `nbr_fytd_collected_cr` and its
trillion conversion `fiscal_nbr_collected_trn` (the id The Brief's NBR card reads) used to be
re-sent every night: under the deployed producer with the RUN date (140 restamped rows each),
and on this branch onto the same period row, bumping it as if freshly written. The shared
fixture's `r2_case_contract.nbr_fytd_period_rows` pins five consecutive runs; The Brief replays
the rows they leave behind through its real NBR card and readiness check.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

import aggregate_latest as agg
from tests.test_aggregator import _build_data_tree
from tests.test_shared_case_contract import BINDING, CASES, RUN, _changed, _produce
from utils import supabase_reader as reader
from utils import supabase_writer as sw
from utils import write_receipts as wr
from utils.observations import serialize_observations
from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

SEQUENCE = CASES["nbr_fytd_period_rows"]
FAMILY = tuple(SEQUENCE["family"])
RUNS = {run["run"]: run for run in SEQUENCE["runs"]}


class SyntheticMetricHistory:
    """In-memory metric_history keyed by (metric_id, as_of), last write wins. Rows are built by
    the REAL row builder from the run's observations, so each row's date is the writer's own."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}
        self.sent: list[list[dict]] = []

    def upsert(self, *, data, as_of, source=_DEFAULT_SOURCE, source_as_of_map=None,
               ingested_at=None, observations=None, **kw) -> int:
        rows = _rows_from_data(data, as_of, source, source_as_of_map, ingested_at,
                               observations=observations)
        self.sent.append(rows)
        for row in rows:
            self.rows[(row["metric_id"], row["as_of"])] = {k: v for k, v in row.items() if k != "ingested_at"}
        return len(rows)

    def read_at(self, keys, **kw) -> list[dict]:
        return [dict(self.rows[tuple(key)]) for key in keys if tuple(key) in self.rows]

    def family_rows(self) -> dict[str, list[tuple[str, float]]]:
        return {mid: sorted((day, row["value"]) for (m, day), row in self.rows.items() if m == mid)
                for mid in FAMILY}


def _night(table: SyntheticMetricHistory, run: dict[str, Any]) -> tuple[dict, dict, list[dict]]:
    """One aggregate night of the fixture: (published NBR records, NBR rows sent, daily skips)."""
    observations = _produce(_changed(BINDING["producer_inputs"], run["input_changes"]), RUN)["observations"]
    values = {mid: obs.value for mid, obs in observations.items() if obs.value is not None}
    to_write, skips = agg._unrecorded_nbr_fytd(values, observations, today=RUN.date(), reader=table.read_at)
    table.upsert(data=to_write, as_of=RUN.date(), ingested_at=RUN, observations=observations)
    records = serialize_observations(observations)
    sent = {row["metric_id"]: {k: v for k, v in row.items() if k != "ingested_at"}
            for row in table.sent[-1] if row["metric_id"] in FAMILY}
    return {mid: records[mid] for mid in FAMILY}, sent, skips


def _nights(*names: str) -> tuple[SyntheticMetricHistory, list[tuple[dict, dict, list[dict]]]]:
    table = SyntheticMetricHistory()
    return table, [_night(table, RUNS[name]) for name in names]


def test_each_night_publishes_sends_and_explains_exactly_what_the_shared_contract_pins():
    """Starting from an empty table, the five nights run in order."""
    table = SyntheticMetricHistory()
    for run in SEQUENCE["runs"]:
        records, sent, skips = _night(table, run)
        assert records == run["observations"], run["run"]
        assert sent == run["rows_sent"], run["run"]
        assert skips == run["daily_skips"], run["run"]


def test_two_nights_with_an_unchanged_fytd_figure_leave_one_row_per_id():
    table, [(_, first, _), (_, second, skips)] = _nights("first-night", "next-night-unchanged")
    assert sorted(first) == sorted(FAMILY)
    assert second == {}
    assert {skip["category"] for skip in skips} == {wr.PERIOD_ALREADY_RECORDED}
    assert table.family_rows() == {
        "tax_revenue": [("2026-06-30", 415473.0)], "nbr_fytd_collected_cr": [("2026-06-30", 415473.0)],
        "fiscal_nbr_collected_trn": [("2026-06-30", 4.15)]}


def test_a_new_months_figure_adds_exactly_one_row_per_id_dated_by_its_period():
    table, nights = _nights("first-night", "next-night-unchanged", "new-month")
    _, sent, skips = nights[-1]
    assert {mid: (row["as_of"], row["value"]) for mid, row in sent.items()} == {
        "tax_revenue": ("2026-07-31", 30512.4), "nbr_fytd_collected_cr": ("2026-07-31", 30512.4),
        "fiscal_nbr_collected_trn": ("2026-07-31", 0.31)}
    assert skips == []
    assert {mid: len(rows) for mid, rows in table.family_rows().items()} == dict.fromkeys(FAMILY, 2)


def test_a_figure_whose_source_states_no_period_writes_nothing_and_the_receipt_says_why():
    table, nights = _nights("first-night", "new-month", "period-missing")
    records, sent, skips = nights[-1]
    assert sent == {}
    assert {mid: record["as_of"] for mid, record in records.items()} == dict.fromkeys(FAMILY)
    assert [(skip["category"], skip["detail"].split(":")[0]) for skip in skips] == [
        (wr.NO_SOURCE_PERIOD, mid) for mid in FAMILY]
    # Nothing new, nothing re-dated: the table still ends at the last properly dated figure.
    assert max(table.family_rows()["fiscal_nbr_collected_trn"]) == ("2026-07-31", 0.31)
    assert all(day != RUN.date().isoformat() for rows in table.family_rows().values() for day, _ in rows)


@pytest.mark.parametrize("name", ["first-night", "new-month", "unchanged-value-new-period"])
def test_the_derived_children_take_exactly_the_parents_period(name):
    records, sent, _ = _night(SyntheticMetricHistory(), RUNS[name])
    period = RUNS[name]["input_changes"].get("v3", {}).get("tax_revenue", {}).get(
        "source_as_of", BINDING["producer_inputs"]["v3"]["tax_revenue"]["source_as_of"])
    assert {mid: record["as_of"] for mid, record in records.items()} == dict.fromkeys(FAMILY, period)
    assert {mid: row["as_of"] for mid, row in sent.items()} == dict.fromkeys(FAMILY, period)


def test_an_unchanged_value_published_for_a_new_period_is_still_written():
    table, nights = _nights("new-month", "unchanged-value-new-period")
    _, sent, skips = nights[-1]
    assert {mid: (row["as_of"], row["value"]) for mid, row in sent.items()} == {
        "tax_revenue": ("2026-08-31", 30512.4), "nbr_fytd_collected_cr": ("2026-08-31", 30512.4),
        "fiscal_nbr_collected_trn": ("2026-08-31", 0.31)}
    assert skips == []


def test_a_revised_figure_for_an_already_recorded_period_is_written():
    """(period, value) is new when the value changed, even for a period already on file."""
    table = SyntheticMetricHistory()
    table.rows[("tax_revenue", "2026-06-30")] = {  # SYNTHETIC earlier vintage of the FY26 total
        "metric_id": "tax_revenue", "as_of": "2026-06-30", "value": 415000.0, "source": "EconDelta"}
    _, sent, skips = _night(table, RUNS["first-night"])
    assert sent["tax_revenue"] == {"metric_id": "tax_revenue", "as_of": "2026-06-30",
                                   "value": 415473.0, "source": "EconDelta"}
    assert skips == []


def _seeded(*mids: str) -> SyntheticMetricHistory:
    """A table already holding the producer's own FY26 row for exactly ``mids`` (values from the fixture)."""
    table = SyntheticMetricHistory()
    for mid in mids:
        table.rows[(mid, "2026-06-30")] = dict(RUNS["first-night"]["rows_sent"][mid])
    return table


def test_each_id_is_judged_on_its_own_row_missing_children_are_written_though_the_parent_is_on_file():
    """Per id, not per family: e.g. an R1 exclusion removed only the children's rows for the period."""
    _, sent, skips = _night(_seeded("tax_revenue"), RUNS["first-night"])
    children = [mid for mid in FAMILY if mid != "tax_revenue"]
    assert sent == {mid: RUNS["first-night"]["rows_sent"][mid] for mid in children}
    assert [(s["category"], s["detail"].split(":")[0]) for s in skips] == [
        (wr.PERIOD_ALREADY_RECORDED, "tax_revenue")]


def test_each_id_is_judged_on_its_own_row_a_missing_parent_is_written_though_the_children_are_on_file():
    children = [mid for mid in FAMILY if mid != "tax_revenue"]
    _, sent, skips = _night(_seeded(*children), RUNS["first-night"])
    assert sent == {"tax_revenue": RUNS["first-night"]["rows_sent"]["tax_revenue"]}
    assert [(s["category"], s["detail"].split(":")[0]) for s in skips] == [
        (wr.PERIOD_ALREADY_RECORDED, mid) for mid in children]


def test_a_press_figure_on_file_for_the_period_is_replaced_by_the_producers_own_row_once_bb_catches_up():
    """'Already recorded' means recorded by this writer. A human-approved press row that BB has since
    superseded (spec D6) must not keep the card crediting the press, even when the 2-dp trillion card
    value happens to equal BB's. All numbers SYNTHETIC: The Daily Star 30600.0 crore front-runs July
    FY27 while BB still shows FY26; BB then prints 30512.4 (both 0.31 trn)."""
    table = SyntheticMetricHistory()
    press = {"id": 1, "metric_id": "tax_revenue", "kind": "fresher_period", "press_as_of": "2026-07-31",
             "press_value": 30600.0, "parsed_value": 415473.0, "source_outlet": "thedailystar",
             "status": "approved"}
    agg._apply_one_media_override(press, {"tax_revenue": 415473.0}, {"tax_revenue": date(2026, 6, 30)},
                                  writer=table.upsert)  # night A: BB still at FY26, the press row is written
    assert table.rows[("fiscal_nbr_collected_trn", "2026-07-31")]["source"] == "media-approved:thedailystar"

    records, sent, skips = _night(table, RUNS["new-month"])  # night B: BB prints July FY27 itself
    bb_values = {mid: record["value"] for mid, record in records.items()}
    _, status = agg._apply_one_media_override(press, bb_values, {"tax_revenue": date(2026, 7, 31)},
                                              writer=table.upsert)
    assert status == "superseded"
    assert sent == RUNS["new-month"]["rows_sent"]
    assert skips == []
    assert {mid: (table.rows[(mid, "2026-07-31")]["value"], table.rows[(mid, "2026-07-31")]["source"])
            for mid in FAMILY} == {mid: (row["value"], _DEFAULT_SOURCE)
                                   for mid, row in RUNS["new-month"]["rows_sent"].items()}

    _, sent_again, skips_again = _night(table, RUNS["new-month"])  # the producer's own rows are now on file
    assert sent_again == {}
    assert {s["category"] for s in skips_again} == {wr.PERIOD_ALREADY_RECORDED}


_NO_SOURCE = object()  # the stored row carries no "source" key at all


@pytest.mark.parametrize("stored_source", [_NO_SOURCE, None, "manual-fix (SYNTHETIC label)"],
                         ids=["source-key-absent", "source-null", "manual-fix-label"])
def test_a_same_value_row_with_no_source_or_a_non_press_label_is_not_already_recorded(stored_source):
    """'Already recorded' needs this daily writer's own source, not only the same value: a same-value
    row with no source, or under another label such as a hand fix, is not proof that the producer
    wrote it, so the producer's own row is sent over it. SYNTHETIC rows; values are the fixture's."""
    table = _seeded("tax_revenue")
    row = {k: v for k, v in table.rows[("tax_revenue", "2026-06-30")].items() if k != "source"}
    table.rows[("tax_revenue", "2026-06-30")] = row if stored_source is _NO_SOURCE else {
        **row, "source": stored_source}
    _, sent, skips = _night(table, RUNS["first-night"])
    assert sent["tax_revenue"] == RUNS["first-night"]["rows_sent"]["tax_revenue"]
    assert table.rows[("tax_revenue", "2026-06-30")]["source"] == _DEFAULT_SOURCE
    assert not [s for s in skips if s["detail"].startswith("tax_revenue:")]


def test_an_unreadable_table_never_withholds_a_dated_row():
    """Unknown is not 'already recorded': the dated rows go out as before (same key, same period)."""
    table = SyntheticMetricHistory()

    def unreadable(keys, **kw):
        raise reader.SupabaseReadError("synthetic outage")

    table.read_at = unreadable
    _, sent, skips = _night(table, RUNS["first-night"])
    assert sent == RUNS["first-night"]["rows_sent"]
    assert skips == []


def test_only_the_nbr_family_is_held_back_other_series_are_sent_as_before():
    table, _ = _nights("first-night")
    _night(table, RUNS["next-night-unchanged"])
    resent = {row["metric_id"] for row in table.sent[-1]}
    assert resent.isdisjoint(FAMILY)
    assert "fiscal_bank_borrow_trn" in resent  # an unchanged non-NBR row keeps its nightly re-send


# --- main(): the same rule on the real nightly path ---


@pytest.fixture
def nightly(tmp_path, monkeypatch):
    """The real main() over a tier-1 data tree plus the real WSEI tax_revenue snapshot."""
    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest = data_dir / "latest.json"
    for name, value in (("DATA_DIR", data_dir), ("LATEST_PATH", latest),
                        ("ARCHIVE_DIR", data_dir / "archive"), ("CONFIG_PATH", cfg_path),
                        ("STALENESS_STATE_PATH", data_dir / "staleness_state.json"),
                        ("WATCHLIST_STALENESS_STATE_PATH", data_dir / "watchlist_staleness_state.json"),
                        ("STALE_FALLBACK_ALERT_STATE_PATH", data_dir / "stale_fallback_alert_state.json")):
        monkeypatch.setattr(agg, name, value)
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: None)
    table = SyntheticMetricHistory()
    monkeypatch.setattr(sw, "upsert_metric_history", table.upsert)
    monkeypatch.setattr(reader, "get_metric_history_at", table.read_at)
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **kw: 0)
    monkeypatch.setattr(sw, "verify_landed_count", lambda *a, **kw: True)
    monkeypatch.setattr(agg, "_write_reserves_monthly_split", lambda *a: 0)
    monkeypatch.setattr(agg, "_run_chart_feeding_monthly_appenders", lambda: {
        "status": "skipped", "attempted_at": "2026-09-25T00:00:00+00:00", "reason": "official lag"})
    monkeypatch.setattr(reader, "get_active_media_review", lambda **kw: [])
    snapshot = dict(BINDING["producer_inputs"]["v3"]["tax_revenue"])
    (data_dir / "tax_revenue").mkdir()

    def night(file_day: str, **changes) -> dict:
        (data_dir / "tax_revenue" / f"{file_day}.json").write_text(json.dumps({**snapshot, **changes}))
        assert agg.main() == 0
        return json.loads(latest.read_text())["write_status"]["daily"]

    return table, night


def test_the_nightly_run_sends_the_unchanged_nbr_total_once_and_says_why_it_skips(nightly):
    table, night = nightly
    first = night("2026-09-24")
    assert {row["metric_id"]: row["as_of"] for row in table.sent[-1] if row["metric_id"] in FAMILY} == \
        dict.fromkeys(FAMILY, "2026-06-30")
    assert first["status"] == "ok" and not first.get("skips")

    second = night("2026-09-24")
    assert [row for row in table.sent[-1] if row["metric_id"] in FAMILY] == []
    assert table.sent[-1], "the rest of the daily write still runs"
    assert second["status"] == "ok"
    assert [(s["category"], s["detail"].split(":")[0]) for s in second["skips"]] == [
        (wr.PERIOD_ALREADY_RECORDED, mid) for mid in FAMILY]

    third = night("2026-09-25", source_as_of=None)  # tonight's WSEI figure carries no period
    assert [row for row in table.sent[-1] if row["metric_id"] in FAMILY] == []
    assert [(s["category"], s["detail"].split(":")[0]) for s in third["skips"]] == [
        (wr.NO_SOURCE_PERIOD, mid) for mid in FAMILY]
    assert table.family_rows()["fiscal_nbr_collected_trn"] == [("2026-06-30", 4.15)]
