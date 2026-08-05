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


# unit -> (scraper source file, relative to repo root) for every unit whose
# main() has a documented, non-error "stale/anomaly, write held or skipped"
# exit code 2 (see utils/supabase_writer._STATUS_BY_EXIT: 2 -> 'stale').
# Deliberately an explicit, named list rather than a generic cross-file
# scanner: `briefing.service` wraps `briefing/__main__.py`'s own main(), but
# other units (e.g. `econdelta-sentinel.service`) import their main() from a
# sibling module (`from .main import main`), which a naive same-file scan
# would misread. Reviewed 2026-08-05 (Opus PR #115 follow-up): these are the
# four units confirmed to have a documented exit-2 path AND a Restart=
# ladder that fits inside its own StartLimit window (i.e. capable of
# actually retry-storming on a deterministic anomaly).
_EXIT_2_UNITS = {
    "econdelta-forex.service": "scrapers/bb_forex.py",
    "econdelta-briefing.service": "briefing/__main__.py",
    "econdelta-dse.service": "scrapers/dse_market.py",
    "econdelta-commodity.service": "scrapers/commodity_prices.py",
}


def test_exit_2_source_files_still_document_a_stale_exit():
    """Sanity check `_EXIT_2_UNITS` against source: each listed scraper's
    main() must still contain a literal `return 2` — if a future edit
    removes that exit path, this list (and the pin test below) goes stale
    and should be updated, not silently trusted.
    """
    repo_root = DEPLOY_DIR.parent
    missing = [
        source
        for source in _EXIT_2_UNITS.values()
        if "return 2" not in (repo_root / source).read_text()
    ]
    assert not missing, (
        "These source files no longer contain a literal 'return 2' — "
        "update _EXIT_2_UNITS in this test file to match:\n" + "\n".join(missing)
    )


def test_documented_exit_2_units_do_not_retry_storm():
    """Every unit in `_EXIT_2_UNITS` must neutralise its documented exit-2
    "anomaly/stale — write held or skipped" path via `RestartPreventExitStatus=2`
    (this repo's established precedent, see `econdelta-forex.service` /
    `econdelta-briefing.service`) or `SuccessExitStatus=2`.

    Without one of these, `Restart=on-failure` treats a deterministic
    anomaly the same way it treated `econdelta-npl-structure.service`'s
    undeclared exit-3 skip: it re-fires identically on every retry (the
    anomaly doesn't go away on a retry), burning a full scrape/fetch each
    time, duplicating the Discord alert once per retry, and running the
    unit right up its StartLimitBurst ladder before landing in 'failed'.
    Found live 2026-08-05 (Opus review of PR #115): `econdelta-dse.service`
    and `econdelta-commodity.service` had neither directive.

    Whether a 'stale' exit should eventually read fully green
    (`SuccessExitStatus=2`) instead of 'failed-but-not-storming'
    (`RestartPreventExitStatus=2`) is an owner-level monitoring-semantics
    call, deliberately left open here — this test only requires ONE of the
    two, not a specific one.
    """
    missing = []
    for unit_name, source in _EXIT_2_UNITS.items():
        text = (DEPLOY_DIR / unit_name).read_text()
        codes: set[str] = set()
        for key in ("RestartPreventExitStatus", "SuccessExitStatus"):
            for value in _values_for_key(text, key):
                codes.update(value.split())
        if "2" not in codes:
            missing.append(
                f"{unit_name}: no RestartPreventExitStatus=2 or SuccessExitStatus=2 "
                f"(documented exit-2 path in {source})"
            )

    assert not missing, (
        "Units below have a documented exit-2 'stale/anomaly' path (see "
        "utils/supabase_writer._STATUS_BY_EXIT) with nothing telling "
        "systemd not to retry-storm on it:\n" + "\n".join(missing)
    )


def _guarded_log_paths(text: str) -> set[str]:
    """Every log path this unit's StandardOutput/StandardError would cause
    systemd to create on disk if absent.

    Both `append:<path>` (the fleet's actual convention) and `file:<path>`
    (systemd's other file-backed redirect — truncate-on-start rather than
    append, but creates the target the identical way if it's missing) are
    included: either one hits the same root-creates-before-privilege-drop
    trap this guard exists to close. StandardOutput and StandardError are
    unioned rather than just reading StandardOutput, since nothing requires
    them to point at the same file.
    """
    paths: set[str] = set()
    for key in ("StandardOutput", "StandardError"):
        for value in _values_for_key(text, key):
            for prefix in ("append:", "file:"):
                if value.startswith(prefix):
                    paths.add(value[len(prefix) :])
    return paths


def _service_owner(text: str) -> str | None:
    """`User:Group` for this unit, or None if either is undeclared."""
    users = _values_for_key(text, "User")
    groups = _values_for_key(text, "Group")
    if not users or not groups:
        return None
    return f"{users[-1]}:{groups[-1]}"


def test_every_appended_log_has_a_privileged_ownership_guard():
    """Every unit that writes stdout/stderr to a persistent log file must
    also touch+chown that exact file to the service's OWN User=/Group= via
    a `+`-privileged ExecStartPre before ExecStart runs.

    Why: if the log file is absent when the unit (re)starts, systemd
    creates the `StandardOutput=`/`StandardError=` file-backed target as
    root BEFORE ExecStart drops privileges to User=/Group= — so the file
    is created root:root even though the service runs as adnan-local. The
    nightly `su adnan-local adnan-local` in /etc/logrotate.d/econdelta then
    cannot open that root-owned file for writing (it's still world-readable
    at 0644 — the break is specifically `copytruncate`'s O_RDWR-and-truncate
    step), and the WHOLE logrotate run for econdelta's logs fails with
    'Permission denied'. The '+' prefix on ExecStartPre runs outside the
    unit's own sandboxing (User=/Group=, ProtectHome=, ProtectSystem=) so
    the chown can always succeed, regardless of who most recently
    (re)created the file. See AGENT_LEARNINGS.md 2026-08-05 and
    deploy/README.md Troubleshooting.

    This check requires the guard line to actually `touch` the path (not
    just chown it — chown alone would fail outright on a path that doesn't
    exist yet) and to chown it to THIS unit's own declared owner, not a
    hardcoded string — a unit that ever runs as something other than
    adnan-local:adnan-local would otherwise pass this test while chowning
    its log to the wrong user.
    """
    missing = []
    for service_file in _all_service_files():
        text = service_file.read_text()
        log_paths = _guarded_log_paths(text)
        if not log_paths:
            continue
        owner = _service_owner(text)
        if owner is None:
            missing.append(f"{service_file.name}: no User=/Group= to derive expected chown owner")
            continue
        for log_path in log_paths:
            guarded = any(
                line.strip().startswith("ExecStartPre=+")
                and log_path in line
                and "touch" in line
                and "chown" in line
                and f"chown {owner} " in line
                for line in text.splitlines()
            )
            if not guarded:
                missing.append(
                    f"{service_file.name}: no privileged touch+chown-to-{owner} guard for {log_path}"
                )

    assert not missing, (
        "Units below write to a log file with no privileged ownership "
        "guard (or a guard chowning to the wrong owner), so a root-recreated "
        "log file can silently break nightly logrotate (AGENT_LEARNINGS.md "
        "2026-08-05):\n" + "\n".join(missing)
    )
