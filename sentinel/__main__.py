"""``python -m sentinel`` entry point.

Wrapped by ``wrap_run`` so every run writes a ``run_logs`` row under
``source='freshness_sentinel'`` — the dead-man's-switch The Brief's off-box
heartbeat checks via ``get_recent_run_ok('freshness_sentinel', within_hours=26)``.
"""
import sys

from utils.supabase_writer import wrap_run

from .main import main

if __name__ == "__main__":
    sys.exit(wrap_run("freshness_sentinel", "econdelta-sentinel.service", main))
