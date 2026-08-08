"""Guard: pdfplumber must stay capped below the version coupled to Pillow>=12.2.0.

Context (PR fix/pillow-dependabot-high-alerts, 2026-08-08 review round): bumping
the Pillow pin to close 26 Dependabot alerts has a side effect that is easy to
miss because neither manifest's ``pdfplumber`` line changes in the diff.
pdfplumber 0.11.10 declares ``Pillow>=12.2.0`` and pins ``pdfminer.six==20260107``
(via its own exact ``==``); pdfplumber 0.11.9 only requires ``Pillow>=9.1`` and
pins ``pdfminer.six==20251230``. Once the Pillow floor moves to 12.3, pip is
free to resolve either pdfplumber release — left unconstrained, it silently
picks the newest one (0.11.10), which drags ``pdfminer.six`` (the PDF
text-extraction engine every Tier-1 deterministic parser regexes against) and
``pypdfium2`` along with it as a resolver side effect, with no line in either
manifest saying so.

``pdfplumber`` is explicitly on VISION.md's "Needs Sign-Off" list ("any bump
of: playwright, playwright-stealth, pdfplumber, pydantic"). This test pins the
manifests' own ceiling below 0.11.10 so a Pillow-only security bump can ship
without an implicit pdfplumber bump riding along, and so any future edit that
raises the ceiling (deliberately, once sign-off is given, or accidentally) has
to touch this test — a loud, explicit failure instead of a silent resolve-time
drift.

When sign-off for the pdfplumber bump is actually given: update
``PDFPLUMBER_NEEDS_SIGNOFF_AT`` to the new sign-off boundary (or delete this
guard if pdfplumber is no longer capped) as part of that dedicated PR, not as
a side effect of an unrelated dependency change.
"""

from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).parent.parent

# The first pdfplumber release that requires Pillow>=12.2.0 and pins a newer
# pdfminer.six/pypdfium2 as a result. Manifests must exclude this and above
# until a dedicated, sign-off'd PR raises the ceiling.
PDFPLUMBER_NEEDS_SIGNOFF_AT = Version("0.11.10")


def _pdfplumber_requirement_from_requirements_txt() -> Requirement:
    text = (REPO_ROOT / "requirements.txt").read_text()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        if canonicalize_name(req.name) == "pdfplumber":
            return req
    raise AssertionError("pdfplumber not found in requirements.txt")


def _pdfplumber_requirement_from_pyproject() -> Requirement:
    text = (REPO_ROOT / "pyproject.toml").read_text()
    start = text.index("dependencies = [")
    end = text.index("]", start)
    block = text[start:end]
    for raw_line in block.splitlines():
        line = raw_line.split("#", 1)[0].strip().strip(",").strip('"').strip("'")
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        if canonicalize_name(req.name) == "pdfplumber":
            return req
    raise AssertionError("pdfplumber not found in pyproject.toml dependencies")


def _allows_signoff_version(req: Requirement) -> bool:
    return req.specifier.contains(str(PDFPLUMBER_NEEDS_SIGNOFF_AT), prereleases=True)


def test_requirements_txt_pdfplumber_excludes_unsignedoff_bump():
    req = _pdfplumber_requirement_from_requirements_txt()
    assert not _allows_signoff_version(req), (
        f"requirements.txt pdfplumber pin ({req}) now allows "
        f"{PDFPLUMBER_NEEDS_SIGNOFF_AT}+, which pulls a newer pdfminer.six/"
        "pypdfium2 as a resolver side effect — pdfplumber is on VISION.md's "
        "sign-off list; raise this cap only as part of a dedicated, "
        "sign-off'd PR (and update PDFPLUMBER_NEEDS_SIGNOFF_AT here)."
    )


def test_pyproject_toml_pdfplumber_excludes_unsignedoff_bump():
    req = _pdfplumber_requirement_from_pyproject()
    assert not _allows_signoff_version(req), (
        f"pyproject.toml pdfplumber pin ({req}) now allows "
        f"{PDFPLUMBER_NEEDS_SIGNOFF_AT}+, which pulls a newer pdfminer.six/"
        "pypdfium2 as a resolver side effect — pdfplumber is on VISION.md's "
        "sign-off list; raise this cap only as part of a dedicated, "
        "sign-off'd PR (and update PDFPLUMBER_NEEDS_SIGNOFF_AT here)."
    )
