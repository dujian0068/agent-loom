"""Returned data and reported failures share validation, not execution exceptions."""

import asyncio
import sys

import pytest
from conftest import assert_finished
from test_attempt_completion import operation, tool_model, tool_options

from agentloom import (
    AgentLoomError,
    ExecutionPolicy,
    FunctionToolAdapter,
    InfrastructureError,
    RetryPolicy,
    RuntimeEngine,
)
from agentloom.capabilities.invocation import normalize_completion, normalize_error_completion
from agentloom.errors import LeaseLost


@pytest.mark.parametrize("channel", ["error", "envelope"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("code", 3),
        ("message", ""),
        ("phase", "outside-enum"),
        ("external_outcome", "outside-enum"),
        ("retryable_transport", 1),
        ("retry_after_at", True),
        ("retry_after_at", "tomorrow"),
        ("retry_after_at", float("inf")),
        ("field_path", ["secret"]),
    ],
)
def test_error_fields_are_validated_identically_before_commit(channel, field, value):
    error = AgentLoomError("REJECTED")
    setattr(error, field, value)
    with pytest.raises(AgentLoomError) as caught:
        if channel == "error":
            normalize_error_completion(error)
        else:
            normalize_completion(
                {
                    "status": "failed",
                    "error": error.to_dict(),
                    "content": [],
                    "metadata": {},
                    "artifacts": [],
                }
            )
    assert caught.value.code == "CAPABILITY_RESULT_INVALID"
    assert caught.value.category == "protocol" and caught.value.execution_fact == "completed"


@pytest.mark.parametrize(
    "error", [LeaseLost(), InfrastructureError("STORE_FAILED"), asyncio.CancelledError()]
)
def test_reported_control_and_storage_exceptions_are_not_business_results(error):
    with pytest.raises(type(error)) as caught:
        normalize_error_completion(error)
    assert caught.value is error
    assert not hasattr(error, "execution_fact")


@pytest.mark.parametrize("receipt_saved", [False, True])
async def test_error_callback_cannot_write_after_lease_takeover(
    runtime_store, build, receipt_saved
):
    ready, release = asyncio.Event(), asyncio.Event()

    class Native:
        async def invoke_once(self, request, context):
            if not receipt_saved:
                ready.set()
                await release.wait()
            try:
                context.report_completion(error=AgentLoomError("KNOWN_RESPONSE"))
            finally:
                if receipt_saved:
                    ready.set()
                    await release.wait()

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, tool_model, **tool_options(Native(), kind="tool")).start("go")
        await asyncio.wait_for(ready.wait(), 2)
        with runtime_store.transaction("default") as tx:
            state = tx.get("runs", handle.run_id)
            state["lease"].update(worker_id="new-owner", epoch=state["lease"]["epoch"] + 1)
            tx.put("runs", handle.run_id, state)
        before = runtime._load(handle.run_id)
        release.set()
        with pytest.raises(LeaseLost):
            await handle.wait(timeout_s=2)
        after = runtime._load(handle.run_id)
        assert after["operations"] == before["operations"]
        assert after["usage"] == before["usage"] and after["terminal_seq"] is None
        assert (operation(after, "capability.invoke")["raw"] is not None) is receipt_saved


async def test_expired_error_callback_cannot_reopen_a_finished_attempt(runtime_store, build):
    callbacks = []

    class Native:
        async def invoke_once(self, request, context):
            callbacks.append(context.report_completion)
            context.report_completion(error=AgentLoomError("KNOWN_RESPONSE"))

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, tool_model, **tool_options(Native(), kind="tool")).ainvoke(
            "go"
        )
        before = assert_finished(runtime, result.run_id, "succeeded")
        for error in (
            AgentLoomError("LATE"),
            AgentLoomError("LATE", retry_after_at=float("nan")),
        ):
            with pytest.raises(AgentLoomError, match="ATTEMPT_COMPLETION_CLOSED"):
                callbacks[0](error=error)
        after = runtime._load(result.run_id)
        assert after["operations"] == before["operations"] and after["usage"] == before["usage"]


@pytest.mark.parametrize("outcome", ["unknown", "not_started"])
async def test_error_callback_uses_explicit_outcome_not_private_fact(runtime_store, build, outcome):
    calls = []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(1)
            error = AgentLoomError(
                "RETRY_ALLOWED",
                category="transport",
                external_outcome=outcome,
                retryable_transport=True,
            )
            error.execution_fact = "completed"
            context.report_completion(error=error)

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            tool_model,
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
            ),
            **tool_options(Native(), kind="tool"),
        ).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "succeeded")
        op = operation(state, "capability.invoke")
        assert len(calls) == len(op["attempts"]) == 2
        assert op["raw"]["execution_fact"] == outcome


@pytest.mark.parametrize("shape", ["cycle", "nan"])
async def test_error_material_is_validated_before_persistence(runtime_store, build, shape):
    calls = []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(1)
            material = []
            material.append(material if shape == "cycle" else float("nan"))
            error = AgentLoomError("FAILED")
            error.result_material = material
            context.report_completion(error=error)

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(runtime, tool_model, **tool_options(Native(), kind="tool"))
        result = await agent.ainvoke("go", session_id="material")
        state = assert_finished(runtime, result.run_id, "succeeded")
        op = operation(state, "capability.invoke")
        assert len(calls) == 1
        assert op["raw"]["error"]["code"] == "CAPABILITY_RESULT_INVALID"
        assert op["raw"]["execution_fact"] == "completed" and op["raw"]["result"] is None
        assert "invalid_tool_result" not in op["attempts"][0]
        # Protocol rejection must release the session for its next turn.
        next_agent = build(runtime, tool_model, **tool_options(Native(), kind="tool"))
        next_result = await next_agent.ainvoke("next", session_id="material")
        assert_finished(runtime, next_result.run_id, "succeeded")


@pytest.mark.parametrize("adapter_kind", ["function", "legacy", "native"])
@pytest.mark.parametrize("shape", ["deep", "shared", "raised"])
async def test_json_return_boundary_preserves_execution_semantics(
    runtime_store, build, adapter_kind, shape
):
    calls = []

    def value():
        calls.append(1)
        if shape == "raised":
            raise RecursionError("adapter failed during execution")
        if shape == "shared":
            child = {"receipt": [1]}
            return [child, child]
        result = None
        for _ in range(sys.getrecursionlimit() + 10):
            result = [result]
        return result

    class Legacy:
        async def invoke(self, member, arguments, context):
            return value()

    class Native:
        async def invoke_once(self, request, context):
            return {
                "status": "succeeded",
                "content": [{"type": "json", "json_ref": {"kind": "inline", "value": value()}}],
                "error": None,
                "artifacts": [],
                "metadata": {},
            }

    adapter = {
        "function": FunctionToolAdapter(lambda arguments, context: value()),
        "legacy": Legacy(),
        "native": Native(),
    }[adapter_kind]
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, tool_model, **tool_options(adapter, kind="tool")).ainvoke(
            "go"
        )
        state = assert_finished(runtime, result.run_id, "succeeded")
        raw = operation(state, "capability.invoke")["raw"]
        assert len(calls) == 1
        if shape == "shared":
            assert raw["status"] == "succeeded" and raw["result"] == [{"receipt": [1]}] * 2
            assert raw["execution_fact"] == "completed"
        elif shape == "deep":
            assert raw["error"]["code"] == "CAPABILITY_RESULT_INVALID"
            assert raw["error"]["category"] == "protocol"
            assert raw["execution_fact"] == "completed"
        else:
            assert raw["error"]["code"] == "ADAPTER_ERROR"
            assert raw["error"]["message"] == "RecursionError"
            assert raw["execution_fact"] == "unknown"
