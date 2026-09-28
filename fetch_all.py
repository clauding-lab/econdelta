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
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import parsers.gsom_total_row  # noqa: F401 — registry side-effect
import parsers.html_dated_table_row  # noqa: F401 — registry side-effect (upstream-poll vintage)
import parsers.html_table_row  # noqa: F401 — registry side-effect
import parsers.pdf_table_row  # noqa: F401 — registry side-effect (upstream-poll vintage)
from fetchers.base import FetchError, FetchResult
from fetchers.dated_form import fetch_dated_form
from fetchers.html_fetcher import fetch_html
from fetchers.news_article_discovery import discover_latest_article_link
from fetchers.pdf_discovery import discover_latest_pdf
from fetchers.pdf_fetcher import fetch_pdf
from fetchers.pdf_fetcher_stealth import fetch_pdf_stealth
from fetchers.tls import ssl_context_for
from parsers.registry import get_parser
from utils.floor import assess_fetch_floor
from utils.notifier import notify
from utils.upstream_poll import DIRECTORY_NAME as UPSTREAM_POLL_DIRECTORY
from utils.upstream_poll import POLLED_SOURCE_IDS, record_upstream_poll

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


def _parses_to_positive(indicator: dict, data_root: Path):
    """Build the `accept` predicate for a date-form fetch.

    "Usable" is defined by the indicator's OWN configured parser: run it over
    the candidate page and accept a strictly positive number. That keeps every
    markup assumption in the parser where it already lives — `fetchers` never
    learns what a total row looks like — and it matches how the rest of the
    pipeline judges a reading, since `_is_bad_snapshot` already treats 0/None
    as a failed parse. A date whose table is empty renders its total as `0`
    and is therefore rejected here, which is the whole point.
    """
    parser = get_parser(indicator["parse"]["deterministic"])
    instruction = indicator["fetch"].get("task", "")
    # Deliberately NOT under `_html/<id>/`: `parse_all._load_artifact_for`
    # globs `*.html` there and takes the newest by name, and pathlib's glob
    # DOES match dotfiles — so a scratch file living in that directory could
    # be selected as the day's artifact and reintroduce the very 0 this
    # predicate exists to reject. Its own tree, outside the glob root.
    probe_dir = data_root / "_probe" / indicator["id"]

    def accept(html: str) -> bool:
        probe_dir.mkdir(parents=True, exist_ok=True)
        # A real file, not a StringIO: parsers read `artifact.artifact_path`.
        # Overwritten per candidate; kept on disk after the walk so a failed
        # night leaves the last rejected page to look at.
        probe_path = probe_dir / "candidate.html"
        probe_path.write_text(html)
        result = parser.parse(
            FetchResult(
                indicator_id=indicator["id"],
                artifact_path=probe_path,
                artifact_type="html",
                fetched_at=datetime.now(timezone.utc),
                source_url=indicator["fetch"]["url"],
                sha256="0" * 64,
                cache_hit=False,
            ),
            instruction,
        )
        value = result.value
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0

    return accept


def _fetch_one(indicator: dict, data_root: Path) -> FetchResult | None:
    fetch_block = indicator["fetch"]
    indicator_id = indicator["id"]
    if fetch_block["type"] == "html":
        target_url = fetch_block["url"]
        # Date-parameterised page: ask it for a day that actually has data
        # instead of accepting whatever "today" renders (landmine 58).
        date_form = fetch_block.get("date_form")
        if date_form:
            # Everything in here is per-indicator config, and the caller's
            # handler catches FetchError ONLY. A missing `field`/`format` key,
            # an unregistered parser name (get_parser raises KeyError), or a
            # client that won't construct would otherwise escape as a bare
            # exception and end the whole fetch stage — and these two
            # indicators are #11 and #12 of 64, so a typo in one config block
            # would take the other 52 down with it. One bad indicator is one
            # bad indicator.
            try:
                return fetch_dated_form(
                    url=target_url,
                    indicator_id=indicator_id,
                    snapshot_dir=data_root / "_html" / indicator_id,
                    field=date_form["field"],
                    date_format=date_form["format"],
                    uppercase=bool(date_form.get("uppercase")),
                    extra_fields=date_form.get("extra_fields"),
                    start_offset_days=date_form.get("start_offset_days", 1),
                    max_lookback_days=date_form.get("max_lookback_days", 10),
                    accept=_parses_to_positive(indicator, data_root),
                )
            except FetchError:
                raise
            except Exception as e:
                raise FetchError(
                    f"date_form fetch is misconfigured for {indicator_id}: "
                    f"{type(e).__name__}: {e}"
                ) from e
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
        # The discovered issue period (year, month), persisted into the artifact
        # sidecar so parse selects the newest ISSUE by recorded period, not mtime
        # (E1 MEI leftover). None for fixed-URL PDFs (no discovery) → mtime fallback.
        period: tuple[int, int] | None = None
        if fetch_block.get("discover") == "latest_pdf_link":
            # Both a failed index download and an HTTP-200 page with no usable
            # PDF link are source-local failures. Keep them inside this
            # indicator's boundary so the rest of the registry still runs.
            try:
                html = _download_index_html(url)
                url, period = discover_latest_pdf(html=html, base_url=url)
            except Exception as e:
                raise FetchError(
                    f"PDF discovery failed for {indicator_id}: {type(e).__name__}: {e}"
                ) from e
        as_of_month = datetime.now(timezone.utc).strftime("%Y-%m")
        if fetch_block.get("stealth"):
            return fetch_pdf_stealth(
                url=url,
                indicator_id=indicator_id,
                snapshot_dir=data_root,
                as_of_month=as_of_month,
                prime_url=fetch_block.get("prime_url", "https://www.bb.org.bd/"),
                period=period,
            )
        return fetch_pdf(
            url=url,
            indicator_id=indicator_id,
            snapshot_dir=data_root,
            as_of_month=as_of_month,
            period=period,
        )
    logger.warning("unsupported fetch.type=%s for %s", fetch_block.get("type"), indicator_id)
    return None


def _poll_clock() -> datetime:
    """When a failed poll was attempted (a successful one carries its fetcher's own time)."""
    return datetime.now(timezone.utc)


def _read_vintage(indicator: dict, result: FetchResult) -> tuple[date | None, str]:
    """The period the fetched page states, read without any model call: the configured
    deterministic parser's `source_as_of`, else its `recover_source_as_of` -- the same date
    the parse stage stamps on an LLM-extracted value (MEI PDF, landmine 29). Returns
    (vintage, why none) and never raises."""
    parser = get_parser(indicator["parse"]["deterministic"])
    why = "the page states no vintage"
    try:
        vintage = parser.parse(result, indicator["fetch"].get("task", "")).source_as_of
    except Exception as exc:  # noqa: BLE001 -- a page we cannot read is a poll outcome, not a crash
        vintage, why = None, type(exc).__name__
    recover = getattr(parser, "recover_source_as_of", None)
    if vintage is None and recover is not None:
        try:
            vintage = recover(result)
        except Exception as exc:  # noqa: BLE001
            why = type(exc).__name__
    return vintage, why


def _record_upstream_poll(indicator: dict, data_root: Path, *, result: FetchResult | None = None,
                          failure: Exception | None = None) -> None:
    """R2 fix H3: this fetch IS the upstream CPI/M2 poll, so it leaves a typed receipt in its
    own directory (never the monthly legs' database-reread receipts). Local file only; a
    receipt problem is logged and never costs the fetch stage."""
    if indicator["id"] not in POLLED_SOURCE_IDS:
        return
    status, vintage, reason = "failed", None, f"fetch failed ({type(failure).__name__})"
    checked_at = _poll_clock()
    try:
        if result is not None:
            vintage, why = _read_vintage(indicator, result)
            status = "ok" if vintage is not None else "unknown"
            reason = None if vintage is not None else (
                f"fetched, but no source vintage could be read from the page ({why})")
            checked_at = result.fetched_at
        record_upstream_poll(
            indicator["id"], status=status, checked_at=checked_at,
            source_url=result.source_url if result is not None else indicator["fetch"]["url"],
            latest_source_vintage=vintage, reason=reason,
            directory=data_root / UPSTREAM_POLL_DIRECTORY,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("upstream poll receipt not written for %s: %s", indicator["id"],
                       type(exc).__name__)


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
            _record_upstream_poll(ind, data_root, failure=e)
            continue
        if r:
            _record_upstream_poll(ind, data_root, result=r)
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

    # E2.5 deterministic floor — fires on a systemic fetch outage BEFORE and
    # independent of the downstream LLM review. Skipped for --only (targeted
    # debug) and --dry-run (no fetch happened).
    if not args.dry_run and not args.only:
        cfg = json.loads(args.config.read_text())
        verdict = assess_fetch_floor(due=len(cfg.get("indicators", [])), fetched=len(results))
        if verdict.breached:
            logger.error("fetch floor breached: %s", verdict.reason)
            notify("error", "fetch floor breached", verdict.reason)
    return 0


if __name__ == "__main__":
    from utils.supabase_writer import wrap_run
    sys.exit(wrap_run("fetch", "econdelta-fetch.service", main))
