"""Completion precedes teardown; control and lease fencing still govern delivery."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from conftest import assert_finished

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    FunctionToolAdapter,
    InfrastructureError,
    McpToolAdapter,
    ModelDescriptor,
    ModelResponse,
    ProviderEvent,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
    ToolResult,
)
from agentloom.errors import LeaseLost
from agentloom.runtime.recovery import validate_recovery
from agentloom.storage.commit import CommitCoordinator


class ClosingModel:
    descriptor = ModelDescriptor("closing-model", 32768)

    def __init__(self):
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def stream_once(self, request, context):
        self.calls += 1
        try:
            yield ProviderEvent("usage", {"input_tokens": 3, "output_tokens": 2})
            yield ProviderEvent("completed", ModelResponse("saved").to_dict())
        finally:
            self.closing.set()
            await self.release.wait()


class ClosingMcp(McpToolAdapter):
    def __init__(self, *, business_error=False, output_schema=None):
        super().__init__(url="https://example.invalid/mcp")
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.business_error, self.output_schema = business_error, output_schema
        self.calls = 0

    @asynccontextmanager
    async def _session(self):
        try:
            yield self
        finally:
            self.closing.set()
            await self.release.wait()

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            nextCursor=None,
            tools=[
                SimpleNamespace(
                    name="work", inputSchema={"type": "object"}, outputSchema=self.output_schema
                )
            ],
        )

    async def call_tool(self, name, arguments, meta=None):
        self.calls += 1
        return SimpleNamespace(
            isError=self.business_error,
            model_dump=lambda **kwargs: {
                "structuredContent": {"receipt": "saved"},
                "isError": self.business_error,
            },
        )


class ClosingNativeError:
    def __init__(self):
        self.closing, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def invoke_once(self, request, context):
        self.calls += 1
        try:
            context.report_completion(
                error=AgentLoomError(
                    "KNOWN_RESPONSE", category="transport", retryable_transport=True
                )
            )
        finally:
            self.closing.set()
            await self.release.wait()


def closing_adapter(kind):
    return {"model": ClosingModel, "mcp": ClosingMcp, "native-error": ClosingNativeError}[kind]()


def closing_options(kind, adapter):
    return (
        {"model": adapter}
        if kind == "model"
        else tool_options(adapter, kind="tool" if kind == "native-error" else "mcp")
    )


def tool_options(adapter, *, kind="mcp"):
    return {
        "definition": AgentDefinition(
            "with-tool", "SYSTEM", capabilities=(CapabilityBinding("work", kind, "work", "a"),)
        ),
        "capabilities": CapabilityRegistry(
            tools=(
                CapabilityRegistration(
                    "a",
                    adapter,
                    (
                        CapabilitySpec(
                            "work",
                            "work",
                            kind=kind,
                            output_schema=getattr(adapter, "output_schema", None),
                        ),
                    ),
                ),
            )
        ),
    }


def tool_model(request):
    return (
        ModelResponse("done")
        if any(item["role"] == "tool" for item in request.messages)
        else ModelResponse(tool_calls=(ToolCall("work", {}),))
    )


def operation(state, kind):
    return next(op for op in state["operations"].values() if op["event_type"] == kind)


@pytest.mark.parametrize("kind", ["model", "mcp", "native-error"])
@pytest.mark.parametrize("control", ["stop", "deadline", "shutdown"])
async def test_completion_is_durable_before_close_and_control_still_wins(
    runtime_store, build, kind, control
):
    adapter = closing_adapter(kind)
    options = closing_options(kind, adapter)
    policy = ExecutionPolicy(run_timeout_seconds=1 if control == "deadline" else 10)
    event_type = "model.generate" if kind == "model" else "capability.invoke"
    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, tool_model, execution_policy=policy, **options).start("go")
        await asyncio.wait_for(adapter.closing.wait(), 2)
        state = runtime._load(handle.run_id)
        saved = operation(state, event_type)
        assert saved["raw"]["execution_fact"] == "completed"
        assert saved["phase"] == "after" and saved["status"] == "pending"
        validate_recovery(state)
        if control == "stop":
            await handle.stop(command_id="during-close")
        if control == "shutdown":
            await runtime.aclose()
            state = runtime_store.load("default", handle.run_id)
            assert state["status"] == "running" and state["terminal_seq"] is None
        else:
            result = await handle.wait(timeout_s=3)
            state = assert_finished(
                runtime, handle.run_id, "cancelled" if control == "stop" else "timed_out"
            )
            assert result.output is None
            assert operation(state, event_type)["response"]["execution_fact"] == "completed"
        attempt = operation(state, event_type)["attempts"][0]
        assert attempt["diagnostics"][-1]["error"]["code"] == "RESOURCE_CLOSE_CANCELLED"
        assert adapter.calls == 1
        if kind == "model":
            assert state["usage"]["input_tokens"] == 3 and state["usage"]["output_tokens"] == 2
        validate_recovery(state)
    if control == "shutdown":
        adapter.release.set()
        async with RuntimeEngine(store=runtime_store) as runtime:
            result = await build(runtime, tool_model, execution_policy=policy, **options).ainvoke(
                run_id=handle.run_id
            )
            assert_finished(runtime, handle.run_id, "succeeded")
            assert result.output.value == ("saved" if kind == "model" else "done")
            assert adapter.calls == 1


@pytest.mark.parametrize("receipt_saved", [False, True])
async def test_lost_owner_cannot_publish_completion_or_teardown_diagnostics(
    runtime_store, build, receipt_saved
):
    ready, release = asyncio.Event(), asyncio.Event()

    class Model:
        descriptor = ModelDescriptor("fenced-completion", 32768)

        async def stream_once(self, request, context):
            if not receipt_saved:
                ready.set()
                await release.wait()
            try:
                yield ProviderEvent("completed", ModelResponse("saved").to_dict())
            finally:
                if receipt_saved:
                    ready.set()
                    await release.wait()

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, None, model=Model()).start("go")
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
        assert (operation(after, "model.generate")["raw"] is not None) is receipt_saved


@pytest.mark.parametrize("kind", ["model", "mcp", "native-error"])
@pytest.mark.parametrize("committed", [False, True], ids=["before-commit", "after-commit"])
async def test_completion_commit_fault_stops_delivery_and_recovers_from_durable_facts(
    runtime_store, build, monkeypatch, kind, committed
):
    adapter = closing_adapter(kind)
    adapter.release.set()
    options = closing_options(kind, adapter)
    event_type = "model.generate" if kind == "model" else "capability.invoke"
    original = CommitCoordinator.commit

    def interrupt(coordinator, command, change, **kwargs):
        # Node progress now uses RuntimeCommit. Keep this fault at the same
        # external-attempt completion boundary, including its lost-ACK case.
        command_id = command if isinstance(command, str) else command.command_id
        if command_id.startswith("attempt-result:"):
            state = coordinator.store.load(coordinator.namespace, coordinator.lease.run_id)
            op = state["operations"][command_id.split(":")[1]]
            if op["event_type"] == event_type:
                if committed:
                    original(coordinator, command, change, **kwargs)
                raise OSError("persistence interrupted")
        return original(coordinator, command, change, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(CommitCoordinator, "commit", interrupt)
        async with RuntimeEngine(store=runtime_store) as runtime:
            handle = await build(runtime, tool_model, **options).start("go")
            with pytest.raises(InfrastructureError, match="ATTEMPT_COMPLETION_COMMIT_FAILED"):
                await handle.wait(timeout_s=2)
            state = runtime._load(handle.run_id)
            assert state["status"] == "running" and state["terminal_seq"] is None
            assert (operation(state, event_type)["raw"] is not None) is committed
            assert adapter.calls == 1
            validate_recovery(state)

    async with RuntimeEngine(store=runtime_store) as recovered:
        result = await build(recovered, tool_model, **options).ainvoke(run_id=handle.run_id)
        state = assert_finished(
            recovered, handle.run_id, "failed" if kind == "model" and not committed else "succeeded"
        )
        op = operation(state, event_type)
        assert adapter.calls == len(op["attempts"]) == 1
        if committed:
            assert op["raw"]["execution_fact"] == "completed"
            assert result.output.value == ("saved" if kind == "model" else "done")
        else:
            assert op["raw"]["execution_fact"] == "unknown"
            assert op["raw"]["error"]["code"] == "CALL_OUTCOME_UNKNOWN"
        if kind == "model":
            assert state["usage"]["input_tokens"] == (3 if committed else None)
            assert state["usage"]["output_tokens"] == (2 if committed else None)
        validate_recovery(state)


@pytest.mark.parametrize("business_error,schema", [(True, None), (False, {"type": "string"})])
async def test_failed_completed_mcp_result_survives_timeout_with_original_error(
    runtime_store, build, business_error, schema
):
    adapter = ClosingMcp(business_error=business_error, output_schema=schema)
    policy = ExecutionPolicy(
        tool_timeout_seconds=1, retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, tool_model, execution_policy=policy, **tool_options(adapter)
        ).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "succeeded")
        op = operation(state, "capability.invoke")
        assert adapter.calls == len(op["attempts"]) == 1
        assert op["raw"]["execution_fact"] == "completed"
        assert op["raw"]["error"]["code"] == (
            "MCP_TOOL_ERROR" if business_error else "MCP_OUTPUT_SCHEMA_INVALID"
        )
        assert op["attempts"][0]["diagnostics"][-1]["error"]["code"] == "RESOURCE_CLOSE_TIMEOUT"


@pytest.mark.parametrize("kind", ["sync", "async", "legacy"])
async def test_value_bridges_only_mark_returned_values_as_completed(runtime_store, build, kind):
    calls = []

    def invalid(arguments, context):
        calls.append(1)
        return object()

    async def invalid_async(arguments, context):
        return invalid(arguments, context)

    class Legacy:
        async def invoke(self, member, arguments, context):
            return invalid(arguments, context)

    adapter = (
        Legacy()
        if kind == "legacy"
        else FunctionToolAdapter(invalid_async if kind == "async" else invalid)
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, tool_model, **tool_options(adapter, kind="tool")).ainvoke(
            "go"
        )
        op = operation(assert_finished(runtime, result.run_id, "succeeded"), "capability.invoke")
        assert calls == [1] and len(op["attempts"]) == 1
        assert op["raw"]["execution_fact"] == "completed"
        assert op["raw"]["error"]["code"] == "CAPABILITY_RESULT_INVALID"
        assert op["raw"]["error"]["category"] == "protocol"


async def test_function_throwing_before_return_is_not_marked_completed(runtime_store, build):
    def failed(arguments, context):
        raise AgentLoomError("BUSINESS_FAILED")

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, tool_model, **tool_options(FunctionToolAdapter(failed), kind="tool")
        ).ainvoke("go")
        op = operation(assert_finished(runtime, result.run_id, "succeeded"), "capability.invoke")
        assert op["raw"]["execution_fact"] == "unknown"
        assert op["raw"]["error"]["code"] == "BUSINESS_FAILED"


async def test_native_completion_callback_expires_and_does_not_repeat_attempt(runtime_store, build):
    callbacks, calls = [], []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(request)
            callbacks.append(context.report_completion)
            result = ToolResult.from_value({"receipt": "saved"})
            context.report_completion(result)
            context.report_completion(result)
            await asyncio.Event().wait()

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            tool_model,
            execution_policy=ExecutionPolicy(
                tool_timeout_seconds=1,
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
            ),
            **tool_options(Native(), kind="tool"),
        ).ainvoke("go")
        before = assert_finished(runtime, result.run_id, "succeeded")
        op = operation(before, "capability.invoke")
        assert len(calls) == len(op["attempts"]) == 1
        assert op["raw"]["result"] == {"receipt": "saved"}
        with pytest.raises(AgentLoomError, match="ATTEMPT_COMPLETION_CLOSED"):
            callbacks[0](ToolResult.from_value("late"))
        after = runtime._load(result.run_id)
        assert after["operations"] == before["operations"]
        assert after["usage"] == before["usage"]


@pytest.mark.parametrize("business_error", [False, True])
async def test_noncanonical_mcp_response_remains_a_completed_protocol_failure(
    runtime_store, build, business_error
):
    class InvalidMcp(ClosingMcp):
        async def call_tool(self, name, arguments, meta=None):
            result = await super().call_tool(name, arguments, meta)
            result.model_dump = lambda **kwargs: {"structuredContent": float("nan")}
            return result

    adapter = InvalidMcp(business_error=business_error)
    policy = ExecutionPolicy(
        tool_timeout_seconds=1, retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, tool_model, execution_policy=policy, **tool_options(adapter)
        ).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "succeeded")
        op = operation(state, "capability.invoke")
        assert adapter.calls == len(op["attempts"]) == 1
        assert op["raw"]["execution_fact"] == "completed"
        assert op["raw"]["error"]["code"] == "MCP_PROTOCOL_ERROR"
        assert op["raw"]["error"]["category"] == "protocol"
        assert op["raw"]["result"] is None
        validate_recovery(state)
