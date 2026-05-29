"""Guard: every deterministic parser referenced in sources-v3.json must be
registered when the production entry point (parse_all) is imported.

Regression test for the 2026-05-29 corridor outage. PR #30 added
``parsers/pdf_table_column_latest.py`` (with its ``@register`` decorator) plus
three ``sources-v3.json`` entries, but forgot the
``import parsers.pdf_table_column_latest`` line in ``parse_all.py``'s
auto-import block. The 26 unit tests passed because they import the parser
module directly (which triggers ``@register``); production ``parse_all`` did
not, so the parser was absent from ``REGISTRY`` and all three corridor metrics
raised ``"no parser registered"`` on every scheduled run.

The check runs in a FRESH interpreter so it exercises ``parse_all``'s own
import block, immune to registration leaked in by sibling test modules that
import parser modules directly.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_config_deterministic_parser_registered_via_parse_all():
    # Import only parse_all (the production entry point), then compare the
    # populated REGISTRY against every parser the config actually needs.
    probe = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "import parse_all  # noqa: F401 -- triggers the auto-import block\n"
        "from parsers.registry import REGISTRY\n"
        "cfg = json.loads(Path('config/sources-v3.json').read_text())\n"
        "needed = sorted({\n"
        "    i['parse']['deterministic']\n"
        "    for i in cfg['indicators']\n"
        "    if i.get('parse', {}).get('deterministic')\n"
        "})\n"
        "missing = [n for n in needed if n not in REGISTRY]\n"
        "print('MISSING=' + ','.join(missing))\n"
        "sys.exit(1 if missing else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "parse_all's auto-import block does not register every deterministic "
        "parser referenced in config/sources-v3.json. Add the missing "
        "`import parsers.<module>` line to parse_all.py.\n"
        f"stdout: {result.stdout.strip()}\n"
        f"stderr: {result.stderr.strip()}"
    )
