"""Dispatch + refusal tests for deploy/copotron-ops-ssh.sh.

The script is the forced command behind Copotron's ExonVPS ops key, so its
refusal path IS the security boundary: whatever it does not recognise must
never reach a shell. These tests drive it exactly the way sshd does — by
setting SSH_ORIGINAL_COMMAND and invoking the script with no arguments.

Verb bodies that would touch the real box (systemctl, git) run under
COPOTRON_OPS_DRYRUN=1, which only prints the dispatch decision. The two verbs
that touch nothing but files — `logs` and `log` — run for real against a
temporary ECONDELTA_HOME, so the log tail and the secret scrubber are
exercised as shipped rather than mocked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "copotron-ops-ssh.sh"


def run(command: str, *, dryrun: bool = True, home: Path | None = None):
    env = {"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": command}
    if dryrun:
        env["COPOTRON_OPS_DRYRUN"] = "1"
    if home is not None:
        env["ECONDELTA_HOME"] = str(home)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# --- the allowed verbs dispatch -------------------------------------------

@pytest.mark.parametrize(
    "command,expected",
    [
        ("pull", "DRYRUN pull"),
        ("head", "DRYRUN head"),
        ("timers", "DRYRUN timers"),
        ("logs", "DRYRUN logs"),
        ("status parse", "DRYRUN status econdelta-parse.service"),
        ("status media-screen", "DRYRUN status econdelta-media-screen.service"),
        ("log parse", "DRYRUN log parse 60"),
        ("log parse 200", "DRYRUN log parse 200"),
    ],
)
def test_allowed_verbs_dispatch(command, expected):
    r = run(command)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == expected


DEPLOY = SCRIPT.parent


def _real_unit_tokens() -> list[str]:
    """`parse`, `media-screen`, … — from the econdelta-<unit>.service filenames."""
    return sorted(p.name[len("econdelta-"):-len(".service")] for p in DEPLOY.glob("econdelta-*.service"))


def _real_log_tokens() -> list[str]:
    """`parse`, `media_screen`, `bb_forex`, … — from the units' StandardOutput paths.

    NOT the same set as the unit tokens: units are hyphenated, the log files
    they write are snake_case. That mismatch is exactly what the charset guard
    has to accommodate.
    """
    seen = set()
    for svc in DEPLOY.glob("econdelta-*.service"):
        for line in svc.read_text().splitlines():
            if "append:" in line and line.strip().endswith("-systemd.log"):
                seen.add(Path(line.split("append:", 1)[1]).name[: -len("-systemd.log")])
    return sorted(seen)


@pytest.mark.parametrize("unit", _real_unit_tokens())
def test_every_real_unit_is_reachable_by_status(unit):
    r = run(f"status {unit}")
    assert r.returncode == 0, f"status {unit} refused: {r.stderr}"
    assert r.stdout.strip() == f"DRYRUN status econdelta-{unit}.service"


@pytest.mark.parametrize("unit", _real_log_tokens())
def test_every_real_log_is_reachable_by_log(unit):
    """Guards the underscore case: half the log files are snake_case, so a
    hyphen-only charset silently puts bb_forex/media_screen/world_bank_pink_sheet
    out of reach — the key would look fine until the day one of them is needed.
    """
    r = run(f"log {unit}")
    assert r.returncode == 0, f"log {unit} refused: {r.stderr}"
    assert r.stdout.strip() == f"DRYRUN log {unit} 60"


def test_extra_whitespace_is_tolerated():
    r = run("   status    parse   ")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "DRYRUN status econdelta-parse.service"


def test_empty_command_prints_the_verb_table():
    """A bare `ssh exonhost` (no command) should teach, not error."""
    r = run("")
    assert r.returncode == 0, r.stderr
    for verb in ("pull", "head", "timers", "status", "log", "logs"):
        assert verb in r.stdout


def test_help_lists_every_verb():
    r = run("help")
    assert r.returncode == 0, r.stderr
    for verb in ("pull", "head", "timers", "status", "log", "logs"):
        assert verb in r.stdout


# --- everything else is refused -------------------------------------------

REFUSED = [
    # A plain shell would be the whole point of the restriction.
    "bash",
    "sh -c 'id'",
    "/bin/bash",
    # Reading the secrets file is the specific thing this key promises it cannot do.
    "cat /etc/econdelta.env",
    "pull /etc/econdelta.env",
    "log ../../etc/econdelta.env",
    "log ../../../etc/passwd",
    "logs /etc",
    # Chaining onto an allowed verb.
    "pull; cat /etc/econdelta.env",
    "pull && id",
    "pull | id",
    "head; rm -rf /",
    "status parse; id",
    "status $(id)",
    "status `id`",
    "status parse\nid",
    # Verbs that take no arguments must reject them outright.
    "pull main",
    "head HEAD",
    "timers all",
    "help me",
    # Unit-token charset.
    "status ../parse",
    "status parse.service",
    "status PARSE",
    "status -all",
    "status parse extra",
    "status",
    # Line-count guard.
    "log parse 0",
    "log parse 201",
    "log parse 9999",
    "log parse all",
    "log parse -5",
    "log parse 60 extra",
    "log",
    # Near-misses on real verbs.
    "pullx",
    "Pull",
    "",
]


@pytest.mark.parametrize("command", [c for c in REFUSED if c])
def test_refused_commands_exit_2_and_explain(command):
    r = run(command)
    assert r.returncode == 2, f"{command!r} was NOT refused: {r.stdout}{r.stderr}"
    assert "refused:" in r.stderr
    # The refusal itself must not leak what it was asked to do into a shell.
    assert "uid=" not in r.stdout


def test_refusal_names_the_allowed_verbs():
    r = run("rm -rf /")
    assert r.returncode == 2
    assert "this key accepts only:" in r.stderr


# --- the two verbs that run for real --------------------------------------

@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "parse-systemd.log").write_text(
        "\n".join(f"line {i}" for i in range(1, 101)) + "\n"
    )
    return tmp_path


def test_log_tails_the_requested_number_of_lines(fake_home):
    r = run("log parse 5", dryrun=False, home=fake_home)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["line 96", "line 97", "line 98", "line 99", "line 100"]


def test_log_on_a_missing_file_says_so_without_traversing(fake_home):
    r = run("log aggregate", dryrun=False, home=fake_home)
    assert r.returncode == 1
    assert "no such log" in r.stderr


def test_logs_lists_the_log_files(fake_home):
    r = run("logs", dryrun=False, home=fake_home)
    assert r.returncode == 0, r.stderr
    assert "parse-systemd.log" in r.stdout


def test_log_output_is_scrubbed_of_key_shaped_strings(fake_home):
    """A secret that leaked into a log must not leak back out through this key.

    The script never loads /etc/econdelta.env, but other services write these
    logs, and a traceback or a debug line can carry a token. All fake values.
    """
    (fake_home / "logs" / "parse-systemd.log").write_text(
        "ERROR calling supabase with key sb_secret_FAKEfakeFAKE0123456789\n"
        "auth header eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiZmFrZSJ9.FAKEsignature\n"
        "anthropic sk-ant-FAKEfakeFAKE0123456789 used\n"
        "harmless line about the parse run\n"
    )
    r = run("log parse 10", dryrun=False, home=fake_home)
    assert r.returncode == 0, r.stderr
    assert "sb_secret_FAKEfakeFAKE0123456789" not in r.stdout
    assert "eyJhbGciOiJIUzI1NiJ9" not in r.stdout
    assert "sk-ant-FAKEfakeFAKE0123456789" not in r.stdout
    assert "REDACTED" in r.stdout
    # Scrubbing must not eat ordinary log lines.
    assert "harmless line about the parse run" in r.stdout


# --- the promise the key was granted on -----------------------------------

def test_script_never_sources_the_env_file():
    """Adnan granted this key on the explicit promise that it cannot read
    /etc/econdelta.env. media-decide-ssh.sh does source it; this one must not.
    """
    body = SCRIPT.read_text()
    for line in body.splitlines():
        code = line.split("#", 1)[0]
        assert "econdelta.env" not in code, f"env file referenced in code: {line}"
        assert not code.strip().startswith(("source ", ". /")), line
