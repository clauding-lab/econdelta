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
        with patch(
            "fetch_all.discover_latest_pdf",
            return_value=("https://example.com/x.pdf", (2026, 5)),
        ), patch("fetch_all._download_index_html", return_value="<html></html>"):
            results = fetch_all.run(config_path=cfg, data_root=tmp_path / "data")

    assert len(results) == 2
    html_mock.assert_called_once()
    pdf_mock.assert_called_once()
    # The discovered issue period is threaded to fetch_pdf so it lands in the
    # artifact sidecar (E1 MEI leftover — parse selects newest issue by period).
    assert pdf_mock.call_args.kwargs["period"] == (2026, 5)


def test_pdf_index_fetch_error_is_contained_not_crashing(tmp_path: Path):
    """A debt-bulletin-style 404 (or TLS error) on a pdf+latest_pdf_link INDEX fetch
    must be contained to that one indicator (FetchError → skip), NOT raise an uncaught
    HTTPError that aborts the whole fetch stage and every later indicator."""
    from urllib.error import HTTPError

    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps({
        "version": "3.0",
        "indicators": [
            {"id": "debt_gdp_ratio", "cadence": "monthly", "domain": "government_finance",
             "fetch": {"type": "pdf", "url": "https://mof.gov.bd/site/page/debt-bulletin",
                       "discover": "latest_pdf_link", "task": "x"}},
            {"id": "policy_rate", "cadence": "daily", "domain": "money_market",
             "fetch": {"type": "html", "url": "https://www.bb.org.bd/en/"}},
        ],
    }))

    def _boom(url):
        raise HTTPError(url, 404, "Not Found", {}, None)

    with patch("fetch_all._download_index_html", side_effect=_boom), \
         patch("fetch_all.fetch_html") as html_mock:
        html_mock.return_value = MagicMock(cache_hit=False, indicator_id="policy_rate")
        results = fetch_all.run(config_path=cfg, data_root=tmp_path / "data")

    # debt_gdp_ratio is skipped (its index 404'd); policy_rate after it still fetched.
    assert [r.indicator_id for r in results] == ["policy_rate"]
    html_mock.assert_called_once()


def test_pdf_discovery_failure_is_contained_and_later_sources_run(tmp_path: Path, caplog):
    """A successful index download with no discoverable PDF must not abort
    the registry walk; all 61 following indicators still get attempted."""
    indicators = []
    for index in range(64):
        fetch = {"type": "pdf", "url": f"https://example.test/{index}"}
        if index == 2:
            fetch["discover"] = "latest_pdf_link"
        indicators.append({
            "id": f"metric_{index}", "cadence": "monthly", "domain": "test",
            "fetch": fetch,
        })
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps({"version": "3.0", "indicators": indicators}))

    fetched = []

    def fake_pdf(*, url, indicator_id, **kwargs):
        fetched.append(indicator_id)
        return MagicMock(cache_hit=False, indicator_id=indicator_id)

    with patch("fetch_all._download_index_html", return_value="<html>200 OK</html>"), \
         patch("fetch_all.discover_latest_pdf", side_effect=ValueError("no dated PDF links found")), \
         patch("fetch_all.fetch_pdf", side_effect=fake_pdf):
        results = fetch_all.run(config_path=cfg, data_root=tmp_path / "data")

    assert len(results) == 63
    assert "metric_2" not in fetched
    assert fetched[-61:] == [f"metric_{i}" for i in range(3, 64)]
    assert "fetch_failed: metric_2" in caplog.text
    assert "no dated PDF links found" in caplog.text
