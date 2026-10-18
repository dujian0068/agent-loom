"""A changed configuration cannot take over a real interrupted process."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("role", ["react", "worker"])
@pytest.mark.parametrize("profile", ["spec", "schema", "number"])
def test_process_exit_rejects_changes_and_resumes_original(tmp_path, role, profile):
    worker = Path(__file__).parent / "support" / "binding_process.py"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}

    def run(phase):
        return subprocess.run(
            [sys.executable, str(worker), str(tmp_path), role, profile, phase],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    first = run("cut")
    assert first.returncode == 75, first.stderr
    audit = tmp_path / "audit.jsonl"
    saved = audit.read_bytes() if audit.exists() else b""
    time.sleep(0.35)
    rejected = run("change")
    assert rejected.returncode == 0 and not rejected.stderr
    assert json.loads(rejected.stdout)["error"] == "RECOVERY_BINDING_MISMATCH"
    assert (audit.read_bytes() if audit.exists() else b"") == saved
    resumed = run("resume")
    assert resumed.returncode == 0 and not resumed.stderr, resumed.stderr
    assert json.loads(resumed.stdout) == {
        "status": "succeeded",
        "loops": 3 if role == "worker" else 1,
    }
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [entry["role"] for entry in calls] == (
        ["planner", "worker", "planner"] if role == "worker" else ["react"]
    )
