"""Invariant check for systemd restart/start-limit windows in deploy/*.service.

systemd refuses to start a unit again once it has been started *more than*
StartLimitBurst times inside a single StartLimitIntervalSec window (see
systemd.unit(5)). For a unit that fails at every attempt, consecutive starts
are spaced one retry cycle apart (TimeoutStartSec + RestartSec), so the
(StartLimitBurst + 1)-th start — the one that must land inside the window
for the limiter to trip — occurs StartLimitBurst * cycle after the first
start. The trip condition is therefore:

    StartLimitBurst * (TimeoutStartSec + RestartSec) <= StartLimitIntervalSec

Sizing the window to fit only ONE cycle (the earlier, weaker form of this
check) merely guarantees two starts land in a window — never "more than
burst" for any unit with burst >= 2. That leaves the limiter never tripping,
so a reproducibly-failing unit (OOM, timeout) restart-loops forever with no
alert. See AGENT_LEARNINGS.md / ops audit item #4.
"""

from pathlib import Path

DEPLOY_DIR = Path(__file__).parent.parent / "deploy"


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
    (TimeoutStartSec + RestartSec each) must fit inside StartLimitIntervalSec.

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
        cycle = timeout_start + restart_sec
        required = burst * cycle
        if required > interval:
            violations.append(
                f"{service_file.name}: StartLimitBurst({burst}) * "
                f"(TimeoutStartSec({timeout_start}) + RestartSec({restart_sec})) "
                f"= {required} > StartLimitIntervalSec({interval})"
            )

    assert not violations, (
        "Units below can restart-loop forever without ever tripping "
        "StartLimitBurst, because StartLimitBurst retry cycles don't fit "
        "inside the start-limit window:\n" + "\n".join(violations)
    )
