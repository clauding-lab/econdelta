"""Persistence receipts must describe actual writes, including partial failures."""
from datetime import datetime, timezone

from utils.write_receipts import monthly_attempt


def test_success_requires_exact_readback(monkeypatch):
    monkeypatch.setattr('utils.supabase_reader.get_metric_history_monthly_at',
                        lambda mid, as_ofs: [{'as_of': '2026-08-01', 'value': 9, 'source': 'official', 'source_as_of': '2026-08-31'}])
    with monthly_attempt() as receipt:
        receipt.confirm([dict(metric_id='cpi', as_of='2026-08-01', value=9,
                              source='official', source_as_of='2026-08-31')])
    assert receipt.result()['status'] == 'ok'


def test_partial_or_concurrent_overwrite_is_not_success(monkeypatch):
    monkeypatch.setattr('utils.supabase_reader.get_metric_history_monthly_at',
                        lambda mid, as_ofs: [{'as_of': '2026-08-01', 'value': 8, 'source': 'official'}])
    with monthly_attempt() as receipt:
        receipt.confirm([dict(metric_id='cpi', as_of='2026-08-01', value=9, source='official')])
    assert receipt.result()['status'] == 'failed'


def test_contained_failure_survives_other_leg_success(monkeypatch):
    monkeypatch.setattr('utils.supabase_reader.get_metric_history_monthly_at',
                        lambda mid, as_ofs: [{'as_of': '2026-08-01', 'value': 9, 'source': 'official'}])
    with monthly_attempt() as receipt:
        receipt.fail('CPI source read failed')
        receipt.confirm([dict(metric_id='exports', as_of='2026-08-01', value=9, source='official')])
    assert receipt.result()['status'] == 'failed'
    assert receipt.result()['confirmed_rows'] == 1


def test_no_new_month_is_skipped_not_failed():
    with monthly_attempt() as receipt:
        pass
    assert receipt.result()['status'] == 'skipped'
    assert datetime.fromisoformat(receipt.result()['attempted_at']) <= datetime.now(timezone.utc)
