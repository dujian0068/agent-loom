"""Real process death/recovery with a separately installed provider package."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["react", "plan"])
def test_installed_provider_crash_change_and_resume(tmp_path, installed_file_package, mode):
    worker = Path(__file__).parent / "support" / "workspace_spi_process.py"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(installed_file_package), str(Path(__file__).parents[1] / "src")]
        ),
    }

    def run(phase):
        return subprocess.run(
            [sys.executable, str(worker), str(tmp_path), mode, phase],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )

    first = run("cut")
    assert first.returncode == 75, first.stdout + first.stderr
    saved = (tmp_path / "calls.jsonl").read_bytes()
    assert len([line for line in saved.splitlines() if json.loads(line)["method"] == "write"]) == 1
    time.sleep(0.35)
    changed = run("change")
    assert changed.returncode == 0 and not changed.stderr, changed.stderr
    assert json.loads(changed.stdout) == {"error": "RECOVERY_BINDING_MISMATCH"}
    assert (tmp_path / "calls.jsonl").read_bytes() == saved
    resumed = run("resume")
    assert resumed.returncode == 0 and not resumed.stderr, resumed.stdout + resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "succeeded"
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    tool_calls = [item for item in calls if item["purpose"] == "tool"]
    from agentloom import FILE_OPERATIONS

    assert set(FILE_OPERATIONS) <= {item["method"] for item in tool_calls}
    # edit and patch each use their own provider's conditional write internally.
    writes = [item for item in tool_calls if item["method"] == "write"]
    assert len(writes) == len({item["operation_id"] for item in writes}) == 3
    assert {c["role"] for c in tool_calls} == {"react" if mode == "react" else "worker"}
    assert not (tmp_path / "workspace").exists()
