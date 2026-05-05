"""Tests for scripts.seed_macro_monthly KEY_MAP and pure row builders."""
from __future__ import annotations

from datetime import date

import pytest

from scripts.seed_macro_monthly import (
    DEFAULT_SOURCE,
    DOMAIN_VALUES,
    KEY_MAP,
    MetricMap,
    SOURCE_ATTRIBUTION,
    SOURCE_URL,
    build_definitions_rows,
    build_history_rows,
    normalise_as_of,
)


# ---------- KEY_MAP shape ----------

class TestKeyMap:
    def test_all_entries_are_metricmap(self):
        for key, val in KEY_MAP.items():
            assert isinstance(val, MetricMap), f"{key} is {type(val).__name__}, not MetricMap"

    def test_no_nested_dicts(self):
        # The actual JSON shape has no multi-tenor dicts; KEY_MAP is flat.
        for key, val in KEY_MAP.items():
            assert not isinstance(val, dict)

    def test_all_domains_valid(self):
        for key, m in KEY_MAP.items():
            assert m.domain in DOMAIN_VALUES, f"{key} domain {m.domain!r}"

    def test_all_metric_ids_unique(self):
        ids = [m.metric_id for m in KEY_MAP.values()]
        assert len(ids) == len(set(ids))

    def test_all_metric_ids_end_with_monthly(self):
        for key, m in KEY_MAP.items():
            assert m.metric_id.endswith("_monthly"), f"{key} -> {m.metric_id}"

    def test_camelcase_upstream_keys(self):
        # Spot-check: the actual JSON uses camelCase, not snake_case.
        assert "genP2P" in KEY_MAP
        assert "tbill364" in KEY_MAP
        assert "fxReserve" in KEY_MAP

    def test_yield_1y_absent(self):
        # Per SHAPE_NOTES: no upstream tr1y, so no yield_1y_monthly.
        ids = {m.metric_id for m in KEY_MAP.values()}
        assert "yield_1y_monthly" not in ids

    def test_dsex_turnover_absent(self):
        # Per SHAPE_NOTES: no upstream turnover series; chart will use dsex only.
        ids = {m.metric_id for m in KEY_MAP.values()}
        assert "dsex_turnover_monthly" not in ids


# ---------- MetricMap dataclass ----------

class TestMetricMap:
    def test_frozen(self):
        m = MetricMap("x", "X", "%", "prices_policy")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            m.metric_id = "y"

    def test_invalid_domain_raises(self):
        with pytest.raises(ValueError):
            MetricMap("x", "X", "%", "bad_domain")


# ---------- normalise_as_of ----------

class TestNormaliseAsOf:
    def test_year_month_only(self):
        # The actual upstream format is 'YYYY-MM'.
        assert normalise_as_of("2024-03") == date(2024, 3, 1)

    def test_full_iso_date_clamps_to_day1(self):
        assert normalise_as_of("2024-03-15") == date(2024, 3, 1)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalise_as_of("not-a-date")


# ---------- build_history_rows ----------

class TestBuildHistoryRows:
    def _payload(self, **series):
        # Helper: months + arbitrary parallel series.
        return {"months": ["2024-01", "2024-02", "2024-03"], **series}

    def test_simple_series_three_months(self):
        rows = build_history_rows(self._payload(genP2P=[10.5, 9.8, 9.2]))
        ids = {r["metric_id"] for r in rows}
        assert "point_to_point_inflation_monthly" in ids
        # 3 months, 1 metric, no nulls -> 3 rows
        assert sum(1 for r in rows if r["metric_id"] == "point_to_point_inflation_monthly") == 3

    def test_skips_nulls(self):
        rows = build_history_rows(self._payload(genP2P=[10.5, None, 9.2]))
        cpi_rows = [r for r in rows if r["metric_id"] == "point_to_point_inflation_monthly"]
        assert len(cpi_rows) == 2
        assert {r["as_of"] for r in cpi_rows} == {"2024-01-01", "2024-03-01"}

    def test_off_by_one_months_longer(self):
        # months has 4, series has 3 -> use first 3
        payload = {"months": ["2024-01", "2024-02", "2024-03", "2024-04"],
                   "genP2P": [10.5, 9.8, 9.2]}
        rows = build_history_rows(payload)
        assert len(rows) == 3

    def test_off_by_one_series_longer(self):
        payload = {"months": ["2024-01", "2024-02"],
                   "genP2P": [10.5, 9.8, 9.2]}
        rows = build_history_rows(payload)
        assert len(rows) == 2

    def test_unknown_upstream_key_skipped(self, caplog):
        # Keys not in KEY_MAP and not 'months' should be ignored quietly.
        payload = self._payload(definitelyNotInKeyMapXyz=[1.0, 2.0, 3.0])
        rows = build_history_rows(payload)
        assert rows == []

    def test_excludes_bool_values(self):
        rows = build_history_rows(self._payload(genP2P=[True, 9.8, False]))
        cpi_rows = [r for r in rows if r["metric_id"] == "point_to_point_inflation_monthly"]
        assert len(cpi_rows) == 1
        assert cpi_rows[0]["value"] == 9.8

    def test_row_shape(self):
        rows = build_history_rows(self._payload(genP2P=[10.5, 9.8, 9.2]))
        first = rows[0]
        assert first["metric_id"] == "point_to_point_inflation_monthly"
        assert first["as_of"] == "2024-01-01"
        assert first["value"] == 10.5
        assert first["source"] == "macro_observer_seed"
        assert first["source_as_of"] == "2024-01-01"

    def test_real_policy_rate_derived(self):
        # repo=8.0, genP2P=9.5 -> real_policy_rate=-1.5
        payload = self._payload(repo=[8.0, 8.5, 9.0], genP2P=[9.5, 8.5, 7.0])
        rows = build_history_rows(payload)
        rpr = [r for r in rows if r["metric_id"] == "real_policy_rate_monthly"]
        assert len(rpr) == 3
        # Sort by as_of and verify
        rpr.sort(key=lambda r: r["as_of"])
        assert rpr[0]["value"] == pytest.approx(-1.5)
        assert rpr[1]["value"] == pytest.approx(0.0)
        assert rpr[2]["value"] == pytest.approx(2.0)

    def test_real_policy_rate_skips_when_input_null(self):
        payload = self._payload(repo=[8.0, None, 9.0], genP2P=[9.5, 8.5, 7.0])
        rows = build_history_rows(payload)
        rpr = [r for r in rows if r["metric_id"] == "real_policy_rate_monthly"]
        assert len(rpr) == 2  # second month has null repo, skipped

    def test_real_policy_rate_absent_when_repo_missing(self):
        # No repo series in payload -> no real_policy_rate rows.
        rows = build_history_rows(self._payload(genP2P=[9.5, 8.5, 7.0]))
        rpr = [r for r in rows if r["metric_id"] == "real_policy_rate_monthly"]
        assert rpr == []

    def test_custom_source_param(self):
        rows = build_history_rows(self._payload(genP2P=[10.5]), source="manual_test")
        assert all(r["source"] == "manual_test" for r in rows)


# ---------- build_definitions_rows ----------

class TestBuildDefinitionsRows:
    def test_one_row_per_metric_id_plus_derived(self):
        rows = build_definitions_rows()
        # Every KEY_MAP value's metric_id + the derived real_policy_rate_monthly
        expected_ids = {m.metric_id for m in KEY_MAP.values()} | {"real_policy_rate_monthly"}
        actual_ids = {r["metric_id"] for r in rows}
        assert actual_ids == expected_ids

    def test_required_fields_present(self):
        rows = build_definitions_rows()
        required = {"metric_id", "display_name", "unit", "source_url",
                    "source_attribution", "domain", "description"}
        for r in rows:
            assert required.issubset(r.keys()), f"missing fields in {r}"
            assert r["domain"] in DOMAIN_VALUES
            assert r["source_url"] == SOURCE_URL
            assert r["source_attribution"] == SOURCE_ATTRIBUTION


# ---------- end-to-end on real fixture ----------

class TestAgainstRealFixture:
    """Smoke tests using the cached scripts/_seed_data/macro_monthly_data.json."""

    @pytest.fixture
    def real_payload(self):
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "scripts" / "_seed_data" / "macro_monthly_data.json"
        return json.loads(p.read_text())

    def test_produces_thousands_of_rows(self, real_payload):
        rows = build_history_rows(real_payload)
        assert 3000 < len(rows) < 6000  # ~28 metrics × ~170 months ≈ 4760

    def test_real_policy_rate_present_in_real_data(self, real_payload):
        rows = build_history_rows(real_payload)
        rpr = [r for r in rows if r["metric_id"] == "real_policy_rate_monthly"]
        # Should have at least 100 months where both repo and genP2P are non-null
        assert len(rpr) > 100

    def test_oldest_date_around_jan_2012(self, real_payload):
        rows = build_history_rows(real_payload)
        oldest = min(r["as_of"] for r in rows)
        # The series start at Jan 2012 per SHAPE_NOTES; some metrics may start later.
        assert oldest <= "2013-01-01"  # generous bound

    def test_no_nulls_in_output(self, real_payload):
        rows = build_history_rows(real_payload)
        for r in rows:
            assert r["value"] is not None

    def test_date_format_is_iso_day1(self, real_payload):
        rows = build_history_rows(real_payload)
        for r in rows:
            assert r["as_of"].endswith("-01")  # always day-1
            assert len(r["as_of"]) == 10  # YYYY-MM-DD
