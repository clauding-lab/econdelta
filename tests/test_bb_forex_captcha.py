"""Tests for scrapers/bb_forex_captcha.py.

CAPTCHA-helper tests live here so scrapers/bb_forex.py can stay focused on
forex/reserves parsing. ParseError is re-exported from bb_forex.py for
backward compatibility, but tests import it directly from this module.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scrapers.bb_forex_captcha import (
    ParseError,
    _extract_captcha_image,
    _is_captcha_page,
    _solve_captcha_via_claude,
    solve_captcha_loop,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# _is_captcha_page
# ---------------------------------------------------------------------------


def test_is_captcha_page_true_for_bb_captcha_fixture():
    html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    assert _is_captcha_page(html) is True


def test_is_captcha_page_false_for_normal_exchange_rates_fixture():
    # bb_forex_reserves.html already exists in fixtures dir (used by existing reserves tests)
    html = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")
    assert _is_captcha_page(html) is False


def test_is_captcha_page_false_for_partial_markers():
    # Has id="ans" alone but missing other markers
    html = '<html><body><input id="ans" /></body></html>'
    assert _is_captcha_page(html) is False


# ---------------------------------------------------------------------------
# _extract_captcha_image
# ---------------------------------------------------------------------------


def test_extract_captcha_image_writes_decoded_png(tmp_path):
    html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    dest = tmp_path / "captcha.png"
    _extract_captcha_image(html, dest)
    assert dest.exists()
    assert dest.stat().st_size > 100
    # PNG magic bytes
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Byte-identical to the saved fixture
    expected = (FIXTURES_DIR / "bb_forex_captcha.png").read_bytes()
    assert dest.read_bytes() == expected


def test_extract_captcha_image_raises_when_no_thumbnail(tmp_path):
    dest = tmp_path / "x.png"
    with pytest.raises(ParseError, match="no captcha image"):
        _extract_captcha_image("<html><body>nothing</body></html>", dest)


# ---------------------------------------------------------------------------
# _solve_captcha_via_claude
# ---------------------------------------------------------------------------


def _mock_completed_process(stdout: str, returncode: int = 0) -> MagicMock:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = ""
    p.returncode = returncode
    return p


def test_solve_captcha_via_claude_returns_lowercase_word(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("Bottle\n")
        result = _solve_captcha_via_claude(img)
    assert result == "bottle"
    # confirm claude was invoked with the image attached
    argv = mock_run.call_args[0][0]
    assert "--print" in argv
    assert "--model" in argv
    assert any("claude-haiku" in a for a in argv)
    # @<path> must appear inside one of the argv elements (the prompt string)
    assert any(f"@{img}" in a for a in argv)


def test_solve_captcha_via_claude_strips_trailing_punctuation(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("arrows.\n")
        assert _solve_captcha_via_claude(img) == "arrows"


def test_solve_captcha_via_claude_returns_first_word_only(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("a red apple")
        result = _solve_captcha_via_claude(img)
    # Take first word, strip junk
    assert result == "a"


def test_solve_captcha_via_claude_returns_none_on_empty_output(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("\n")
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_too_long(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("a" * 31)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_nonzero_exit(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("error", returncode=1)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_timeout(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_logs_stderr_and_elapsed_on_nonzero_exit(tmp_path, caplog):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex_captcha.subprocess.run") as mock_run:
        mp = MagicMock()
        mp.stdout = ""
        mp.stderr = "claude: model claude-haiku-4-5 not available\n"
        mp.returncode = 1
        mock_run.return_value = mp
        with caplog.at_level(logging.WARNING, logger="bb_forex"):
            assert _solve_captcha_via_claude(img) is None
    # Must log the stderr snippet so first-failure diagnostics don't require SSH
    record_text = " ".join(r.getMessage() for r in caplog.records)
    assert "not available" in record_text
    # Must log elapsed time (seconds)
    assert "elapsed" in record_text.lower() or "s)" in record_text or "after" in record_text.lower()


# ---------------------------------------------------------------------------
# solve_captcha_loop integration tests
# ---------------------------------------------------------------------------
#
# We test the captcha-handling logic in isolation via a fake page object,
# rather than mocking the full sync_playwright() context. This matches the
# existing TestFetchRetry pattern (patch at the smallest reasonable boundary).
#
# solve_captcha_loop drives the captcha challenge using a passed-in page:
# read content, check for captcha markers, extract image, solve, fill #ans,
# click #jar, wait for navigation, re-check. Up to 3 attempts.


class _FakePage:
    """Minimal sync_playwright Page stub for captcha-loop tests.

    Pages return successive HTML payloads from `content_returns` on each
    page.content() call. fill/click record their calls for assertion;
    wait_for_load_state is a no-op.
    """

    def __init__(self, content_returns: list[str]) -> None:
        self._content_returns = list(content_returns)
        self.fill_calls: list[tuple[str, str]] = []
        self.click_calls: list[str] = []
        self.wait_for_load_state_calls: list[dict] = []

    def content(self) -> str:
        if not self._content_returns:
            # If a test drained the queue, repeat the last response.
            raise AssertionError("FakePage.content() called more times than queued")
        return self._content_returns.pop(0)

    def fill(self, selector: str, value: str) -> None:
        self.fill_calls.append((selector, value))

    def click(self, selector: str) -> None:
        self.click_calls.append(selector)

    def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        self.wait_for_load_state_calls.append({"state": state, "timeout": timeout})


_CAPTCHA_HTML = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
_RESERVES_HTML = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")


def test_solve_captcha_loop_returns_html_when_no_captcha_present():
    """Happy path — initial HTML is not a captcha page → return immediately, no fills/clicks."""
    page = _FakePage(content_returns=[])  # content() should not be called
    result = solve_captcha_loop(page, _RESERVES_HTML, timeout_ms=60_000)

    assert result == _RESERVES_HTML
    assert page.fill_calls == []
    assert page.click_calls == []


def test_solve_captcha_loop_clears_captcha_on_first_solve():
    """First attempt: captcha detected → solver returns 'arrows' → page navigates → re-check shows non-captcha HTML."""
    # After click+navigation, page.content() returns the real HTML.
    page = _FakePage(content_returns=[_RESERVES_HTML])

    with patch(
        "scrapers.bb_forex_captcha._solve_captcha_via_claude", return_value="arrows"
    ) as mock_solve:
        result = solve_captcha_loop(page, _CAPTCHA_HTML, timeout_ms=60_000)

    assert result == _RESERVES_HTML
    assert mock_solve.call_count == 1
    assert page.fill_calls == [("#ans", "arrows")]
    assert page.click_calls == ["#jar"]
    # wait_for_load_state was called after click
    assert len(page.wait_for_load_state_calls) == 1
    assert page.wait_for_load_state_calls[0]["state"] == "domcontentloaded"


def test_solve_captcha_loop_raises_after_3_failed_attempts():
    """Every re-check returns captcha HTML → ParseError after 3 attempts; solver called 3x."""
    # Each iteration re-reads page.content() after submit. All return captcha HTML.
    page = _FakePage(content_returns=[_CAPTCHA_HTML, _CAPTCHA_HTML, _CAPTCHA_HTML])

    with patch(
        "scrapers.bb_forex_captcha._solve_captcha_via_claude", return_value="wrong"
    ) as mock_solve:
        with pytest.raises(ParseError, match="captcha solve failed after 3 attempts"):
            solve_captcha_loop(page, _CAPTCHA_HTML, timeout_ms=60_000)

    assert mock_solve.call_count == 3
    assert page.fill_calls == [("#ans", "wrong")] * 3
    assert page.click_calls == ["#jar"] * 3


def test_solve_captcha_loop_propagates_extract_failure_without_retry():
    """_extract_captcha_image raising inside the loop bypasses the 3-attempt
    counter and propagates to the outer fetch_rendered_html retry layer.

    This pins the current behaviour: when the page is still recognised as
    a captcha wall (all four markers present) but the embedded image data
    URI is malformed (e.g. BB changes its src encoding), ParseError
    propagates immediately rather than silently consuming the 3-attempt
    budget. fetch_rendered_html's outer retry layer catches it and
    re-launches the browser.

    We corrupt the data URI prefix so the four captcha markers stay
    intact (so _is_captcha_page returns True and the loop enters), but
    _CAPTCHA_IMG_RE cannot find a match.
    """
    captcha_html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    # Break the data URI so the image regex fails to match, but leave all
    # four captcha markers intact so _is_captcha_page still returns True.
    broken_html = captcha_html.replace("data:image/png;base64,", "data:broken,")
    page = _FakePage(content_returns=[])  # not consulted — extract fails before any submit

    with patch("scrapers.bb_forex_captcha._solve_captcha_via_claude") as mock_solve:
        with pytest.raises(ParseError, match="no captcha image"):
            solve_captcha_loop(page, broken_html, timeout_ms=30000)
    # Solver should never be called when extraction fails
    assert mock_solve.call_count == 0
    # No fill/click attempts
    assert page.fill_calls == []
    assert page.click_calls == []
