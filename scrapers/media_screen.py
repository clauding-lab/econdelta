"""Daily media screen — detection only (Phase 1).

Collects press articles (Daily Star + TBS banking sections, or a caller-supplied
``--url`` list), extracts dated figures via the Max CLI, compares each to the
currently-parsed value, applies the strict filter, dedups against open review
rows, inserts survivors as 'pending', and pings one Discord digest.
Writes ONLY media_review — never metric_history (Phases 2/3 handle apply).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import date as _date
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from media_screen.catalog import load_catalog
from media_screen.dedup import drop_already_open
from media_screen.digest import format_report
from media_screen.extract import extract_numbers
from media_screen.filter import classify
from media_screen.types import Candidate, Skip
from utils.notifier import notify
from utils.supabase_reader import SupabaseReadError, get_metric_history, get_open_media_review
from utils.supabase_writer import SupabaseWriteError, insert_media_review_rows

logger = logging.getLogger("media_screen")

# (outlet_label, listing_url, article_pattern). Daily Star + TBS only at this
# stage. Patterns match these sites' real article hrefs (section path + a
# trailing numeric article id) — NOT a keyword slug. Relevance is decided
# downstream by the catalog match + strict filter, so the sweep only needs the
# latest articles in each banking section.
#   TBS:        /economy/banking/<slug>-<id>     (7-digit id)
#   Daily Star: /business/news/<slug>-<id> and /business/economy/news/<slug>-<id>
_OUTLET_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("tbsnews", "https://www.tbsnews.net/economy/banking", r"/economy/[^\"']+-\d{5,}"),
    ("thedailystar", "https://www.thedailystar.net/business/banking", r"/business/[^\"']+-\d{6,}"),
)

# How many latest articles to read per outlet each run. The figure-bearing story
# usually isn't the single top item, so one-per-outlet missed everything.
_MAX_ARTICLES_PER_OUTLET = 6

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
    # <div> (usually the header/nav wrapper), so trust only semantic tags and
    # otherwise fall back to the whole document text.
    for tag in ("article", "main"):
        el = soup.find(tag)
        if el:
            return el.get_text(" ", strip=True)
    return soup.get_text(" ", strip=True)


def _discover_article_links(listing_html: str, base_url: str, pattern: str, limit: int) -> list[str]:
    """Latest `limit` distinct article URLs in the listing matching `pattern`,
    in document order (newest-first on these sites), as absolute URLs."""
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r'href="([^"]+)"', listing_html):
        href = m.group(1)
        if re.search(pattern, href) and href not in seen:
            seen.add(href)
            out.append(urljoin(base_url, href))
            if len(out) >= limit:
                break
    return out


def _outlet_of(url: str) -> str:
    """Infer the outlet label from a URL's host (for the --url feed)."""
    host = urlparse(url).netloc.lower()
    if "tbsnews" in host:
        return "tbsnews"
    if "thedailystar" in host:
        return "thedailystar"
    return host or "press"


def _articles_from_urls(urls) -> list[tuple[str, str, str]]:
    """Fetch a caller-supplied list of article URLs directly (the --url feed).
    Best-effort: a fetch failure for one URL is logged and skipped."""
    out: list[tuple[str, str, str]] = []
    for u in urls:
        try:
            out.append((_fetch_article_text(u), u, _outlet_of(u)))
        except Exception as e:  # noqa: BLE001 — never crash on one bad URL
            logger.warning("media_screen: fed-URL fetch failed (%s): %s", u, e)
    return out


def _collect_articles(specs) -> list[tuple[str, str, str]]:
    """Return [(text, url, outlet)] for the latest articles in each outlet's
    banking section. Best-effort per outlet — fetch/parse failures skip-and-log,
    so the screen never crashes on a bad source."""
    results: list[tuple[str, str, str]] = []
    for outlet, listing_url, article_pattern in _OUTLET_SOURCES:
        try:
            listing_html = _download_listing(listing_url)
        except Exception as e:  # noqa: BLE001
            logger.warning("media_screen: listing fetch failed for %s (%s): %s", outlet, listing_url, e)
            continue
        links = _discover_article_links(listing_html, listing_url, article_pattern, _MAX_ARTICLES_PER_OUTLET)
        if not links:
            logger.warning("media_screen: no article links matched for %s", outlet)
            continue
        for article_url in links:
            try:
                text = _fetch_article_text(article_url)
            except Exception as e:  # noqa: BLE001
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


def _dedup_candidates(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse the same (metric_id, press_as_of) seen in multiple articles;
    prefer the one carrying a source_quote."""
    best: dict[tuple, Candidate] = {}
    for c in candidates:
        key = (c.metric_id, c.press_as_of)
        cur = best.get(key)
        if cur is None or (c.source_quote and not cur.source_quote):
            best[key] = c
    return list(best.values())


def _dedup_skips(skips: list[Skip]) -> list[Skip]:
    """Collapse duplicate (metric_id, period, reason) skips."""
    best: dict[tuple, Skip] = {}
    for s in skips:
        best.setdefault((s.metric_id, s.period, s.reason), s)
    return list(best.values())


def run_screen(*, dry_run: bool, urls=None) -> int:
    specs = load_catalog()
    by_name = {n.lower(): s for s in specs for n in s.press_names}
    articles = _articles_from_urls(urls) if urls else _collect_articles(specs)
    n_tbs = sum(1 for _, _, outlet in articles if outlet == "tbsnews")
    n_ds = sum(1 for _, _, outlet in articles if outlet == "thedailystar")

    candidates: list[Candidate] = []
    skips: list[Skip] = []
    for text, url, outlet in articles:
        for ex in extract_numbers(text, specs=specs, source_url=url, source_outlet=outlet):
            spec = by_name.get(ex.indicator_hint.lower())
            if spec is None:
                continue
            parsed_value, parsed_as_of = _parsed_for(spec.metric_id)
            result = classify(spec.metric_id, parsed_value, parsed_as_of, ex,
                              tolerance=spec.tolerance, valid_range=spec.valid_range)
            if isinstance(result, Candidate):
                candidates.append(result)
            else:
                skips.append(result)

    candidates = _dedup_candidates(candidates)
    skips = _dedup_skips(skips)

    # Dedup candidates against open review rows; the dropped ones become skips.
    try:
        open_rows = get_open_media_review()
    except SupabaseReadError as e:
        logger.exception("media screen: could not read open review rows")
        notify("error", "media screen failed", f"open-review read failed: {e}")
        return 1
    kept = drop_already_open(candidates, open_rows)
    for c in candidates:
        if c not in kept:
            skips.append(Skip(c.metric_id, c.press_value, c.press_as_of, "already-in-review"))
    candidates = kept

    if dry_run:
        title, message, _ = format_report([(None, c) for c in candidates], skips, n_tbs, n_ds)
        print(f"[DRY-RUN] {title}\n{message}")
        logger.info("dry-run: %d candidate(s), %d skip(s), no insert/notify",
                    len(candidates), len(skips))
        return 0

    ids: list[int] = []
    if candidates:
        try:
            ids = insert_media_review_rows(candidates)
        except SupabaseWriteError as e:
            logger.exception("media screen: insert into media_review failed")
            notify("error", "media screen failed", f"media_review insert failed: {e}")
            return 1

    title, message, fields = format_report(list(zip(ids, candidates)), skips, n_tbs, n_ds)
    level = "warning" if candidates else "info"
    webhook = os.environ.get("MEDIA_SCREEN_WEBHOOK_URL", "").strip()
    if webhook:
        notify(level, title, message, fields=fields, webhook_url=webhook)
    else:
        logger.warning("MEDIA_SCREEN_WEBHOOK_URL not set — skipping #thebrief report")
    logger.info("media screen: %d candidate(s) inserted, %d skip(s)", len(candidates), len(skips))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--url", action="append", dest="urls", default=None,
                    help="Feed specific article URL(s) instead of sweeping sections (repeatable).")
    args = ap.parse_args()
    return run_screen(dry_run=args.dry_run, urls=args.urls)


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("media_screen", "econdelta-media-screen.service", main))
