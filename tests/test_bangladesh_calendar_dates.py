"""A reading dated by a Bangladesh calendar day is judged against the Bangladesh date.

The aggregate runs about 02:56 BDT, which is still the previous UTC day. The day's
bb_forex rate is dated by the Dhaka calendar, so a UTC comparison made it look one day
in the future on every scheduled run: quality "unavailable", no metric_history row, and
The Brief's USD/BDT card disappeared. A genuinely future date must still be rejected, and
families whose dates are not Bangladesh calendar days keep the UTC comparison.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "tests/fixtures/contracts/brief-observations-v1.json").read_text())
BD_CONTRACT = CONTRACT["bangladesh_calendar_contract"]
RUN_0256 = datetime(2026, 9, 24, 20, 56, tzinfo=timezone.utc)  # 02:56 BDT on 25 Sep


def decode(record: dict):
    from utils.observations import Observation

    return Observation(**{**record, "as_of": date.fromisoformat(record["as_of"]),
                          "captured_at": datetime.fromisoformat(record["captured_at"]),
                          "dependencies": tuple(record["dependencies"])})


def forex(**changes):
    base = decode(BD_CONTRACT["cases"][0]["record"])  # usd_bdt_mid, 25 Sep, captured 01:35 BDT
    return replace(base, **changes)


def writer_rows(obs, now: datetime) -> list[dict]:
    from utils.supabase_writer import _DEFAULT_SOURCE, _rows_from_data

    # Same arguments as aggregate_latest.main's upsert_metric_history call.
    rows = _rows_from_data({obs.metric_id: obs.value}, now.date(), _DEFAULT_SOURCE,
                           ingested_at=now, observations={obs.metric_id: obs})
    return [{k: v for k, v in row.items() if k != "ingested_at"} for row in rows]


def test_the_producer_names_the_same_bangladesh_calendar_sources_as_the_contract():
    from utils.observations import BANGLADESH_CALENDAR_SOURCES

    assert sorted(BANGLADESH_CALENDAR_SOURCES) == BD_CONTRACT["sources"]


@pytest.mark.parametrize("case", BD_CONTRACT["cases"], ids=lambda c: c["case_id"])
def test_each_shared_boundary_case_gets_the_contract_verdict_and_row(case):
    from utils.observations import eligible, select_observation

    obs = decode(case["record"])
    now = datetime.fromisoformat(case["aggregate_now"])
    assert eligible(obs, today=now.date()) is case["producer_eligible"]
    assert (select_observation([obs], today=now.date()) == obs) is case["producer_eligible"]
    expected_rows = [{"metric_id": obs.metric_id, "as_of": case["record"]["as_of"],
                      "value": obs.value, "source": "EconDelta"}] if case["producer_writes_row"] else []
    assert writer_rows(obs, now) == expected_rows


def test_the_days_usd_bdt_rate_from_a_0256_bdt_run_is_verified_and_written():
    """The realistic run's own inputs (builder_binding_contract.producer_inputs)."""
    import aggregate_latest as agg
    from utils.calendar import load_holidays

    binding = CONTRACT["builder_binding_contract"]
    run = datetime.fromisoformat(binding["aggregate_captured_at"])
    assert run == RUN_0256
    snapshots = {key: schema.model_validate(binding["producer_inputs"]["tier1"][key])
                 for key, (_, schema, _) in agg.SCRAPER_SPEC.items()}
    status = {key: agg.compute_status(snap, None, run, key=key,
                                      holidays=load_holidays(agg.HOLIDAYS_PATH))
              for key, snap in snapshots.items()}
    observations = agg._build_observations(snapshots, {}, status, now=run, holidays=set(),
                                           yield_values={}, yield_dates={})
    for mid in ("usd_bdt_mid", "usd_bdt_buy", "usd_bdt_sell", "usd_bdt_exchange_rate"):
        assert (observations[mid].quality, observations[mid].as_of) == ("verified", date(2026, 9, 25)), mid
    assert writer_rows(observations["usd_bdt_mid"], run) == [
        {"metric_id": "usd_bdt_mid", "as_of": "2026-09-25", "value": 123.22, "source": "EconDelta"}]


@pytest.mark.parametrize("source,metric_id", [
    ("commodity_prices", "brent_crude_usd_barrel"),  # exchange price-history quote date
    ("general_inflation", "general_inflation"),  # a v3 record names only its indicator id
])
def test_a_family_not_dated_by_the_bangladesh_calendar_keeps_the_utc_comparison(source, metric_id):
    from utils.observations import eligible

    obs = forex(source=source, metric_id=metric_id)  # dated 25 Sep, captured 01:35 BDT 25 Sep
    assert eligible(obs, today=RUN_0256.date()) is False
    assert writer_rows(obs, RUN_0256) == []


def test_a_capture_stamped_after_the_run_day_cannot_widen_the_bound():
    """A corrupt capture time on a later UTC day must not make tomorrow's date eligible."""
    from utils.observations import eligible

    obs = forex(as_of=date(2026, 9, 26), captured_at=datetime(2026, 9, 25, 20, tzinfo=timezone.utc))
    assert eligible(obs, today=RUN_0256.date()) is False


@pytest.mark.parametrize("as_of,captured_at,expected", [
    # Yesterday's file (01:35 BDT on 24 Sep) still carries its own Dhaka day: dated evidence.
    (date(2026, 9, 24), datetime(2026, 9, 23, 19, 35, tzinfo=timezone.utc), True),
    # Captured 16:00 BDT on 23 Sep but dated 24 Sep: after its own capture day.
    (date(2026, 9, 24), datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc), False),
], ids=["dated-its-capture-day", "dated-after-its-capture-day"])
def test_an_older_capture_is_bound_by_its_own_dhaka_day(as_of, captured_at, expected):
    """A date after the Dhaka day of its capture is in the future even when the run's UTC
    day has already reached it."""
    from utils.observations import eligible

    obs = forex(as_of=as_of, captured_at=captured_at)
    assert eligible(obs, today=RUN_0256.date()) is expected
    assert writer_rows(obs, RUN_0256) == ([{"metric_id": "usd_bdt_mid", "as_of": as_of.isoformat(),
                                            "value": 123.22, "source": "EconDelta"}] if expected else [])


def _archive(obs) -> dict:
    from utils.observations import serialize_observations

    return {"observations": serialize_observations({obs.metric_id: obs})}


@pytest.mark.parametrize("as_of,recovered", [(date(2026, 9, 25), True), (date(2026, 9, 26), False)])
def test_quarantine_holds_the_earlier_runs_bangladesh_dated_rate_but_never_a_future_one(as_of, recovered):
    """The 03:16 BDT retry quarantines USD/BDT: the 02:56 BDT run's archived record (dated the
    Dhaka day, captured before UTC midnight) is dated evidence, a next-day date is not."""
    from utils.observations import quarantine_observations

    retry = datetime(2026, 9, 24, 21, 16, tzinfo=timezone.utc)
    current = {"usd_bdt_mid": forex(value=124.0, captured_at=retry)}
    archived = forex(as_of=as_of)
    accepted, quarantined, hard_reject = quarantine_observations(
        current, ["usd_bdt_mid"], [_archive(archived)], breadth_count=1)
    assert (quarantined, hard_reject) == (["usd_bdt_mid"], False)
    if recovered:
        assert (accepted["usd_bdt_mid"].value, accepted["usd_bdt_mid"].as_of,
                accepted["usd_bdt_mid"].quality) == (123.22, as_of, "held")
    else:
        assert "usd_bdt_mid" not in accepted
