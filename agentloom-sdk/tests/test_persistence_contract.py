import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentloom import ModelResponse, RuntimeEngine


@pytest.mark.parametrize(
    "cut,first_assembly,second_assembly",
    [
        (cut, "direct", "direct")
        for cut in [
            "raw",
            "tool_permit",
            "tool_raw",
            "hook_result",
            "hook_apply",
            "child",
            "batch",
            "terminal",
        ]
    ]
    + [
        ("tool_raw", "builder", "direct"),
        ("hook_apply", "direct", "builder"),
        ("terminal", "builder", "builder"),
        ("tool_permit", "properties", "direct"),
        ("tool_raw", "direct", "properties"),
        ("hook_apply", "builder", "properties"),
        ("child", "properties", "properties"),
    ],
)
def test_process_death_resumes_committed_work_exactly_once(
    tmp_path, cut, first_assembly, second_assembly, durable_store_factory
):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    worker = Path(__file__).parent / "support" / "crash_process.py"
    first = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), cut, first_assembly],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 73, first.stderr
    time.sleep(0.45)
    second = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "none", second_assembly],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == {"status": "succeeded", "loops": 6, "cleanup": "succeeded"}
    audit = (tmp_path / "audit").read_text().splitlines()
    assert audit.count("tool:one") == audit.count("tool:two") == 1
    assert audit.count("model:planner") == 2 and audit.count("model:worker") == 4
    hooks = [line for line in audit if line.startswith("hook:")]
    assert len(hooks) == len(set(hooks)) == 6
    store = durable_store_factory(tmp_path)
    state = store.load("default", "run")
    assert state["usage"]["loops_reserved"] == 0
    assert len([e for e in state["events"] if e["type"] == "run.completed"]) == 1
    assert not Path(state["workspace"]["path"]).exists()
    store.close()


async def test_two_hosts_only_one_owner_and_waits_do_not_own_work(
    tmp_path, build, durable_store_factory
):
    a, b = durable_store_factory(tmp_path), durable_store_factory(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def model(request):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ModelResponse("one")

    try:
        async with RuntimeEngine(store=a) as ra, RuntimeEngine(store=b) as rb:
            ha, hb = await asyncio.gather(
                build(ra, model).start("question", run_id="shared", session_id="session"),
                build(rb, model).start("question", run_id="shared", session_id="session"),
            )
            await started.wait()
            await asyncio.sleep(0.02)
            assert calls == 1
            release.set()
            left, right = await asyncio.gather(ha.wait(timeout_s=2), hb.wait(timeout_s=2))
            assert left.output == right.output
    finally:
        a.close()
        b.close()


async def test_shutdown_keeps_active_run_and_unknown_attempt_is_not_replayed(
    tmp_path, build, durable_store_factory
):
    store = durable_store_factory(tmp_path)
    started = asyncio.Event()

    async def slow(request):
        started.set()
        await asyncio.sleep(60)
        return ModelResponse("late")

    try:
        async with RuntimeEngine(store=store) as first:
            await build(first, slow).start("question", run_id="shutdown")
            await started.wait()
        state = store.load("default", "shutdown")
        assert state["status"] == "running" and Path(state["workspace"]["path"]).exists()
        async with RuntimeEngine(store=store) as second:
            agent = build(
                second, lambda r: pytest.fail("Ambiguous attempt must not be blindly replayed")
            )
            result = await (await agent.start(run_id="shutdown")).wait(timeout_s=2)
            assert result.status == "failed" and result.error["code"] == "CALL_OUTCOME_UNKNOWN"
    finally:
        store.close()
