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


def test_unwritable_receipt_does_not_block_valid_export_append(monkeypatch, tmp_path, caplog):
    epb = module()
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer
    monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
    monkeypatch.setattr(epb, "fetch_exports", lambda: ([(date(2026, 8, 1), 4429.45)], "official.xlsx"))
    monkeypatch.setattr(reader, "get_metric_history_monthly", lambda mid: [])
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing file")
    written = []
    monkeypatch.setattr(writer, "upsert_metric_history_monthly", lambda rows: written.extend(rows) or len(rows))
    assert epb.write_exports_monthly(date(2026, 9, 25), evidence_dir=blocked) == 1
    assert written[0]["value"] == 4429.45
    assert "receipt" in caplog.text.lower()


def test_database_failure_still_propagates_after_receipt_failure(monkeypatch, tmp_path):
    epb = module()
    import utils.supabase_reader as reader
    import utils.supabase_writer as writer
    monkeypatch.delenv("ECONDELTA_SKIP_SUPABASE", raising=False)
    monkeypatch.setattr(epb, "fetch_exports", lambda: ([(date(2026, 8, 1), 4429.45)], "official.xlsx"))
    monkeypatch.setattr(reader, "get_metric_history_monthly", lambda mid: [])
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing file")
    def reject(rows):
        raise writer.SupabaseWriteError("write unavailable")
    monkeypatch.setattr(writer, "upsert_metric_history_monthly", reject)
    with pytest.raises(writer.SupabaseWriteError, match="write unavailable"):
        epb.write_exports_monthly(date(2026, 9, 25), evidence_dir=blocked)


@pytest.mark.parametrize("failure", ["timeout", "bad-zip", "bad-xml", "bad-index"])
def test_bad_companion_does_not_hide_later_valid_summary(monkeypatch, caplog, failure):
    import io
    import zipfile
    epb = module()
    workbook = (FIXTURES / "epb-jul-aug-2026-a.xlsx").read_bytes()
    bad = b"not a zip"
    if failure in {"bad-xml", "bad-index"}:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", "<bad" if failure == "bad-xml" else
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row><c r="A1" t="s"><v>999</v></c></row></sheetData></worksheet>')
        bad = out.getvalue()
    monkeypatch.setattr(epb, "discover_workbooks", lambda html: ["bad.xlsx", "summary.xlsx"])
    def download(url):
        if url == epb.INDEX_URL:
            return b"index"
        if url == "bad.xlsx":
            if failure == "timeout":
                raise TimeoutError("companion unavailable")
            return bad
        return workbook
    monkeypatch.setattr(epb, "_download", download)
    assert epb.fetch_exports() == ([(date(2026, 7, 1), 4727.45), (date(2026, 8, 1), 4429.45)], "summary.xlsx")
    assert "bad.xlsx" in caplog.text


def test_conflicting_valid_summaries_still_fail_closed(monkeypatch):
    import io
    import zipfile
    epb = module()
    workbook = (FIXTURES / "epb-jul-aug-2026-a.xlsx").read_bytes()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(workbook)) as original, zipfile.ZipFile(out, "w") as changed:
        for name in original.namelist():
            content = original.read(name)
            if name == "xl/worksheets/sheet1.xml":
                content = content.replace(b"4429.45", b"4400.45")
            changed.writestr(name, content)
    monkeypatch.setattr(epb, "discover_workbooks", lambda html: ["one.xlsx", "two.xlsx"])
    monkeypatch.setattr(epb, "_download", lambda url: b"index" if url == epb.INDEX_URL else
                        workbook if url == "one.xlsx" else out.getvalue())
    with pytest.raises(ValueError, match="Conflicting"):
        epb.fetch_exports()


def test_no_surviving_verified_summary_fails_overall_fetch(monkeypatch, caplog):
    epb = module()
    monkeypatch.setattr(epb, "discover_workbooks", lambda html: ["broken.xlsx"])
    monkeypatch.setattr(epb, "_download", lambda url: b"index" if url == epb.INDEX_URL else b"bad zip")
    with pytest.raises(ValueError, match="no verified goods summary"):
        epb.fetch_exports()
    assert "broken.xlsx" in caplog.text
