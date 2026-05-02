"""Find the URL of the most recent article matching a regex pattern.

News sites (TBS, Daily Star) list articles newest-first within tag/section
pages. For NBR coverage we want the latest article whose URL slug
indicates revenue/collection/target reporting (vs unrelated NBR news like
'NBR chair appointed'). The caller passes `article_pattern` — a regex
matched against the href — and we return the first matching link in
document order, which on these sites is the most recently published.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup


def discover_latest_article_link(
    *,
    html: str,
    base_url: str,
    article_pattern: str,
) -> str:
    """Return the first <a href> in the listing matching article_pattern.

    Raises ValueError when no anchor matches; the caller (fetch_all.py)
    converts that into a FetchError so the indicator stays at needs_review.
    """
    pat = re.compile(article_pattern, re.IGNORECASE)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pat.search(href):
            return urljoin(base_url, href)
    raise ValueError(
        f"no anchors matching {article_pattern!r} found in listing"
    )
