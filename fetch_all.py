"""Stage 1 entry point: walk sources-v3.json, fetch every due indicator,
write artifacts under data/_pdfs/ and data/_html/.

Usage:
    python fetch_all.py [--dry-run] [--config config/sources-v3.json] [--only INDICATOR_ID]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from fetchers.base import FetchError, FetchResult
from fetchers.html_fetcher import fetch_html
from fetchers.news_article_discovery import discover_latest_article_link
from fetchers.pdf_discovery import discover_latest_pdf_link
from fetchers.pdf_fetcher import fetch_pdf
from fetchers.pdf_fetcher_stealth import fetch_pdf_stealth
from fetchers.tls import ssl_context_for

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "sources-v3.json"
DEFAULT_DATA_ROOT = REPO_ROOT / "data"

logger = logging.getLogger("fetch_all")


def _download_index_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": "EconDelta/3.0"})
    # Chain-completing TLS context for hosts that serve an incomplete cert chain
    # (e.g. mof.gov.bd, which the 3 debt_* metrics hit here FIRST via latest_pdf_link
    # discovery); None for every other host = urllib default.
    with urlopen(req, timeout=60, context=ssl_context_for(url)) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_one(indicator: dict, data_root: Path) -> FetchResult | None:
    fetch_block = indicator["fetch"]
    indicator_id = indicator["id"]
    if fetch_block["type"] == "html":
        target_url = fetch_block["url"]
        # Optional 2-step discovery: list page → article URL → article body.
        # Used by news-source NBR indicators where the listing carries
        # headlines + lede snippets but the actual numbers live inside
        # individual article pages.
        if fetch_block.get("discover") == "latest_article_link":
            try:
                listing_html = _download_index_html(target_url)
            except Exception as e:
                raise FetchError(
                    f"listing fetch failed for {target_url}: {e}"
                ) from e
            try:
                target_url = discover_latest_article_link(
                    html=listing_html,
                    base_url=target_url,
                    article_pattern=fetch_block["article_pattern"],
                )
            except ValueError as e:
                raise FetchError(f"article discovery failed: {e}") from e
            logger.info("discovered latest article for %s: %s", indicator_id, target_url)
        return fetch_html(
            url=target_url,
            indicator_id=indicator_id,
            snapshot_dir=data_root / "_html" / indicator_id,
        )
    if fetch_block["type"] == "pdf":
        url = fetch_block["url"]
        if fetch_block.get("discover") == "latest_pdf_link":
            # Contain a per-indicator index-fetch failure (e.g. a moved page → 404,
            # or a TLS error) as a FetchError so run() skips just this indicator
            # instead of an uncaught HTTPError/URLError aborting the whole fetch
            # stage. Mirrors the html discovery branch above.
            try:
                html = _download_index_html(url)
            except Exception as e:
                raise FetchError(f"index fetch failed for {url}: {e}") from e
            url = discover_latest_pdf_link(html=html, base_url=url)
        as_of_month = datetime.now(timezone.utc).strftime("%Y-%m")
        if fetch_block.get("stealth"):
            return fetch_pdf_stealth(
                url=url,
                indicator_id=indicator_id,
                snapshot_dir=data_root,
                as_of_month=as_of_month,
                prime_url=fetch_block.get("prime_url", "https://www.bb.org.bd/"),
            )
        return fetch_pdf(
            url=url,
            indicator_id=indicator_id,
            snapshot_dir=data_root,
            as_of_month=as_of_month,
        )
    logger.warning("unsupported fetch.type=%s for %s", fetch_block.get("type"), indicator_id)
    return None


def run(*, config_path: Path, data_root: Path, only: str | None = None, dry_run: bool = False) -> list[FetchResult]:
    cfg = json.loads(config_path.read_text())
    results: list[FetchResult] = []
    for ind in cfg["indicators"]:
        if only and ind["id"] != only:
            continue
        if dry_run:
            logger.info("[dry-run] would fetch %s (%s)", ind["id"], ind["fetch"]["type"])
            continue
        try:
            r = _fetch_one(ind, data_root)
        except FetchError as e:
            logger.error("fetch_failed: %s — %s", ind["id"], e)
            continue
        if r:
            results.append(r)
            logger.info("fetched %s sha=%s cache_hit=%s", r.indicator_id, r.sha256[:8], r.cache_hit)
    return results


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--only", type=str, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    results = run(config_path=args.config, data_root=args.data_root, only=args.only, dry_run=args.dry_run)
    cache_hits = sum(1 for r in results if r.cache_hit)
    print(f"Fetched: {len(results)} · Cache hits: {cache_hits} · Failed: see log")
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("fetch", "econdelta-fetch.service", main))
