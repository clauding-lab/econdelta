"""The chart-feeding monthly appenders must survive an Opus hard reject.

Landmine 53 (2026-08-31): `main()`'s `hard_reject` branch `return 1`s roughly
190 lines above where both monthly appenders used to sit, so a verdict about
today's export/treasury numbers silently froze The Brief's yield-curve and
inflation charts. August 2026's yield-ladder rung had to be hand-written.

Two layers of cover here:

* behavioural — `_run_chart_feeding_monthly_appenders` runs both legs, contains
  each leg's failure independently, and never raises (so a caller can put it
  immediately before its own `return` without changing that return value);
* structural — an AST walk over the REAL `main()` source asserting the
  `hard_reject` branch still calls it BEFORE its `return`. A behavioural test
  of that ordering would have to drive the whole aggregate run; the ordering
  is the entire bug, so it gets asserted against the shipped source directly.
"""
from __future__ import annotations

import ast
import inspect

import pytest


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    """Swallow Discord notifies and record them for assertions."""
    import aggregate_latest as a

    sent: list[tuple] = []
    monkeypatch.setattr(a, "notify", lambda *args, **kw: sent.append(args))
    monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "0")
    return sent


class TestRunChartFeedingMonthlyAppenders:
    def test_runs_both_legs(self, monkeypatch):
        import aggregate_latest as a

        calls: list[str] = []
        monkeypatch.setattr(
            a, "_write_macro_monthly_append", lambda: calls.append("macro") or 3
        )
        monkeypatch.setattr(
            a, "_write_yield_ladder_monthly_append", lambda: calls.append("yield") or 8
        )

        a._run_chart_feeding_monthly_appenders()

        assert calls == ["macro", "yield"]

    def test_macro_failure_does_not_stop_the_yield_leg(self, monkeypatch, _no_notify):
        """The whole point of the two separate try/excepts.

        A dead BB remittance page must not cost the yield ladder its write.
        """
        import aggregate_latest as a

        calls: list[str] = []

        def _boom():
            raise RuntimeError("remittance page down")

        monkeypatch.setattr(a, "_write_macro_monthly_append", _boom)
        monkeypatch.setattr(
            a, "_write_yield_ladder_monthly_append", lambda: calls.append("yield") or 8
        )

        a._run_chart_feeding_monthly_appenders()

        assert calls == ["yield"]
        assert any("macro monthly append" in args[1] for args in _no_notify)

    def test_yield_failure_is_contained_and_notified(self, monkeypatch, _no_notify):
        import aggregate_latest as a

        def _boom():
            raise RuntimeError("auction_results unreachable")

        monkeypatch.setattr(a, "_write_macro_monthly_append", lambda: 0)
        monkeypatch.setattr(a, "_write_yield_ladder_monthly_append", _boom)

        a._run_chart_feeding_monthly_appenders()  # must not raise

        assert any("yield ladder append" in args[1] for args in _no_notify)

    def test_never_raises_even_when_both_legs_fail(self, monkeypatch, _no_notify):
        """Load-bearing: the hard-reject call site sits immediately before
        `return 1`, so an escaping exception there would convert a clean
        "rejected, exit 1" into an unhandled crash with a different alert."""
        import aggregate_latest as a

        def _boom():
            raise RuntimeError("everything is on fire")

        monkeypatch.setattr(a, "_write_macro_monthly_append", _boom)
        monkeypatch.setattr(a, "_write_yield_ladder_monthly_append", _boom)

        a._run_chart_feeding_monthly_appenders()

        assert len(_no_notify) == 2

    def test_skip_supabase_disables_both_legs(self, monkeypatch):
        import aggregate_latest as a

        monkeypatch.setenv("ECONDELTA_SKIP_SUPABASE", "1")
        called: list[str] = []
        monkeypatch.setattr(
            a, "_write_macro_monthly_append", lambda: called.append("macro")
        )
        monkeypatch.setattr(
            a, "_write_yield_ladder_monthly_append", lambda: called.append("yield")
        )

        a._run_chart_feeding_monthly_appenders()

        assert called == []


def _main_tree() -> ast.FunctionDef:
    import aggregate_latest as a

    module = ast.parse(inspect.getsource(a))
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("aggregate_latest.main() not found")


def _calls_appenders(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_run_chart_feeding_monthly_appenders"
        for n in ast.walk(node)
    )


class TestHardRejectPathOrdering:
    """Assert the shipped source, not a mock of it — the ordering IS the bug."""

    def _hard_reject_branch(self) -> ast.If:
        for node in ast.walk(_main_tree()):
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "hard_reject"
            ):
                return node
        raise AssertionError("`if hard_reject:` branch not found in main()")

    def test_hard_reject_branch_calls_the_appenders(self):
        assert _calls_appenders(self._hard_reject_branch())

    def test_call_comes_before_the_return(self):
        branch = self._hard_reject_branch()
        call_line = min(
            n.lineno
            for n in ast.walk(branch)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_run_chart_feeding_monthly_appenders"
        )
        return_line = min(
            n.lineno for n in ast.walk(branch) if isinstance(n, ast.Return)
        )
        assert call_line < return_line

    def test_hard_reject_branch_still_returns_nonzero(self):
        """Running the appenders must not turn a rejected run into a pass."""
        returns = [
            n for n in ast.walk(self._hard_reject_branch()) if isinstance(n, ast.Return)
        ]
        assert returns, "hard_reject branch no longer returns"
        assert all(
            isinstance(r.value, ast.Constant) and r.value.value == 1 for r in returns
        )

    def test_hard_reject_branch_does_not_publish(self):
        """Falling through to the bottom of main() would run write_latest() and
        publish exactly the bundle Opus rejected — the branch must still own
        its own return rather than reaching the happy path's writes."""
        branch = self._hard_reject_branch()
        assert not any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "write_latest"
            for n in ast.walk(branch)
        )

    def test_happy_path_call_site_still_exists(self):
        """Exactly two call sites: the happy path and the hard-reject path."""
        sites = [
            n
            for n in ast.walk(_main_tree())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_run_chart_feeding_monthly_appenders"
        ]
        assert len(sites) == 2
