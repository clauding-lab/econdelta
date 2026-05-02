"""Find the most recent PDF link on a Bangladesh Bank publication index page."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"[,\s]+(\d{4})",
    re.IGNORECASE,
)


def discover_latest_pdf_link(*, html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[tuple[int, int], str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        row = a.find_parent("tr") or a
        text = row.get_text(" ", strip=True).lower()
        m = _MONTH_RE.search(text)
        if not m:
            continue
        month_num = _MONTHS[m.group(1).lower()]
        year = int(m.group(2))
        candidates.append(((year, month_num), urljoin(base_url, href)))
    if not candidates:
        raise ValueError("no pdf links with month/year context found")
    candidates.sort(reverse=True)
    return candidates[0][1]
