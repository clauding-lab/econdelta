"""Find the URL of the most recent BB auction-result press release (`rrpt`).

Bangladesh Bank publishes one money-market auction-result press release per
business day under ``mediaroom/press_release_details/rrpt/<id>`` — titled
"Result of the Auction of Repo, ALS, SLF, SDF and IBLF held on <date>". The
listing page (``mediaroom/press_release``) carries every press-release link
newest-first, but it mixes auction results with unrelated notices (circulars,
appointments, holidays), so we cannot just take the first link.

We therefore match anchors against ``/rrpt/<digits>`` and rank by the numeric
``rrpt`` id: BB mints these ids monotonically, so the HIGHEST id is the most
recent auction-result page. (Document order on the listing is also newest-first,
but ranking by the numeric id is robust to a re-ordered or paginated listing.)

This mirrors ``news_article_discovery`` (the NBR pattern) but for BB's own
press-release detail route, and is wired into ``fetch_all.py`` via
``fetch.discover == "latest_rrpt_link"``.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# Capture the numeric id in a press-release-detail rrpt href, e.g.
# ".../mediaroom/press_release_details/rrpt/12345" or "...rrpt/12345/".
_RRPT_RE = re.compile(r"/rrpt/(\d+)", re.IGNORECASE)


def discover_latest_rrpt_link(
    *,
    html: str,
    base_url: str,
    title_pattern: str | None = None,
) -> str:
    """Return the press-release-detail URL with the highest ``rrpt`` id.

    Args:
        html: the press-release listing page markup.
        base_url: the listing URL, used to resolve relative hrefs.
        title_pattern: optional regex matched (case-insensitively) against the
            anchor's visible text to keep only auction-result notices (e.g.
            ``Result of the Auction``). When omitted, every ``/rrpt/<id>``
            anchor is a candidate and recency alone (highest id) decides.

    Raises:
        ValueError: when no ``/rrpt/<id>`` anchor (matching ``title_pattern``
            if given) is found; the caller (``fetch_all.py``) converts that
            into a ``FetchError`` so the indicator stays at needs_review.
    """
    title_re = re.compile(title_pattern, re.IGNORECASE) if title_pattern else None
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _RRPT_RE.search(href)
        if not m:
            continue
        if title_re is not None and not title_re.search(a.get_text(" ", strip=True)):
            continue
        candidates.append((int(m.group(1)), urljoin(base_url, href)))
    if not candidates:
        suffix = f" matching title {title_pattern!r}" if title_pattern else ""
        raise ValueError(f"no /rrpt/<id> anchors{suffix} found in listing")
    candidates.sort(reverse=True)
    return candidates[0][1]
