"""Control signals and damaged records must not masquerade as business results."""

import asyncio
import time

import pytest

from agentloom import (
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    RuntimeEngine,
)
from agentloom.errors import AgentLoomError, InfrastructureError, LeaseLost
from agentloom.storage.commit import CommitCoordinator, claim


async def test_prestart_stop_keeps_its_admission_barrier(runtime_store, build):
    async with RuntimeEngine(store=runtime_store) as runtime:
        receipt = await runtime.runs.stop("future", command_id="stop")
        assert receipt["admission_closed"] and receipt["status_at_commit"] is None
        assert await runtime.runs.stop("future", command_id="stop") == receipt
        with pytest.raises(AgentLoomError, match="RUN_ADMISSION_CLOSED"):
            await build(runtime, lambda r: ModelResponse("unused")).start("go", run_id="future")


@pytest.mark.parametrize("reason", ["stop", "deadline", "ordinary_error"])
async def test_hook_control_propagates_and_ordinary_errors_still_allow(
    runtime_store, build, reason
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def hook(event, context):
        entered.set()
        await release.wait()
        if reason == "ordinary_error":
            raise ValueError("private hook details")
        await context.reader.read_payload()
        return HookDecision()

    hooks = HookSet(
        (HookBinding("gate", "model.generate", "request", "h"),), {"h": FunctionHookHandler(hook)}
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(
            runtime, lambda r: (calls.append(r), ModelResponse("ok"))[1], hooks=hooks
        ).start("go")
        await asyncio.wait_for(entered.wait(), 3)
        if reason == "stop":
            await runtime.runs.stop(handle.run_id, command_id="stop")
        elif reason == "deadline":
            with runtime_store.transaction("default") as tx:
                state = tx.get("runs", handle.run_id)
                state["deadline_at"] = time.time() - 1
                tx.put("runs", handle.run_id, state)
        release.set()
        result = await handle.wait(timeout_s=3)
        hook = next(
            h
            for op in runtime._load(handle.run_id)["operations"].values()
            for h in op["hooks"].values()
        )
        if reason == "ordinary_error":
            assert result.status == "succeeded" and len(calls) == 1
            assert hook["result"]["decision"] == "allow"
        else:
            assert result.status == ("cancelled" if reason == "stop" else "timed_out")
            assert not calls and hook["result"] is None
            assert hook["control_reason"]["code"] in {"RUN_STOPPED", "RUN_TIMEOUT"}


def test_missing_run_loses_lease_without_recreating_it(runtime_store):
    with runtime_store.transaction("n") as tx:
        tx.put("runs", "r", {"run_id": "r", "status": "running"})
    lease = claim(runtime_store, "n", "r", "worker", 5)
    coordinator = CommitCoordinator(runtime_store, "n", lease)
    with runtime_store.transaction("n") as tx:
        tx.delete("runs", "r")
    with pytest.raises(LeaseLost):
        coordinator.renew(5)
    assert runtime_store.load("n", "r") is None
    coordinator.release()


async def test_missing_run_during_model_call_preserves_worker_lease_failure(runtime_store, build):
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        entered.set()
        await release.wait()
        return ModelResponse("late")

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, model).start("go")
        await asyncio.wait_for(entered.wait(), 3)
        worker = runtime._workers[handle.run_id]
        with runtime_store.transaction("default") as tx:
            tx.delete("runs", handle.run_id)
        release.set()
        await asyncio.wait_for(worker, 3)
        with pytest.raises(LeaseLost):
            runtime.raise_local_failure(handle.run_id)
        assert runtime_store.load("default", handle.run_id) is None


def test_nested_transaction_rejection_preserves_outer_write(runtime_store):
    with runtime_store.transaction("n") as outer:
        outer.put("records", "r", {"value": "outer"})
        with pytest.raises(InfrastructureError, match="STORE_TRANSACTION_NESTED"):
            with runtime_store.transaction("n") as inner:
                inner.put("records", "r", {"value": "inner"})
        with runtime_store.transaction("other") as other:
            other.put("records", "r", {"value": "separate"})
    with runtime_store.transaction("n") as tx:
        assert tx.get("records", "r") == {"value": "outer"}


async def test_broken_input_delivery_index_has_invariant_error(runtime_store, build):
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        entered.set()
        await release.wait()
        return ModelResponse("ok")

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, model).start("go")
        await asyncio.wait_for(entered.wait(), 3)
        await runtime.inputs.append(handle.run_id, input_id="supp", content="extra")
        with runtime_store.transaction("default") as tx:
            state = tx.get("runs", handle.run_id)
            state["inputs"] = [i for i in state["inputs"] if i["input_id"] != "supp"]
            tx.put("runs", handle.run_id, state)
        with pytest.raises(InfrastructureError, match="PERSISTENCE_INVARIANT_BROKEN"):
            await runtime.inputs.append(handle.run_id, input_id="supp", content="extra")
        assert all(i["input_id"] != "supp" for i in runtime._load(handle.run_id)["inputs"])
        release.set()
        assert (await handle.wait(timeout_s=3)).status == "succeeded"
