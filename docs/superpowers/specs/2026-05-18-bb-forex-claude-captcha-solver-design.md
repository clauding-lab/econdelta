---
date: 2026-05-18
project: EconDelta
topic: BB forex CAPTCHA solver via Claude vision
status: design-approved
authors: Adnan Rashid (with Claude)
---

# BB Forex CAPTCHA Solver — Claude Vision

## Summary

Replace the inactive `econdelta-forex.timer` workaround (Mac laptop launchd feed at 06:05 BDT) with an in-place CAPTCHA-bypass that allows the existing Playwright-stealth scraper on ExonVPS to once again fetch BB exchange-rate and reserves pages directly. The bypass works by detecting BB's image-CAPTCHA wall when it appears, extracting the embedded base64 PNG, sending it to Claude via `claude -p @path/to/image.png` for object identification, typing the answer into the form, submitting, and proceeding with the existing parse logic.

## Problem

Bangladesh Bank (`www.bb.org.bd`) serves an image-CAPTCHA wall to flagged IPs — confirmed empirically from ExonVPS via `curl`:

```
HTTP=200 size=47620 time=0.324
Body: "What is in the image?" + <input id="ans"> + <button id="jar">
       Your support ID is: 10812410398024593521
       <img class="thumbnails" src="data:image/png;base64,...">
```

ExonVPS is on the flagged-IP list; the user's Mac at home (residential IP) is not. Since 2026-05-05, ExonVPS's `econdelta-forex.timer` has been disabled and the data is sourced via a Mac laptop launchd job (`com.clauding-lab.econdelta.bb-forex` at 06:05 BDT) that runs the same scraper locally and rsyncs results to ExonVPS. The laptop feed is fragile (depends on the Mac being awake and connected); a server-resident solution is preferable.

The image CAPTCHA is a simple "name the object" challenge — the image shows everyday objects (bottle, arrows, dot, etc.) and the answer is a single common noun. This is a trivial task for any modern vision model; Claude Haiku 4.5 handles it natively via the existing `claude -p @filepath` syntax, which embeds the image into a vision-enabled API call. Verified empirically: 48×48 px BB CAPTCHA correctly identified by `claude --print "@captcha.png"` against `claude-haiku-4-5`.

## Goals

- **Restore ExonVPS native BB forex scraping** without depending on the Mac laptop.
- **Stay on Claude Max subscription** for any inference — no `ANTHROPIC_API_KEY`. Use the existing `CLAUDE_CODE_OAUTH_TOKEN` env var (from `claude setup-token`, configured 2026-05-17).
- **Minimal surface area** — modify `scrapers/bb_forex.py` only. No new modules, no new dependencies. Tests slot into the existing `tests/test_bb_forex.py`.
- **Preserve the Mac laptop feed as belt-and-suspenders** — its launchd job stays, just becomes redundant on successful days.

## Non-Goals

- **Solving general-purpose CAPTCHAs.** This is BB-specific. Other Akamai/Cloudflare protections would need different approaches.
- **Audio CAPTCHA fallback.** The page exposes an `<img onclick="document.getElementById('captcha_audio').play()">` element, but the image path is reliably solvable; audio is over-engineering for now.
- **CAPTCHA-solving service integration** (2Captcha, AntiCaptcha). Free Claude vision is cheaper and stays in-account.
- **Removing playwright-stealth.** Stealth still helps avoid escalation to harder challenges (full JS challenges, browser fingerprinting). Keep it.
- **Changing the data model or output schema** (`ForexRates`, `ForexReserves`, `ForexSnapshot`). Unchanged.

## Architecture

```text
fetch_rendered_html(url) [unchanged outer fn]
   └─ _fetch_once(url) [modified]
        page.goto(url)
        if _is_captcha_page(page.content()):
            for attempt in 1..3:
                _extract_captcha_image(page.content(), tmp_path)
                answer = _solve_captcha_via_claude(tmp_path)
                if answer is None: continue
                page.fill("#ans", answer)
                page.click("#jar")
                page.wait_for_navigation()
                if not _is_captcha_page(page.content()): break
            else:
                raise ParseError("captcha solve failed after 3 attempts")
        if wait_for_selector: page.wait_for_selector(...)
        return page.content()
```

Three new helpers (private, module-level) in `scrapers/bb_forex.py`:

### 1. `_is_captcha_page(html: str) -> bool`

Detects BB's CAPTCHA wall. Signature markers:
- `<input ... id="ans"` AND
- `<button ... id="jar"` AND
- `<img ... class="thumbnails"` AND
- the text "support ID" anywhere in the body

All four required (any single one alone could be a false positive). Pure function, no I/O.

### 2. `_extract_captcha_image(html: str, dest_path: Path) -> None`

Parses the captcha image from `<img class="thumbnails" src="data:image/png;base64,...">`. Steps:
1. Regex: `<img[^>]+class=.thumbnails.[^>]+src=.data:image/png;base64,([^"\']+).`
2. Base64-decode the matched group.
3. Atomic write to `dest_path` via `.tmp + os.replace` (existing pattern).

Raises `ParseError` if no thumbnail image found or base64 decode fails. Returns `None` on success.

### 3. `_solve_captcha_via_claude(image_path: Path) -> str | None`

Calls `claude -p` with the image as an `@filepath` reference. Args:

```python
argv = [
    binary, "--print", "--strict-mcp-config",
    "--model", "claude-haiku-4-5",
    "--no-session-persistence",
    "--tools", "",
    "--permission-mode", "bypassPermissions",
]
prompt = (
    "What single common object is shown in this image? "
    "Examples of valid answers: 'bottle', 'arrows', 'dot', 'apple'. "
    "Reply with ONLY a single English lowercase common noun, no other text. "
    f"@{image_path}"
)
```

60-second timeout. Parse output: strip whitespace, lowercase, drop trailing punctuation, take first word. If output is empty or longer than 30 chars, return `None`. Otherwise return the cleaned word.

Reads `CLAUDE_BINARY` env (existing convention) — uses systemd-injected `CLAUDE_CODE_OAUTH_TOKEN` for auth (also from the env, set via `/etc/econdelta.env` since 2026-05-17).

### Integration in `_fetch_once`

Wrap the existing `page.goto + wait_for_selector` block with the CAPTCHA-loop above. Max 3 solve attempts. If still on the CAPTCHA page after 3 attempts, raise `ParseError("captcha solve failed after 3 attempts")` and let the existing retry layer (`fetch_rendered_html`) handle backoff.

The `wait_for_selector` step happens AFTER the CAPTCHA is cleared, against the now-real page content. Same behavior as today.

## Decisions Made

- **claude-haiku-4-5 over Opus** — reason: the task is single-frame object recognition on a 48×48 PNG. Haiku 4.5 is multimodal and handles this at 10× lower cost than Opus. Empirically verified ("arrows" identified correctly on first try).

- **`@filepath` syntax over base64-in-prompt** — reason: cleaner, no `cat | base64` pipe, and Claude Code's file-reference mechanism handles image attachment internally. Verified empirically to work in `-p` non-interactive mode.

- **3 retries, then escalate** — reason: at typical haiku error rates (~5%), one retry covers 99.75%. Three is generous defense without unbounded loop risk.

- **Mac laptop feed stays** — reason: redundancy is cheap; the launchd job already exists. On days when CAPTCHA-solve succeeds, the laptop's rsync is a no-op (idempotent). On the rare day Claude misidentifies the object 3× in a row, the laptop covers.

- **Helpers private (`_` prefix)** — reason: implementation detail of `_fetch_once`. Not part of public scraper surface.

- **No new module file** — reason: per existing convention (utils/ vs scrapers/), CAPTCHA solving is part of the BB scraper's fetch logic, not a general utility. Live in `bb_forex.py`.

- **Tests via fixture HTML** — reason: mirrors existing test convention (`tests/fixtures/bb_forex_reserves.html`). Add `tests/fixtures/bb_forex_captcha_page.html` (full captured wall) and `tests/fixtures/bb_forex_captcha.png` (the embedded image, base64-decoded as a binary).

## Open Questions

- **None.** All design decisions empirically verified.

## Definition of Done

- [ ] `_is_captcha_page` correctly detects BB CAPTCHA fixture and returns False on the normal exchange-rate fixture.
- [ ] `_extract_captcha_image` produces a valid PNG file from the fixture HTML, byte-identical to `tests/fixtures/bb_forex_captcha.png`.
- [ ] `_solve_captcha_via_claude` returns a single lowercase word for a fixture PNG (mocked subprocess in tests; real `claude -p` invocation works in manual smoke).
- [ ] `_fetch_once` integration retries up to 3× and ultimately raises `ParseError` after failure; tests cover both happy path and exhaustion.
- [ ] All existing `tests/test_bb_forex.py` tests still pass.
- [ ] On ExonVPS: `sudo systemctl enable --now econdelta-forex.timer` re-enables; manual `sudo systemctl start econdelta-forex.service` succeeds; transient cron-fire test (`systemd-run --on-active=15min`) succeeds; `~/econdelta/data/bb_forex/YYYY-MM-DD.json` has fresh non-zero values.
- [ ] Mac laptop launchd job remains in place (no action required — it's already running daily at 06:05 BDT).
