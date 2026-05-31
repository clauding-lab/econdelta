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


def _registry_with_rrpt_indicator() -> dict:
    return {
        "version": "3.0",
        "indicators": [
            {
                "id": "slf_draw_cr",
                "cadence": "daily",
                "fetch": {
                    "type": "html",
                    "url": "https://www.bb.org.bd/en/index.php/mediaroom/press_release",
                    "discover": "latest_rrpt_link",
                    "title_pattern": "Result of the Auction",
                },
                "domain": "money_market",
            },
        ],
    }


def test_rrpt_listing_uses_solver_path_not_plain_urllib(tmp_path: Path):
    """R2: the rrpt LISTING (slf_draw_cr / bb_repo_usage_cr) must be fetched through
    the CAPTCHA-solving rendered path, NOT plain urllib (_download_index_html) —
    behind the wall the latter sees zero /rrpt/ anchors and discovery dies."""
    cfg = tmp_path / "sources-v3.json"
    cfg.write_text(json.dumps(_registry_with_rrpt_indicator()))

    with (
        patch(
            "fetch_all._download_rendered_html", return_value="<html>listing</html>"
        ) as rendered_mock,
        patch("fetch_all._download_index_html") as index_mock,
        patch(
            "fetch_all.discover_latest_rrpt_link",
            return_value="https://www.bb.org.bd/en/index.php/mediaroom/press_release_details/rrpt/9001",
        ),
        patch("fetch_all.fetch_html") as html_mock,
    ):
        html_mock.return_value = MagicMock(cache_hit=False, indicator_id="slf_draw_cr")
        results = fetch_all.run(config_path=cfg, data_root=tmp_path / "data")

    assert len(results) == 1
    rendered_mock.assert_called_once_with(
        "https://www.bb.org.bd/en/index.php/mediaroom/press_release"
    )
    index_mock.assert_not_called()  # the rrpt listing must NOT use plain urllib


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
