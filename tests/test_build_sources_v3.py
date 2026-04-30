"""Tests the one-shot v2 → v3 migration script."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_build_sources_v3_preserves_all_v2_ids(tmp_path):
    out = tmp_path / "sources-v3.json"
    subprocess.run(
        [sys.executable, str(REPO / "scripts" / "build_sources_v3.py"),
         "--in", str(REPO / "config" / "sources-v2.json"),
         "--out", str(out)],
        check=True,
    )
    v2 = json.loads((REPO / "config" / "sources-v2.json").read_text())
    v3 = json.loads(out.read_text())

    v2_ids = {i["id"] for i in v2["indicators"]}
    v3_ids = {i["id"] for i in v3["indicators"]}
    assert v2_ids == v3_ids, f"id mismatch: missing={v2_ids - v3_ids}, extra={v3_ids - v2_ids}"

    for ind in v3["indicators"]:
        assert ind["domain"] in {
            "forex_and_reserves", "money_market", "monetary_aggregates",
            "inflation", "government_finance", "external_sector",
            "commodities", "equities", "macro",
        }, f"{ind['id']} has bad domain {ind['domain']}"
        assert ind["parse"]["value_type"] in {
            "percent", "amount_bdt_crore", "amount_usd_bn", "ratio", "count", "rate"
        }
        assert isinstance(ind["parse"]["valid_range"], list) and len(ind["parse"]["valid_range"]) == 2
        assert isinstance(ind["anomaly_threshold"], (int, float))
