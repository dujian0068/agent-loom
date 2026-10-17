"""Cleanup lease ownership and deadline finalization without reconstructing Agents."""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import assert_finished

from agentloom import (
    ExecutionPolicy,
    FileRuntimeStore,
    LocalEnvironment,
    ModelResponse,
    RuntimeEngine,
)
from agentloom.errors import LeaseLost
from agentloom.runtime.cleanup import CleanupService
from agentloom.runtime.cleanup_lease import CleanupLeaseGuard
from agentloom.runtime.deadlines import request_expired_stops


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


class GatedCleanup(LocalEnvironment):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0

    def cleanup(self, scope):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("Test did not release cleanup")
        return super().cleanup(scope)


async def orphan(store, build, run_id="orphan", *, timeout=10, start=True):
    entered = asyncio.Event()

    async def block(request):
        entered.set()
        await asyncio.Event().wait()

    async with RuntimeEngine(store=store) as runtime:
        await build(
            runtime, block, execution_policy=ExecutionPolicy(run_timeout_seconds=timeout)
        ).start("question", run_id=run_id, session_id=run_id + "-session")
        if start:
            await entered.wait()


async def test_cleanup_renewal_excludes_other_host_and_preserves_terminal(runtime_store, build):
    environment = GatedCleanup()
    try:
        async with RuntimeEngine(
            store=runtime_store, environment=environment, lease_seconds=0.18
        ) as one:
            agent = build(one, lambda r: ModelResponse("done"))
            result = await agent.ainvoke("question")
            await until(environment.entered.is_set)
            terminal_seq = one._load(result.run_id)["terminal_seq"]
            async with RuntimeEngine(
                store=runtime_store, environment=environment, lease_seconds=0.18
            ):
                await asyncio.sleep(0.55)
                pending = one._load(result.run_id)
                assert environment.calls == 1
                assert pending["cleanup_job"]["attempts"] == 1
                assert pending["cleanup_job"]["lease"]["expires_at"] > time.time()
                environment.release.set()
                await until(lambda: one._load(result.run_id)["cleanup_state"] == "succeeded")
            settled = assert_finished(one, result.run_id, "succeeded")
            assert settled["terminal_seq"] == terminal_seq
            assert len(agent.model.requests) == 1
    finally:
        environment.release.set()


@pytest.mark.parametrize("loss", ["replacement", "expiry"])
async def test_old_cleanup_epoch_cannot_renew_or_commit(runtime_store, build, monkeypatch, loss):
    monkeypatch.setattr(CleanupService, "scan_once", lambda self: None)
    environment = GatedCleanup()
    try:
        async with RuntimeEngine(
            store=runtime_store, environment=environment, lease_seconds=0.18
        ) as runtime:
            result = await build(runtime, lambda r: ModelResponse("done")).ainvoke("question")
            work = asyncio.create_task(
                asyncio.to_thread(CleanupService(runtime)._clean_run, result.run_id)
            )
            await until(environment.entered.is_set)
            old = runtime._load(result.run_id)["cleanup_job"]
            stale = CleanupLeaseGuard(runtime, result.run_id, old["job_id"], old["lease"]["epoch"])
            with runtime_store.transaction(runtime.namespace) as tx:
                state = tx.get("runs", result.run_id)
                if loss == "replacement":
                    state["cleanup_job"]["lease"].update(
                        worker_id="replacement",
                        epoch=old["lease"]["epoch"] + 1,
                        expires_at=tx.now + 5,
                    )
                else:
                    state["cleanup_job"]["lease"]["expires_at"] = 0
                tx.put("runs", result.run_id, state)
            with pytest.raises(LeaseLost):
                stale.renew()
            environment.release.set()
            outcome = (await asyncio.gather(work, return_exceptions=True))[0]
            assert outcome is None or isinstance(outcome, LeaseLost)
            final = assert_finished(runtime, result.run_id, "succeeded")
            assert final["cleanup_state"] == "running"
            if loss == "replacement":
                assert final["cleanup_job"]["lease"]["worker_id"] == "replacement"
            else:
                assert final["cleanup_job"]["lease"]["expires_at"] == 0
    finally:
        environment.release.set()


async def test_slow_cleanup_does_not_block_deadline_control(runtime_store, build):
    await orphan(runtime_store, build, timeout=1)
    environment = GatedCleanup()
    try:
        async with RuntimeEngine(
            store=runtime_store, environment=environment, lease_seconds=0.18
        ) as runtime:
            await build(runtime, lambda r: ModelResponse("done")).ainvoke("other question")
            await until(environment.entered.is_set)
            result = await (await runtime.runs.attach("orphan")).wait(timeout_s=3)
            assert result.status == "timed_out"
            assert not environment.release.is_set()
            assert_finished(runtime, "orphan", "timed_out")
            environment.release.set()
    finally:
        environment.release.set()


@pytest.mark.parametrize("start", [False, True])
async def test_deadline_control_waits_for_live_lease_and_retries_revoke(
    runtime_store, build, start
):
    await orphan(runtime_store, build, start=start)
    with runtime_store.transaction("default") as tx:
        state = tx.get("runs", "orphan")
        state["deadline_at"] = tx.now - 1
        state["lease"] = {"worker_id": "remote", "epoch": 50, "expires_at": tx.now + 0.25}
        expiry = state["lease"]["expires_at"]
        tx.put("runs", "orphan", state)

    class FlakyRevoke(LocalEnvironment):
        calls = 0

        def revoke(self, scope):
            if scope["run_id"] == "orphan":
                self.calls += 1
                assert time.time() >= expiry
                if self.calls == 1:
                    raise OSError("injected revocation failure")
            return super().revoke(scope)

    environment = FlakyRevoke()
    async with RuntimeEngine(
        store=runtime_store, environment=environment, cleanup_retry_seconds=0.1
    ) as runtime:
        await until(lambda: runtime._load("orphan")["status"] == "stopping")
        assert runtime._load("orphan")["terminal_seq"] is None
        result = await (await runtime.runs.attach("orphan")).wait(timeout_s=3)
        assert result.error["code"] == "RUN_TIMEOUT"
        state = assert_finished(runtime, "orphan", "timed_out")
        if start:
            assert environment.calls == 2
            assert state["maintenance"]["stop"]["recovered_at"] is not None
        else:
            assert environment.calls == 0, "A queued Run has no writer to revoke"
        assert not runtime._workers
        assert (
            await build(runtime, lambda r: ModelResponse("next")).ainvoke(
                "next question", session_id="orphan-session"
            )
        ).status == "succeeded"


@pytest.mark.parametrize("first", ["stop", "deadline"])
async def test_first_control_reason_survives_later_competing_control(runtime_store, build, first):
    await orphan(runtime_store, build)
    async with RuntimeEngine(store=runtime_store) as runtime:
        # Keep a live remote lease until both control commands have been committed.
        with runtime_store.transaction(runtime.namespace) as tx:
            state = tx.get("runs", "orphan")
            state["lease"] = {"worker_id": "remote", "epoch": 50, "expires_at": tx.now + 5}
            tx.put("runs", "orphan", state)
        if first == "stop":
            await runtime.runs.stop("orphan", command_id="user-stop")
        with runtime_store.transaction(runtime.namespace) as tx:
            state = tx.get("runs", "orphan")
            state["deadline_at"] = tx.now - 1
            tx.put("runs", "orphan", state)
        request_expired_stops(runtime)
        if first == "deadline":
            await runtime.runs.stop("orphan", command_id="user-stop")
        with runtime_store.transaction(runtime.namespace) as tx:
            state = tx.get("runs", "orphan")
            state["lease"]["expires_at"] = 0
            tx.put("runs", "orphan", state)
        result = await (await runtime.runs.attach("orphan")).wait(timeout_s=3)
        expected = "cancelled" if first == "stop" else "timed_out"
        assert_finished(runtime, "orphan", expected)
        assert result.error["code"] == ("RUN_STOPPED" if first == "stop" else "RUN_TIMEOUT")


async def test_file_deadline_finalizes_after_actual_process_exit(tmp_path):
    sdk = Path(__file__).resolve().parents[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).parent / "support" / "orphan_deadline_process.py"),
        str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(sdk / "src")},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 0, (out, err)
    store = FileRuntimeStore(tmp_path / "state")
    try:
        async with RuntimeEngine(store=store) as runtime:
            result = await (await runtime.runs.attach("process-orphan")).wait(timeout_s=4)
            assert result.error["code"] == "RUN_TIMEOUT"
            assert_finished(runtime, "process-orphan", "timed_out")
            assert not runtime._workers
    finally:
        store.close()
