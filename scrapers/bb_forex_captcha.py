"""BB exchange-rate / reserves page CAPTCHA bypass — Claude vision solver.

Bangladesh Bank serves an image-CAPTCHA wall to flagged IPs (e.g. ExonVPS's
data-center address). When the wall appears, we detect it via four required
markers (id="ans", id="jar", class="thumbnails", "support ID"), decode the
embedded base64 PNG to a tempfile, send it to `claude --print` for vision
inference, and submit the predicted answer to the form. Up to 3 attempts;
otherwise ParseError, letting the outer fetch_rendered_html retry layer
re-launch the browser.

Auth via CLAUDE_CODE_OAUTH_TOKEN env var (subscription-billed; set in
/etc/econdelta.env). Model: claude-haiku-4-5 (cheapest multimodal,
sufficient for single-frame object identification).

Public API: solve_captcha_loop(page, html, timeout_ms) -> str.
ParseError is also exported and re-exported by bb_forex.py so callers
can catch a single exception type for both parse + captcha failures.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("bb_forex")


class ParseError(Exception):
    """Raised when bb_forex parsing or captcha handling fails."""


def _is_captcha_page(html: str) -> bool:
    """Detect BB's image-CAPTCHA wall.

    BB serves a CAPTCHA challenge to flagged IPs (e.g. data-center addresses
    like ExonVPS). The wall contains an "answer" input, a "jar" submit button,
    a thumbnail image to identify, and a "support ID" footer. All four markers
    must be present — any one alone could be a false positive.
    """
    markers = ('id="ans"', 'id="jar"', 'class="thumbnails"', "support ID")
    return all(m in html for m in markers)


# BB renders the challenge image as either:
#   <img ... class="thumbnails" ... src="data:image/png;base64,...">  (plan order)
#   <img ... src="data:image/png;base64,..." ... class="thumbnails">  (live fixture order)
# We accept both orderings, anchored on class="thumbnails" so we don't
# accidentally match the unrelated red-dot / audio-icon images on the page.
_CAPTCHA_IMG_RE = re.compile(
    r'<img[^>]+(?:'
    r'class="thumbnails"[^>]+src="data:image/png;base64,([^"]+)"'
    r'|'
    r'src="data:image/png;base64,([^"]+)"[^>]+class="thumbnails"'
    r')',
    re.IGNORECASE,
)


def _extract_captcha_image(html: str, dest_path: Path) -> None:
    """Extract the base64-encoded captcha PNG from BB's captcha-wall HTML.

    BB embeds the challenge image as a data URI on an <img class="thumbnails">
    tag. We decode and write atomically (tmp + rename), mirroring the
    write_snapshot() pattern in bb_forex.py.
    """
    m = _CAPTCHA_IMG_RE.search(html)
    if m is None:
        raise ParseError("no captcha image found in captcha-page HTML")
    b64 = m.group(1) or m.group(2)
    try:
        png_bytes = base64.b64decode(b64)
    except Exception as e:
        raise ParseError(f"failed to decode captcha image base64: {e}") from e

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    tmp_path.write_bytes(png_bytes)
    os.replace(tmp_path, dest_path)


_CAPTCHA_SOLVE_TIMEOUT_S = 60
_CAPTCHA_SOLVE_MAX_ANSWER_LEN = 30
_CAPTCHA_SOLVE_MAX_ATTEMPTS = 3
_CAPTCHA_SOLVE_PROMPT = (
    "What single common object is shown in this image? "
    "Examples of valid answers: 'bottle', 'arrows', 'dot', 'apple'. "
    "Reply with ONLY a single English lowercase common noun, no other text."
)


def _solve_captcha_via_claude(image_path: Path) -> str | None:
    """Identify the object in a BB captcha image via Claude vision.

    Returns the predicted single-word answer (lowercase, no punctuation), or
    None on any failure (timeout, non-zero exit, empty output, over-long
    output). Caller wraps with retry.

    Uses `claude -p` with the image attached via @filepath syntax —
    Claude Code's prompt-side file reference triggers vision-mode
    attachment. Model is claude-haiku-4-5 (cheapest multimodal, sufficient
    for the simple object-identification task BB asks).

    Auth via CLAUDE_CODE_OAUTH_TOKEN env var (set in /etc/econdelta.env).
    """
    binary = os.environ.get("CLAUDE_BINARY", "claude")
    prompt_with_image = f"{_CAPTCHA_SOLVE_PROMPT}\n\n@{image_path}"
    argv = [
        binary, "--print", "--strict-mcp-config",
        "--model", "claude-haiku-4-5",
        "--no-session-persistence",
        "--tools", "",
        "--permission-mode", "bypassPermissions",
        prompt_with_image,
    ]
    t0 = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CAPTCHA_SOLVE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        # Mirror parse_all._claude_preflight's log shape so first-failure
        # diagnostics on ExonVPS don't require SSHing to read journal.
        logger.warning(
            "captcha solver exited %d after %.1fs — stdout=%r stderr=%r",
            result.returncode, elapsed,
            result.stdout.strip()[:200], result.stderr.strip()[:200],
        )
        return None

    raw = result.stdout.strip().lower()
    if not raw:
        return None

    first_word = raw.split()[0].rstrip(".,!?;:'\"")
    if not first_word or len(first_word) > _CAPTCHA_SOLVE_MAX_ANSWER_LEN:
        return None

    return first_word


def solve_captcha_loop(page, html: str, timeout_ms: int) -> str:
    """Drive BB's CAPTCHA challenge until cleared, or fail after 3 attempts.

    Refactored out of `_fetch_once` so the captcha-handling logic can be
    tested in isolation with a fake page stub — mocking the entire
    sync_playwright() context manager chain would be brittle.

    The caller passes the initial HTML (from page.content() after page.goto)
    so we can short-circuit when no captcha is present.

    Loop body: extract challenge PNG to a temp file → ask Claude what object
    is shown → fill #ans with the answer → click #jar → wait for navigation
    → re-read page.content(). If the new HTML is still a captcha page, retry.

    Returns the final non-captcha HTML. Raises ParseError if 3 attempts pass
    without clearing.
    """
    for attempt in range(1, _CAPTCHA_SOLVE_MAX_ATTEMPTS + 1):
        if not _is_captcha_page(html):
            return html
        logger.info(
            "BB captcha detected (attempt %d/%d)",
            attempt,
            _CAPTCHA_SOLVE_MAX_ATTEMPTS,
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp_path = Path(tf.name)
        try:
            _extract_captcha_image(html, tmp_path)
            answer = _solve_captcha_via_claude(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        if answer is None:
            logger.warning("captcha solver returned None on attempt %d", attempt)
            continue
        logger.info("captcha solver returned %r — submitting", answer)
        page.fill("#ans", answer)
        page.click("#jar")
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        html = page.content()
    raise ParseError("captcha solve failed after 3 attempts")
