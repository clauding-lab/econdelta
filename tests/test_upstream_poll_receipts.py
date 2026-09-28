"""The real CPI/M2 upstream polls leave their own receipts, so liveness can read ok (R2 fix H3).

The only network poll of Bangladesh Bank's CPI and M2 pages is the daily fetch stage
(econdelta-fetch.timer, 01:10 BDT): general_inflation and m2_growth_yoy_pct from BB's
econdata HTML tables, food_inflation and non_food_inflation from BB's MEI PDF. Before this
fix it left no per-page evidence, and the monthly legs' database rereads were all fix 5's
`cpi_upstream_poll` / `m2_upstream_poll` could see, so both read `missing` every night.

Each poll now writes a typed upstream-poll receipt in its own directory
(`data/upstream_polls/<source id>.json`), which the monthly legs' `<metric_id>.json`
database-reread receipts in `data/monthly_evidence/` cannot overwrite. The aggregate reads
them into sources_status. Vintages below come from real captured BB pages
(tests/fixtures/bb_inflation.html, bb_moneysupply.html, tests/_pdfs/bb_mei_2026_june.pdf);
the fetch itself, every database edge and the clock are faked. No real BB page is polled.
"""
from __future__ import annotations

import functools
import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import aggregate_latest as agg
import fetch_all
import utils.monthly_evidence as evidence
import utils.upstream_poll as poll
from fetchers.base import FetchError, FetchResult
from tests.test_aggregator import _build_data_tree
from tests.test_monthly_leg_receipts import (  # noqa: F401
    _DAILY,
    _daily_scrapers_stuck_at_july,
    _raise,
    _row,
    world,
)
from utils import supabase_reader as reader
from utils import supabase_writer as sw
from utils.schema import SourceStatus

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "tests/fixtures/contracts/brief-observations-v1.json")
                      .read_text())["upstream_liveness_contract"]
LIVE = CONTRACT["live_upstream_polls"]["sources_status"]
REGISTRY = {ind["id"]: ind for ind in
            json.loads((ROOT / "config/sources-v3.json").read_text())["indicators"]}
POLL_TIME = datetime(2026, 9, 24, 19, 10, tzinfo=timezone.utc)   # 01:10 BDT fetch slot
READ_TIME = datetime(2026, 9, 24, 20, 55, tzinfo=timezone.utc)   # 02:55 BDT aggregate slot
MEI_URL = "https://www.bb.org.bd//pub/monthly/selectedecooind/2026_june.pdf"
# The real captured page each upstream source serves, and the vintage that page states.
PAGES = {
    "general_inflation": ("tests/fixtures/bb_inflation.html", "2026-07-31"),
    "food_inflation": ("tests/_pdfs/bb_mei_2026_june.pdf", "2026-06-30"),
    "non_food_inflation": ("tests/_pdfs/bb_mei_2026_june.pdf", "2026-06-30"),
    "m2_growth_yoy_pct": ("tests/fixtures/bb_moneysupply.html", "2026-06-30"),
}
CHALLENGE_PAGE = "<html><body><h1>Please enable JavaScript</h1></body></html>"
# The newest month our own daily table holds per monthly id, as the monthly legs' database
# rereads record it: caught up with the period each live page states.
CAUGHT_UP = {monthly: PAGES[source][1] for monthly, source in poll.UPSTREAM_POLL_SOURCES.items()}


def _config(tmp_path: Path, ids: tuple[str, ...]) -> Path:
    """The real registry entries whose fetch is the CPI/M2 upstream poll."""
    path = tmp_path / "sources-v3.json"
    path.write_text(json.dumps({"indicators": [REGISTRY[sid] for sid in ids]}))
    return path


def _serve(tmp_path: Path, polled_at: datetime = POLL_TIME, *, broken: dict | None = None):
    """Fake fetchers: each returns the real captured page as today's artifact. `broken` maps a
    source id to an exception to raise or to replacement page text."""
    broken = broken or {}

    def artifact(indicator_id: str, kind: str, url: str) -> FetchResult:
        failure = broken.get(indicator_id)
        if isinstance(failure, Exception):
            raise failure
        target = tmp_path / "served" / indicator_id / Path(PAGES[indicator_id][0]).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(failure, str):
            target.write_text(failure)
        else:
            shutil.copyfile(ROOT / PAGES[indicator_id][0], target)
        # An unchanged MEI PDF is a cache hit: the poll ran, the file was not rewritten.
        return FetchResult(indicator_id=indicator_id, artifact_path=target, artifact_type=kind,
                           fetched_at=polled_at, source_url=url, sha256="0" * 64,
                           cache_hit=kind == "pdf")

    return {
        "fetch_html": lambda *, url, indicator_id, snapshot_dir: artifact(indicator_id, "html", url),
        "fetch_pdf": lambda *, url, indicator_id, snapshot_dir, as_of_month, period: (
            artifact(indicator_id, "pdf", url)),
        "_download_index_html": lambda url: "<html>MEI listing</html>",
        "discover_latest_pdf": lambda *, html, base_url: (MEI_URL, (2026, 6)),
    }


def _poll(tmp_path: Path, monkeypatch, data_root: Path, *, ids: tuple[str, ...] = tuple(PAGES),
          polled_at: datetime = POLL_TIME, broken: dict | None = None) -> None:
    """The real fetch stage (fetch_all.run) over real registry entries, with fake fetchers and
    its clock set to the poll time."""
    for name, fake in _serve(tmp_path, polled_at, broken=broken).items():
        monkeypatch.setattr(fetch_all, name, fake)
    monkeypatch.setattr(fetch_all, "_poll_clock", lambda: polled_at)
    fetch_all.run(config_path=_config(tmp_path, ids), data_root=data_root)


@pytest.fixture(scope="module")
def live_polls(tmp_path_factory) -> Path:
    """One real poll of every page at POLL_TIME (reading the MEI PDF's vintage costs seconds,
    so the module polls once and each test works on its own copy)."""
    base = tmp_path_factory.mktemp("live-poll")
    with pytest.MonkeyPatch.context() as mp:
        _poll(base, mp, base / "data")
    return base / "data" / "upstream_polls"


@pytest.fixture
def data_root(tmp_path, live_polls) -> Path:
    shutil.copytree(live_polls, tmp_path / "data" / "upstream_polls")
    return tmp_path / "data"


def _receipt(data_root: Path, source_id: str) -> dict:
    return json.loads((data_root / "upstream_polls" / f"{source_id}.json").read_text())


def _database_rereads(directory: Path, periods: dict[str, str]) -> None:
    """The CPI/M2 legs' database-reread receipts, each stating the newest month our own daily
    table holds (the value is synthetic; only the period matters here)."""
    for mid, period in periods.items():
        evidence.record_source_check(
            mid, [(date.fromisoformat(period).replace(day=1), 1.0)], [], today=READ_TIME.date(),
            revisions=[], source_url="daily metric_history observations (synthetic)",
            evidence_kind="database-observations", directory=directory)


def _families(data_root: Path, now: datetime = READ_TIME,
              database: dict[str, str] = CAUGHT_UP) -> dict:
    """What the aggregate publishes: E6 rows (the monthly legs' database rereads), overridden by
    the upstream-poll receipts."""
    monitor_dir = data_root / "monthly_evidence"
    _database_rereads(monitor_dir, database)
    monitor = evidence.source_monitor(directory=monitor_dir, now=now)
    polls = poll.read_polls(data_root / "upstream_polls", now=now)
    return {key: SourceStatus(**entry).model_dump(mode="json")
            for key, entry in evidence.upstream_poll_status(monitor, polls=polls, now=now).items()}


# ── The poll writes its own receipt ─────────────────────────────────────────────────────

def test_each_live_cpi_m2_poll_writes_a_typed_receipt_with_the_vintage_its_page_states(data_root):
    assert sorted(p.name for p in (data_root / "upstream_polls").iterdir()) == sorted(
        f"{source_id}.json" for source_id in PAGES)
    for source_id, (_, vintage) in PAGES.items():
        receipt = _receipt(data_root, source_id)
        assert receipt["receipt_kind"] == "upstream-poll", source_id
        assert (receipt["source_id"], receipt["status"], receipt["reason"]) == (source_id, "ok", None)
        checked = datetime.fromisoformat(receipt["checked_at"])
        assert checked.utcoffset() is not None and checked == POLL_TIME, source_id
        assert receipt["latest_source_vintage"] == vintage, source_id
        assert receipt["last_success_at"] == receipt["checked_at"], source_id
    # Nothing of this lands where the database rereads live.
    assert not (data_root / "monthly_evidence").exists()


def test_a_failed_fetch_is_a_failed_poll_that_keeps_the_last_success(tmp_path, monkeypatch, data_root):
    next_day = POLL_TIME + timedelta(days=1)                      # yesterday all polled ok
    _poll(tmp_path, monkeypatch, data_root, ids=("m2_growth_yoy_pct", "general_inflation"),
          polled_at=next_day, broken={"m2_growth_yoy_pct": FetchError("playwright fetch failed: Timeout")})

    receipt = _receipt(data_root, "m2_growth_yoy_pct")
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "fetch failed (FetchError)"
    assert receipt["latest_source_vintage"] is None
    assert datetime.fromisoformat(receipt["checked_at"]) == next_day
    assert datetime.fromisoformat(receipt["checked_at"]).utcoffset() is not None
    assert receipt["last_success_at"] == POLL_TIME.isoformat()
    # One page failing never stops the others' polls.
    assert _receipt(data_root, "general_inflation")["checked_at"] == next_day.isoformat()


def test_a_fetched_page_that_states_no_vintage_is_an_unknown_poll_not_ok(tmp_path, monkeypatch, data_root):
    """html_fetcher saves BB's bot-challenge page too: a download is not a read of BB's table."""
    _poll(tmp_path, monkeypatch, data_root, ids=("general_inflation",),
          broken={"general_inflation": CHALLENGE_PAGE})

    receipt = _receipt(data_root, "general_inflation")
    assert (receipt["status"], receipt["latest_source_vintage"]) == ("unknown", None)
    assert receipt["reason"] == "fetched, but no source vintage could be read from the page (ParseError)"
    # The last successful poll stays on record; the current one still reads not-ok.
    assert receipt["last_success_at"] == POLL_TIME.isoformat()


# ── The aggregate's reading of the receipts ─────────────────────────────────────────────

def test_live_polls_publish_ok_families_with_last_success_and_age(data_root):
    assert _families(data_root) == LIVE
    assert all(entry["status"] == "ok" for entry in LIVE.values())


def test_a_failed_poll_makes_its_family_failed_and_names_why(tmp_path, monkeypatch, data_root):
    _poll(tmp_path, monkeypatch, data_root, ids=("food_inflation",),
          broken={"food_inflation": FetchError("PDF download failed: 404")})

    families = _families(data_root)
    assert families["m2_upstream_poll"] == LIVE["m2_upstream_poll"]
    cpi = families["cpi_upstream_poll"]
    # Failed now; the family's last successful poll is still reported, never hidden.
    assert (cpi["status"], cpi["last_success"], cpi["age_hours"]) == (
        "failed", "2026-09-24T19:10:00Z", 1.75)
    assert cpi["error"] == ("CPI upstream source-poll liveness not established: last upstream "
                            "poll failed: fetch failed (FetchError) (cpi_p2p_food_monthly)")


def test_an_unknown_poll_is_missing_liveness(tmp_path, monkeypatch, data_root):
    _poll(tmp_path, monkeypatch, data_root, ids=("m2_growth_yoy_pct",),
          broken={"m2_growth_yoy_pct": CHALLENGE_PAGE})

    m2 = _families(data_root)["m2_upstream_poll"]
    assert m2["status"] == "missing"
    assert m2["error"] == (
        "M2 upstream source-poll liveness not established: last upstream poll outcome unknown: "
        "fetched, but no source vintage could be read from the page (ParseError) "
        "(m2_growth_yoy_monthly)")


@pytest.mark.parametrize("hours, status", [(26, "ok"), (26.5, "stale")], ids=["at-cadence", "past-cadence"])
def test_a_poll_receipt_older_than_the_familys_poll_cadence_is_a_stale_poll(
        data_root, hours, status):
    families = _families(data_root, now=POLL_TIME + timedelta(hours=hours))
    assert {key: entry["status"] for key, entry in families.items()} == {
        "cpi_upstream_poll": status, "m2_upstream_poll": status}
    m2 = families["m2_upstream_poll"]
    assert (m2["last_success"], m2["age_hours"]) == ("2026-09-24T19:10:00Z", hours)
    if status == "stale":
        assert m2["error"] == ("M2 upstream source-poll liveness not established: stale poll: last "
                               "upstream poll older than the 26-hour poll cadence (m2_growth_yoy_monthly)")


def test_a_family_is_only_as_fresh_as_its_oldest_successful_poll(tmp_path, monkeypatch, data_root):
    _poll(tmp_path, monkeypatch, data_root, ids=("general_inflation",),
          polled_at=POLL_TIME + timedelta(hours=1))

    cpi = _families(data_root, now=POLL_TIME + timedelta(hours=2))["cpi_upstream_poll"]
    assert (cpi["status"], cpi["last_success"], cpi["age_hours"]) == ("ok", "2026-09-24T19:10:00Z", 2.0)


def test_the_poll_cadence_is_the_daily_fetch_timers(tmp_path):
    timer = (ROOT / "deploy/econdelta-fetch.timer").read_text()
    assert "OnCalendar=*-*-* 19:10:00 UTC" in timer          # once a day, 01:10 BDT
    assert poll.POLL_CADENCE_HOURS == {"cpi_upstream_poll": 26, "m2_upstream_poll": 26}


def test_a_missing_receipt_is_never_ok_and_keeps_fix_5s_reason(data_root):
    (data_root / "upstream_polls" / "non_food_inflation.json").unlink()
    # No trace of that source at all: neither a poll receipt nor a database reread.
    database = {mid: period for mid, period in CAUGHT_UP.items() if mid != "cpi_p2p_nonfood_monthly"}

    cpi = _families(data_root, database=database)["cpi_upstream_poll"]
    assert cpi["status"] == "missing"
    assert cpi["error"] == ("CPI upstream source-poll liveness not established: no readable "
                            "source-poll receipt (cpi_p2p_nonfood_monthly)")


@pytest.mark.parametrize("text", ["{not json", json.dumps({"receipt_kind": "upstream-poll", "status": "ok",
                                                           "checked_at": 1727222160})],
                         ids=["corrupt", "numeric-checked-at"])
def test_an_unreadable_poll_receipt_is_missing_never_ok_and_never_raises(data_root, text):
    (data_root / "upstream_polls" / "m2_growth_yoy_pct.json").write_text(text)

    m2 = _families(data_root)["m2_upstream_poll"]
    assert m2["status"] == "missing"
    assert m2["error"].endswith("unreadable upstream-poll receipt (m2_growth_yoy_monthly)")


@pytest.mark.parametrize("damage", [b"\xff\xfe{\"status\": \"ok\"}", b"[" * 100_000 + b"]" * 100_000],
                         ids=["not-utf8", "deeply-nested"])
def test_a_damaged_poll_receipt_file_reads_missing_and_never_crashes_the_aggregate(data_root, damage):
    """read_polls runs before write_latest: one bad file must not cost the night's snapshot."""
    (data_root / "upstream_polls" / "m2_growth_yoy_pct.json").write_bytes(damage)

    m2 = _families(data_root)["m2_upstream_poll"]
    assert m2["status"] == "missing"
    assert m2["error"].endswith("unreadable upstream-poll receipt (m2_growth_yoy_monthly)")


@pytest.mark.parametrize("damage", [b"\xff\xfe{", b"[" * 100_000 + b"]" * 100_000],
                         ids=["not-utf8", "deeply-nested"])
def test_a_new_poll_replaces_a_damaged_receipt(data_root, damage):
    """The next poll writes a fresh receipt over a damaged one, so it cannot stick for good."""
    path = data_root / "upstream_polls" / "m2_growth_yoy_pct.json"
    path.write_bytes(damage)

    poll.record_upstream_poll("m2_growth_yoy_pct", status="failed", checked_at=READ_TIME,
                              source_url="https://www.bb.org.bd/econdata/moneysupply.php",
                              latest_source_vintage=None, reason="fetch failed (FetchError)",
                              directory=path.parent)

    receipt = _receipt(data_root, "m2_growth_yoy_pct")
    assert (receipt["status"], receipt["last_success_at"]) == ("failed", None)


# ── A live poll is healthy lag only when our pipeline holds the period the page states ──

@pytest.mark.parametrize("behind, recorded, family", [
    ("cpi_12m_avg_monthly", "2026-06-30", "cpi_upstream_poll"),    # BB HTML page states July
    ("cpi_p2p_food_monthly", "2026-05-31", "cpi_upstream_poll"),   # MEI report states June
    ("m2_growth_yoy_monthly", "2026-05-31", "m2_upstream_poll"),   # BB HTML page states June
], ids=["general-html", "food-mei-pdf", "m2-html"])
def test_a_live_poll_of_a_page_newer_than_our_recorded_month_is_not_healthy_lag(
        data_root, behind, recorded, family):
    """BB states a newer period than our own table holds: 'no newer database vintage' is then
    our pipeline lagging the source (e.g. an MEI value the model failed to extract), not BB's
    publication lag, so the family must not read ok (fix-8 review; controller ruling (2))."""
    families = _families(data_root, database={**CAUGHT_UP, behind: recorded})

    other = next(key for key in LIVE if key != family)
    assert families[other] == LIVE[other]
    entry = families[family]
    assert entry["status"] == "stale"
    label = "CPI" if family == "cpi_upstream_poll" else "M2"
    page = CAUGHT_UP[behind]
    assert entry["error"] == (
        f"{label} upstream source-poll liveness not established: source states a newer period "
        f"than we have recorded (page {page}, recorded {recorded}) ({behind})")
    # The poll itself did answer: when it last did stays on record.
    assert (entry["last_success"], entry["age_hours"]) == ("2026-09-24T19:10:00Z", 1.75)


def test_a_live_poll_with_no_recorded_month_to_compare_is_not_ok(data_root):
    """Unknown stays unknown: without the monthly legs' reread nobody knows we are caught up."""
    families = _families(data_root, database={})

    assert {key: entry["status"] for key, entry in families.items()} == {
        "cpi_upstream_poll": "missing", "m2_upstream_poll": "missing"}
    assert families["m2_upstream_poll"]["error"] == (
        "M2 upstream source-poll liveness not established: no recorded month to compare the "
        "source's period with (m2_growth_yoy_monthly)")


def test_the_poll_sources_are_the_ids_the_monthly_legs_derive_from():
    assert poll.UPSTREAM_POLL_SOURCES == {
        **{monthly: daily for daily, monthly in agg._CPI_DAILY_TO_MONTHLY.items()},
        agg._M2_MONTHLY_ID: agg._M2_DAILY_ID}
    assert set(poll.UPSTREAM_POLL_SOURCES) == {
        mid for ids in evidence.UPSTREAM_FAMILIES.values() for mid in ids}
    assert set(PAGES) == set(poll.UPSTREAM_POLL_SOURCES.values())


# ── End to end: the database rereads cannot overwrite the poll receipts ─────────────────

def _run_main(tmp_path: Path, monkeypatch, data_dir: Path) -> dict:
    """aggregate_latest.main with the daily write confirmed and the CPI/M2 legs' database
    reread succeeding (fix 5's harness; every network/database edge faked)."""
    for name in ("STALENESS_STATE_PATH", "WATCHLIST_STALENESS_STATE_PATH",
                 "STALE_FALLBACK_ALERT_STATE_PATH"):
        monkeypatch.setattr(agg, name, tmp_path / "state" / f"{name}.json")
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    monkeypatch.setattr(agg, "LATEST_PATH", data_dir / "latest.json")
    monkeypatch.setattr(agg, "ARCHIVE_DIR", data_dir / "archive")
    monkeypatch.setenv("ECONDELTA_DRY_RUN", "1")
    monkeypatch.setattr(agg, "_derive_daily_yields_from_auctions", lambda **kw: ({}, {}))
    monkeypatch.setattr(agg, "_apply_media_overrides", lambda *a, **kw: {"written": [], "failures": []})
    monkeypatch.setattr(agg, "_write_reserves_monthly_split", lambda *a: 0)
    monkeypatch.setattr(sw, "upsert_metric_definitions_seed", lambda *a, **kw: 0)
    monkeypatch.setattr(sw, "upsert_metric_history", lambda **kw: 1)
    monkeypatch.setattr(sw, "verify_landed_count", lambda *a, **kw: True)
    assert agg.main() == 0
    return json.loads((data_dir / "latest.json").read_text())


def test_database_rereads_cannot_overwrite_the_poll_receipts_and_a_live_poll_reads_ok(
        world, tmp_path, monkeypatch):  # noqa: F811
    table, sent = world
    _daily_scrapers_stuck_at_july(table, monkeypatch)
    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    polled_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=105)
    _poll(tmp_path, monkeypatch, data_dir, polled_at=polled_at)
    before = {p.name: p.read_bytes() for p in (data_dir / "upstream_polls").iterdir()}

    payload = _run_main(tmp_path, monkeypatch, data_dir)

    # The CPI/M2 legs did reread our own table and wrote their database receipts...
    for family in CONTRACT["families"].values():
        for mid in family["metric_ids"]:
            receipt = json.loads((evidence.DEFAULT_DIRECTORY / f"{mid}.json").read_text())
            assert receipt["evidence_kind"] == "database-observations", mid
    # ...into their own namespace: every poll receipt is byte-for-byte what the fetch wrote.
    assert {p.name: p.read_bytes() for p in (data_dir / "upstream_polls").iterdir()} == before
    assert len(before) == len(PAGES)
    # So the snapshot tells The Brief both families were polled live.
    for key in LIVE:
        entry = payload["sources_status"][key]
        assert (entry["status"], entry["error"], entry["url"]) == ("ok", None, None), key
        assert datetime.fromisoformat(entry["last_success"]) == polled_at, key
        assert entry["age_hours"] == pytest.approx(1.75, abs=0.05), key
    # Still no new alert names the families (decision j).
    assert not [call for call in sent if any(key in str(call) for key in LIVE)]


def _find_leg(receipt: dict, name: str) -> dict | None:
    """The named leg anywhere in a nested monthly write receipt."""
    legs = receipt.get("legs") or {}
    if name in legs:
        return legs[name]
    return next((found for child in legs.values() if (found := _find_leg(child, name))), None)


def _daily_table_stuck_at_june(table, monkeypatch) -> None:
    """Every daily CPI/M2 reading we hold stopped at 30 Jun and June is already recorded
    (synthetic values), while BB's live CPI page already states July."""
    stuck = {mid: (value, "2026-06-30") for mid, (value, _) in _DAILY.items()}
    table.rows = [_row(mid, "2026-06-01", stuck[daily][0])
                  for daily, mid in agg._CPI_DAILY_TO_MONTHLY.items()]
    table.rows += [_row("m2_growth_yoy_monthly", "2026-06-01", 8.10),
                   _row("remittance_usd_mn_monthly", "2026-08-01", 2400.0),
                   _row("imports_usd_mn_monthly", "2026-08-01", 6300.0)]
    monkeypatch.setattr(reader, "get_metric_history", lambda mid, *, days, **kw: (
        [{"metric_id": mid, "value": stuck[mid][0], "as_of": stuck[mid][1]}] if mid in stuck else []))
    monkeypatch.setattr(agg, "_fetch_imports_mei_pdf", _raise(RuntimeError("must not be fetched")))
    monkeypatch.setattr(agg, "_write_macro_monthly_append", functools.partial(
        agg._write_macro_monthly_append.func, today=date(2026, 9, 25)))


def test_the_aggregate_never_publishes_ok_while_the_live_page_is_ahead_of_our_table(
        world, tmp_path, monkeypatch):  # noqa: F811
    table, sent = world
    _daily_table_stuck_at_june(table, monkeypatch)
    data_dir, cfg_path = _build_data_tree(tmp_path)
    monkeypatch.setattr(agg, "CONFIG_PATH", cfg_path)
    polled_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=105)
    _poll(tmp_path, monkeypatch, data_dir, polled_at=polled_at)

    payload = _run_main(tmp_path, monkeypatch, data_dir)

    # The CPI legs found only June, already recorded: "no newer database vintage"...
    cpi_leg = _find_leg(payload["write_status"]["monthly"], "cpi")
    assert {s["category"] for s in cpi_leg["skips"]} == {"no newer database vintage"}
    # ...while BB's page states July, so CPI is our pipeline lagging, never healthy lag.
    cpi = payload["sources_status"]["cpi_upstream_poll"]
    assert cpi["status"] == "stale"
    assert cpi["error"] == ("CPI upstream source-poll liveness not established: source states a "
                            "newer period than we have recorded (page 2026-07-31, recorded "
                            "2026-06-30) (cpi_12m_avg_monthly)")
    # M2's page states June, which we hold: that family is live and caught up.
    assert (payload["sources_status"]["m2_upstream_poll"]["status"],
            payload["sources_status"]["m2_upstream_poll"]["error"]) == ("ok", None)
    # Still no new alert names the families (decision j).
    assert not [call for call in sent if any(key in str(call) for key in LIVE)]
