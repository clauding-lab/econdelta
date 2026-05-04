import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import parse_all


def test_parse_all_writes_per_indicator_snapshots(tmp_path: Path):
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps({
        "version": "3.0",
        "indicators": [
            {"id": "x", "name": "X", "domain": "money_market", "cadence": "daily",
             "fetch": {"type": "html", "url": "https://example.com", "task": "x"},
             "parse": {"deterministic": "html_footer_ticker", "value_type": "percent",
                       "valid_range": [0, 100], "llm_prompt": "html_footer_ticker.txt"}},
        ],
    }))
    fake_artifact_path = tmp_path / "x.html"
    fake_artifact_path.write_text("<html></html>")
    fake_artifact = type("FR", (), {
        "indicator_id": "x", "artifact_path": fake_artifact_path, "artifact_type": "html",
        "fetched_at": datetime.now(timezone.utc), "source_url": "x", "sha256": "y"*64, "cache_hit": False,
    })()
    fake_snapshot = {"indicator_id": "x", "value": 10.0, "_provenance": "deterministic"}
    with patch("parse_all._load_artifact_for", return_value=fake_artifact), \
         patch("parse_all.parse_one", return_value=fake_snapshot):
        results = parse_all.run(config_path=cfg, data_root=tmp_path / "data")
    assert results
    assert results[0]["value"] == 10.0
    out_files = list((tmp_path / "data" / "x").glob("*.json"))
    assert len(out_files) == 1
