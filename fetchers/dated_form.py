"""Fetch a date-parameterised page by asking it for a date that has data.

Some sources render "the position as at <date>" and default that date to
*today*. BB's rebuilt Government Securities Online Market portal
(``gsom.bb.org.bd/index.php/tbill``) is one: it carries a
``<input name="picker_date" value="03-SEP-26">`` and a POST form, and when
the requested day has no data it still returns 200 with a well-formed table
whose body is empty and whose total row reads ``0``.

That is how ``treasury_bill_outstanding`` broke (landmine 58). The fetch
stage runs at 01:11 BDT, before BB populates the day's T-bill row, so the
page honestly answered 0 — and 0 is what ``_is_bad_snapshot`` treats as a
failed parse. Probing the form directly showed the zero is not only an
early-hours artefact: T-bill also returns 0 on the Friday/Saturday weekend
and on the occasional ordinary weekday. T-bond, on the same portal, answers
for every date, which is why only one of the pair ever failed.

So "ask for yesterday" is not the fix — yesterday can be a Friday. This
module walks backwards from ``start_offset_days`` until a candidate date
returns a page the caller's ``accept`` predicate is happy with, and reports
which date that was so the value can be dated by the day it describes rather
than the day we downloaded it.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fetchers.base import FetchError, FetchResult
from utils.http_client import HttpClient

logger = logging.getLogger("dated_form")

# The pipeline runs on UTC but BB publishes on Dhaka time; "today" for the
# purpose of choosing a first candidate date is the Dhaka day.
DHAKA_UTC_OFFSET = timedelta(hours=6)

# Give up after this many candidates. Ten days clears the longest observed
# run of empty days (a Fri/Sat weekend either side of a blank weekday) with
# room to spare, and bounds the request burst we put on BB's server.
DEFAULT_MAX_LOOKBACK_DAYS = 10

# Start at yesterday, not today: today's row is routinely still empty at the
# 01:11 BDT fetch hour, and spending the first candidate on it is a wasted
# request every single night.
DEFAULT_START_OFFSET_DAYS = 1


def dhaka_today(now: datetime | None = None) -> date:
    """Today's date in Asia/Dhaka, derived from a UTC instant."""
    moment = now or datetime.now(timezone.utc)
    return (moment.astimezone(timezone.utc) + DHAKA_UTC_OFFSET).date()


def candidate_dates(
    *,
    today: date,
    start_offset_days: int = DEFAULT_START_OFFSET_DAYS,
    max_lookback_days: int = DEFAULT_MAX_LOOKBACK_DAYS,
) -> list[date]:
    """Newest-first candidate dates to try, starting `start_offset_days` back."""
    return [
        today - timedelta(days=start_offset_days + n) for n in range(max_lookback_days)
    ]


def fetch_dated_form(
    *,
    url: str,
    indicator_id: str,
    snapshot_dir: Path,
    field: str,
    date_format: str,
    accept: Callable[[str], bool],
    uppercase: bool = False,
    extra_fields: dict[str, str] | None = None,
    start_offset_days: int = DEFAULT_START_OFFSET_DAYS,
    max_lookback_days: int = DEFAULT_MAX_LOOKBACK_DAYS,
    now: datetime | None = None,
    client: HttpClient | None = None,
) -> FetchResult:
    """POST `url` for successive dates until `accept` likes the response.

    Args:
        field: form field carrying the date (e.g. ``"picker_date"``).
        date_format: ``strftime`` pattern the field expects (e.g. ``"%d-%b-%y"``).
        accept: called with each candidate's HTML; the first True wins. The
            caller supplies this — typically "the configured parser extracts a
            positive number" — so this module never has to know the markup.
        uppercase: upper-case the formatted date (the portal renders
            ``03-SEP-26``, and its own JS upper-cases what the picker writes).
        extra_fields: additional form fields posted alongside the date.

    Raises:
        FetchError: no candidate date was accepted, or every request failed.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    http = client or HttpClient()
    dates = candidate_dates(
        today=dhaka_today(now),
        start_offset_days=start_offset_days,
        max_lookback_days=max_lookback_days,
    )

    last_error: str | None = None
    rejected: list[str] = []
    for candidate in dates:
        rendered = candidate.strftime(date_format)
        if uppercase:
            rendered = rendered.upper()
        payload = {field: rendered, **(extra_fields or {})}
        # Try the SAME date twice before demoting it. The walk is newest-first,
        # so treating a transient connection reset as "this day has no data"
        # would silently publish an older figure — a network blip must not cost
        # a day of freshness. `post()` sits outside the session's Retry policy
        # (that policy covers GET/HEAD only), so this is the only retry there is.
        response = None
        for attempt in (1, 2):
            try:
                response = http.post(url, data=payload)
                break
            except Exception as e:  # noqa: BLE001 — one bad date must not end the walk
                last_error = f"{rendered}: {e}"
                logger.warning(
                    "%s: POST failed for %s (attempt %d/2): %s",
                    indicator_id, rendered, attempt, e,
                )
        if response is None:
            continue
        if response.status_code != 200:
            last_error = f"{rendered}: HTTP {response.status_code}"
            continue
        html = response.text
        try:
            usable = accept(html)
        except Exception as e:  # noqa: BLE001 — a parse blow-up is a rejection
            logger.info("%s: accept() raised for %s: %s", indicator_id, rendered, e)
            usable = False
        if not usable:
            rejected.append(rendered)
            continue

        if rejected:
            logger.info(
                "%s: %s carries data; skipped %d empty date(s): %s",
                indicator_id, rendered, len(rejected), ", ".join(rejected),
            )
        return _persist(
            html=html,
            indicator_id=indicator_id,
            snapshot_dir=snapshot_dir,
            url=url,
            # The injected clock, not a fresh one: a run that straddles UTC
            # midnight must not name the artifact after a different day than
            # the one the walk was computed for.
            fetched_at=now or datetime.now(timezone.utc),
        )

    raise FetchError(
        f"no date in the last {max_lookback_days} day(s) returned usable data for "
        f"{indicator_id} at {url} "
        f"(rejected: {', '.join(rejected) or 'none'}; last error: {last_error or 'none'})"
    )


def _persist(
    *,
    html: str,
    indicator_id: str,
    snapshot_dir: Path,
    url: str,
    fetched_at: datetime,
) -> FetchResult:
    """Write the accepted page to the same path `fetch_html` would use.

    Keeping the artifact layout identical means the parse stage's
    ``_load_artifact_for`` needs no special case for date-form indicators.
    """
    out_path = snapshot_dir / f"{fetched_at.strftime('%Y-%m-%d')}.html"
    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    cache_hit = out_path.exists() and (
        hashlib.sha256(out_path.read_bytes()).hexdigest() == sha
    )
    if not cache_hit:
        out_path.write_text(html)
    return FetchResult(
        indicator_id=indicator_id,
        artifact_path=out_path,
        artifact_type="html",
        fetched_at=fetched_at,
        source_url=url,
        sha256=sha,
        cache_hit=cache_hit,
    )
