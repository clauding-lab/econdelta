"""Phase-1 collection-layer rework: multi-link discovery, outlet inference, --url feed."""
from __future__ import annotations

from datetime import date

import scrapers.media_screen as ms
from media_screen.types import Extracted


def test_discover_article_links_latest_n_absolute_deduped():
    html = """
      <a href="/economy/banking/npls-surge-1452366">a</a>
      <a href="/economy/banking/islami-bank-1452211">b</a>
      <a href="/economy/banking/npls-surge-1452366">dup</a>
      <a href="/about">nav</a>
      <a href="/economy/banking/third-1451000">c</a>
    """
    out = ms._discover_article_links(
        html, "https://www.tbsnews.net/economy/banking", r"/economy/[^\"']+-\d{5,}", limit=2,
    )
    assert out == [
        "https://www.tbsnews.net/economy/banking/npls-surge-1452366",
        "https://www.tbsnews.net/economy/banking/islami-bank-1452211",
    ]


def test_dailystar_pattern_matches_business_news_path():
    """Daily Star banking articles live under /business/news/ and
    /business/economy/news/ — not /business/banking/ (the old miss)."""
    html = (
        '<a href="/business/news/banking-vulnerabilities-persist-bb-report-4183471">x</a>'
        '<a href="/business/economy/news/npls-tk-31487cr-just-three-months-4189176">y</a>'
    )
    out = ms._discover_article_links(
        html, "https://www.thedailystar.net/business/banking", r"/business/[^\"']+-\d{6,}", limit=5,
    )
    assert "https://www.thedailystar.net/business/economy/news/npls-tk-31487cr-just-three-months-4189176" in out
    assert len(out) == 2


def test_outlet_inference():
    assert ms._outlet_of("https://www.tbsnews.net/economy/banking/x-1452366") == "tbsnews"
    assert ms._outlet_of("https://www.thedailystar.net/business/economy/news/x-4189176") == "thedailystar"
    assert ms._outlet_of("https://example.com/x") == "example.com"


def test_articles_from_urls_fetches_and_infers_outlet(monkeypatch):
    monkeypatch.setattr(ms, "_fetch_article_text", lambda u: f"text:{u}")
    out = ms._articles_from_urls([
        "https://www.tbsnews.net/economy/banking/a-1452366",
        "https://www.thedailystar.net/business/economy/news/b-4189176",
    ])
    assert [o[2] for o in out] == ["tbsnews", "thedailystar"]
    assert out[0][0] == "text:https://www.tbsnews.net/economy/banking/a-1452366"


def test_articles_from_urls_skips_fetch_failure(monkeypatch):
    def boom(u):
        raise RuntimeError("fetch fail")
    monkeypatch.setattr(ms, "_fetch_article_text", boom)
    assert ms._articles_from_urls(["https://www.tbsnews.net/a-1452366"]) == []


def test_run_screen_with_fed_urls_bypasses_sweep(monkeypatch):
    monkeypatch.setattr(ms, "_fetch_article_text", lambda u: "body")
    monkeypatch.setattr(
        ms, "extract_numbers",
        lambda text, *, specs, source_url, source_outlet: [
            Extracted("NPL ratio", 32.26, date(2026, 3, 31), "NPLs 32.26% end-March 2026",
                      source_url, source_outlet)
        ],
    )
    monkeypatch.setattr(ms, "_parsed_for", lambda mid: (35.73, date(2025, 9, 30)))
    monkeypatch.setattr(ms, "get_open_media_review", lambda **k: [])

    def _sweep_must_not_run(specs):
        raise AssertionError("section sweep must be skipped when --url is given")

    monkeypatch.setattr(ms, "_collect_articles", _sweep_must_not_run)
    captured = {}
    monkeypatch.setattr(ms, "insert_media_review_rows",
                        lambda c, **k: captured.setdefault("c", c) or len(c))
    monkeypatch.setattr(ms, "notify", lambda *a, **k: True)

    rc = ms.run_screen(dry_run=False, urls=["https://www.tbsnews.net/economy/banking/npls-1452366"])
    assert rc == 0
    assert captured["c"][0].metric_id == "gross_npl_ratio"
    assert captured["c"][0].kind == "fresher_period"
    assert captured["c"][0].press_value == 32.26
