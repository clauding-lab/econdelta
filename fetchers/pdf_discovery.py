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


def discover_latest_pdf(*, html: str, base_url: str) -> tuple[str, tuple[int, int]]:
    """Return ``(url, (year, month))`` for the most recent PDF on a BB index page.

    The ``(year, month)`` is the issue's reporting period, parsed from the link's
    row text — the authoritative vintage of the chosen issue. Callers persist it
    into the artifact's ``.meta.json`` sidecar so parse can select the newest
    ISSUE by recorded period rather than by file mtime, which is immune to both
    mtime races and filename-convention drift when a month-dir accumulates
    multiple issues (E1 MEI leftover — see parse_all._load_artifact_for).
    """
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
    period, url = candidates[0]
    return url, period


def discover_latest_pdf_link(*, html: str, base_url: str) -> str:
    """Back-compat wrapper returning only the URL (period discarded)."""
    url, _period = discover_latest_pdf(html=html, base_url=base_url)
    return url
