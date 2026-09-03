"""Asking a date-parameterised page for a date that actually has data.

`treasury_bill_outstanding` published one 30 June reading for 60 days and
nothing in the pipeline said a word (landmine 58). The proximate cause was a
dead URL, but the reason the repoint did NOT fix it is the subject of this
file: BB's rebuilt gsom portal renders "the position as at <picker_date>",
that field defaults to *today*, and fetch runs at 01:11 BDT — hours before
BB populates the day's T-bill row. The page answered honestly with an empty
table and a total of 0, and 0 is what `_is_bad_snapshot` calls a failed
parse. Probing the form directly showed the zero is not just an early-hours
artefact: T-bill also reads 0 across the Fri/Sat weekend and on the odd
ordinary weekday. T-bond, same portal, answers for every date — which is
why only one of the pair ever broke.

So "ask for yesterday" is not the fix; yesterday can be a Friday. These
tests pin the three things that make the fix hold: the walk goes backwards
past empty days, "empty" is judged by the indicator's own parser rather than
a markup guess living in `fetchers/`, and the accepted day is recorded as
`source_as_of` so the figure is dated by the day it reports.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import parsers.gsom_total_row  # noqa: F401 — registry side-effect
import parsers.html_table_row  # noqa: F401 — registry side-effect
from fetchers.base import FetchError, FetchResult
from fetchers.dated_form import candidate_dates, dhaka_today, fetch_dated_form
from parsers.gsom_total_row import _PICKER_DATE_FORMAT
from parsers.registry import get_parser

# ---------------------------------------------------------------- fixtures


def _page(picker: str | None, total: str) -> str:
    """The rebuilt portal's shape: a picker echoing the answered date, and a
    tfoot total row with a colspan'd label and lakh-crore grouping."""
    picker_input = (
        f'<input type="text" name="picker_date" id="picker_date" value="{picker}">'
        if picker is not None
        else ""
    )
    return f"""
<html><body>
<form method="post" id="form-filter" action="https://gsom.bb.org.bd/index.php/tbill">
  <input type="hidden" name="ci_csrf_token" value="">
  {picker_input}
</form>
<table id="tbill_table">
  <tfoot>
    <tr class="footer-total">
      <td colspan="10" style="text-align:right">Total Outstanding Balance:</td>
      <td>{total}</td>
    </tr>
  </tfoot>
</table>
</body></html>
"""


# A day with data, and the empty day the portal serves for dates it has
# nothing for — well-formed, HTTP 200, total 0.
FULL_PAGE = _page("01-SEP-26", "22,10,000.00")
EMPTY_PAGE = _page("03-SEP-26", "0")

INSTRUCTION = "row=Total Outstanding Balance col=2"


class _Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _FakeClient:
    """Records every POST and answers from a {rendered_date: page} map."""

    def __init__(self, pages: dict[str, str], *, statuses: dict[str, int] | None = None,
                 raises: set[str] | None = None) -> None:
        self._pages = pages
        self._statuses = statuses or {}
        self._raises = raises or set()
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, data: dict | None = None, **kwargs):
        payload = data or {}
        self.calls.append((url, payload))
        rendered = payload.get("picker_date", "")
        if rendered in self._raises:
            raise RuntimeError(f"connection reset for {rendered}")
        return _Response(
            self._pages.get(rendered, EMPTY_PAGE),
            self._statuses.get(rendered, 200),
        )


def _accepts_positive_total(html: str) -> bool:
    """Stand-in for the production predicate: a strictly positive total."""
    return "Total Outstanding Balance" in html and ">0<" not in html


# ------------------------------------------------------------ date walking


class TestCandidateDates:
    def test_the_walk_runs_newest_first(self):
        got = candidate_dates(today=date(2026, 9, 3), max_lookback_days=3)
        assert got == [date(2026, 9, 2), date(2026, 9, 1), date(2026, 8, 31)]

    def test_it_starts_before_today_by_default(self):
        """Today's row is routinely still empty at the 01:11 BDT fetch hour,
        so spending the first request on it is wasted every single night."""
        assert candidate_dates(today=date(2026, 9, 3))[0] == date(2026, 9, 2)

    def test_the_offset_is_configurable(self):
        got = candidate_dates(
            today=date(2026, 9, 3), start_offset_days=0, max_lookback_days=2
        )
        assert got == [date(2026, 9, 3), date(2026, 9, 2)]

    def test_the_lookback_bounds_the_request_burst(self):
        """However many empty days there are, the walk is finite — BB's
        server must not see an unbounded probe. Assert the actual dates, not
        just the count: `len(range(n)) == n` is true of any implementation,
        including one that asks for the wrong ten days."""
        got = candidate_dates(today=date(2026, 9, 3), max_lookback_days=10)
        assert got == [date(2026, 9, 3) - timedelta(days=n) for n in range(1, 11)]
        assert len(set(got)) == len(got)  # no date asked for twice
        assert got[-1] == date(2026, 8, 24)


class TestDhakaToday:
    """The pipeline runs on UTC; BB publishes on Dhaka time. Picking the
    first candidate off the UTC date would ask for the wrong day for the
    six hours either side of midnight — including 01:11 BDT, the exact hour
    fetch runs."""

    def test_the_fetch_hour_resolves_to_the_dhaka_day(self):
        # 2026-09-02 19:11 UTC == 2026-09-03 01:11 BDT, the fetch hour.
        assert dhaka_today(datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc)) == date(
            2026, 9, 3
        )

    def test_just_before_dhaka_midnight_is_still_the_previous_day(self):
        assert dhaka_today(datetime(2026, 9, 2, 17, 59, tzinfo=timezone.utc)) == date(
            2026, 9, 2
        )


# --------------------------------------------------------------- fetching


class TestFetchDatedForm:
    def test_it_posts_the_date_the_page_expects(self, tmp_path: Path):
        client = _FakeClient({"02-SEP-26": FULL_PAGE})
        fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            extra_fields={"ci_csrf_token": ""},
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=client,
        )
        _url, payload = client.calls[0]
        # The portal's own JS upper-cases what the picker writes; posting
        # "02-Sep-26" is a different string to the page.
        assert payload["picker_date"] == "02-SEP-26"
        assert payload["ci_csrf_token"] == ""

    def test_it_walks_back_past_the_empty_days(self, tmp_path: Path):
        """The Fri/Sat weekend is the ordinary case, not the edge case."""
        client = _FakeClient({"31-AUG-26": FULL_PAGE})  # 2/1 Sep empty
        result = fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=client,
        )
        assert [p["picker_date"] for _u, p in client.calls] == [
            "02-SEP-26",
            "01-SEP-26",
            "31-AUG-26",
        ]
        assert "22,10,000.00" in result.artifact_path.read_text()

    def test_it_stops_at_the_first_usable_day(self, tmp_path: Path):
        """No point asking BB for ten dates once one has answered."""
        client = _FakeClient({"02-SEP-26": FULL_PAGE, "01-SEP-26": FULL_PAGE})
        fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=client,
        )
        assert len(client.calls) == 1

    def test_an_all_empty_window_is_a_fetch_failure_not_a_zero(self, tmp_path: Path):
        """THE regression this file exists for. Before the fix, an empty
        page was persisted, parsed to 0, and handed to the stale-fallback
        machinery, which quietly republished a two-month-old figure. A
        FetchError instead means `run()` logs `fetch_failed` and the fetch
        floor can see it."""
        client = _FakeClient({})
        with pytest.raises(FetchError) as exc:
            fetch_dated_form(
                url="https://gsom.bb.org.bd/index.php/tbill",
                indicator_id="treasury_bill_outstanding",
                snapshot_dir=tmp_path / "tbill",
                field="picker_date",
                date_format="%d-%b-%y",
                uppercase=True,
                accept=_accepts_positive_total,
                max_lookback_days=3,
                now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
                client=client,
            )
        assert "treasury_bill_outstanding" in str(exc.value)
        assert list(tmp_path.glob("tbill/*.html")) == []

    def test_a_non_200_costs_a_candidate_not_the_run(self, tmp_path: Path):
        client = _FakeClient(
            {"01-SEP-26": FULL_PAGE}, statuses={"02-SEP-26": 500}
        )
        result = fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=client,
        )
        assert "22,10,000.00" in result.artifact_path.read_text()

    def test_a_dropped_connection_costs_a_candidate_not_the_run(self, tmp_path: Path):
        """POST is deliberately outside the session's Retry policy, so a
        transient failure must be absorbed here or it ends the walk."""
        client = _FakeClient({"01-SEP-26": FULL_PAGE}, raises={"02-SEP-26"})
        result = fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=client,
        )
        assert "22,10,000.00" in result.artifact_path.read_text()

    def test_an_exploding_accept_is_a_rejection_not_a_crash(self, tmp_path: Path):
        """`accept` runs the real parser, which raises `ParseError` on
        markup it doesn't recognise. That must reject the date and move on,
        exactly like a 0 does."""
        seen: list[str] = []

        def accept(html: str) -> bool:
            seen.append(html)
            if len(seen) < 2:
                raise ValueError("row not found")
            return True

        fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            accept=accept,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=_FakeClient({}),
        )
        assert len(seen) == 2


class TestArtifactLayout:
    """The parse stage finds artifacts by globbing `<id>/*.html` and sorting.
    A date-form fetch that wrote anywhere else — or under the ANSWERED date
    rather than the fetch date — would either be invisible to parse or would
    silently reorder the sort. Path parity with `fetch_html` is what lets
    `_load_artifact_for` stay free of a special case."""

    def test_it_writes_where_fetch_html_would(self, tmp_path: Path):
        now = datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc)
        result = fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=now,
            client=_FakeClient({"02-SEP-26": FULL_PAGE}),
        )
        # The INJECTED clock, not `datetime.now()` — comparing against a fresh
        # now() (as this test first did) can never detect a date-stamp bug,
        # and a run straddling UTC midnight would name the artifact after a
        # different day than the walk was computed for.
        assert result.artifact_path == tmp_path / "tbill" / "2026-09-02.html"
        assert result.artifact_type == "html"
        assert result.artifact_path.exists()

    def test_re_fetching_the_same_page_is_a_cache_hit(self, tmp_path: Path):
        kwargs = dict(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
        )
        first = fetch_dated_form(client=_FakeClient({"02-SEP-26": FULL_PAGE}), **kwargs)
        second = fetch_dated_form(client=_FakeClient({"02-SEP-26": FULL_PAGE}), **kwargs)
        assert first.cache_hit is False
        assert second.cache_hit is True
        assert first.sha256 == second.sha256


# ----------------------------------------------------------------- parsing


def _artifact(tmp_path: Path, html: str) -> FetchResult:
    p = tmp_path / "page.html"
    p.write_text(html)
    return FetchResult(
        indicator_id="treasury_bill_outstanding",
        artifact_path=p,
        artifact_type="html",
        fetched_at=datetime.now(timezone.utc),
        source_url="https://gsom.bb.org.bd/index.php/tbill",
        sha256="x" * 64,
        cache_hit=False,
    )


class TestGsomTotalRowParser:
    def test_it_extracts_the_same_number_as_the_generic_parser(self, tmp_path: Path):
        """`gsom_total_row` adds a date to `html_table_row`; it must not
        change the figure. Any divergence here is a silent revaluation of
        two money-market series."""
        artifact = _artifact(tmp_path, FULL_PAGE)
        generic = get_parser("html_table_row").parse(artifact, INSTRUCTION)
        gsom = get_parser("gsom_total_row").parse(artifact, INSTRUCTION)
        assert gsom.value == generic.value == 2_210_000.0

    def test_it_dates_the_figure_by_the_day_the_page_answered_for(self, tmp_path: Path):
        """The whole point. The walk means the accepted page is often NOT
        today's, and an undated figure is what let a correct fiscal-year
        reset read as a collapse (landmine 56)."""
        r = get_parser("gsom_total_row").parse(_artifact(tmp_path, FULL_PAGE), INSTRUCTION)
        assert r.source_as_of == date(2026, 9, 1)

    def test_the_strategy_is_recorded_under_its_own_name(self, tmp_path: Path):
        r = get_parser("gsom_total_row").parse(_artifact(tmp_path, FULL_PAGE), INSTRUCTION)
        assert r._parse_strategy == "gsom_total_row"

    def test_a_missing_picker_costs_the_date_not_the_value(self, tmp_path: Path):
        """A reshaped page must degrade to an undated figure, not to a
        parse failure — losing the value would trip the stale fallback,
        which is the machinery this whole PR is trying to stop misfiring."""
        artifact = _artifact(tmp_path, _page(None, "22,10,000.00"))
        r = get_parser("gsom_total_row").parse(artifact, INSTRUCTION)
        assert r.value == 2_210_000.0
        assert r.source_as_of is None

    @pytest.mark.parametrize("picker", ["", "not-a-date", "31-FEB-26", "2026-09-01"])
    def test_a_garbled_picker_degrades_quietly(self, tmp_path: Path, picker: str):
        artifact = _artifact(tmp_path, _page(picker, "22,10,000.00"))
        r = get_parser("gsom_total_row").parse(artifact, INSTRUCTION)
        assert r.value == 2_210_000.0
        assert r.source_as_of is None

    def test_the_llm_fallback_path_can_still_recover_the_date(self, tmp_path: Path):
        """`parsers.hybrid` calls `recover_source_as_of` when deterministic
        extraction failed, so a figure the LLM rescues is still dated —
        the gap that produced the undated NPL reading."""
        parser = get_parser("gsom_total_row")
        assert parser.recover_source_as_of(_artifact(tmp_path, FULL_PAGE)) == date(
            2026, 9, 1
        )
        assert parser.recover_source_as_of(
            _artifact(tmp_path, _page(None, "22,10,000.00"))
        ) is None


# ------------------------------------------------- the acceptance predicate


class TestParsesToPositive:
    """`fetchers/` must never learn what a total row looks like — the
    predicate delegates to the indicator's OWN configured parser, so the
    markup assumption stays in exactly one place."""

    @staticmethod
    def _indicator() -> dict:
        return {
            "id": "treasury_bill_outstanding",
            "fetch": {
                "type": "html",
                "url": "https://gsom.bb.org.bd/index.php/tbill",
                "task": INSTRUCTION,
            },
            "parse": {"deterministic": "gsom_total_row"},
        }

    def test_a_day_with_data_is_accepted(self, tmp_path: Path):
        import fetch_all

        accept = fetch_all._parses_to_positive(self._indicator(), tmp_path)
        assert accept(FULL_PAGE) is True

    def test_the_empty_days_zero_total_is_rejected(self, tmp_path: Path):
        """A 200 with a well-formed empty table is the failure mode that
        started all this: it looks like success everywhere downstream."""
        import fetch_all

        accept = fetch_all._parses_to_positive(self._indicator(), tmp_path)
        assert accept(EMPTY_PAGE) is False

    def test_unrecognisable_markup_propagates_the_parse_error(self, tmp_path: Path):
        """The parser raises; `fetch_dated_form` is what turns that into a
        rejection (see test_an_exploding_accept_...), so the predicate
        itself is allowed to propagate — the dead-URL "File not found."
        body must not be mistaken for a usable day."""
        import fetch_all
        from parsers.base import ParseError

        accept = fetch_all._parses_to_positive(self._indicator(), tmp_path)
        with pytest.raises(ParseError):
            accept("<html><body>File not found.</body></html>")

    def test_the_probe_file_cannot_be_mistaken_for_a_days_artifact(self, tmp_path: Path):
        """Each candidate is written to disk so the parser can read a real
        path. `parse_all._load_artifact_for` globs `_html/<id>/*.html` and
        takes the newest by name — and pathlib's glob matches dotfiles — so
        a scratch file living in that directory could be selected as the
        day's artifact, reintroducing the exact 0 this predicate rejects.
        It gets its own tree instead."""
        import fetch_all

        accept = fetch_all._parses_to_positive(self._indicator(), tmp_path)
        accept(EMPTY_PAGE)
        html_dir = tmp_path / "_html" / "treasury_bill_outstanding"
        assert not html_dir.exists() or list(html_dir.iterdir()) == []
        assert (tmp_path / "_probe" / "treasury_bill_outstanding" / "candidate.html").exists()


class TestRegistryWiring:
    """End-to-end config check: the two treasury indicators must actually
    route through the dated fetch, with a parser the registry knows."""

    def test_the_configured_parser_exists_for_every_date_form_indicator(self):
        repo_root = Path(__file__).resolve().parent.parent
        cfg = json.loads((repo_root / "config" / "sources-v3.json").read_text())
        dated = [i for i in cfg["indicators"] if i["fetch"].get("date_form")]
        assert {i["id"] for i in dated} == {
            "treasury_bill_outstanding",
            "treasury_bond_outstanding",
        }
        for ind in dated:
            # get_parser raises KeyError on an unregistered name — a typo in
            # `deterministic` would otherwise only surface at 01:11 BDT.
            assert get_parser(ind["parse"]["deterministic"]) is not None

    def test_the_parse_stage_imports_every_parser_the_registry_names(self):
        """Parsers register by import side-effect, so a new parser that
        `parse_all` does not import is a `KeyError` at 02:10 BDT — for an
        indicator whose config, fetcher and parser are all otherwise
        correct. Import `parse_all` alone and resolve every name in the
        registry through it; nothing else may have imported them first."""
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        script = (
            "import json, parse_all\n"
            "from parsers.registry import REGISTRY\n"
            "cfg = json.load(open('config/sources-v3.json'))\n"
            "missing = sorted({i['parse']['deterministic'] for i in cfg['indicators']\n"
            "                  if i['parse'].get('deterministic') not in REGISTRY})\n"
            "print(','.join(missing))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root, capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        assert proc.stdout.strip() == ""

    def test_every_date_form_field_the_fetcher_reads_is_present(self):
        repo_root = Path(__file__).resolve().parent.parent
        cfg = json.loads((repo_root / "config" / "sources-v3.json").read_text())
        for ind in cfg["indicators"]:
            form = ind["fetch"].get("date_form")
            if not form:
                continue
            # `_fetch_one` indexes these two directly; the rest have defaults.
            assert form["field"]
            # NOT `assert datetime.now().strftime(form["format"])` — strftime
            # returns any unrecognised string unchanged, so "hello" passes
            # that. The format has to actually render a date, and it has to
            # render one the parser can read back: the format lives in TWO
            # places (here and gsom_total_row._PICKER_DATE_FORMAT) and nothing
            # else stops them drifting apart.
            rendered = date(2026, 9, 2).strftime(form["format"])
            if form.get("uppercase"):
                rendered = rendered.upper()
            assert rendered != form["format"]
            assert (
                datetime.strptime(rendered.title(), _PICKER_DATE_FORMAT).date()
                == date(2026, 9, 2)
            )


# --------------------------------------------------------------- http post


class TestHttpClientPost:
    def test_a_connection_error_becomes_a_fetch_error(self, monkeypatch):
        import requests

        from utils.http_client import HttpClient

        client = HttpClient()

        def boom(*_a, **_k):
            raise requests.exceptions.ConnectionError("reset by peer")

        monkeypatch.setattr(client._session, "post", boom)
        with pytest.raises(HttpClient.FetchError):
            client.post("https://gsom.bb.org.bd/index.php/tbill", data={})

    def test_the_instance_timeout_applies_by_default(self, monkeypatch):
        """A POST with no timeout can hang the whole fetch stage on a
        half-open socket — the timers give it no deadline of its own."""
        from utils.http_client import HttpClient

        client = HttpClient(timeout=17)
        seen: dict = {}

        def capture(url, **kwargs):
            seen.update(kwargs)
            return _Response("ok")

        monkeypatch.setattr(client._session, "post", capture)
        client.post("https://example.com")
        assert seen["timeout"] == 17

    def test_post_is_not_covered_by_the_get_retry_policy(self):
        """Blind POST retries are unsafe in general, so the session mounts
        its Retry with GET/HEAD only. The date walk is what provides
        resilience instead — pin the policy so a future 'helpful' widening
        is a deliberate, visible act."""
        from utils.http_client import HttpClient

        adapter = HttpClient()._session.get_adapter("https://example.com")
        assert set(adapter.max_retries.allowed_methods) == {"GET", "HEAD"}


class TestTheRegressionInPlainTerms:
    def test_two_months_of_empty_pages_never_yields_a_number(self, tmp_path: Path):
        """What actually happened, replayed: every fetch for 60 days
        returned a page with no data. The old path persisted it, parsed 0,
        and let the stale fallback republish 30 June. The new path refuses
        to persist anything at all."""
        import fetch_all

        indicator = TestParsesToPositive._indicator()
        for day_offset in range(0, 60, 10):
            now = datetime(2026, 7, 1, 19, 11, tzinfo=timezone.utc) + timedelta(
                days=day_offset
            )
            with pytest.raises(FetchError):
                fetch_dated_form(
                    url=indicator["fetch"]["url"],
                    indicator_id=indicator["id"],
                    snapshot_dir=tmp_path / "_html" / indicator["id"],
                    field="picker_date",
                    date_format="%d-%b-%y",
                    uppercase=True,
                    accept=fetch_all._parses_to_positive(indicator, tmp_path),
                    max_lookback_days=3,
                    now=now,
                    client=_FakeClient({}),
                )
        assert list((tmp_path / "_html" / indicator["id"]).glob("*.html")) == []


# ------------------------------------------------ the dispatch in fetch_all


class TestFetchOneDispatch:
    """Nothing exercised `_fetch_one`'s date_form branch — the config shape
    was checked, and the predicate was checked, but the wiring BETWEEN them
    was not. A swapped keyword there (say `date_format=date_form["field"]`)
    would have shipped green and failed at 01:11 BDT against the live portal,
    where the only symptom is another silent holdover.
    """

    @staticmethod
    def _indicator() -> dict:
        return {
            "id": "treasury_bill_outstanding",
            "fetch": {
                "type": "html",
                "url": "https://gsom.bb.org.bd/index.php/tbill",
                "task": INSTRUCTION,
                "date_form": {
                    "field": "picker_date",
                    "format": "%d-%b-%y",
                    "uppercase": True,
                    "extra_fields": {"ci_csrf_token": ""},
                    "start_offset_days": 1,
                    "max_lookback_days": 10,
                },
            },
            "parse": {"deterministic": "gsom_total_row"},
        }

    def test_the_config_reaches_the_fetcher_unscrambled(self, tmp_path, monkeypatch):
        import fetch_all

        seen: dict = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return FetchResult(
                indicator_id=kwargs["indicator_id"],
                artifact_path=tmp_path / "x.html",
                artifact_type="html",
                fetched_at=datetime.now(timezone.utc),
                source_url=kwargs["url"],
                sha256="0" * 64,
                cache_hit=False,
            )

        monkeypatch.setattr(fetch_all, "fetch_dated_form", spy)
        fetch_all._fetch_one(self._indicator(), tmp_path)

        assert seen["field"] == "picker_date"
        assert seen["date_format"] == "%d-%b-%y"
        assert seen["uppercase"] is True
        assert seen["extra_fields"] == {"ci_csrf_token": ""}
        assert seen["start_offset_days"] == 1
        assert seen["max_lookback_days"] == 10
        assert seen["url"] == "https://gsom.bb.org.bd/index.php/tbill"
        assert seen["indicator_id"] == "treasury_bill_outstanding"
        # the artifact must land where fetch_html would put it, or the parse
        # stage's `_load_artifact_for` will not find it
        assert seen["snapshot_dir"] == tmp_path / "_html" / "treasury_bill_outstanding"
        # and `accept` must be the indicator's OWN parser, not a markup guess
        assert seen["accept"](FULL_PAGE) is True
        assert seen["accept"](EMPTY_PAGE) is False

    def test_a_misconfigured_date_form_does_not_take_the_whole_stage_down(
        self, tmp_path
    ):
        """`fetch_all`'s per-indicator handler catches FetchError ONLY. These
        two indicators are #11 and #12 of 64, so a bare KeyError escaping here
        would abandon the other 52 — a config typo in one source must cost one
        source."""
        import fetch_all

        broken = self._indicator()
        del broken["fetch"]["date_form"]["field"]
        with pytest.raises(FetchError) as excinfo:
            fetch_all._fetch_one(broken, tmp_path)
        assert "misconfigured" in str(excinfo.value)

    def test_an_unregistered_parser_name_is_also_contained(self, tmp_path):
        """`get_parser` raises KeyError, not FetchError."""
        import fetch_all

        broken = self._indicator()
        broken["parse"]["deterministic"] = "no_such_parser"
        with pytest.raises(FetchError):
            fetch_all._fetch_one(broken, tmp_path)

    def test_a_real_fetch_failure_stays_a_fetch_failure(self, tmp_path, monkeypatch):
        """The containment must not relabel the genuine 'no date had data'
        failure, which callers already handle."""
        import fetch_all

        def boom(**kwargs):
            raise FetchError("no date in the last 10 day(s) returned usable data")

        monkeypatch.setattr(fetch_all, "fetch_dated_form", boom)
        with pytest.raises(FetchError) as excinfo:
            fetch_all._fetch_one(self._indicator(), tmp_path)
        assert "misconfigured" not in str(excinfo.value)


class TestTransientNetworkErrors:
    def test_a_blip_does_not_cost_a_day_of_freshness(self, tmp_path):
        """The walk is newest-first, so treating a connection reset as 'this
        day has no data' silently publishes an OLDER figure. `post()` sits
        outside the session's Retry policy (GET/HEAD only), so this same-date
        retry is the only retry there is."""

        class _FlakyOnce:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def post(self, url, data=None, **kwargs):
                rendered = (data or {}).get("picker_date", "")
                self.calls.append(rendered)
                if self.calls.count(rendered) == 1:
                    raise RuntimeError("connection reset")
                return _Response(FULL_PAGE if rendered == "02-SEP-26" else EMPTY_PAGE, 200)

        client = _FlakyOnce()
        result = fetch_dated_form(
            url="https://gsom.bb.org.bd/index.php/tbill",
            indicator_id="treasury_bill_outstanding",
            snapshot_dir=tmp_path / "tbill",
            field="picker_date",
            date_format="%d-%b-%y",
            uppercase=True,
            accept=_accepts_positive_total,
            now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
            client=client,
        )
        # yesterday was retried and won — the walk never reached 01-SEP
        assert client.calls == ["02-SEP-26", "02-SEP-26"]
        assert result.artifact_path.exists()

    def test_a_persistently_dead_date_is_still_only_two_attempts(self, tmp_path):
        """The retry must not turn a 10-day walk into an unbounded probe."""

        client = _FakeClient({}, raises={"02-SEP-26", "01-SEP-26"})
        with pytest.raises(FetchError):
            fetch_dated_form(
                url="https://gsom.bb.org.bd/index.php/tbill",
                indicator_id="treasury_bill_outstanding",
                snapshot_dir=tmp_path / "tbill",
                field="picker_date",
                date_format="%d-%b-%y",
                uppercase=True,
                accept=_accepts_positive_total,
                max_lookback_days=2,
                now=datetime(2026, 9, 2, 19, 11, tzinfo=timezone.utc),
                client=client,
            )
        assert len(client.calls) == 4  # 2 dates x 2 attempts, and no more
