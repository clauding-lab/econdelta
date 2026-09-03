"""The stale fallback must eventually say something.

When today's snapshot is bad, `_build_v3_blocks` republishes the most recent
good one so the brief shows a real number instead of a 0. That is the right
default for a source that is a day late. It is the wrong default for a source
that is GONE, and nothing in the pipeline could tell the two apart: the
fallback logged at INFO, the bundle looked healthy, freshness counted the
indicator as stale among dozens of others, and The Brief printed the held-over
figure with no marking of its age.

`treasury_bill_outstanding` is what that costs. BB retired the page around
30 June; every fetch after that failed; the fallback republished the 30 June
reading for sixty days — through a real move from ~20,60,000 to ~22,10,000 —
until the 60-day window expired and the indicator simply vanished from the
bundle. The Brief ran roughly 14,000 crore (6.8%) light for two months and no
alert fired at any point.

These tests pin the alarm: past `STALE_FALLBACK_ALERT_DAYS`, a held-over
reading raises an Alert carrying its age and sends one Discord message per
run. The bundle still publishes — a stale number beats no number — but the
silence is over.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import aggregate_latest as agg
from fetchers.dated_form import DEFAULT_MAX_LOOKBACK_DAYS

INDICATOR = "treasury_bill_outstanding"


def _snapshot(day: datetime, value: float | None, *, bad: bool = False) -> dict:
    snap = {
        "indicator_id": INDICATOR,
        "domain": "money_market",
        "cadence": "monthly",
        "scraped_at": day.isoformat(),
        "source_url": "https://gsom.bb.org.bd/index.php/tbill",
        "value": value,
        "value_type": "amount_bdt_mn",
        "previous_value": None,
        "change_pct": None,
        "_provenance": "needs_review" if bad else "deterministic",
        "_parse_strategy": "extract_failed" if bad else "gsom_total_row",
    }
    return snap


def _registry(tmp_path: Path) -> Path:
    p = tmp_path / "sources-v3.json"
    p.write_text(
        json.dumps(
            {
                "indicators": [
                    {"id": INDICATOR, "domain": "money_market", "cadence": "monthly"}
                ]
            }
        )
    )
    return p


def _write_history(data_dir: Path, *, now: datetime, good_age_days: int) -> None:
    """One good snapshot `good_age_days` back, and a bad one for today."""
    d = data_dir / INDICATOR
    d.mkdir(parents=True)
    good_day = now - timedelta(days=good_age_days)
    (d / f"{good_day:%Y-%m-%d}.json").write_text(
        json.dumps(_snapshot(good_day, 2_070_000.0))
    )
    (d / f"{now:%Y-%m-%d}.json").write_text(json.dumps(_snapshot(now, 0.0, bad=True)))


def _run(tmp_path, monkeypatch, *, good_age_days: int):
    now = datetime(2026, 9, 3, 20, 55, tzinfo=timezone.utc)
    data_dir = tmp_path / "data"
    _write_history(data_dir, now=now, good_age_days=good_age_days)
    monkeypatch.setattr(agg, "SOURCES_V3_PATH", _registry(tmp_path))
    monkeypatch.setattr(agg, "DATA_DIR", data_dir)
    data_additions, _domains, _freshness, alerts = agg._build_v3_blocks(now)
    return data_additions, alerts


NOW = datetime(2026, 9, 3, 20, 55, tzinfo=timezone.utc)


def _run_date_form(
    tmp_path,
    monkeypatch,
    *,
    source_as_of: str | None = None,
    source_as_of_age_days: int | None = None,
    max_lookback_days: int | None = None,
    date_form: bool = True,
):
    """A GOOD snapshot written today — the blind-spot shape.

    This is what the parse stage produces when the fetch raised and it fell
    back to re-parsing yesterday's artifact: real value, provenance
    `deterministic`, `scraped_at` of today. Nothing but `source_as_of`
    distinguishes it from a healthy one.
    """
    data_dir = tmp_path / INDICATOR
    data_dir.mkdir(parents=True)
    snap = _snapshot(NOW, 2_070_000.0)
    if source_as_of_age_days is not None:
        source_as_of = f"{NOW - timedelta(days=source_as_of_age_days):%Y-%m-%d}"
    if source_as_of is not None:
        snap["source_as_of"] = source_as_of
    (data_dir / f"{NOW:%Y-%m-%d}.json").write_text(json.dumps(snap))

    fetch: dict = {"type": "html", "url": "https://gsom.bb.org.bd/index.php/tbill"}
    if date_form:
        fetch["date_form"] = {"field": "picker_date", "format": "%d-%b-%y"}
        if max_lookback_days is not None:
            fetch["date_form"]["max_lookback_days"] = max_lookback_days
    reg = tmp_path / "sources-v3.json"
    reg.write_text(
        json.dumps(
            {
                "indicators": [
                    {
                        "id": INDICATOR,
                        "domain": "money_market",
                        "cadence": "monthly",
                        "fetch": fetch,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(agg, "SOURCES_V3_PATH", reg)
    monkeypatch.setattr(agg, "DATA_DIR", tmp_path)
    return agg._build_v3_blocks(NOW)


class TestAgeArithmetic:
    def test_the_age_comes_off_the_snapshots_own_stamp(self):
        snap = {"_stale_from": "2026-06-30"}
        assert agg._stale_fallback_age_days(snap, datetime(2026, 8, 29).date()) == 60

    @pytest.mark.parametrize(
        "stamp", [None, "", "latest", "2026-13-01", 20260630, {"d": 1}]
    )
    def test_an_unusable_stamp_yields_no_age_rather_than_a_wrong_one(self, stamp):
        """The alarm must never fire on a number it cannot justify — a
        false 'stale for 19000 days' would teach us to ignore it."""
        assert agg._stale_fallback_age_days({"_stale_from": stamp}, datetime(2026, 8, 29).date()) is None

    def test_a_snapshot_that_is_not_a_fallback_has_no_age(self):
        assert agg._stale_fallback_age_days({}, datetime(2026, 8, 29).date()) is None


class TestTheAlarmFires:
    def test_a_two_month_holdover_raises_an_alert(self, tmp_path, monkeypatch):
        """The treasury_bill case, replayed at the aggregate."""
        _data, alerts = _run(tmp_path, monkeypatch, good_age_days=60)
        stale = [a for a in alerts if a.type == "stale_fallback"]
        assert len(stale) == 1
        assert stale[0].indicator_id == INDICATOR
        assert stale[0].severity == "error"
        assert stale[0].age_days == 60

    def test_the_alert_carries_the_value_being_republished(self, tmp_path, monkeypatch):
        """Naming the figure is what makes the Discord message actionable —
        it is the number sitting in this morning's brief."""
        _data, alerts = _run(tmp_path, monkeypatch, good_age_days=60)
        assert alerts[0].value == 2_070_000.0

    def test_the_indicator_still_publishes(self, tmp_path, monkeypatch):
        """The alarm reports; it does not withhold. A held-over number is
        still better than a hole in the bundle — and dropping it here would
        hand Opus a missing field, which is a whole-run hard reject."""
        data, _alerts = _run(tmp_path, monkeypatch, good_age_days=60)
        assert data[INDICATOR] == 2_070_000.0

    def test_it_fires_exactly_at_the_threshold(self, tmp_path, monkeypatch):
        _data, alerts = _run(
            tmp_path, monkeypatch, good_age_days=agg.STALE_FALLBACK_ALERT_DAYS
        )
        assert [a.type for a in alerts] == ["stale_fallback"]


class TestTheAlarmStaysQuiet:
    def test_a_late_source_is_not_an_alarm(self, tmp_path, monkeypatch):
        """One or two days is an ordinary late publication. Alerting on
        that would bury the real signal in noise within a week."""
        _data, alerts = _run(tmp_path, monkeypatch, good_age_days=1)
        assert [a for a in alerts if a.type == "stale_fallback"] == []

    def test_the_day_before_the_threshold_is_silent(self, tmp_path, monkeypatch):
        _data, alerts = _run(
            tmp_path, monkeypatch, good_age_days=agg.STALE_FALLBACK_ALERT_DAYS - 1
        )
        assert [a for a in alerts if a.type == "stale_fallback"] == []

    def test_a_healthy_indicator_raises_nothing(self, tmp_path, monkeypatch):
        now = datetime(2026, 9, 3, 20, 55, tzinfo=timezone.utc)
        data_dir = tmp_path / "data"
        d = data_dir / INDICATOR
        d.mkdir(parents=True)
        (d / f"{now:%Y-%m-%d}.json").write_text(
            json.dumps(_snapshot(now, 2_210_000.0))
        )
        monkeypatch.setattr(agg, "SOURCES_V3_PATH", _registry(tmp_path))
        monkeypatch.setattr(agg, "DATA_DIR", data_dir)
        _add, _dom, _fresh, alerts = agg._build_v3_blocks(now)
        assert alerts == []

    @pytest.mark.parametrize("walked_back", [7, 8, 9, 10])
    def test_a_successful_holiday_walk_does_not_alarm(self, tmp_path, monkeypatch, walked_back):
        """The Eid case, end to end — and the finding that the first version
        of this file missed by asserting only that a constant sat in a range.

        The date_form walk goes back `max_lookback_days` (10), so a long
        closure ends in a SUCCESSFUL fetch publishing an 8- to 10-day-old
        figure: the freshest one that exists. Eid closures run 5-6 days and
        are bracketed by the Fri/Sat weekend either side, so this is the
        normal shape of Eid, twice a year. With the threshold at a flat 7 —
        as it originally was — every one of those nights paged while nothing
        was wrong. The fetcher must not be able to succeed into its own alarm.
        """
        _add, _dom, _fresh, alerts = _run_date_form(
            tmp_path, monkeypatch, source_as_of_age_days=walked_back, max_lookback_days=10
        )
        assert alerts == []

    def test_every_configured_walk_length_is_silent(self, tmp_path, monkeypatch):
        """The same invariant, read off the real config rather than a literal,
        so widening a lookback without widening the alarm fails here."""
        repo_root = Path(__file__).resolve().parent.parent
        cfg = json.loads((repo_root / "config" / "sources-v3.json").read_text())
        windows = {
            ind["fetch"]["date_form"].get("max_lookback_days", DEFAULT_MAX_LOOKBACK_DAYS)
            for ind in cfg["indicators"]
            if ind["fetch"].get("date_form")
        }
        assert windows, "no date_form indicators — this test is guarding nothing"
        for window in sorted(windows):
            _add, _dom, _fresh, alerts = _run_date_form(
                tmp_path / f"w{window}",
                monkeypatch,
                source_as_of_age_days=window,
                max_lookback_days=window,
            )
            assert alerts == [], f"a full {window}-day walk should not alarm"

    def test_a_freeze_past_the_walk_still_alarms(self, tmp_path, monkeypatch):
        """One day past the walk plus grace: the fetch can no longer explain
        the age, so this is a freeze, not a holiday. Without this the wider
        threshold would just be a way of never alarming."""
        age = 10 + agg.DATE_FORM_STALE_GRACE_DAYS
        _add, _dom, _fresh, alerts = _run_date_form(
            tmp_path, monkeypatch, source_as_of_age_days=age, max_lookback_days=10
        )
        assert [a.type for a in alerts] == ["stale_fallback"]
        assert alerts[0].age_days == age


class TestTheBlindSpotBehindTheFallback:
    """A fetch that RAISES leaves no artifact for today, so the parse stage
    globs the directory, picks yesterday's page and re-parses it. The
    resulting snapshot is indistinguishable from a healthy one — real value,
    `_provenance=deterministic`, `scraped_at` of today — so the stale
    fallback never runs and the alarm above never sees it.

    `source_as_of` is the only field that still tells the truth, and only a
    parser that recovers it can supply one. For a `date_form` source it is
    the day the page itself answered for, so it is exact.
    """

    _run = staticmethod(_run_date_form)

    def test_a_healthy_looking_snapshot_with_an_old_source_date_still_alarms(
        self, tmp_path, monkeypatch
    ):
        """THE regression. Everything about this snapshot says fresh except
        the day the source says it describes."""
        _add, _dom, freshness, alerts = self._run(
            tmp_path, monkeypatch, source_as_of="2026-06-30"
        )
        assert freshness.indicators_failed == 0  # nothing else noticed
        assert [(a.indicator_id, a.age_days) for a in alerts] == [(INDICATOR, 65)]

    def test_a_current_source_date_is_silent(self, tmp_path, monkeypatch):
        """The normal case: the walk answered for yesterday."""
        _add, _dom, _fresh, alerts = self._run(
            tmp_path, monkeypatch, source_as_of="2026-09-02"
        )
        assert alerts == []

    def test_a_missing_source_date_is_itself_the_alarm(self, tmp_path, monkeypatch):
        """The hole the first version of this alarm left open, and the one
        that would have reproduced the whole 60-day bug.

        `_iso_age_days` returns None for a missing stamp, so "no date" used to
        mean "no alarm, ever" — while every other sensor also stayed quiet:
        the fallback never runs (the snapshot looks healthy), the fetch floor
        needs 50% of 64 sources down, and the monthly unchanged-budget is 75
        days. A date_form page states its own date on EVERY render — that
        echoed date is what the fetcher steers by — so losing it means the
        page has been reshaped and the POST is no longer asking for the day
        it thinks. That is the alarm, not an excuse to skip it.
        """
        _add, _dom, _fresh, alerts = self._run(tmp_path, monkeypatch, source_as_of=None)
        assert [a.type for a in alerts] == ["undated_source"]
        assert alerts[0].severity == "error"
        assert alerts[0].value == 2_070_000.0
        assert alerts[0].age_days is None

    def test_an_undated_indicator_without_a_date_form_is_still_left_alone(
        self, tmp_path, monkeypatch
    ):
        """Most of the registry legitimately has no `source_as_of`; only a
        date_form source is promised to carry one."""
        _add, _dom, _fresh, alerts = self._run(
            tmp_path, monkeypatch, source_as_of=None, date_form=False
        )
        assert alerts == []

    def test_indicators_without_a_date_form_are_not_judged_this_way(
        self, tmp_path, monkeypatch
    ):
        """A monthly PDF publication legitimately carries a `source_as_of`
        weeks old — that is the reporting period, not staleness. Applying
        this check registry-wide would alarm on most of it."""
        _add, _dom, _fresh, alerts = self._run(
            tmp_path, monkeypatch, source_as_of="2026-06-30", date_form=False
        )
        assert alerts == []

    def test_the_two_paths_do_not_double_alert(self, tmp_path, monkeypatch):
        """A held-over reading recovered by the fallback also carries an old
        `source_as_of`. One indicator, one alert."""
        now = datetime(2026, 9, 3, 20, 55, tzinfo=timezone.utc)
        d = tmp_path / INDICATOR
        d.mkdir(parents=True)
        good_day = now - timedelta(days=60)
        good = _snapshot(good_day, 2_070_000.0)
        good["source_as_of"] = f"{good_day:%Y-%m-%d}"
        (d / f"{good_day:%Y-%m-%d}.json").write_text(json.dumps(good))
        (d / f"{now:%Y-%m-%d}.json").write_text(json.dumps(_snapshot(now, 0.0, bad=True)))

        reg = tmp_path / "sources-v3.json"
        reg.write_text(
            json.dumps(
                {
                    "indicators": [
                        {
                            "id": INDICATOR,
                            "domain": "money_market",
                            "cadence": "monthly",
                            "fetch": {
                                "type": "html",
                                "url": "x",
                                "date_form": {"field": "picker_date", "format": "%d-%b-%y"},
                            },
                        }
                    ]
                }
            )
        )
        monkeypatch.setattr(agg, "SOURCES_V3_PATH", reg)
        monkeypatch.setattr(agg, "DATA_DIR", tmp_path)
        _add, _dom, _fresh, alerts = agg._build_v3_blocks(now)
        assert len(alerts) == 1


class TestTheDiscordMessage:
    """An Alert inside `latest.json` is not a signal anyone reads — nothing
    downstream consumes the bundle's `alerts` list. The Discord message is
    the part a human sees, so it has to actually be sent, and it has to name
    the indicator and its age."""

    @staticmethod
    def _capture(monkeypatch, tmp_path, *, delivered: bool = True) -> list[tuple]:
        sent: list[tuple] = []

        def fake_notify(level, title, message, *a, **k):
            sent.append((level, title, message))
            return delivered

        monkeypatch.setattr(agg, "notify", fake_notify)
        # Never let a test touch the repo's real dedup state file — and give
        # each test a clean one, or they would suppress each other.
        monkeypatch.setattr(
            agg, "STALE_FALLBACK_ALERT_STATE_PATH", tmp_path / "alert_state.json"
        )
        return sent

    def test_one_message_names_every_long_stale_indicator(self, monkeypatch, tmp_path):
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        assert agg._notify_long_stale_fallbacks(
            [
                Alert(indicator_id="a", type="stale_fallback", severity="error",
                      value=1.0, age_days=9),
                Alert(indicator_id="b", type="stale_fallback", severity="error",
                      value=2.0, age_days=60),
                Alert(indicator_id="c", type="anomaly", severity="warn", change_pct=0.3),
            ]
        )
        assert len(sent) == 1
        level, _title, message = sent[0]
        assert level == "error"
        assert "a" in message and "b" in message
        assert "60d" in message and "9d" in message

    def test_the_oldest_holdover_leads(self, monkeypatch, tmp_path):
        """The 60-day one is a dead source; the 9-day one may still be a
        late source. Read the worst first."""
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        agg._notify_long_stale_fallbacks(
            [
                Alert(indicator_id="recent", type="stale_fallback", severity="error",
                      age_days=9),
                Alert(indicator_id="ancient", type="stale_fallback", severity="error",
                      age_days=60),
            ]
        )
        message = sent[0][2]
        assert message.index("ancient") < message.index("recent")

    def test_a_clean_run_sends_nothing(self, monkeypatch, tmp_path):
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        assert (
            agg._notify_long_stale_fallbacks(
                [Alert(indicator_id="c", type="anomaly", severity="warn", change_pct=0.3)]
            )
            is False
        )
        assert sent == []

    def test_the_same_freeze_pages_once_a_night_not_once_a_run(
        self, monkeypatch, tmp_path
    ):
        """The finding that the first version of this file could not have
        caught, because every test here monkeypatches `notify` and none of
        them ran the helper twice.

        The aggregate runs at least TWICE a night unconditionally
        (aggregate.timer 20:55 UTC, aggregate-retry.timer 21:15 UTC — the
        retry has its own OnCalendar and does not check whether the first run
        succeeded), plus up to two more from `Restart=on-failure`, and this
        helper is called BEFORE the Opus review so a hard-reject run reaches
        it too. Every one is a fresh process, so utils.notifier's in-memory
        (level, title) dedup — which dies with the process — cannot collapse
        them. 2-6 identical red messages a night is how an alert gets muted.
        """
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        alerts = [
            Alert(indicator_id="a", type="stale_fallback", severity="error", age_days=60)
        ]
        assert agg._notify_long_stale_fallbacks(alerts, today=date(2026, 9, 3)) is True
        assert agg._notify_long_stale_fallbacks(alerts, today=date(2026, 9, 3)) is False
        assert agg._notify_long_stale_fallbacks(alerts, today=date(2026, 9, 3)) is False
        assert len(sent) == 1

    def test_a_still_broken_source_pages_again_the_next_day(self, monkeypatch, tmp_path):
        """Deduping per day, not forever — a freeze that is still there
        tomorrow is still news."""
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        alerts = [
            Alert(indicator_id="a", type="stale_fallback", severity="error", age_days=60)
        ]
        agg._notify_long_stale_fallbacks(alerts, today=date(2026, 9, 3))
        agg._notify_long_stale_fallbacks(alerts, today=date(2026, 9, 4))
        assert len(sent) == 2

    def test_a_newly_broken_indicator_gets_through_the_same_day(
        self, monkeypatch, tmp_path
    ):
        """The dedup key carries the indicator set, so the second run of a
        night still pages if something NEW has gone stale since the first."""
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        first = [
            Alert(indicator_id="a", type="stale_fallback", severity="error", age_days=60)
        ]
        second = first + [
            Alert(indicator_id="b", type="stale_fallback", severity="error", age_days=8)
        ]
        agg._notify_long_stale_fallbacks(first, today=date(2026, 9, 3))
        agg._notify_long_stale_fallbacks(second, today=date(2026, 9, 3))
        assert len(sent) == 2
        assert "b" in sent[1][2]

    def test_the_title_carries_no_count(self, monkeypatch, tmp_path):
        """A count in the title makes every change in the set look like a
        different alert — to the notifier's own dedup, and to a human
        scanning the channel."""
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        agg._notify_long_stale_fallbacks(
            [
                Alert(indicator_id="a", type="stale_fallback", severity="error", age_days=60),
                Alert(indicator_id="b", type="stale_fallback", severity="error", age_days=8),
            ],
            today=date(2026, 9, 3),
        )
        title = sent[0][1]
        assert not any(ch.isdigit() for ch in title)

    def test_an_undated_source_is_reported_too(self, monkeypatch, tmp_path):
        """It has no age, so it must not be described as "None days old"."""
        from utils.schema import Alert

        sent = self._capture(monkeypatch, tmp_path)
        assert agg._notify_long_stale_fallbacks(
            [Alert(indicator_id="a", type="undated_source", severity="error", value=7.0)],
            today=date(2026, 9, 3),
        )
        message = sent[0][2]
        assert "a" in message
        assert "None" not in message

    def test_a_dropped_webhook_is_logged_rather_than_swallowed(
        self, monkeypatch, tmp_path, caplog
    ):
        """`notify()` returns False for a missing webhook URL or an HTTP
        failure, and the dedup state has already recorded the attempt — so
        without a log line a dropped message leaves no trace anywhere."""
        from utils.schema import Alert

        self._capture(monkeypatch, tmp_path, delivered=False)
        with caplog.at_level(logging.ERROR):
            assert (
                agg._notify_long_stale_fallbacks(
                    [
                        Alert(
                            indicator_id="tbill",
                            type="stale_fallback",
                            severity="error",
                            age_days=60,
                        )
                    ],
                    today=date(2026, 9, 3),
                )
                is False
            )
        assert "NOT delivered" in caplog.text
        assert "tbill" in caplog.text

    def test_main_wires_the_alarm_to_the_alerts_it_built(self):
        """The helper is only worth anything if `main()` calls it. Pin the
        call site by source, since running the full aggregate here would
        need the whole data tree."""
        import inspect

        src = inspect.getsource(agg.main)
        assert "_notify_long_stale_fallbacks(alerts)" in src


class TestSchemaCompatibility:
    def test_age_days_is_optional_so_existing_alerts_are_unchanged(self):
        """Anomaly alerts predate this field and are constructed without
        it; a required field would have failed every one of them at
        bundle-validation time — i.e. no bundle would publish at all."""
        from utils.schema import Alert

        a = Alert(indicator_id="x", type="anomaly", severity="warn", change_pct=0.2)
        assert a.age_days is None

    def test_the_bundle_validates_with_a_stale_fallback_alert(self, tmp_path, monkeypatch):
        """`Alert` is `extra="forbid"`; if `age_days` were not declared on
        the model, this alert would raise and take down the whole
        aggregate — a louder failure than the silence it replaces."""
        from utils.schema import Alert

        _data, alerts = _run(tmp_path, monkeypatch, good_age_days=60)
        assert all(isinstance(a, Alert) for a in alerts)
        # round-trips through the JSON the bundle is written as
        assert json.loads(alerts[0].model_dump_json())["age_days"] == 60
