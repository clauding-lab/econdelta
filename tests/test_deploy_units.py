"""Invariant check for systemd restart/start-limit windows in deploy/*.service.

systemd refuses to start a unit again once it has been started *more than*
StartLimitBurst times inside a single StartLimitIntervalSec window (see
systemd.unit(5)). For a unit that fails at every attempt, consecutive starts
are spaced one retry cycle apart (TimeoutStartSec + RestartSec), so the
(StartLimitBurst + 1)-th start — the one that must land inside the window
for the limiter to trip — occurs StartLimitBurst * cycle after the first
start. On a start timeout systemd also sends SIGTERM and waits up to
TimeoutStopSec before RestartSec starts counting, so the real cycle
includes that stop phase too. The trip condition is therefore:

    StartLimitBurst * (TimeoutStartSec + RestartSec + TIMEOUT_STOP_DEFAULT) <= StartLimitIntervalSec

Sizing the window to fit only ONE cycle (the earlier, weaker form of this
check) merely guarantees two starts land in a window — never "more than
burst" for any unit with burst >= 2. That leaves the limiter never tripping,
so a reproducibly-failing unit (OOM, timeout) restart-loops forever with no
alert. See AGENT_LEARNINGS.md / ops audit item #4.
"""

from pathlib import Path

DEPLOY_DIR = Path(__file__).parent.parent / "deploy"

# No unit in deploy/ sets TimeoutStopSec, so a start-timeout's SIGTERM wait
# falls back to systemd's DefaultTimeoutStopSec (90s) before RestartSec starts.
TIMEOUT_STOP_DEFAULT = 90


def _parse_service_ints(path: Path) -> dict[str, int]:
    """Extract integer-valued systemd keys from a .service file.

    Ignores comments and blank lines. Only the small set of keys this
    invariant cares about are parsed; anything else is skipped.
    """
    wanted = {
        "TimeoutStartSec",
        "RestartSec",
        "StartLimitIntervalSec",
        "StartLimitBurst",
    }
    values: dict[str, int] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in wanted:
            values[key] = int(value.strip())
        elif key == "Restart":
            values["_has_restart"] = 1
    return values


def _all_service_files() -> list[Path]:
    return sorted(DEPLOY_DIR.glob("*.service"))


def test_deploy_dir_has_service_files():
    """Sanity check the invariant test is actually looking at real units."""
    assert len(_all_service_files()) > 0


def test_restart_cycle_fits_inside_start_limit_window():
    """For every unit that retries on failure, StartLimitBurst retry cycles
    (TimeoutStartSec + RestartSec + TIMEOUT_STOP_DEFAULT each) must fit
    inside StartLimitIntervalSec.

    systemd only refuses to restart a unit once it has started *more than*
    StartLimitBurst times inside one StartLimitIntervalSec window — so the
    (StartLimitBurst + 1)-th start must land inside the window. Consecutive
    starts of a unit failing at every attempt are spaced one retry cycle
    apart, so that start lands StartLimitBurst * cycle after the first one.
    Sizing the window for a single cycle (cycle <= interval) is not enough:
    it only guarantees two starts share a window, never "more than burst"
    for any unit with burst >= 2. Get this wrong and a unit that fails on
    every attempt (OOM, hung timeout) restarts forever with no alert.
    """
    violations = []
    for service_file in _all_service_files():
        values = _parse_service_ints(service_file)
        if "_has_restart" not in values:
            continue  # unit doesn't retry on failure; invariant doesn't apply
        timeout_start = values.get("TimeoutStartSec")
        restart_sec = values.get("RestartSec")
        interval = values.get("StartLimitIntervalSec")
        burst = values.get("StartLimitBurst")
        if None in (timeout_start, restart_sec, interval, burst):
            continue  # unit doesn't declare the full quartet; nothing to check
        cycle = timeout_start + restart_sec + TIMEOUT_STOP_DEFAULT
        required = burst * cycle
        if required > interval:
            violations.append(
                f"{service_file.name}: StartLimitBurst({burst}) * "
                f"(TimeoutStartSec({timeout_start}) + RestartSec({restart_sec}) + "
                f"TIMEOUT_STOP_DEFAULT({TIMEOUT_STOP_DEFAULT})) "
                f"= {required} > StartLimitIntervalSec({interval})"
            )

    assert not violations, (
        "Units below can restart-loop forever without ever tripping "
        "StartLimitBurst, because StartLimitBurst retry cycles don't fit "
        "inside the start-limit window:\n" + "\n".join(violations)
    )


def _values_for_key(text: str, key: str) -> list[str]:
    """Return every value assigned to `key` in a unit file, in file order.

    Systemd list-valued directives (like SuccessExitStatus=) can repeat or
    hold multiple space-separated tokens on one line; this collects both.
    """
    values = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith(f"{key}="):
            _, _, value = line.partition("=")
            values.append(value.strip())
    return values


def test_npl_structure_treats_documented_skip_exit_as_success():
    """`scrapers/bb_npl_structure.py::main()` returns 3 for its documented
    "already captured for this position date — skip" case (see
    `utils/supabase_writer._STATUS_BY_EXIT`, which maps exit 3 to run_logs
    status 'skip'). Without `SuccessExitStatus=3`, systemd's
    `Restart=on-failure` treats that by-design skip as a failure: it
    restart-loops (burning a real ~50s extraction attempt each time) until
    `StartLimitBurst` trips, and the unit is left in 'failed' state — which
    is exactly the signal `systemctl --failed` is relied on to surface real
    box problems. See AGENT_LEARNINGS.md 2026-08-05.
    """
    text = (DEPLOY_DIR / "econdelta-npl-structure.service").read_text()
    codes: set[str] = set()
    for value in _values_for_key(text, "SuccessExitStatus"):
        codes.update(value.split())
    assert "3" in codes, (
        "econdelta-npl-structure.service must declare SuccessExitStatus=3 "
        "so the documented skip exit (bb_npl_structure.py main() `return 3`) "
        "is not treated as a failure by Restart=on-failure / systemctl --failed."
    )


def _appended_log_path(text: str) -> str | None:
    for value in _values_for_key(text, "StandardOutput"):
        if value.startswith("append:"):
            return value[len("append:") :]
    return None


def test_every_appended_log_has_a_privileged_ownership_guard():
    """Every unit that appends stdout/stderr to a persistent log file must
    also touch+chown that exact file to the service user via a
    `+`-privileged ExecStartPre.

    Why: if the log file is absent when the unit (re)starts, systemd
    creates the `StandardOutput=append:`/`StandardError=append:` target as
    root BEFORE ExecStart drops privileges to User=/Group= — so the file
    is created root:root even though the service runs as adnan-local. The
    nightly `su adnan-local adnan-local` in /etc/logrotate.d/econdelta then
    cannot open that root-owned file, and the WHOLE logrotate run for
    econdelta's logs fails with 'Permission denied'. The '+' prefix on
    ExecStartPre runs outside the unit's own sandboxing (User=/Group=,
    ProtectHome=, ProtectSystem=) so the chown can always succeed,
    regardless of who most recently (re)created the file.
    See AGENT_LEARNINGS.md 2026-08-05 and deploy/README.md Troubleshooting.
    """
    missing = []
    for service_file in _all_service_files():
        text = service_file.read_text()
        log_path = _appended_log_path(text)
        if log_path is None:
            continue
        guarded = any(
            line.strip().startswith("ExecStartPre=+")
            and log_path in line
            and "chown" in line
            for line in text.splitlines()
        )
        if not guarded:
            missing.append(f"{service_file.name}: no privileged chown guard for {log_path}")

    assert not missing, (
        "Units below append to a log file with no privileged ownership "
        "guard, so a root-recreated log file can silently break nightly "
        "logrotate (AGENT_LEARNINGS.md 2026-08-05):\n" + "\n".join(missing)
    )
