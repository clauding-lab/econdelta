"""Final aggregate snapshot exposes persistence outcomes after the attempts."""
import json

import pytest

import aggregate_latest as agg
from tests.test_aggregator import _build_data_tree
from utils import supabase_writer as sw


@pytest.mark.parametrize('confirmation,expected', [(True, 'ok'), (False, 'failed'), (None, 'failed'), ('raise', 'failed')])
def test_latest_finalized_after_daily_attempt(tmp_path, monkeypatch, confirmation, expected):
    data_dir, cfg_path = _build_data_tree(tmp_path)
    latest = data_dir / 'latest.json'
    latest.write_text('{"previous": true}')
    monkeypatch.setattr(agg, 'DATA_DIR', data_dir)
    monkeypatch.setattr(agg, 'LATEST_PATH', latest)
    monkeypatch.setattr(agg, 'ARCHIVE_DIR', data_dir / 'archive')
    monkeypatch.setattr(agg, 'CONFIG_PATH', cfg_path)
    monkeypatch.setenv('ECONDELTA_DRY_RUN', '1')
    monkeypatch.setenv('ECONDELTA_SKIP_SUPABASE', '0')
    monkeypatch.setattr(agg, 'notify', lambda *a, **kw: None)
    monkeypatch.setattr(sw, 'upsert_metric_definitions_seed', lambda *a, **kw: 0)
    monkeypatch.setattr(agg, '_apply_media_overrides', lambda *a: None)
    monkeypatch.setattr(agg, '_write_reserves_monthly_split', lambda *a: 0)
    monkeypatch.setattr(agg, '_run_chart_feeding_monthly_appenders', lambda: {
        'status': 'skipped', 'attempted_at': '2026-09-25T00:00:00+00:00', 'reason': 'official lag'})

    def write(**kw):
        assert json.loads(latest.read_text()) == {'previous': True}
        if confirmation == 'raise':
            raise sw.SupabaseWriteError('simulated failure')
        return 1

    monkeypatch.setattr(sw, 'upsert_metric_history', write)
    monkeypatch.setattr(sw, 'verify_landed_count', lambda *a, **kw: confirmation)
    assert agg.main() == 0
    payload = json.loads(latest.read_text())
    assert payload['write_status']['daily']['status'] == expected
    assert payload['write_status']['monthly']['status'] == 'skipped'
    assert not latest.with_suffix('.json.tmp').exists()
    archived = list((data_dir / 'archive').glob('latest_*.json'))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text()) == payload
