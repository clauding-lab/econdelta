"""Daily media screen — detection only (Phase 1).

Collects press articles, extracts dated figures via the Max CLI, compares each
to the currently-parsed value, applies the strict filter, dedups against open
review rows, inserts survivors as 'pending', and pings one Discord digest.
Writes ONLY media_review — never metric_history (Phases 2/3 handle apply).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date as _date
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from fetchers.base import FetchError
from fetchers.news_article_discovery import discover_latest_article_link
from media_screen.catalog import load_catalog
from media_screen.dedup import drop_already_open
from media_screen.digest import format_digest
from media_screen.extract import extract_numbers
from media_screen.filter import classify
from utils.notifier import notify
from utils.supabase_reader import SupabaseReadError, get_metric_history, get_open_media_review
from utils.supabase_writer import SupabaseWriteError, insert_media_review_rows

logger = logging.getLogger("media_screen")

# (outlet_label, listing_url, article_pattern) — one entry per outlet to sweep.
# Phase 1 seed; extend or move to config as real candidate volume reveals more.
_OUTLET_SOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "tbsnews",
        "https://www.tbsnews.net/economy/banking",
        r"/economy/banking/[^\"/]+(npl|crar|reserve|inflation|credit)[^\"/]*",
    ),
    (
        "thedailystar",
        "https://www.thedailystar.net/business/banking",
        r"/business/banking/[^\"/]+(npl|reserve|inflation|credit)[^\"/]*",
    ),
    (
        "dhakatribune",
        "https://www.dhakatribune.com/business/banking",
        r"/business/banking/[^\"/]+(npl|reserve|inflation|credit)[^\"/]*",
    ),
)

_LISTING_TIMEOUT_S = 30
_USER_AGENT = "EconDelta-MediaScreen/1.0"


def _download_listing(url: str) -> str:
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=_LISTING_TIMEOUT_S) as r:
        return r.read().decode("utf-8", errors="replace")


def _fetch_article_text(url: str) -> str:
    """Fetch an article URL with a simple HTTP GET and extract visible text."""
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=_LISTING_TIMEOUT_S) as r:
        html = r.read().decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    # Prefer a semantic body element. A bare soup.find("div") returns the first
    # <div> in the document (usually the page header/nav wrapper) rather than the
    # article body, so we only trust the semantic tags and otherwise fall back to
    # the whole document text — never to an arbitrary leading <div>.
    for tag in ("article", "main"):
        el = soup.find(tag)
        if el:
            return el.get_text(" ", strip=True)
    return soup.get_text(" ", strip=True)


def _collect_articles(specs) -> list[tuple[str, str, str]]:
    """Return [(text, url, outlet)] for the day's relevant press articles.

    Reuses fetchers.news_article_discovery.discover_latest_article_link + a
    lightweight HTTP fetch per outlet (no Playwright: news articles don't need
    bot-bypass). Best-effort: a fetch failure for one outlet is logged and
    skipped — the screen must never crash on a bad source.
    """
    results: list[tuple[str, str, str]] = []
    for outlet, listing_url, article_pattern in _OUTLET_SOURCES:
        try:
            listing_html = _download_listing(listing_url)
        except Exception as e:
            logger.warning("media_screen: listing fetch failed for %s (%s): %s", outlet, listing_url, e)
            continue
        try:
            article_url = discover_latest_article_link(
                html=listing_html,
                base_url=listing_url,
                article_pattern=article_pattern,
            )
        except (ValueError, FetchError) as e:
            logger.warning("media_screen: article discovery failed for %s: %s", outlet, e)
            continue
        try:
            text = _fetch_article_text(article_url)
        except Exception as e:
            logger.warning("media_screen: article fetch failed for %s (%s): %s", outlet, article_url, e)
            continue
        logger.info("media_screen: collected article from %s: %s", outlet, article_url)
        results.append((text, article_url, outlet))
    return results


def _parsed_for(metric_id: str) -> tuple[float | None, _date | None]:
    """Return (value, as_of) of the current latest, or (None, None)."""
    try:
        rows = get_metric_history(metric_id, days=1)
    except SupabaseReadError as e:
        logger.warning("could not read parsed value for %s: %s", metric_id, e)
        return None, None
    if not rows:
        return None, None
    return float(rows[0]["value"]), _date.fromisoformat(str(rows[0]["as_of"])[:10])


def run_screen(*, dry_run: bool) -> int:
    specs = load_catalog()
    by_name = {n.lower(): s for s in specs for n in s.press_names}
    candidates = []
    for text, url, outlet in _collect_articles(specs):
        for ex in extract_numbers(text, specs=specs, source_url=url, source_outlet=outlet):
            spec = by_name.get(ex.indicator_hint.lower())
            if spec is None:
                continue
            parsed_value, parsed_as_of = _parsed_for(spec.metric_id)
            c = classify(spec.metric_id, parsed_value, parsed_as_of, ex, tolerance=spec.tolerance)
            if c is not None:
                candidates.append(c)

    # Dedup against open review rows. A read failure here must not silently
    # discard the day's detected candidates — log, notify, and bail with rc=1.
    try:
        open_rows = get_open_media_review()
    except SupabaseReadError as e:
        logger.exception("media screen: could not read open review rows")
        notify("error", "media screen failed", f"open-review read failed: {e}")
        return 1
    candidates = drop_already_open(candidates, open_rows)
    digest = format_digest(candidates)

    if dry_run:
        for c in candidates:
            print(f"[DRY-RUN] {c.metric_id} {c.kind} press={c.press_value}@{c.press_as_of}")
        logger.info("dry-run: %d candidate(s), no insert/notify", len(candidates))
        return 0

    # Insert survivors. A write failure must be logged and surfaced — the digest
    # has already been computed, so a silent throw would leave rows unwritten with
    # no recorded failure.
    if candidates:
        try:
            insert_media_review_rows(candidates)
        except SupabaseWriteError as e:
            logger.exception("media screen: insert into media_review failed")
            notify("error", "media screen failed", f"media_review insert failed: {e}")
            return 1
    if digest is not None:
        notify("warning", digest[0], digest[1], fields=digest[2])
    logger.info("media screen: %d candidate(s) inserted", len(candidates))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run_screen(dry_run=args.dry_run)


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("media_screen", "econdelta-media-screen.service", main))
