---
date: 2026-05-18
project: EconDelta
spec: docs/superpowers/specs/2026-05-18-bb-forex-claude-captcha-solver-design.md
status: ready-to-execute
---

# BB Forex CAPTCHA Solver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. TDD per task (RED → GREEN → REFACTOR). Each task ends with a single squash-able commit.

**Goal:** Add three private helpers + integration into `_fetch_once` in `scrapers/bb_forex.py` so the BB exchange-rate scraper can autonomously solve BB's image CAPTCHA via Claude vision, re-enabling `econdelta-forex.timer` on ExonVPS.

**Architecture:** All work in `scrapers/bb_forex.py` + `tests/test_bb_forex.py` + 2 new fixture files. No new modules. No new dependencies. Same Playwright-stealth, same `CLAUDE_BINARY` env, same `CLAUDE_CODE_OAUTH_TOKEN` auth (already in `/etc/econdelta.env`).

**Tech Stack:** Python 3.12 · playwright + playwright-stealth (existing) · `claude` CLI 2.1.104 with vision (`-p @filepath`) · pytest with `unittest.mock.patch`.

---

## Working Branch

`feat/bb-forex-claude-captcha`, rooted off `9d28c7a` (current main). All work commits onto this branch. PR opens against `main` at the end.

## Approval Gates

None require user approval — all work is in-repo + tests. Production-side `systemctl enable` is a separate post-PR step (Task T5, outside this plan, performed manually).

## File Structure

### Modified
| File | Responsibility |
| --- | --- |
| `scrapers/bb_forex.py` | Add `_is_captcha_page`, `_extract_captcha_image`, `_solve_captcha_via_claude`; modify `_fetch_once` to use them |
| `tests/test_bb_forex.py` | Add tests for all 3 new helpers + integration tests for `_fetch_once` |

### Created
| File | Responsibility |
| --- | --- |
| `tests/fixtures/bb_forex_captcha_page.html` | Full HTML of a real BB CAPTCHA wall response (~47KB) |
| `tests/fixtures/bb_forex_captcha.png` | The base64-decoded image extracted from the captcha page (~5KB, 48×48 PNG) |

### Untouched
- `scrapers/__init__.py`, `scrapers/commodity_prices.py`, `scrapers/dse_market.py`
- `utils/*` (no new utilities needed)
- `claude_max/*` (existing wrapper pattern is for JSON-mode parse work; CAPTCHA-solve uses a separate inline subprocess.run call with a different argv shape)
- `parse_all.py`, `aggregate_latest.py`
- Any systemd unit files (production-side ops happen post-PR)

---

## Task T1 — CAPTCHA page detection helper

**Goal:** A pure-function helper `_is_captcha_page(html: str) -> bool` that recognises BB's CAPTCHA wall.

### Step 1: Fixture

Capture a real BB CAPTCHA page response to `tests/fixtures/bb_forex_captcha_page.html`. Use the existing curl-able response — the user's session has one stored at `/tmp/bb-curl.html` on ExonVPS (see session note 2026-05-18). If not available, regenerate via:

```
ssh adnan-local@103.187.23.22 'curl -sS "https://www.bb.org.bd/en/index.php/econdata/exchangerate" --max-time 30' > tests/fixtures/bb_forex_captcha_page.html
```

Confirm the fixture contains `id="ans"`, `id="jar"`, `class="thumbnails"`, and "support ID".

### Step 2: RED test

In `tests/test_bb_forex.py` add:

```python
from scrapers.bb_forex import _is_captcha_page  # ADD to existing imports

def test_is_captcha_page_true_for_bb_captcha_fixture():
    html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    assert _is_captcha_page(html) is True

def test_is_captcha_page_false_for_normal_exchange_rates_fixture():
    # bb_forex_reserves.html already exists in fixtures dir
    html = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")
    assert _is_captcha_page(html) is False

def test_is_captcha_page_false_for_partial_markers():
    # Has id="ans" alone but missing other markers
    html = '<html><body><input id="ans" /></body></html>'
    assert _is_captcha_page(html) is False
```

Run `pytest tests/test_bb_forex.py -k captcha -v` — confirms ImportError (helper doesn't exist yet).

### Step 3: GREEN implementation

In `scrapers/bb_forex.py`, add the helper near the top of the module (above `_fetch_once`):

```python
def _is_captcha_page(html: str) -> bool:
    """Detect BB's image-CAPTCHA wall.

    BB serves a CAPTCHA challenge to flagged IPs (e.g. data-center addresses
    like ExonVPS). The wall contains an "answer" input, a "jar" submit button,
    a thumbnail image to identify, and a "support ID" footer. All four markers
    must be present — any one alone could be a false positive.
    """
    markers = ('id="ans"', 'id="jar"', 'class="thumbnails"', "support ID")
    return all(m in html for m in markers)
```

Run pytest again — three tests now pass.

### Step 4: Commit

```
git add scrapers/bb_forex.py tests/test_bb_forex.py tests/fixtures/bb_forex_captcha_page.html
git commit -m "feat(bb_forex): add _is_captcha_page helper to detect BB CAPTCHA wall"
```

---

## Task T2 — CAPTCHA image extraction helper

**Goal:** A helper `_extract_captcha_image(html: str, dest_path: Path) -> None` that decodes the embedded captcha PNG from the captcha-page HTML and writes it atomically.

### Step 1: Fixture

Extract the real captcha image from the fixture HTML and save as `tests/fixtures/bb_forex_captcha.png` (binary). One-liner:

```python
import re, base64, pathlib
html = pathlib.Path("tests/fixtures/bb_forex_captcha_page.html").read_text()
m = re.search(r'<img[^>]+class="thumbnails"[^>]+src="data:image/png;base64,([^"]+)"', html)
pathlib.Path("tests/fixtures/bb_forex_captcha.png").write_bytes(base64.b64decode(m.group(1)))
```

Confirm: `file tests/fixtures/bb_forex_captcha.png` returns `PNG image data, …`.

### Step 2: RED test

```python
import tempfile
from scrapers.bb_forex import _extract_captcha_image  # ADD

def test_extract_captcha_image_writes_decoded_png(tmp_path):
    html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    dest = tmp_path / "captcha.png"
    _extract_captcha_image(html, dest)
    assert dest.exists()
    assert dest.stat().st_size > 100  # non-empty
    # PNG magic bytes
    assert dest.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Byte-identical to the saved fixture
    expected = (FIXTURES_DIR / "bb_forex_captcha.png").read_bytes()
    assert dest.read_bytes() == expected

def test_extract_captcha_image_raises_when_no_thumbnail(tmp_path):
    dest = tmp_path / "x.png"
    with pytest.raises(ParseError, match="no captcha image"):
        _extract_captcha_image("<html><body>nothing</body></html>", dest)
```

### Step 3: GREEN

Add to `scrapers/bb_forex.py` (near `_is_captcha_page`):

```python
import base64
import re

_CAPTCHA_IMG_RE = re.compile(
    r'<img[^>]+class="thumbnails"[^>]+src="data:image/png;base64,([^"]+)"',
    re.IGNORECASE,
)


def _extract_captcha_image(html: str, dest_path: Path) -> None:
    """Extract the base64-encoded captcha PNG from BB's captcha-wall HTML.

    BB embeds the challenge image as a data URI on an <img class="thumbnails">
    tag. We decode and write atomically (tmp + rename) following the same
    pattern as write_snapshot().
    """
    m = _CAPTCHA_IMG_RE.search(html)
    if m is None:
        raise ParseError("no captcha image found in captcha-page HTML")
    try:
        png_bytes = base64.b64decode(m.group(1))
    except Exception as e:
        raise ParseError(f"failed to decode captcha image base64: {e}") from e

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    tmp_path.write_bytes(png_bytes)
    os.replace(tmp_path, dest_path)
```

Run pytest — both tests pass.

### Step 4: Commit

```
git add scrapers/bb_forex.py tests/test_bb_forex.py tests/fixtures/bb_forex_captcha.png
git commit -m "feat(bb_forex): add _extract_captcha_image helper to decode embedded captcha PNG"
```

---

## Task T3 — Claude vision CAPTCHA solver

**Goal:** A helper `_solve_captcha_via_claude(image_path: Path) -> str | None` that calls `claude -p` with the image and returns the predicted object name (or None on failure).

### Step 1: RED test

```python
from unittest.mock import patch, MagicMock
import subprocess
from scrapers.bb_forex import _solve_captcha_via_claude  # ADD

def _mock_completed_process(stdout: str, returncode: int = 0) -> MagicMock:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = ""
    p.returncode = returncode
    return p


def test_solve_captcha_via_claude_returns_lowercase_word(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("Bottle\n")
        result = _solve_captcha_via_claude(img)
    assert result == "bottle"
    # confirm we invoked claude with the image attached
    argv = mock_run.call_args[0][0]
    assert "--print" in argv
    assert "--model" in argv
    assert any("claude-haiku" in a for a in argv)
    assert any(f"@{img}" in a for a in argv)


def test_solve_captcha_via_claude_strips_trailing_punctuation(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("arrows.\n")
        assert _solve_captcha_via_claude(img) == "arrows"


def test_solve_captcha_via_claude_returns_first_word_only(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("a red apple")
        # We asked for ONE word; if model returns more, take the most likely (last common noun)
        # Simpler choice: take first word, strip junk
        result = _solve_captcha_via_claude(img)
    assert result == "a"  # documents current behaviour; refine if vision misbehaves in prod


def test_solve_captcha_via_claude_returns_none_on_empty_output(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("\n")
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_too_long(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        # 31 chars — over the 30-char cap
        mock_run.return_value = _mock_completed_process("a" * 31)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_nonzero_exit(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.return_value = _mock_completed_process("error", returncode=1)
        assert _solve_captcha_via_claude(img) is None


def test_solve_captcha_via_claude_returns_none_on_timeout(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"dummy")
    with patch("scrapers.bb_forex.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
        assert _solve_captcha_via_claude(img) is None
```

### Step 2: GREEN

Add `import subprocess` if not present. Then:

```python
_CAPTCHA_SOLVE_TIMEOUT_S = 60
_CAPTCHA_SOLVE_MAX_ANSWER_LEN = 30
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

    Uses `claude -p @<path>` with claude-haiku-4-5 — cheapest multimodal
    model, sufficient for the simple object-identification task BB asks.
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
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_CAPTCHA_SOLVE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    raw = result.stdout.strip().lower()
    if not raw:
        return None

    first_word = raw.split()[0].rstrip(".,!?;:'\"")
    if not first_word or len(first_word) > _CAPTCHA_SOLVE_MAX_ANSWER_LEN:
        return None

    return first_word
```

### Step 3: Commit

```
git add scrapers/bb_forex.py tests/test_bb_forex.py
git commit -m "feat(bb_forex): add _solve_captcha_via_claude helper using claude vision"
```

---

## Task T4 — Integrate solver into `_fetch_once` with retry

**Goal:** Modify `_fetch_once` so it transparently solves CAPTCHA challenges when encountered. Existing happy-path behavior unchanged.

### Step 1: RED tests

The existing test suite mocks `fetch_rendered_html` rather than `_fetch_once`. For this task, mock `_fetch_once`'s dependencies more narrowly. Suggested:

```python
class _FakePage:
    """Minimal Playwright page stub for testing _fetch_once captcha flow."""
    def __init__(self, html_sequence):
        # html_sequence: list of HTML strings to return on successive content() calls
        self._htmls = list(html_sequence)
        self.filled = []
        self.clicked = []

    def content(self):
        if not self._htmls:
            raise IndexError("FakePage ran out of HTML responses")
        return self._htmls.pop(0)

    def goto(self, *a, **k): pass
    def wait_for_timeout(self, *a, **k): pass
    def wait_for_selector(self, *a, **k): pass
    def reload(self, *a, **k): pass
    def fill(self, sel, val): self.filled.append((sel, val))
    def click(self, sel): self.clicked.append(sel)
    def wait_for_navigation(self, *a, **k): pass


# Test 1: happy path - no captcha
def test_fetch_once_no_captcha_returns_html_directly(tmp_path):
    # Use the existing reserves fixture (not a captcha page)
    real_html = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")
    page = _FakePage([real_html])
    # ... use whatever stub pattern the existing tests use, possibly via patching sync_playwright
    # (refer to existing fetch test for shape)
    ...

# Test 2: CAPTCHA cleared on first solve
def test_fetch_once_solves_captcha_on_first_try(...):
    captcha_html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    real_html = (FIXTURES_DIR / "bb_forex_reserves.html").read_text(encoding="utf-8")
    page = _FakePage([captcha_html, real_html])  # captcha first, real after submit
    with patch("scrapers.bb_forex._solve_captcha_via_claude", return_value="arrows"):
        ...
    assert page.filled == [("#ans", "arrows")]
    assert page.clicked == ["#jar"]

# Test 3: CAPTCHA solve fails 3x → ParseError
def test_fetch_once_raises_after_3_captcha_failures(...):
    captcha_html = (FIXTURES_DIR / "bb_forex_captcha_page.html").read_text(encoding="utf-8")
    page = _FakePage([captcha_html] * 4)  # captcha returns every time
    with patch("scrapers.bb_forex._solve_captcha_via_claude", return_value="wrong"):
        with pytest.raises(ParseError, match="captcha solve failed"):
            ...
```

Subagent: refer to the existing `tests/test_bb_forex.py` patterns (which mock `sync_playwright` via `@patch("scrapers.bb_forex.sync_playwright")` or similar) and align the new tests with that style.

### Step 2: GREEN modification of `_fetch_once`

In `scrapers/bb_forex.py`, modify `_fetch_once` to:

```python
_CAPTCHA_SOLVE_MAX_ATTEMPTS = 3


def _fetch_once(url, timeout_ms, wait_for_selector):
    stealth = Stealth()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(...)  # unchanged
        context = browser.new_context(...)  # unchanged
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(10000)

            # NEW: CAPTCHA loop
            html = page.content()
            for attempt in range(1, _CAPTCHA_SOLVE_MAX_ATTEMPTS + 1):
                if not _is_captcha_page(html):
                    break
                logger.info("BB captcha detected (attempt %d/%d)", attempt, _CAPTCHA_SOLVE_MAX_ATTEMPTS)
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
                page.wait_for_navigation(wait_until="domcontentloaded", timeout=timeout_ms)
                html = page.content()
            else:
                raise ParseError("captcha solve failed after 3 attempts")

            # Existing selector-wait logic (unchanged)
            if wait_for_selector is not None:
                ...

            return page.content()
        finally:
            browser.close()
```

Add `import tempfile` at module top.

### Step 3: Commit

```
git add scrapers/bb_forex.py tests/test_bb_forex.py
git commit -m "feat(bb_forex): solve BB CAPTCHA via Claude vision in _fetch_once with 3-attempt retry"
```

---

## Task T5 — Re-enable timer + verify production cron fire (post-PR, manual)

**Not in this plan file.** After PR merges into `main` and Hetzner pulls (`git pull origin main`), perform manually:

1. Verify `claude-haiku-4-5` is reachable from ExonVPS:
   ```
   ssh adnan-local@103.187.23.22 'claude --print --model claude-haiku-4-5 "say HELLO" 2>&1 | head -3'
   ```
   Expect: `HELLO` on stdout.

2. Pull main on ExonVPS:
   ```
   ssh adnan-local@103.187.23.22 'cd ~/econdelta && git pull origin main'
   ```

3. Manual smoke:
   ```
   ssh adnan-local@103.187.23.22 'sudo systemctl start econdelta-forex.service'
   ```
   Tail `~/econdelta/logs/forex-systemd.log` — expect "BB captcha detected", "captcha solver returned <word>", then normal scrape lines, exit 0, fresh `~/econdelta/data/bb_forex/<today>.json`.

4. Re-enable timer:
   ```
   ssh adnan-local@103.187.23.22 'sudo systemctl enable --now econdelta-forex.timer && systemctl list-timers econdelta-forex.timer --no-pager'
   ```

5. Cron-fire test:
   ```
   ssh adnan-local@103.187.23.22 'sudo systemd-run --on-active=15min --unit=test-forex-cron-fire-$(date +%Y%m%d-%H%M) /bin/systemctl start econdelta-forex.service'
   ```
   Wait 15 min + ~3 min run time. Verify exit 0 + fresh data file.

6. If all green: leave Mac laptop launchd job in place (belt-and-suspenders). No removal.

---

## Final cross-task review

After T1–T4 commit, dispatch an Opus subagent with the `code-reviewer` agent type and `model: opus` to review the entire diff against the spec. Address Critical + High issues before opening PR.

## PR

Title: `feat(bb_forex): solve BB CAPTCHA via Claude vision`
Body: paste the spec's Summary + Architecture sections + a "Test plan" section listing T5's verification steps as checkboxes.
