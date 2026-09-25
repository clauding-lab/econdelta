"""Official EPB goods exports: monthly labels, not FY totals or comparators."""
import importlib
from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures/epb"


def module():
    return importlib.import_module("utils.epb_monthly")


def test_real_workbook_selects_current_fy_single_month_goods_values():
    rows = module().parse_exports_workbook((FIXTURES / "epb-jul-aug-2026-a.xlsx").read_bytes())
    assert rows == [(date(2026, 7, 1), 4727.45), (date(2026, 8, 1), 4429.45)]


def test_region_cumulative_workbook_cannot_be_spliced_into_goods_series():
    with pytest.raises(ValueError, match="summary"):
        module().parse_exports_workbook((FIXTURES / "epb-jul-aug-2026-b.xlsx").read_bytes())


def test_current_index_discovers_both_unlabelled_attachments_not_a_pinned_edition():
    urls = module().discover_workbooks((FIXTURES / "epb-index-20260925.html").read_text())
    assert len(urls) == 5  # all attachments in the latest edition, including companions
    assert any("dc513db2" in url for url in urls)
    assert any("2c8b50c8" in url for url in urls)


def test_writer_preserves_accepted_rows_and_emits_revision_diff(monkeypatch, tmp_path):
    epb = module()
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer
    monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
    monkeypatch.setattr(epb, "fetch_exports", lambda: (
        [(date(2026, 6, 1), 4200), (date(2026, 7, 1), 4727.45), (date(2026, 8, 1), 4429.45)], "official.xlsx"))
    monkeypatch.setattr(reader, "get_metric_history_monthly", lambda mid: [
        {"metric_id": mid, "as_of": "2026-06-01", "value": 4202.69, "source": "epb_bss"}])
    written = []
    monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda rows: written.extend(rows) or len(rows))
    assert epb.write_exports_monthly(date(2026, 9, 25), evidence_dir=tmp_path) == 2
    assert [(r["as_of"], r["value"], r["source_as_of"]) for r in written] == [
        ("2026-07-01", 4727.45, "2026-07-31"), ("2026-08-01", 4429.45, "2026-08-31")]
    import json
    report = json.loads((tmp_path / "exports_usd_mn_monthly.json").read_text())
    assert report["revisions"][0]["stored_value"] == 4202.69
    assert report["revisions"][0]["source_value"] == 4200
    assert report["latest_source_period"] == "2026-08-31"
    assert report["status"] == "supported"


def test_future_and_open_month_values_are_withheld():
    epb = module()
    rows, revisions = epb.plan_exports(
        [(date(2026, 9, 1), 4429.45), (date(2026, 10, 1), 4429.45)], [], date(2026, 9, 25))
    assert rows == []
    assert revisions == []


def test_shared_monthly_contract_matches_real_workbook():
    import hashlib
    import json
    contract = json.loads((FIXTURES.parent / "contracts/brief-observations-v1.json").read_text())["monthly_contract"]
    workbook = (FIXTURES / "epb-jul-aug-2026-a.xlsx").read_bytes()
    assert hashlib.sha256(workbook).hexdigest() == contract["epb_evidence"]["sha256"]
    rows, revisions = module().plan_exports(module().parse_exports_workbook(workbook), [], date(2026, 9, 25))
    assert rows == contract["epb_evidence"]["rows"]
    assert revisions == []
