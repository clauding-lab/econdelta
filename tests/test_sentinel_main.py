"""sentinel.main() pre-read guards.

The cadence-map build (sources-v3.json read + lazy aggregate_latest import) sat
unguarded between the holidays and Supabase-read try/excepts, so a malformed
config or a broken aggregate_latest crashed the sentinel with run_logs=fail and
NO Discord alert — the silent-freeze class the sentinel exists to catch. These
tests pin the guard: any cadence-map failure alerts and fails 1, before the read.
"""
from unittest.mock import patch

from sentinel import main as sentinel_main


def test_cadence_map_failure_alerts_and_fails_without_reading():
    with patch(
        "sentinel.main.load_cadence_map",
        side_effect=ValueError("malformed sources-v3.json"),
    ), patch("sentinel.main.notify") as notify_mock, patch(
        "sentinel.main.fetch_all_freshness_rows"
    ) as read_mock:
        rc = sentinel_main.main()

    assert rc == 1
    notify_mock.assert_called_once()
    assert notify_mock.call_args.args[0] == "error"  # error-level Discord ping
    # The guard fires BEFORE the Supabase read — the sentinel never pretends to
    # have judged freshness it could not compute.
    read_mock.assert_not_called()


def test_aggregate_latest_import_error_is_guarded():
    """The lazy `from aggregate_latest import ...` inside load_cadence_map is the
    other named failure mode (deploy drift / a broken aggregate_latest). It must
    alert + fail 1 too, not raise out of main()."""
    with patch(
        "sentinel.main.load_cadence_map",
        side_effect=ImportError("cannot import name 'BRIEF_ALIASES'"),
    ), patch("sentinel.main.notify") as notify_mock, patch(
        "sentinel.main.fetch_all_freshness_rows"
    ) as read_mock:
        rc = sentinel_main.main()

    assert rc == 1
    notify_mock.assert_called_once()
    read_mock.assert_not_called()
