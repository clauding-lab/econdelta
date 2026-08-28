"""Wiring tests for the DOMMR/BOFR source: aggregate fan-out, headline
promotion, and — critically — source_as_of propagation to the fanned ids.

The date-propagation tests are the load-bearing ones: the four minted ids
(dommr/dommr_1w/bofr/bofr_1w) are NOT v3-registry ids, so
``_build_source_as_of_map``'s main loop never dates them — without the
explicit propagation step, ``upsert_metric_history``'s ``as_of=today``
fallback would silently re-forge run-date stamps on every fanned row while
the parent carried the page's real date (the landmine 26/47 as_of-forgery
class; the exact mechanism that masked a rate cut for 62 nights). The
end-to-end test below parses the REAL fixture and asserts the fanned
metric_history rows carry the fixture's own date-header date, not today.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import aggregate_latest as agg
from fetchers.base import FetchResult
from parsers.html_money_market_ref_rate import _SERIES_KEYS
from parsers.registry import get_parser
from utils.supabase_writer import _rows_from_data

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "bb_money_market_ref_rate.html"

FIXTURE_DATE = date(2026, 8, 27)  # the real capture's own date header
FANOUT_IDS = ("dommr", "dommr_1w", "bofr", "bofr_1w")


def _parse_fixture():
    artifact = FetchResult(
        indicator_id="money_market_ref_rate",
        artifact_path=FIXTURE,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate",
        sha256="x" * 64,
        cache_hit=False,
    )
    return get_parser("html_money_market_ref_rate").parse(artifact, "")


class TestFanout:
    def test_dict_fans_out_to_four_ids_and_promotes_dommr_headline(self):
        data = {
            "money_market_ref_rate": {
                "dommr": 9.18, "dommr_1w": 9.33, "bofr": 9.23, "bofr_1w": 9.28,
            },
        }
        agg._flatten_dict_indicators(data)
        assert data["dommr"] == 9.18
        assert data["dommr_1w"] == 9.33
        assert data["bofr"] == 9.23
        assert data["bofr_1w"] == 9.28
        # DOMMR overnight promoted as the parent's scalar headline
        assert data["money_market_ref_rate"] == 9.18

    def test_unknown_dict_keys_are_never_minted(self):
        """Fanned keys land as TOP-LEVEL metric ids, so minting is
        allow-listed — an LLM-path or structure-change surprise key must
        never become a metric_history id."""
        data = {
            "money_market_ref_rate": {
                "dommr": 9.18, "dommr_1w": 9.33, "bofr": 9.23, "bofr_1w": 9.28,
                "dommr_1m": 9.86, "../evil": 1.0,
            },
        }
        agg._flatten_dict_indicators(data)
        assert "dommr_1m" not in data
        assert "../evil" not in data

    def test_existing_keys_are_left_alone(self):
        data = {
            "money_market_ref_rate": {
                "dommr": 9.18, "dommr_1w": 9.33, "bofr": 9.23, "bofr_1w": 9.28,
            },
            "dommr": 7.77,  # hand-set upstream wins (idempotence contract)
        }
        agg._flatten_dict_indicators(data)
        assert data["dommr"] == 7.77

    def test_non_dict_value_is_untouched(self):
        data = {"money_market_ref_rate": 9.18}
        agg._flatten_dict_indicators(data)
        assert data == {"money_market_ref_rate": 9.18}

    def test_fanout_ids_match_parser_series_keys(self):
        """Drift guard: the aggregate's mint/date-propagation list and the
        parser's dict keys are the same contract — a key added to one side
        only would silently either not mint or not date."""
        assert set(agg.MONEY_MARKET_REF_RATE_FANOUT_IDS) == set(_SERIES_KEYS)
        assert set(FANOUT_IDS) == set(_SERIES_KEYS)

    def test_partial_null_headline_is_announced_hole_not_buried_dict(
        self, caplog, monkeypatch
    ):
        """2026-08-28 review finding 4 (CONFIRMED pre-fix): the LLM-extract
        fallback preserves null tenors, so a dict with dommr null but
        healthy siblings reaches the fan-out. The parent then STAYED a dict
        and the Supabase writer's scalar-only filter silently dropped it — a
        zero-row day on the headline series while the siblings look healthy
        (the PR-#31 failure class). Now: warning + notify fire, the parent
        key is REMOVED (announced hole), the non-null siblings still mint,
        and the fanned rows still carry the page's real as_of."""
        notifications: list[tuple] = []
        monkeypatch.setattr(agg, "notify", lambda *a, **k: notifications.append(a))

        value = {"dommr": None, "dommr_1w": 9.33, "bofr": 9.23, "bofr_1w": 9.28}
        data = {"money_market_ref_rate": dict(value)}
        with caplog.at_level(logging.WARNING):
            agg._flatten_dict_indicators(data)

        assert "money_market_ref_rate" not in data  # removed, not a buried dict
        assert "dommr" not in data                  # a null is never minted
        assert data["dommr_1w"] == 9.33
        assert data["bofr"] == 9.23
        assert data["bofr_1w"] == 9.28
        assert any(
            "money_market_ref_rate" in rec.message for rec in caplog.records
        ), "the headline hole must be logged, not silent"
        assert notifications and notifications[0][0] == "warning"
        assert "money_market_ref_rate" in notifications[0][1]

        # The three fanned rows still land at the page's real date.
        domains = {
            "money_market": {
                "money_market_ref_rate": {
                    "value": value,
                    "cadence": "daily",
                    "source_as_of": FIXTURE_DATE.isoformat(),
                    "_parse_strategy": "html_money_market_ref_rate",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
        source_as_of_map = agg._build_source_as_of_map(domains)
        rows = _rows_from_data(data, date.today(), "EconDelta", source_as_of_map)
        by_id = {r["metric_id"]: r for r in rows}
        assert "money_market_ref_rate" not in by_id  # the hole is real
        for metric_id in ("dommr_1w", "bofr", "bofr_1w"):
            assert by_id[metric_id]["as_of"] == FIXTURE_DATE.isoformat()


class TestSourceAsOfPropagation:
    def test_fanned_ids_inherit_parent_date(self):
        domains = {
            "money_market": {
                "money_market_ref_rate": {
                    "value": {"dommr": 9.18, "dommr_1w": 9.33,
                              "bofr": 9.23, "bofr_1w": 9.28},
                    "cadence": "daily",
                    "source_as_of": "2026-08-27",
                    "_parse_strategy": "html_money_market_ref_rate",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
        result = agg._build_source_as_of_map(domains)
        assert result["money_market_ref_rate"] == FIXTURE_DATE
        for fanned_id in FANOUT_IDS:
            assert result[fanned_id] == FIXTURE_DATE

    def test_no_parent_date_means_no_fanned_dates(self):
        """When the parser recovered nothing, the fanned ids must be absent
        from the map (writer falls back for parent AND fanned alike) — never
        dated from thin air."""
        domains = {
            "money_market": {
                "money_market_ref_rate": {
                    "value": {"dommr": 9.18},
                    "cadence": "daily",
                    "_parse_strategy": "html_money_market_ref_rate",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
        result = agg._build_source_as_of_map(domains)
        for fanned_id in FANOUT_IDS:
            assert fanned_id not in result

    def test_end_to_end_fanned_rows_carry_fixture_date_not_today(self):
        """THE acceptance criterion: parse the REAL capture, run the real
        fan-out + date map + writer row-builder, and assert every fanned
        metric_history row's as_of is the page's own date header
        (2026-08-27) — never the run date."""
        parsed = _parse_fixture()
        assert parsed.source_as_of == FIXTURE_DATE  # precondition, from the page

        # Mirror hybrid._build_snapshot's shape for the domains dict.
        domains = {
            "money_market": {
                "money_market_ref_rate": {
                    "value": parsed.value,
                    "cadence": "daily",
                    "source_as_of": parsed.source_as_of.isoformat(),
                    "_parse_strategy": parsed._parse_strategy,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
        data = {"money_market_ref_rate": dict(parsed.value)}
        agg._flatten_dict_indicators(data)
        source_as_of_map = agg._build_source_as_of_map(domains)

        run_date = date.today()
        assert FIXTURE_DATE != run_date  # the assertion below must be able to fail

        rows = _rows_from_data(data, run_date, "EconDelta", source_as_of_map)
        by_id = {r["metric_id"]: r for r in rows}
        for metric_id in FANOUT_IDS + ("money_market_ref_rate",):
            assert by_id[metric_id]["as_of"] == FIXTURE_DATE.isoformat(), (
                f"{metric_id} was stamped {by_id[metric_id]['as_of']} — "
                "the fan-out lost the page's real value date"
            )


class TestHybridDictShapeWiring:
    def test_indicator_is_dict_shaped_for_llm_fallback(self):
        from parsers.hybrid import _DICT_SHAPED_LLM_INDICATOR_IDS
        assert "money_market_ref_rate" in _DICT_SHAPED_LLM_INDICATOR_IDS

    def test_expected_dict_keys_match_parser(self):
        from parsers.hybrid import _expected_dict_keys
        assert _expected_dict_keys("money_market_ref_rate") == _SERIES_KEYS


class TestConfigEntry:
    def _indicator(self) -> dict:
        cfg = json.loads((REPO_ROOT / "config" / "sources-v3.json").read_text())
        matches = [i for i in cfg["indicators"] if i["id"] == "money_market_ref_rate"]
        assert len(matches) == 1, "exactly ONE money_market_ref_rate entry expected"
        return matches[0]

    def test_config_shape(self):
        ind = self._indicator()
        assert ind["domain"] == "money_market"
        assert ind["cadence"] == "daily"
        assert ind["fetch"]["type"] == "html"
        assert ind["fetch"]["url"] == (
            "https://www.bb.org.bd/en/index.php/monetaryactivity/money_market_ref_rate"
        )
        assert ind["parse"]["deterministic"] == "html_money_market_ref_rate"
        assert ind["parse"]["llm_prompt"] == "html_money_market_ref_rate.txt"
        assert ind["parse"]["value_type"] == "percent"
        assert ind["parse"]["valid_range"] == [0.0, 25.0]
        # anomaly_threshold note (2026-08-28 review 6b): in the DAILY
        # aggregate this field is inert for every v3 indicator — the
        # aggregate's anomaly alert keys on snapshot["change_pct"], which
        # parsers/hybrid._build_snapshot never populates. Its one live
        # consumer is the WEEKLY briefing's candidate scan
        # (briefing/anomalies.compute_candidates, change-vs-prior against
        # metric_history), and only for the parent headline id — never the
        # fanned ids. Kept at 2.0 for config-shape consistency and for that
        # weekly path.
        assert ind["anomaly_threshold"] == 2.0

    def test_llm_prompt_file_exists(self):
        ind = self._indicator()
        prompt = REPO_ROOT / "claude_max" / "prompts" / ind["parse"]["llm_prompt"]
        assert prompt.exists()
        text = prompt.read_text()
        # The fallback fires precisely when structure changed — it must
        # anchor on the two header TEXTS, never on table position.
        assert "Dhaka Overnight Money Market Rate (DOMMR)" in text
        assert "Bangladesh Overnight Financing Rate (BOFR)" in text

    def test_definition_seed_keys_present(self):
        """label/short_label/unit/description/source on the config entry so
        _build_definition_seeds self-seeds a real metric_definitions row."""
        ind = self._indicator()
        for key in ("label", "short_label", "unit", "description", "source"):
            assert ind.get(key), f"config entry missing {key!r}"

    def test_fanned_ids_have_derived_definition_seeds(self):
        seeds = {d["metric_id"]: d for d in agg.DERIVED_DEFINITION_SEEDS}
        for fanned_id in FANOUT_IDS:
            assert fanned_id in seeds, f"{fanned_id} missing from DERIVED_DEFINITION_SEEDS"
            assert seeds[fanned_id]["cadence"] == "daily"
            assert seeds[fanned_id]["unit"] == "%"

    def test_fanned_ids_in_catalog_derived_keys(self):
        from scripts.build_catalog import DERIVED_KEYS
        by_id = {mid for mid, _u, _c, _d in DERIVED_KEYS}
        for fanned_id in FANOUT_IDS:
            assert fanned_id in by_id, f"{fanned_id} missing from build_catalog.DERIVED_KEYS"

    def test_sentinel_cadence_resolves_fanned_ids_as_daily(self):
        from sentinel.cadence import load_cadence_map
        cadence = load_cadence_map()
        assert cadence["money_market_ref_rate"] == "daily"
        for fanned_id in FANOUT_IDS:
            assert cadence.get(fanned_id) == "daily", (
                f"{fanned_id} unmapped in sentinel cadence — the freshness "
                "sentinel would report it unmapped instead of judging it"
            )
