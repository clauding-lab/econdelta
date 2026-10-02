"""R2 task G1: the producer side of the shared case list (contract `r2_case_contract`).

Each case starts from a raw source input and must end in exactly the accepted observation,
metric_history row or persistence receipt the shared fixture pins, because The Brief replays
those records through its real builders, readiness check and fake-model publish (its
tests/test_shared_case_contract.py). Real values come from the 25 Sep 2026 02:56 BDT run in
`builder_binding_contract`; only the altered field of each case, and the dates of the
holiday case, are synthetic (each case's note says which).
"""

from __future__ import annotations

import copy
import functools
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

import aggregate_latest as agg

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "tests/fixtures/contracts/brief-observations-v1.json").read_text())
CASES = CONTRACT["r2_case_contract"]
BINDING = CONTRACT["builder_binding_contract"]
RUN = datetime.fromisoformat(BINDING["aggregate_captured_at"])  # 02:56 BDT on 25 Sep 2026


def _changed(base: dict, changes: dict) -> dict:
    """Deep-merge a case's input changes; a null deletes the field (it is absent at source)."""
    out = copy.deepcopy(base)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _changed(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _produce(inputs: dict, run: datetime) -> dict:
    """aggregate_latest.main's observation stage, exactly as the binding contract test runs it."""
    from utils.calendar import load_holidays

    urls = json.loads(agg.CONFIG_PATH.read_text())["sources"]
    holidays = load_holidays(agg.HOLIDAYS_PATH)
    snapshots, status = {}, {}
    for key, (_, schema, url_key) in agg.SCRAPER_SPEC.items():
        if key not in inputs["tier1"]:
            continue
        snapshots[key] = schema.model_validate(inputs["tier1"][key])
        url = urls.get(url_key, {}).get("url") if url_key else None
        status[key] = agg.compute_status(snapshots[key], url, run, key=key, holidays=holidays)
    registry = {item["id"]: item for item in agg._load_v3_registry()}
    domains: dict[str, dict] = {}
    for mid, snapshot in inputs.get("v3", {}).items():
        domains.setdefault(registry[mid]["domain"], {})[mid] = dict(snapshot)
    yields, yield_dates = agg._daily_yields_from_auction_rows(inputs.get("auction_rows", []))
    observations = agg._build_observations(snapshots, domains, status, now=run, holidays=holidays,
                                           yield_values=yields, yield_dates=yield_dates)
    return {"observations": observations, "sources_status": status}


def _published(observations: dict) -> tuple[dict, dict]:
    """(latest.json observation records, metric_history rows without ingested_at)."""
    from utils.observations import serialize_observations
    from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

    records = serialize_observations(observations)
    values = {mid: obs.value for mid, obs in observations.items() if obs.value is not None}
    # Same arguments as aggregate_latest.main's upsert_metric_history call (default source).
    rows = _rows_from_data(values, RUN.date(), _DEFAULT_SOURCE, ingested_at=RUN, observations=observations)
    return records, {row["metric_id"]: {k: v for k, v in row.items() if k != "ingested_at"} for row in rows}


def _variant_run(variant: dict) -> tuple[dict, dict]:
    inputs = _changed(BINDING["producer_inputs"], variant["input_changes"])
    observations = _produce(inputs, RUN)["observations"]
    if variant["review_flagged"]:
        # The Opus granular-reject path (aggregate_latest.main -> _quarantine_flagged).
        observations, quarantined, hard_reject = agg._quarantine_flagged(
            observations, variant["review_flagged"], variant["archived_snapshots"])
        assert (sorted(quarantined), hard_reject) == (sorted(variant["review_flagged"]), False)
    return _published(observations)


@pytest.mark.parametrize("variant_id", sorted(CASES["run_variants"]))
def test_each_run_variant_is_what_the_aggregate_and_daily_writer_make_of_its_raw_input(variant_id):
    """The case's own records are exactly the producer's; every other record the Brief binds
    is unchanged from the real run, so the Brief may replay the variant as a whole run."""
    variant = CASES["run_variants"][variant_id]
    records, rows = _variant_run(variant)
    for mid, expected in variant["observations"].items():
        assert records.get(mid) == expected, mid
    for mid, expected in variant["metric_history_rows"].items():
        assert rows.get(mid) == expected, mid
    for mid, record in BINDING["records"].items():
        if mid not in variant["observations"]:
            assert records[mid] == record["observation"], mid
            assert rows.get(mid) == record["metric_history_row"], mid


def test_a_provisional_flash_stays_provisional_through_its_conversion_and_row():
    variant = CASES["run_variants"]["provisional-remittance-flash"]
    assert variant["input_changes"] == {"v3": {"monthly_remittance": {"release_status": "provisional"}}}
    records, _ = _variant_run(variant)
    assert {records[mid]["release_status"] for mid in ("monthly_remittance", "remit_monthly_mn")} == {"provisional"}


def test_a_source_value_without_its_period_is_never_dated_or_written():
    variant = CASES["run_variants"]["missing-period-tax-revenue"]
    records, rows = _variant_run(variant)
    for mid in ("tax_revenue", "fiscal_nbr_collected_trn", "nbr_fytd_collected_cr"):
        assert (records[mid]["as_of"], records[mid]["quality"]) == (None, "unavailable"), mid
        assert mid not in rows, mid


def test_a_rejected_family_is_held_at_its_dated_predecessor_or_omitted_without_one():
    held, _ = _variant_run(CASES["run_variants"]["rejected-brent-with-dated-predecessor"])
    (predecessor,) = CASES["run_variants"]["rejected-brent-with-dated-predecessor"]["archived_snapshots"]
    archived = predecessor["observations"]["brent_crude_usd_barrel"]
    assert (held["brent_crude_usd_barrel"]["value"], held["brent_crude_usd_barrel"]["as_of"],
            held["brent_crude_usd_barrel"]["quality"]) == (archived["value"], archived["as_of"], "held")
    omitted, rows = _variant_run(CASES["run_variants"]["rejected-brent-without-predecessor"])
    assert "brent_crude_usd_barrel" not in omitted and "brent_crude_usd_barrel" not in rows


# ── Holiday closure: DSE shut for Eid-ul-Adha, 25-31 May 2026 (config/holidays_2026.json) ──
HOLIDAY = CASES["holiday_closure"]


def test_a_session_before_a_holiday_closure_is_a_verified_reading_not_a_held_one():
    from utils.calendar import load_holidays
    from utils.observations import serialize_observations
    from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

    run = datetime.fromisoformat(HOLIDAY["aggregate_now"])
    closed = {date.fromisoformat(day) for day in HOLIDAY["closed_days"]}
    assert closed <= load_holidays(agg.HOLIDAYS_PATH)
    produced = _produce({"tier1": {"dse_market": HOLIDAY["dse_market"]}}, run)
    status = {key: s.model_dump(mode="json") for key, s in produced["sources_status"].items()}
    assert status == HOLIDAY["sources_status"]
    records = serialize_observations(produced["observations"])
    rows = _rows_from_data({m: o.value for m, o in produced["observations"].items()}, run.date(),
                           _DEFAULT_SOURCE, ingested_at=run, observations=produced["observations"])
    assert records == HOLIDAY["observations"]
    assert {r["metric_id"]: {k: v for k, v in r.items() if k != "ingested_at"} for r in rows} == \
        HOLIDAY["metric_history_rows"]


def test_the_same_session_without_the_holiday_calendar_would_read_as_held():
    """Proves the case exercises the closure: only the holiday list keeps it verified.

    The held publication differs from the holiday case's records in quality ALONE (same
    values, dates, status and metric_history rows), so The Brief replays it as the case's
    records re-marked 'held' (its test_shared_case_contract held-DSE test)."""
    import utils.calendar as calendar
    from utils.observations import serialize_observations
    from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

    run = datetime.fromisoformat(HOLIDAY["aggregate_now"])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(calendar, "load_holidays", lambda path: set())  # _produce imports it per call
        produced = _produce({"tier1": {"dse_market": HOLIDAY["dse_market"]}}, run)
    assert produced["observations"]["dsex"].quality == "held"
    held = {mid: {**record, "quality": "held"} for mid, record in HOLIDAY["observations"].items()}
    assert serialize_observations(produced["observations"]) == held
    assert {key: s.model_dump(mode="json") for key, s in produced["sources_status"].items()} == \
        HOLIDAY["sources_status"]
    rows = _rows_from_data({m: o.value for m, o in produced["observations"].items()}, run.date(),
                           _DEFAULT_SOURCE, ingested_at=run, observations=produced["observations"])
    assert {r["metric_id"]: {k: v for k, v in r.items() if k != "ingested_at"} for r in rows} == \
        HOLIDAY["metric_history_rows"]


# ── Bangladesh-calendar crossings: the raw bb_forex file through the observation stage ──
BD_CASES = [c for c in CONTRACT["bangladesh_calendar_contract"]["cases"] if c["record"]["source"] == "bb_forex"]


@pytest.mark.parametrize("case", BD_CASES, ids=lambda c: c["case_id"])
def test_each_bb_forex_calendar_case_is_published_verified_or_as_an_unavailable_record(case):
    """The Dhaka-dated bb_forex file the case describes, through _build_observations: an
    eligible date publishes the case record itself; a future one publishes that same record
    marked unavailable (the producer's fallback), which The Brief lists as unavailable."""
    from utils.observations import serialize_observations

    record = case["record"]
    tier1 = _changed(BINDING["producer_inputs"]["tier1"]["bb_forex"],
                     {"date": record["as_of"], "scraped_at": record["captured_at"], "reserves": None})
    run = datetime.fromisoformat(case["aggregate_now"])
    produced = serialize_observations(_produce({"tier1": {"bb_forex": tier1}}, run)["observations"])
    expected = record if case["producer_eligible"] else {**record, "quality": "unavailable"}
    assert produced["usd_bdt_mid"] == expected


# ── Equal yield on a new auction (monthly_contract.yield_refresh) ──
REFRESH = CONTRACT["monthly_contract"]["yield_refresh"]


def _ladder(monkeypatch, stored_source_as_of: str, listed_auction: str, today: date) -> list[dict]:
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer

    month = REFRESH["grouping_key"]
    monkeypatch.setattr(reader, "get_metric_history_monthly", lambda mid: [
        {"metric_id": mid, "as_of": month, "value": REFRESH["stored_value"], "source_as_of": stored_source_as_of}])
    monkeypatch.setattr(reader, "get_auction_results_through", lambda day: [
        {"tenor": tenor, "auction_date": listed_auction, "cutoff": REFRESH["candidate_value"]}
        for tenor in agg._YIELD_TENOR_TO_MONTHLY_ID])
    written: list[dict] = []
    monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda rows: written.extend(rows) or len(rows))
    monkeypatch.setattr(agg, "notify", lambda *args, **kwargs: None)
    agg._write_yield_ladder_monthly_append(today)
    return written


@pytest.mark.parametrize("today", [date(2026, 9, 25), date(2026, 10, 1)], ids=["open-month", "just-closed"])
def test_an_equal_cutoff_from_a_newer_auction_moves_the_rungs_evidence_date(monkeypatch, today):
    assert REFRESH["stored_value"] == REFRESH["candidate_value"]
    written = _ladder(monkeypatch, REFRESH["stored_source_as_of"], REFRESH["candidate_source_as_of"], today)
    assert {(r["as_of"], r["value"], r["source_as_of"]) for r in written} == {
        (REFRESH["grouping_key"], REFRESH["candidate_value"], REFRESH["candidate_source_as_of"])}
    assert len(written) == len(agg._YIELD_TENOR_TO_MONTHLY_ID)


@pytest.mark.parametrize("stored,listed", [
    ("candidate_source_as_of", "candidate_source_as_of"),  # identical pair: no-op
    ("candidate_source_as_of", "stored_source_as_of"),     # older evidence: blocked
], ids=["identical-pair", "older-evidence"])
def test_an_identical_or_older_auction_never_rewrites_the_rung(monkeypatch, stored, listed):
    assert _ladder(monkeypatch, REFRESH[stored], REFRESH[listed], date(2026, 9, 25)) == []


# ── Partial writer failure: one monthly leg fails, a sibling's rows stay confirmed ──
PARTIAL = CASES["partial_writer_failure"]


def _normalised(receipt: dict[str, Any]) -> dict[str, Any]:
    """The leg as latest.json carries it, attempt times normalised to the contract's."""
    from utils.schema import WriteReceipt

    def stamp(node: dict) -> dict:
        legs = node.get("legs")
        return {**node, "attempted_at": PARTIAL["attempted_at"],
                **({"legs": {k: stamp(v) for k, v in legs.items()}} if legs else {})}

    return WriteReceipt.model_validate(stamp(receipt)).model_dump(mode="json", exclude_none=True)


def test_a_failed_leg_is_published_beside_the_rows_its_sibling_confirmed(monkeypatch, tmp_path):
    """The real macro appender with BB's remittance page unreachable (tests/test_monthly_leg_receipts
    world): the CPI rows are written and confirmed, remittance fails, and the published macro
    receipt is failed while still counting the confirmed rows."""
    import utils.monthly_evidence as evidence
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer
    from tests.test_monthly_leg_receipts import _DAILY, FakeMonthlyTable, _row

    table = FakeMonthlyTable([_row(*row) for row in PARTIAL["recorded_before"]])
    monkeypatch.setattr(evidence, "DEFAULT_DIRECTORY", tmp_path / "monthly_evidence")  # never E/data
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    monkeypatch.setattr(reader, "_get", table.get)
    monkeypatch.setattr(reader, "get_metric_history", lambda mid, *, days, **kw: (
        [{"metric_id": mid, "value": _DAILY[mid][0], "as_of": _DAILY[mid][1]}] if mid in _DAILY else []))
    monkeypatch.setattr(writer, "_upsert_monthly_table", table.upsert)
    monkeypatch.setattr(agg, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(agg, "_write_macro_monthly_append", functools.partial(
        agg._write_macro_monthly_append, today=date.fromisoformat(PARTIAL["run_date"])))
    monkeypatch.setattr(agg, "_write_yield_ladder_monthly_append", lambda: 0)
    monkeypatch.setattr("utils.epb_monthly.write_exports_monthly", lambda: 0)
    monkeypatch.setattr(agg, "_fetch_remittance_html",
                        lambda: (_ for _ in ()).throw(RuntimeError("challenge page")))

    macro = agg._run_chart_feeding_monthly_appenders()["legs"]["macro"]

    assert _normalised(macro) == PARTIAL["macro"]
    confirmed = sorted([r["metric_id"], r["as_of"], r["value"]] for r in table.rows
                       if r["metric_id"].startswith("cpi_"))
    assert confirmed == PARTIAL["confirmed_in_table"]


# ── Report-cover lag and Bangladesh FY rollover: the reviewed `cases` through the parsers ──
# A case whose metric the registry reads from ANOTHER publication (private_sector_credit from
# the WSEI, the LC pair from the MEI) is not the producer's path for that evidence.
_STRATEGY = {"mei": "mei_observation", "wsei": "wsei_observation"}
# The review case records the WSEI "6.a Import (C&F)" table (6.78 USD bn); the registry's
# monthly_import reads "b) Import(f.o.b)" (6.44). Two concepts under one id; The Brief reads
# neither (fx.py uses imports_usd_mn_monthly; fetch_fx_flows is not dispatched). Open owner
# question in C/r2-claude/fixG1-report.md -- pinned here so it cannot grow silently.
CONCEPT_SPLITS = {("weekly-components-have-independent-periods", "monthly_import"): (6.78, 6.44)}
# The MEI deficit table is a fiscal-year-to-date total: the parser keeps that qualifier.
_UNIT_QUALIFIER = {"BDT crore": ("BDT crore", "BDT crore cumulative")}


def _case_pairs() -> list[tuple[dict, dict, dict]]:
    registry = {item["id"]: item for item in agg._load_v3_registry()}
    pairs = []
    for case in CONTRACT["cases"]:
        (evidence_id,) = {o["evidence_id"] for o in case["source_observations"]}
        for expected in case["expected_outputs"]:
            indicator = registry[expected["metric_id"]]
            if indicator["parse"]["deterministic"] == _STRATEGY[evidence_id.split("_")[0]]:
                pairs.append((case, expected, indicator))
    return pairs


def _parse_case(case: dict, indicator: dict):
    import parse_all  # noqa: F401  (registers every deterministic parser)
    from fetchers.base import FetchResult
    from parsers.registry import get_parser

    (evidence_id,) = {o["evidence_id"] for o in case["source_observations"]}
    evidence = CONTRACT["evidence"][evidence_id]
    artifact = FetchResult(indicator_id=indicator["id"], artifact_path=ROOT / evidence["fixture"],
                           artifact_type="pdf", fetched_at=RUN, source_url=indicator["fetch"]["url"],
                           sha256=evidence["sha256"], cache_hit=False)
    return get_parser(indicator["parse"]["deterministic"]).parse(artifact, indicator["fetch"]["task"])


@pytest.mark.parametrize("case,expected,indicator", _case_pairs(),
                         ids=lambda x: x.get("case_id") or x.get("metric_id") or x.get("id"))
def test_each_reviewed_case_is_what_the_producers_parser_reads_from_its_evidence(case, expected, indicator):
    """The observation period is the column's own month (June under FY26, not the July cover;
    July 2025 is FY26's first month, not the latest), never the report edition or run date."""
    result = _parse_case(case, indicator)
    split = CONCEPT_SPLITS.get((case["case_id"], expected["metric_id"]))
    reviewed, produced = split if split else (expected["value"], expected["value"])
    assert (expected["value"], result.value) == (reviewed, produced)
    assert result.source_as_of.isoformat() == expected["as_of"]
    assert result.unit in _UNIT_QUALIFIER.get(expected["unit"], (expected["unit"],))


def test_the_reviewed_cases_the_producer_parses_cover_both_evidence_documents_and_the_fy_edge():
    parsed = {(c["case_id"], e["metric_id"]) for c, e, _ in _case_pairs()}
    assert ("mei-lc-select-latest-month-within-fy26", "monthly_import_lc_opening") in parsed
    assert ("mei-deficit-financing-fy26", "bank_borrowing_for_deficit_financing") in parsed
    assert ("weekly-components-have-independent-periods", "monthly_remittance") in parsed
    assert set(CONCEPT_SPLITS) <= parsed
