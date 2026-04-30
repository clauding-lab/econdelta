"""Tests fetch_all entry point with mocked fetchers."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import fetch_all


def _registry_with_two_indicators() -> dict:
    return {
        "version": "3.0",
        "indicators": [
            {
                "id": "policy_rate",
                "cadence": "daily",
                "fetch": {"type": "html", "url": "https://www.bb.org.bd/en/"},
                "domain": "money_market",
            },
            {
                "id": "broad_money",
                "cadence": "monthly",
                "fetch": {
                    "type": "pdf",
                    "url": "https://www.bb.org.bd/en/index.php/publication/publictn/5/27",
                    "discover": "latest_pdf_link",
                    "task": "Component 11a",
                },
                "domain": "monetary_aggregates",
            },
        ],
    }


def test_fetch_all_dispatches_html_and_pdf(tmp_path: Path):
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps(_registry_with_two_indicators()))

    with patch("fetch_all.fetch_html") as html_mock, patch("fetch_all.fetch_pdf") as pdf_mock:
        html_mock.return_value = MagicMock(cache_hit=False, indicator_id="policy_rate")
        pdf_mock.return_value = MagicMock(cache_hit=False, indicator_id="broad_money")
        with patch("fetch_all.discover_latest_pdf_link", return_value="https://example.com/x.pdf"), \
             patch("fetch_all._download_index_html", return_value="<html></html>"):
            results = fetch_all.run(config_path=cfg, data_root=tmp_path / "data")

    assert len(results) == 2
    html_mock.assert_called_once()
    pdf_mock.assert_called_once()
