"""tests/test_bb_npl_structure_main.py"""
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import scrapers.bb_npl_structure as mod
from tests.test_bb_npl_structure_gate import GOOD

POS = date(2025, 12, 31)


def test_already_captured_exact_date_match_only():
    rows = [{"as_of": "2026-12-31"}, {"as_of": "2025-12-31"}]
    with patch.object(mod, "get_metric_history", return_value=rows):
        assert mod.already_captured(POS) is True
        assert mod.already_captured(date(2024, 12, 31)) is False  # older issue → backfillable


def test_already_captured_false_on_empty_or_read_error():
    from utils.supabase_reader import SupabaseReadError
    with patch.object(mod, "get_metric_history", return_value=[]):
        assert mod.already_captured(POS) is False
    with patch.object(mod, "get_metric_history", side_effect=SupabaseReadError("down")):
        assert mod.already_captured(POS) is False  # fail-open: duplicate run is idempotent


def test_payload_to_rows_converts_billions_to_crore_and_drops_check_field():
    rows = mod.payload_to_rows(dict(GOOD))
    assert rows["total_bank_advances"] == 1_820_430.0     # 18,204.30 bn -> crore
    assert rows["gross_npl_stock"] == 557_032.0
    assert rows["npl_rate_sector_trade_commerce"] == 49.88  # percents untouched
    assert "overall_npl_ratio_fsr" not in rows              # check-only, never stored
    assert "npl_rate_band_lt1cr" not in rows                # seed-only ids never written here


def test_payload_to_rows_skips_null_sub_rates():
    p = dict(GOOD)
    p["npl_rate_sub_rmg"] = None
    assert "npl_rate_sub_rmg" not in mod.payload_to_rows(p)


def test_main_skips_before_llm_when_position_captured(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="full text"), \
         patch.object(mod, "slice_table_window", return_value="w"), \
         patch.object(mod, "derive_position_date", return_value=POS), \
         patch.object(mod, "already_captured", return_value=True), \
         patch.object(mod, "run_extraction") as rex:
        assert mod.main() == 3
    rex.assert_not_called()


def test_main_gate_reject_writes_nothing_and_notifies(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    bad = dict(GOOD)
    bad["gross_npl_stock"] = 557.032
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="full text"), \
         patch.object(mod, "slice_table_window", return_value="w"), \
         patch.object(mod, "derive_position_date", return_value=POS), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=bad), \
         patch.object(mod, "upsert_metric_history") as up, \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    up.assert_not_called()
    assert noti.call_args.args[0] == "error"
    assert "stock" in noti.call_args.args[2]


def test_main_happy_path_seeds_definitions_then_writes(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="full text"), \
         patch.object(mod, "slice_table_window", return_value="w"), \
         patch.object(mod, "derive_position_date", return_value=POS), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=dict(GOOD)), \
         patch.object(mod, "upsert_metric_definitions_seed", return_value=0) as seed, \
         patch.object(mod, "upsert_metric_history", return_value=22) as up, \
         patch.object(mod, "verify_landed_count") as vlc:
        assert mod.main() == 0
    seed.assert_called_once()
    kwargs = up.call_args.kwargs
    assert kwargs["as_of"] == POS
    assert kwargs["source"] == "BB FSR"
    assert "url" not in kwargs
    # D1: ingested_at reaches the writer and is the SAME stamp verify_landed_count
    # reads back against — without this, verify_landed_count's since= proves nothing.
    assert isinstance(kwargs["ingested_at"], datetime)
    assert kwargs["ingested_at"] == vlc.call_args.kwargs["since"]


def test_main_fetch_failure_notifies_and_fails():
    with patch.object(mod, "fetch_latest_fsr", side_effect=RuntimeError("wall")), \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    assert noti.call_args.args[0] == "error"


def test_main_seed_failure_notifies_and_fails(tmp_path):
    # upsert_metric_definitions_seed does not always wrap failures in
    # SupabaseWriteError (a raw ConnectionError can escape it) — pin that
    # main() still catches, notifies, and returns 1 rather than propagating.
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value="full text"), \
         patch.object(mod, "slice_table_window", return_value="w"), \
         patch.object(mod, "derive_position_date", return_value=POS), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=dict(GOOD)), \
         patch.object(mod, "upsert_metric_definitions_seed", side_effect=ConnectionError("dns fail")), \
         patch.object(mod, "upsert_metric_history") as up, \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    up.assert_not_called()
    assert noti.call_args.args[0] == "error"
    # C5: bare str(ConnectionError()) is empty — the type name must carry the signal.
    assert "ConnectionError" in noti.call_args.args[2]


# --- B: as_of hardening (owner-approved, amended after final review 2026-08-05) ---


def test_main_stale_position_exits_2_before_llm(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    ancient = date(2015, 12, 31)  # always > 800 days old, regardless of run date
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full",
                      return_value="SECTOR-WISE NON-PERFORMING LOANS DISTRIBUTION end-December 2015"), \
         patch.object(mod, "derive_position_date", return_value=ancient), \
         patch.object(mod, "already_captured") as captured, \
         patch.object(mod, "run_extraction") as rex, \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 2
    rex.assert_not_called()
    captured.assert_not_called()  # date sanity gates BEFORE the exact-date skip check
    assert noti.call_args.args[0] == "warning"  # matches the repo's existing stale-path convention


def test_main_future_position_exits_1_before_llm(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    future = date(date.today().year + 5, 12, 31)
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full",
                      return_value="SECTOR-WISE NON-PERFORMING LOANS DISTRIBUTION"), \
         patch.object(mod, "derive_position_date", return_value=future), \
         patch.object(mod, "already_captured") as captured, \
         patch.object(mod, "run_extraction") as rex, \
         patch.object(mod, "notify") as noti:
        assert mod.main() == 1
    rex.assert_not_called()
    captured.assert_not_called()
    assert noti.call_args.args[0] == "error"


def test_main_derives_position_date_from_window_not_full_document(tmp_path):
    fr = MagicMock(artifact_path=tmp_path / "f.pdf")
    # A newer decoy position date sits FAR outside the table-window slice
    # (_SLICE_BEFORE=2000 chars); the real table + its own date sits inside it.
    decoy = "end-December 2099 decoy " + ("filler " * 3000)
    real = "SECTOR-WISE NON-PERFORMING LOANS DISTRIBUTION table body end-December 2025 position"
    full_text = decoy + real
    with patch.object(mod, "fetch_latest_fsr", return_value=fr), \
         patch.object(mod, "extract_pdf_text_full", return_value=full_text), \
         patch.object(mod, "already_captured", return_value=False), \
         patch.object(mod, "run_extraction", return_value=dict(GOOD)), \
         patch.object(mod, "upsert_metric_definitions_seed", return_value=0), \
         patch.object(mod, "upsert_metric_history", return_value=22) as up, \
         patch.object(mod, "verify_landed_count"):
        assert mod.main() == 0
    assert up.call_args.kwargs["as_of"] == date(2025, 12, 31)  # window's date, not the 2099 decoy
