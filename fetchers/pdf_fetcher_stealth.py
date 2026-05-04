"""Stealth-PDF fetcher for Akamai/Radware-protected PDF endpoints.

Uses Playwright stealth to first 'prime' an HTML page on the same domain
(solving the Akamai TSPD challenge and setting cookies in the browser
context), then downloads the PDF via context.request.get() which carries
those cookies. Without this priming, direct urllib/requests fetches of
www.bb.org.bd PDF endpoints return HTTP 200 with the JS challenge HTML
instead of the PDF body.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pdfplumber
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from fetchers.base import FetchError, FetchResult

logger = logging.getLogger("pdf_fetcher_stealth")

_DEFAULT_PRIME_URL = "https://www.bb.org.bd/"

# Time for Akamai challenge JS to run + set TSPD cookies on the prime page.
_PRIME_SETTLE_MS = 10_000

# Time for the second visit to render the real (post-challenge) page.
_RELOAD_SETTLE_MS = 5_000

# Page.goto / context.request.get timeout — same value bb_forex.py uses
# after the dawn-hour latency bump (45s → 90s).
_DEFAULT_TIMEOUT_MS = 90_000


def _derive_filename(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name or "download.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def _looks_like_pdf(body: bytes) -> bool:
    return body[:5] == b"%PDF-"


def _stealth_download(*, url: str, prime_url: str, timeout_ms: int) -> bytes:
    """Prime Akamai cookies on prime_url with the bb_forex.py double-load
    pattern (goto + 10s settle + reload + 5s settle), then download `url`
    via the context-shared HTTP client so the cookies travel with it.

    The double-load is critical: BB's Radware TSPD challenge sets cookies
    on the FIRST visit but only renders real content on the SECOND visit
    once those cookies replay back. A single goto + context.request.get
    yields the challenge HTML for the PDF endpoint.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Asia/Dhaka",
            accept_downloads=True,
            extra_http_headers={
                "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        try:
            # First load — challenge page renders, sets TSPD cookies.
            page.goto(prime_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(_PRIME_SETTLE_MS)
            # Second load — challenge cookies replay, real page renders.
            page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(_RELOAD_SETTLE_MS)
            # Now the context has a full session of cookies. Pull the PDF
            # via context.request which carries them.
            response = context.request.get(url, timeout=timeout_ms)
            if response.status != 200:
                raise FetchError(f"stealth GET {url} returned HTTP {response.status}")
            body = response.body()
        finally:
            browser.close()
    return body


def _safe_page_count(path: Path) -> int:
    try:
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def fetch_pdf_stealth(
    *,
    url: str,
    indicator_id: str,
    snapshot_dir: Path,
    as_of_month: str,
    prime_url: str = _DEFAULT_PRIME_URL,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
) -> FetchResult:
    out_dir = snapshot_dir / "_pdfs" / indicator_id / as_of_month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / _derive_filename(url)

    body = _stealth_download(url=url, prime_url=prime_url, timeout_ms=timeout_ms)
    if not _looks_like_pdf(body):
        raise FetchError(
            f"stealth fetch returned non-PDF for {url} "
            f"(first 8 bytes={body[:8]!r}, total {len(body)} bytes); "
            f"Akamai challenge probably persisted past prime"
        )

    sha = hashlib.sha256(body).hexdigest()
    cache_hit = out_path.exists() and (
        hashlib.sha256(out_path.read_bytes()).hexdigest() == sha
    )
    if not cache_hit:
        out_path.write_bytes(body)
        sidecar = {
            "source_url": url,
            "prime_url": prime_url,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "sha256": sha,
            "page_count": _safe_page_count(out_path),
            "byte_size": len(body),
            "fetch_strategy": "stealth_playwright",
        }
        out_path.with_suffix(".meta.json").write_text(json.dumps(sidecar, indent=2))

    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=out_path,
        artifact_type="pdf",
        fetched_at=datetime.now(timezone.utc),
        source_url=url,
        sha256=sha,
        cache_hit=cache_hit,
    )
