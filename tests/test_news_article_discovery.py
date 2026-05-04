"""Tests for the news-article discovery helper used by NBR indicators."""
import pytest

from fetchers.news_article_discovery import discover_latest_article_link


_TBS_FIXTURE = """
<html><body>
<div class="listing">
  <a href="/videos/tbs-news-day">News of the day</a>
  <a href="/nbr/fresh-move-split-nbr-nine-member-committee-formed-1425471">Fresh move to split NBR</a>
  <a href="/nbr/nbr-faces-tk98000cr-revenue-shortfall-despite-11-growth-1417621">NBR faces Tk98,000cr revenue shortfall</a>
  <a href="/nbr/uniform-15-vat-rate-planned-all-sectors-nbr-chief-1417716">Uniform 15% VAT rate planned</a>
</div>
</body></html>
"""


def test_returns_first_matching_anchor_in_document_order():
    """News listings render newest-first; the first matching anchor IS
    the latest article. Document order = recency on these sites."""
    url = discover_latest_article_link(
        html=_TBS_FIXTURE,
        base_url="https://www.tbsnews.net/nbr",
        article_pattern=r"/nbr/[^\"]*(revenue|collect|target|shortfall)",
    )
    assert url == "https://www.tbsnews.net/nbr/nbr-faces-tk98000cr-revenue-shortfall-despite-11-growth-1417621"


def test_skips_anchors_that_dont_match_pattern():
    url = discover_latest_article_link(
        html=_TBS_FIXTURE,
        base_url="https://www.tbsnews.net/nbr",
        article_pattern=r"vat-rate",
    )
    assert "vat-rate" in url


def test_raises_when_no_anchor_matches():
    with pytest.raises(ValueError, match="no anchors matching"):
        discover_latest_article_link(
            html=_TBS_FIXTURE,
            base_url="https://www.tbsnews.net/nbr",
            article_pattern=r"nonexistent-keyword",
        )
