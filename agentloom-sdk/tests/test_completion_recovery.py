"""A process lost during teardown resumes the already committed outcome."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentloom import FileRuntimeStore
from agentloom.runtime.recovery import validate_recovery


@pytest.mark.parametrize("kind", ["model", "mcp", "mcp-error", "native-error", "native-invalid"])
def test_process_exit_during_close_preserves_completion(tmp_path, kind):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    worker = Path(__file__).parent / "support" / "completion_process.py"
    first = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), kind, "crash"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 74, first.stderr
    store = FileRuntimeStore(tmp_path / "state")
    state = store.load("default", "fixed")
    target = "model.generate" if kind == "model" else "capability.invoke"
    op = next(op for op in state["operations"].values() if op["event_type"] == target)
    assert op["raw"]["execution_fact"] == "completed"
    if kind.startswith("native-"):
        assert op["raw"]["error"]["code"] == (
            "CAPABILITY_RESULT_INVALID" if kind == "native-invalid" else "KNOWN_RESPONSE"
        )
    assert op["phase"] == "after" and len(op["attempts"]) == 1
    validate_recovery(state)
    store.close()
    time.sleep(0.35)
    second = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), kind, "resume"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {
        "status": "succeeded",
        "output": "saved" if kind == "model" else "done",
    }
    audit = (tmp_path / "audit").read_text().splitlines()
    assert audit.count("model") == (1 if kind == "model" else 2)
    assert audit.count("tool") == (0 if kind == "model" else 1)
    store = FileRuntimeStore(tmp_path / "state")
    state = store.load("default", "fixed")
    validate_recovery(state)
    assert state["status"] == "succeeded" and state["usage"]["loops_reserved"] == 0
    assert len([event for event in state["events"] if event["type"] == "run.completed"]) == 1
    if kind == "model":
        assert state["usage"]["input_tokens"] == 3 and state["usage"]["output_tokens"] == 2
    store.close()
