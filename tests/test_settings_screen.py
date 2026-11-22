"""The Settings screen's repair summary, run under Node when it is installed."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCREEN = Path(__file__).parents[1] / "ha_spark" / "api" / "static" / "settings.js"
NODE = shutil.which("node")


def _summary(run):
    script = (
        f"const {{repairSummary}} = await import({json.dumps(SCREEN.as_uri())});"
        f"console.log(repairSummary({json.dumps(run)}));"
    )
    done = subprocess.run(  # Fixed arguments, no shell.
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return done.stdout.strip()


def _run(status, **extra):
    return {"status": status, "started_at": "2026-11-22T14:30:00+00:00",
            "finished_at": None, "checked_days": 3, "fixed_slots": 5, "still_gaps": 2, **extra}


pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def test_no_run_yet_says_never():
    assert _summary(None) == "Never run."


def test_a_running_run_says_running():
    assert _summary(_run("running")) == "Running."


def test_a_done_run_reports_the_counts():
    summary = _summary(_run("done"))
    assert "checked 3 days, fixed 5 slots, 2 still gaps" in summary
    assert "failed" not in summary


def test_a_failed_run_reports_failure_without_counts():
    assert _summary(_run("failed")).endswith("failed.")


def test_missing_counts_report_as_zero_and_a_bad_timestamp_degrades():
    run = {"status": "done", "started_at": "not a date"}
    assert _summary(run) == "Last run unknown time: checked 0 days, fixed 0 slots, 0 still gaps."
