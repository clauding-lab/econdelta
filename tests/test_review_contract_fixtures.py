"""Keep the reviewed source documents attached to the dated cross-project contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "tests/fixtures/contracts/brief-observations-v1.json"


def test_review_contract_references_the_exact_pdf_pages() -> None:
    contract = json.loads(CONTRACT.read_text())
    assert contract["contract_version"] == 1
    assert len({case["case_id"] for case in contract["cases"]}) == len(contract["cases"])

    page_counts: dict[str, int] = {}
    for evidence_id, evidence in contract["evidence"].items():
        document = ROOT / evidence["fixture"]
        assert hashlib.sha256(document.read_bytes()).hexdigest() == evidence["sha256"]
        metadata = json.loads(document.with_suffix(".meta.json").read_text())
        assert metadata["sha256"] == evidence["sha256"]
        with pdfplumber.open(document) as pdf:
            page_counts[evidence_id] = len(pdf.pages)
        assert page_counts[evidence_id] == metadata["page_count"]

    for case in contract["cases"]:
        assert case["run_date_bdt"] == "2026-09-25"
        assert case["expected_outputs"]
        for observation in case["source_observations"]:
            assert 1 <= observation["page_pdf"] <= page_counts[observation["evidence_id"]]
            assert observation["row"] and observation["column"]
            assert observation["observation_period"]


def test_writer_confirmation_allowlist_and_records_match_the_shared_contract() -> None:
    """The Brief pins the same ids and consumes these exact records; a one-sided
    allowlist change, or a producer change to what a confirmation date means,
    must fail here rather than silently holding or mis-dating the brief."""
    from datetime import datetime

    from utils.observations import (
        WRITER_CONFIRMATION_IDS,
        from_snapshot,
        serialize_observations,
    )

    contract = json.loads(CONTRACT.read_text())["writer_confirmation_contract"]
    assert sorted(contract["metric_ids"]) == sorted(WRITER_CONFIRMATION_IDS)
    run = datetime.fromisoformat(contract["aggregate_captured_at"])
    produced = {}
    for record in contract["records"]:
        mid = record["expected"]["metric_id"]
        observation = from_snapshot(mid, record["producer_input"], captured_at=run)
        produced[mid] = serialize_observations({mid: observation})[mid]
        assert produced[mid] == record["expected"]
    assert sorted(produced) == sorted(WRITER_CONFIRMATION_IDS)


def test_writer_confirmation_rows_are_what_the_daily_writer_stores() -> None:
    """The Brief builds its corridor cards from each record's `metric_history_row`, so the
    fixture must hold what aggregate_latest.main really upserts (the confirmation date is
    metric_history.as_of for these ids). A producer change to that row must fail here
    rather than let the Brief's contract test model data EconDelta never writes."""
    from datetime import datetime

    from utils.observations import from_snapshot
    from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

    contract = json.loads(CONTRACT.read_text())["writer_confirmation_contract"]
    run = datetime.fromisoformat(contract["aggregate_captured_at"])
    for record in contract["records"]:
        mid = record["expected"]["metric_id"]
        observation = from_snapshot(mid, record["producer_input"], captured_at=run)
        # Same arguments as aggregate_latest.main's upsert_metric_history call (default source).
        rows = _rows_from_data({mid: observation.value}, run.date(), _DEFAULT_SOURCE,
                               ingested_at=run, observations={mid: observation})
        stored = [{k: v for k, v in row.items() if k != "ingested_at"} for row in rows]
        assert stored == [record["metric_history_row"]]


def test_builder_binding_records_are_what_the_aggregate_and_writer_produce() -> None:
    """The Brief replays these records through its real builders and its readiness check
    (its tests/test_producer_consumer_contract.py). They must be exactly what one
    aggregate run makes of the stored inputs, and exactly what its daily writer stores,
    so a producer change to a record's source id, unit, date or value fails here instead
    of silently holding, or silently passing, The Brief's publication."""
    from datetime import datetime

    import aggregate_latest as agg
    from utils.calendar import load_holidays
    from utils.observations import serialize_observations
    from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

    contract = json.loads(CONTRACT.read_text())["builder_binding_contract"]
    run = datetime.fromisoformat(contract["aggregate_captured_at"])
    inputs = contract["producer_inputs"]
    urls = json.loads(agg.CONFIG_PATH.read_text())["sources"]
    holidays = load_holidays(agg.HOLIDAYS_PATH)
    snapshots, status = {}, {}
    for key, (_, schema, url_key) in agg.SCRAPER_SPEC.items():
        snapshots[key] = schema.model_validate(inputs["tier1"][key])
        url = urls.get(url_key, {}).get("url") if url_key else None
        status[key] = agg.compute_status(snapshots[key], url, run, key=key, holidays=holidays)
    assert {key: s.model_dump(mode="json") for key, s in status.items()} == contract["sources_status"]
    registry = {item["id"]: item for item in agg._load_v3_registry()}
    domains: dict[str, dict] = {}
    for mid, snapshot in inputs["v3"].items():
        domains.setdefault(registry[mid]["domain"], {})[mid] = dict(snapshot)
    yields, yield_dates = agg._daily_yields_from_auction_rows(inputs["auction_rows"])
    observations = agg._build_observations(snapshots, domains, status, now=run, holidays=holidays,
                                           yield_values=yields, yield_dates=yield_dates)
    produced = serialize_observations(observations)
    # Same arguments as aggregate_latest.main's upsert_metric_history call (default source).
    rows = _rows_from_data({mid: obs.value for mid, obs in observations.items() if obs.value is not None},
                           run.date(), _DEFAULT_SOURCE, ingested_at=run, observations=observations)
    stored = {row["metric_id"]: {k: v for k, v in row.items() if k != "ingested_at"} for row in rows}
    assert contract["records"]
    for mid, record in contract["records"].items():
        assert produced[mid] == record["observation"], mid
        assert stored.get(mid) == record["metric_history_row"], mid


def test_builder_binding_monthly_inputs_are_what_the_parsers_read_from_the_evidence() -> None:
    """The stored WSEI/MEI inputs are the registry's own deterministic parser output for
    the reviewed BB documents in `evidence`, not hand-typed numbers or dates."""
    from datetime import datetime

    import aggregate_latest as agg
    import parse_all  # noqa: F401  (registers every deterministic parser)
    from fetchers.base import FetchResult
    from parsers.registry import get_parser

    contract = json.loads(CONTRACT.read_text())
    documents = {item["sha256"]: ROOT / item["fixture"] for item in contract["evidence"].values()}
    registry = {item["id"]: item for item in agg._load_v3_registry()}
    checked = []
    for mid, stored in contract["builder_binding_contract"]["producer_inputs"]["v3"].items():
        if stored["_parse_strategy"] not in {"wsei_observation", "mei_observation"}:
            continue
        indicator = registry[mid]
        assert stored["_parse_strategy"] == indicator["parse"]["deterministic"]
        artifact = FetchResult(
            indicator_id=mid, artifact_path=documents[stored["_artifact_sha256"]], artifact_type="pdf",
            fetched_at=datetime.fromisoformat(stored["scraped_at"]), source_url=indicator["fetch"]["url"],
            sha256=stored["_artifact_sha256"], cache_hit=False)
        result = get_parser(stored["_parse_strategy"]).parse(artifact, indicator["fetch"]["task"])
        assert (result.value, result.source_as_of.isoformat(), result.unit, result.release_status) == (
            stored["value"], stored["source_as_of"], stored["unit"], stored["release_status"]), mid
        checked.append(mid)
    assert sorted(checked) == ["bank_borrowing_for_deficit_financing",
                               "domestic_borrowing_for_budget_deficit", "monthly_remittance", "tax_revenue"]
