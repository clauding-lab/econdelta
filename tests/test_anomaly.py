"""Tests for utils/anomaly.py."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from utils.anomaly import (
    CORRIDOR_REPO_ID,
    CORRIDOR_SDF_ID,
    CORRIDOR_SLF_ID,
    check_corridor_coherence,
    check_threshold,
    load_thresholds,
)


class TestCheckThreshold:
    def test_anomaly_detected_exceeds_threshold(self):
        # 5500 vs 5000 = 10% change, threshold 5% -> anomalous
        ok, pct = check_threshold("dsex", 5500.0, 5000.0, {"dsex": 0.05})
        assert ok is False
        assert abs(pct - 0.10) < 1e-9

    def test_within_threshold(self):
        # 5100 vs 5000 = 2% change, threshold 5% -> ok
        ok, pct = check_threshold("dsex", 5100.0, 5000.0, {"dsex": 0.05})
        assert ok is True
        assert abs(pct - 0.02) < 1e-9

    def test_first_run_prev_none(self):
        ok, pct = check_threshold("dsex", 5000.0, None, {"dsex": 0.05})
        assert ok is True
        assert pct == 0.0

    def test_first_run_prev_zero(self):
        ok, pct = check_threshold("usd_bdt_mid", 110.0, 0.0, {"usd_bdt_mid": 0.02})
        assert ok is True
        assert pct == 0.0

    def test_exact_threshold_boundary_is_ok(self):
        # exactly 5% change against a 5% threshold -> ok (<=)
        ok, pct = check_threshold("dsex", 5250.0, 5000.0, {"dsex": 0.05})
        assert ok is True
        assert abs(pct - 0.05) < 1e-9

    def test_unknown_metric_uses_default_10pct_threshold(self):
        # 14% change against default 10% -> anomalous
        ok, pct = check_threshold("unknown_metric", 1.14, 1.0, {})
        assert ok is False

        # 9% change against default 10% -> ok
        ok2, pct2 = check_threshold("unknown_metric", 1.09, 1.0, {})
        assert ok2 is True

    def test_decrease_also_triggers_anomaly(self):
        # 4500 vs 5000 = 10% down, threshold 5% -> anomalous
        ok, pct = check_threshold("dsex", 4500.0, 5000.0, {"dsex": 0.05})
        assert ok is False
        assert abs(pct - 0.10) < 1e-9

    def test_multiple_metrics_in_thresholds(self):
        thresholds = {"dsex": 0.05, "usd_bdt_mid": 0.02, "gold": 0.06}
        ok, _ = check_threshold("usd_bdt_mid", 112.5, 110.0, thresholds)
        # 2.5/110 ~ 2.27% > 2% threshold -> anomalous
        assert ok is False


class TestCheckCorridorCoherence:
    """The BB policy corridor invariant SDF <= repo <= SLF (E1.4).

    The three legs are parsed independently, so this cross-metric check runs at
    aggregate time on the assembled `data` dict. It detects-and-alerts: a
    mis-ordered corridor fires one loud notify; it never rejects the run.
    """

    def test_misordered_corridor_fires_one_error_notify(self):
        # SDF above repo violates SDF <= repo <= SLF -> loud alert.
        data = {
            CORRIDOR_SDF_ID: 8.5,
            CORRIDOR_REPO_ID: 8.0,
            CORRIDOR_SLF_ID: 11.5,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is False
        assert mock_notify.call_count == 1
        args, _kwargs = mock_notify.call_args
        # level is the first positional arg; message body the third.
        assert args[0] == "error"
        message = args[2]
        assert "8.5" in message
        assert "8.0" in message
        assert "11.5" in message
        assert CORRIDOR_SDF_ID in message
        assert CORRIDOR_REPO_ID in message
        assert CORRIDOR_SLF_ID in message

    def test_correct_real_corridor_is_silent(self):
        # Live corridor confirmed 2026-07-10: SDF 7.50 < repo 10.00 < SLF 11.50.
        data = {
            CORRIDOR_SDF_ID: 7.5,
            CORRIDOR_REPO_ID: 10.0,
            CORRIDOR_SLF_ID: 11.5,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()

    def test_missing_leg_is_silent(self):
        # A None leg is absent data, not a violation — never false-alarm.
        data = {
            CORRIDOR_SDF_ID: 7.5,
            CORRIDOR_REPO_ID: None,
            CORRIDOR_SLF_ID: 11.5,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()

    def test_absent_key_is_silent(self):
        # An entirely missing leg (key not in data) is also silent.
        data = {CORRIDOR_SDF_ID: 7.5, CORRIDOR_SLF_ID: 11.5}
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()

    def test_equal_legs_are_coherent(self):
        # SDF == repo == SLF satisfies the <= boundary — no alert.
        data = {
            CORRIDOR_SDF_ID: 10.0,
            CORRIDOR_REPO_ID: 10.0,
            CORRIDOR_SLF_ID: 10.0,
        }
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is True
        mock_notify.assert_not_called()


class TestCorridorCoherenceRealAssembly:
    """Contract test: the guard must fire when fed a `data` dict built by the
    PRODUCTION assembly path, not a hand-authored one.

    The five unit tests above feed idealized ``{id: float}`` dicts, so they
    would keep passing even if a future change made a corridor leg dict-shaped
    (like ``call_money_rate`` already is) or moved its value off the bare id —
    ``check_corridor_coherence`` would silently revert to a permanent no-op and
    CI would stay green. These tests pin the guard to the real shape contract:
    each leg must land under its BARE id as a scalar number after the real
    ``_build_v3_blocks`` -> merge -> ``_apply_brief_aliases`` chain (mirroring
    aggregate_latest.main ~989-1006). A dict-shape or key regression makes the
    guard skip -> notify never fires -> THIS test fails.
    """

    @staticmethod
    def _write_corridor_snapshot(data_dir: Path, indicator_id: str, value: float) -> None:
        import json
        from datetime import datetime, timezone

        snap_dir = data_dir / indicator_id
        snap_dir.mkdir(parents=True)
        snap = {
            "indicator_id": indicator_id,
            "domain": "money_market",
            "cadence": "monthly",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "source_url": "https://www.bb.org.bd/x",
            "value": value,  # a bare scalar float, as pdf_table_column_latest writes it
            "value_type": "percent",
            "previous_value": None,
            "change_pct": None,
            "_provenance": "deterministic",
            "_parse_strategy": "pdf_table_column_latest",
        }
        (snap_dir / "2026-07-10.json").write_text(json.dumps(snap))

    def test_misordered_corridor_from_production_assembly_fires_notify(
        self, tmp_path: Path, monkeypatch
    ):
        import json
        from datetime import datetime, timezone

        import aggregate_latest as agg

        # Synthetic registry: exactly the three corridor legs, keyed by the
        # guard's own ids so the test exercises the real assembly, not a mock.
        registry = {
            "indicators": [
                {"id": CORRIDOR_SDF_ID, "domain": "money_market", "cadence": "monthly"},
                {"id": CORRIDOR_REPO_ID, "domain": "money_market", "cadence": "monthly"},
                {"id": CORRIDOR_SLF_ID, "domain": "money_market", "cadence": "monthly"},
            ]
        }
        reg_path = tmp_path / "sources-v3.json"
        reg_path.write_text(json.dumps(registry))

        data_dir = tmp_path / "data"
        # Mis-ordered on purpose: SDF 8.5 > repo 8.0 violates SDF <= repo <= SLF.
        self._write_corridor_snapshot(data_dir, CORRIDOR_SDF_ID, 8.5)
        self._write_corridor_snapshot(data_dir, CORRIDOR_REPO_ID, 8.0)
        self._write_corridor_snapshot(data_dir, CORRIDOR_SLF_ID, 11.5)

        monkeypatch.setattr(agg, "SOURCES_V3_PATH", reg_path)
        monkeypatch.setattr(agg, "DATA_DIR", data_dir)

        # Reproduce main()'s data-assembly chain (aggregate_latest.py ~989-1006).
        now = datetime.now(timezone.utc)
        data_additions, _domains, _freshness, _alerts = agg._build_v3_blocks(now)
        data: dict = {}
        data.update(data_additions)
        agg._apply_brief_aliases(data)

        # Pin the shape contract the guard depends on: each leg is present under
        # its bare id as a scalar number (not a dict, not moved to another key).
        # This assertion fails first, with a clear message, on a shape/alias
        # regression — before the behavioural check below.
        for leg in (CORRIDOR_SDF_ID, CORRIDOR_REPO_ID, CORRIDOR_SLF_ID):
            got = data.get(leg)
            assert isinstance(got, (int, float)) and not isinstance(got, bool), (
                f"{leg} must land under its bare id as a scalar number; got "
                f"{type(got).__name__} — a dict-shape or key-alias regression "
                f"would silently turn the corridor guard into a no-op"
            )

        # The guard, fed the production-built data, must detect the mis-order.
        with patch("utils.anomaly.notify") as mock_notify:
            ok = check_corridor_coherence(data)

        assert ok is False
        mock_notify.assert_called_once()
        assert mock_notify.call_args.args[0] == "error"

    def test_guard_ids_exist_in_real_sources_v3_config(self):
        # A config-side rename of a corridor id would silently break the guard
        # while every constant-driven test above stays green. Pin the guard's
        # ids to the real registry so such a rename fails CI here.
        import json

        cfg_path = Path(__file__).parent.parent / "config" / "sources-v3.json"
        ids = {ind["id"] for ind in json.loads(cfg_path.read_text())["indicators"]}
        assert CORRIDOR_SDF_ID in ids
        assert CORRIDOR_REPO_ID in ids
        assert CORRIDOR_SLF_ID in ids


class TestLoadThresholds:
    def test_loads_valid_json(self):
        data = {
            "_meta": {"description": "test"},
            "dsex": 0.05,
            "gold": 0.06,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f)
            tmp_path = f.name

        result = load_thresholds(tmp_path)
        assert result["dsex"] == 0.05
        assert result["gold"] == 0.06
        assert "_meta" not in result

    def test_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_thresholds("/nonexistent/path/thresholds.json")

    def test_loads_actual_config_file(self):
        config_path = Path(__file__).parent.parent / "config" / "thresholds.json"
        result = load_thresholds(config_path)
        assert "dsex" in result
        assert "usd_bdt_mid" in result
        assert "_meta" not in result
