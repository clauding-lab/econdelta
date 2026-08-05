"""Invariant check for systemd restart/start-limit windows in deploy/*.service.

systemd's StartLimitBurst only counts starts that land inside a single
StartLimitIntervalSec window. If a unit's failure-to-retry cycle
(TimeoutStartSec + RestartSec) is longer than that window, consecutive
restarts never land in the same window and the limiter never trips — a
reproducibly-failing unit (OOM, timeout) restart-loops forever with no
alert. See AGENT_LEARNINGS.md / ops audit item #4.
"""

from pathlib import Path

DEPLOY_DIR = Path(__file__).parent.parent / "deploy"


def _parse_service_ints(path: Path) -> dict[str, int]:
    """Extract integer-valued systemd keys from a .service file.

    Ignores comments and blank lines. Only the small set of keys this
    invariant cares about are parsed; anything else is skipped.
    """
    wanted = {"TimeoutStartSec", "RestartSec", "StartLimitIntervalSec"}
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
    """For every unit that retries on failure, one full retry cycle
    (TimeoutStartSec + RestartSec) must fit inside StartLimitIntervalSec.

    Otherwise a unit that fails on every attempt (OOM, hung timeout) never
    produces two starts within the same window, so StartLimitBurst never
    trips and systemd restarts it forever with no alert.
    """
    violations = []
    for service_file in _all_service_files():
        values = _parse_service_ints(service_file)
        if "_has_restart" not in values:
            continue  # unit doesn't retry on failure; invariant doesn't apply
        timeout_start = values.get("TimeoutStartSec")
        restart_sec = values.get("RestartSec")
        interval = values.get("StartLimitIntervalSec")
        if timeout_start is None or restart_sec is None or interval is None:
            continue  # unit doesn't declare the full trio; nothing to check
        cycle = timeout_start + restart_sec
        if cycle > interval:
            violations.append(
                f"{service_file.name}: TimeoutStartSec({timeout_start}) + "
                f"RestartSec({restart_sec}) = {cycle} > "
                f"StartLimitIntervalSec({interval})"
            )

    assert not violations, (
        "Units below can restart-loop forever without ever tripping "
        "StartLimitBurst, because one retry cycle doesn't fit inside the "
        "start-limit window:\n" + "\n".join(violations)
    )
