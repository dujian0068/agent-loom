"""Internal model fragments must survive recovery without entering public views."""

import asyncio
import json
from dataclasses import asdict

import pytest
from conftest import assert_finished

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    ExecutionPolicy,
    ModelDescriptor,
    ModelResponse,
    PlanAgent,
    ProviderEvent,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
)

MARKER = "PRIVATE_PARTIAL_AUDIT"


async def public_views(runtime, handle):
    return json.dumps(
        {
            "result": asdict(await handle.wait()),
            "snapshot": await handle.snapshot(),
            "tasks": await runtime.tasks.list(handle.run_id),
            "events": await runtime.events.read(handle.run_id),
            "stream": [asdict(event) async for event in handle.events()],
            "history": await runtime.sessions.history(handle.session_id),
        }
    )


async def test_executor_audit_never_enters_successful_public_result(runtime_store, build):
    class Model:
        descriptor = ModelDescriptor("test", 32768)

        async def stream_once(self, request, context):
            if request.role_key == "planner":
                response = (
                    ModelResponse("public answer")
                    if any(m["role"] == "tool" for m in request.messages)
                    else ModelResponse(
                        tool_calls=(
                            ToolCall("delegate_task", {"executor": "worker", "task": "work"}),
                        )
                    )
                )
                yield ProviderEvent("completed", response.to_dict())
            else:
                yield ProviderEvent("text_delta", MARKER)
                yield ProviderEvent("reasoning_delta", MARKER)
                yield ProviderEvent("tool_arguments_delta", MARKER, provider_index=0)
                raise AgentLoomError("INTERRUPTED", category="transport")

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(
            runtime,
            None,
            cls=PlanAgent,
            model=Model(),
            executors={"worker": AgentDefinition("worker", "PRIVATE SYSTEM", "work")},
        ).start("go")
        result = await handle.wait()
        assert result.status == "succeeded" and result.output.value == "public answer"
        assert MARKER not in await public_views(runtime, handle)
        state = assert_finished(runtime, handle.run_id, "succeeded")
        assert MARKER in json.dumps(state["partial_results"])
        partial = state["partial_results"][0]
        assert state["operations"][partial["operation_id"]]["attempts"][0]["partial"] == partial
        run_id = handle.run_id
    async with RuntimeEngine(store=runtime_store) as reopened:
        attached = await reopened.runs.attach(run_id)
        assert MARKER not in await public_views(reopened, attached)
        assert MARKER in json.dumps(reopened._load(run_id)["partial_results"])


@pytest.mark.parametrize("mode", ["failure", "retry", "stop"])
async def test_root_partial_is_audit_only_for_every_terminal_outcome(runtime_store, build, mode):
    entered = asyncio.Event()
    calls = []

    class Model:
        descriptor = ModelDescriptor("test", 32768)

        async def stream_once(self, request, context):
            calls.append(context.attempt_id)
            if len(calls) == 2:
                yield ProviderEvent("completed", ModelResponse("public answer").to_dict())
                return
            yield ProviderEvent("text_delta", MARKER)
            yield ProviderEvent("reasoning_delta", MARKER)
            yield ProviderEvent("tool_arguments_delta", MARKER, provider_index=0)
            entered.set()
            if mode == "stop":
                await asyncio.Event().wait()
            raise AgentLoomError("RESET", category="transport", retryable_transport=True)

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(
            runtime,
            None,
            model=Model(),
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(enabled=mode == "retry", max_attempts=2, delay_seconds=0)
            ),
        ).start("go")
        if mode == "stop":
            await asyncio.wait_for(entered.wait(), 2)
            await handle.stop(command_id="stop")
        result = await handle.wait(timeout_s=3)
        assert (
            result.status == {"failure": "failed", "retry": "succeeded", "stop": "cancelled"}[mode]
        )
        assert result.partial_results == () and MARKER not in await public_views(runtime, handle)
        state = assert_finished(runtime, handle.run_id, result.status)
        assert MARKER in json.dumps(state["partial_results"])
        assert len(calls) == (2 if mode == "retry" else 1)
