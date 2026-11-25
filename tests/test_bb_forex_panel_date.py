"""Trading-date recovery for USD/BDT from BB's homepage inter-bank panel.

The 01:00 BDT chain scrapes the MORNING AFTER the trading day, so dating the
WAR by scrape day put 23 Nov's Tk 124.75 on 24 Nov (The Brief #288) and
22 Nov's 124.73 on 23 Nov (#287). Fixtures are real archived homepages.
"""
from datetime import date, datetime, timezone
from pathlib import Path

from scrapers.bb_forex import parse_interbank_panel, rates_observation_date

FIX = Path(__file__).parent / "fixtures"
_SCRAPE = date(2026, 11, 24)


def _html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_real_weekday_panel():
    assert parse_interbank_panel(_html("bb_homepage_fx_2026-11-23.html")) == (124.75, date(2026, 11, 23))


def test_real_weekend_panel_keeps_thursday():
    # Fetched Sat 21 Nov 01:13 BDT: Fri/Sat are non-working, so Thursday.
    assert parse_interbank_panel(_html("bb_homepage_fx_2026-11-20.html")) == (124.75, date(2026, 11, 19))


def test_matching_war_gives_panel_date():
    assert rates_observation_date((124.75, date(2026, 11, 23)), 124.75, _SCRAPE) == date(2026, 11, 23)


def test_mismatched_war_refuses_date():
    # Panel already moved to the next day while the rates page has not (or vice versa).
    assert rates_observation_date((124.73, date(2026, 11, 22)), 124.75, _SCRAPE) is None


def test_missing_panel_refuses_date():
    assert parse_interbank_panel("<html><body>nothing</body></html>") is None
    assert rates_observation_date(None, 124.75, _SCRAPE) is None


def test_panel_without_stamp_refuses():
    html = ('<div class="exchange"><div class="display_table"><div><div>USD</div>'
            '<div>1</div><div>1</div><div>124.75</div></div></div></div>')
    assert parse_interbank_panel(html) is None


def test_stale_stamp_refuses_even_when_war_matches():
    # Landmine 39(c): stamp months old while a flat WAR still matches.
    assert rates_observation_date((124.75, date(2026, 2, 15)), 124.75, _SCRAPE) is None


def test_future_stamp_refuses():
    assert rates_observation_date((124.75, date(2026, 11, 25)), 124.75, _SCRAPE) is None


def test_long_holiday_gap_still_accepted():
    # Eid-length closure: 10 days back is the edge, inclusive.
    assert rates_observation_date((124.75, date(2026, 11, 14)), 124.75, _SCRAPE) == date(2026, 11, 14)
    assert rates_observation_date((124.75, date(2026, 11, 13)), 124.75, _SCRAPE) is None


def _panel(header, usd, stamp="Last update: 23rd November, 2026"):
    def row(cs):
        return "<div>" + "".join(f"<div>{c}</div>" for c in cs) + "</div>"
    return (f'<div class="exchange"><div class="display_table">{row(header)}{row(usd)}</div>'
            f'<p class="last_update">{stamp}</p></div>')


def test_war_found_by_header_not_position():
    html = _panel(["Currency", "WAR", "Highest", "Lowest"], ["USD", "124.75", "125.10", "124.10"])
    assert parse_interbank_panel(html) == (124.75, date(2026, 11, 23))


def test_no_war_header_refuses():
    html = _panel(["Currency", "Highest", "Lowest", "Close"], ["USD", "125.10", "124.10", "124.75"])
    assert parse_interbank_panel(html) is None


def test_usd_row_must_match_exactly():
    html = _panel(["Currency", "Highest", "Lowest", "WAR"], ["USD (Spot)", "1", "1", "124.75"])
    assert parse_interbank_panel(html) is None


def test_stamp_without_space_before_year():
    html = _panel(["Currency", "Highest", "Lowest", "WAR"], ["USD", "1", "1", "124.75"],
                  stamp="Last update: 23rd November,2026")
    assert parse_interbank_panel(html) == (124.75, date(2026, 11, 23))


def test_aggregate_prefers_rates_as_of():
    import aggregate_latest as agg
    from utils.schema import ForexRates, ForexSnapshot

    rates = ForexRates(usd_bdt_mid=124.75, usd_bdt_buy=124.7, usd_bdt_sell=124.8,
                       eur_bdt=140.0, gbp_bdt=160.0, source_url="x")
    snap = ForexSnapshot(date=date(2026, 11, 24), scraped_at=datetime.now(timezone.utc),
                         rates=rates, rates_as_of=date(2026, 11, 23))
    m = agg._build_tier1_source_as_of_map({"bb_forex": snap}, bb_forex_ok=True)
    assert m["usd_bdt_mid"] == date(2026, 11, 23)
    assert m["usd_bdt_exchange_rate"] == date(2026, 11, 23)

    legacy = ForexSnapshot(date=date(2026, 11, 24), scraped_at=datetime.now(timezone.utc), rates=rates)
    assert agg._build_tier1_source_as_of_map({"bb_forex": legacy}, bb_forex_ok=True)["usd_bdt_mid"] == date(2026, 11, 24)
