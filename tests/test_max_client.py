"""Tests for the claude-p subprocess wrapper.

The wrapper invokes `claude -p` via subprocess and parses the JSON envelope
the CLI returns. We mock subprocess.run so tests don't actually call Claude.
"""
import json
import subprocess
from unittest.mock import patch

import pytest

from claude_max.max_client import MaxCallError, run_max


def _fake_cli_output(result_text: str = '{"value": 42}') -> str:
    return json.dumps({
        "result": result_text,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "total_cost_usd": 0.0,
    })


def test_run_max_parses_clean_json_result():
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=_fake_cli_output(), stderr="")
    with patch("subprocess.run", return_value=fake):
        r = run_max(prompt="hi")
    assert r.parsed == {"value": 42}
    assert r.tokens["input"] == 100


def test_run_max_strips_markdown_fences():
    fake = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=_fake_cli_output('```json\n{"value": 7}\n```'),
        stderr="",
    )
    with patch("subprocess.run", return_value=fake):
        r = run_max(prompt="hi")
    assert r.parsed == {"value": 7}


def test_run_max_raises_on_nonzero_exit():
    fake = subprocess.CompletedProcess(args=[], returncode=2, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=fake):
        with pytest.raises(MaxCallError, match="exited 2"):
            run_max(prompt="hi")


def test_run_max_raises_on_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        with pytest.raises(MaxCallError, match="timed out"):
            run_max(prompt="hi", timeout_s=1)


def test_run_max_uses_opus_4_6_default():
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=_fake_cli_output(), stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        run_max(prompt="hi")
    assert "--model" in captured["argv"]
    assert "claude-opus-4-6" in captured["argv"]
