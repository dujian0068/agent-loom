"""Independent third-round review probes; assertions state required behavior.

Run: .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round3.py
No production source is changed by this review.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ConfigurationError,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionToolAdapter,
    McpToolAdapter,
    MemoryRuntimeStore,
    ModelDescriptor,
    ModelResponse,
    ProviderEvent,
    ReactAgent,
    RetryPolicy,
    RuntimeEngine,
    RuntimeProperties,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)


@pytest.fixture(autouse=True)
def isolated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    backend = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield backend
    backend.close()


def build(runtime, model, *, adapter=None, kind="tool", policy=None):
    bindings, registrations = (), ()
    if adapter is not None:
        bindings = (CapabilityBinding("work", kind, "work", "work", "work"),)
        registrations = (
            CapabilityRegistration("work", adapter, (CapabilitySpec("work", "work", kind=kind),)),
        )
    return ReactAgent(
        definition=AgentDefinition("review3", "SYSTEM", capabilities=bindings),
        workspace=WorkspaceBinding("workspace"),
        runtime_engine=runtime,
        model=model,
        capabilities=CapabilityRegistry(tools=registrations),
        execution_policy=policy or ExecutionPolicy(run_timeout_seconds=10),
    )


def tool_model(request):
    return (
        ModelResponse("done")
        if any(message["role"] == "tool" for message in request.messages)
        else ModelResponse(tool_calls=(ToolCall("work", {}),))
    )


def tool_operation(runtime, run_id):
    return next(
        op
        for op in runtime._load(run_id)["operations"].values()
        if op["event_type"] == "capability.invoke"
    )


class DelayedCloseMcp(McpToolAdapter):
    def __init__(self, delay, business_error):
        super().__init__(url="https://example.invalid/mcp")
        self.delay, self.business_error, self.calls = delay, business_error, 0

    @asynccontextmanager
    async def _session(self):
        try:
            yield self
        finally:
            await asyncio.sleep(self.delay)

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            nextCursor=None,
            tools=[SimpleNamespace(name="work", inputSchema={"type": "object"}, outputSchema=None)],
        )

    async def call_tool(self, name, arguments, meta=None):
        self.calls += 1
        return SimpleNamespace(
            isError=self.business_error,
            model_dump=lambda **kwargs: {
                "structuredContent": {"receipt": "ALREADY_COMPLETED"},
                "isError": self.business_error,
            },
        )


@pytest.mark.parametrize("delay", [0, 1.2], ids=["immediate-close", "close-timeout"])
@pytest.mark.parametrize("business_error", [False, True], ids=["success", "business-error"])
async def test_completed_mcp_call_is_not_repeated_when_only_teardown_times_out(
    store, delay, business_error
):
    adapter = DelayedCloseMcp(delay, business_error)
    policy = ExecutionPolicy(
        run_timeout_seconds=10,
        tool_timeout_seconds=1,
        retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await build(
            runtime, ScriptedModel(tool_model), adapter=adapter, kind="mcp", policy=policy
        ).ainvoke("go")
        op = tool_operation(runtime, result.run_id)
        print(
            json.dumps({"calls": adapter.calls, "raw": op["raw"], "attempts": len(op["attempts"])})
        )
        assert result.status == "succeeded"
        assert adapter.calls == 1, (
            "Teardown after a known response must not repeat the business call"
        )
        assert len(op["attempts"]) == 1
        assert op["raw"]["execution_fact"] == "completed"
        assert "ALREADY_COMPLETED" in json.dumps(op["raw"])
        assert op["raw"]["status"] == ("failed" if business_error else "succeeded")


async def test_stop_during_model_close_preserves_already_received_completion(store):
    closing = asyncio.Event()

    class Model:
        descriptor = ModelDescriptor("completed-before-stop", 32768)

        async def stream_once(self, request, context):
            try:
                yield ProviderEvent(
                    "completed",
                    ModelResponse(
                        "ALREADY_COMPLETED", usage={"input_tokens": 3, "output_tokens": 2}
                    ).to_dict(),
                )
            finally:
                closing.set()
                await asyncio.Event().wait()

    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, Model()).start("go")
        await asyncio.wait_for(closing.wait(), 2)
        await handle.stop(command_id="stop-during-close")
        result = await handle.wait(timeout_s=3)
        op = next(iter(runtime._load(result.run_id)["operations"].values()))
        print(json.dumps({"status": result.status, "raw": op["raw"], "response": op["response"]}))
        assert result.status == "cancelled" and result.output is None
        assert op["raw"] is not None, (
            "A received complete response must survive cancellation during close"
        )
        assert op["raw"]["result"]["text"] == "ALREADY_COMPLETED"
        assert op["response"]["execution_fact"] == "completed"


@pytest.mark.parametrize(
    "final_usage, expected",
    [
        (None, (3, 2)),
        ({}, (3, 2)),
        ({"input_tokens": None, "output_tokens": 5}, (3, 5)),
        ({"input_tokens": 9, "output_tokens": 5}, (9, 5)),
    ],
    ids=["null", "empty", "partial", "complete-control"],
)
async def test_known_usage_events_survive_missing_counters_in_completion(
    store, final_usage, expected
):
    class Model:
        descriptor = ModelDescriptor("usage-events", 32768)

        async def stream_once(self, request, context):
            yield ProviderEvent("usage", {"input_tokens": 3, "output_tokens": 2})
            yield ProviderEvent("completed", ModelResponse("done", usage=final_usage).to_dict())

    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, Model()).ainvoke("go")
        actual = (result.usage.input_tokens, result.usage.output_tokens)
        print(json.dumps({"final_usage": final_usage, "actual": actual, "expected": expected}))
        assert result.status == "succeeded"
        assert actual == expected
        assert result.usage.token_counts_complete


@pytest.mark.parametrize(
    "count",
    [-1, True, 2**53],
    ids=["negative-control", "boolean-control", "over-canonical-limit"],
)
async def test_usage_event_counter_is_validated_before_completion(store, count):
    class Model:
        descriptor = ModelDescriptor("invalid-usage-event", 32768)

        async def stream_once(self, request, context):
            yield ProviderEvent("usage", {"input_tokens": count, "output_tokens": 0})
            yield ProviderEvent("completed", ModelResponse("done").to_dict())

    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, Model()).ainvoke("go")
        print(json.dumps({"count": count, "status": result.status, "error": result.error}))
        assert result.status == "failed"
        assert result.error["code"] == "MODEL_PROTOCOL_ERROR"


@pytest.mark.parametrize(
    "adapter_kind", ["value", "native"], ids=["function-adapter", "native-control"]
)
async def test_invalid_returned_value_keeps_completed_execution_fact(store, adapter_kind):
    calls = []

    def function(arguments, context):
        calls.append(1)
        return float("nan")

    class Native:
        async def invoke_once(self, request, context):
            return {
                "status": "succeeded",
                "content": [
                    {"type": "json", "json_ref": {"kind": "inline", "value": function({}, context)}}
                ],
                "error": None,
                "artifacts": [],
                "metadata": {},
            }

    adapter = FunctionToolAdapter(function) if adapter_kind == "value" else Native()
    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, ScriptedModel(tool_model), adapter=adapter).ainvoke("go")
        op = tool_operation(runtime, result.run_id)
        print(json.dumps({"kind": adapter_kind, "raw": op["raw"]}))
        assert result.status == "succeeded" and len(calls) == 1
        assert op["raw"]["status"] == "failed"
        assert op["raw"]["execution_fact"] == "completed"
        assert op["raw"]["error"]["category"] == "protocol"


@pytest.mark.parametrize("entry", ["properties", "file-store"])
@pytest.mark.parametrize(
    "value",
    [None, True, float("nan"), float("inf"), -1, 10**1000, -(10**1000)],
    ids=["null", "bool", "nan", "infinity", "negative", "huge-positive", "huge-negative"],
)
def test_invalid_storage_wait_limit_raises_configuration_error_before_io(tmp_path, entry, value):
    path = tmp_path / "not-created"
    with pytest.raises(ConfigurationError, match="STORE_LOCK_TIMEOUT_INVALID"):
        if entry == "properties":
            RuntimeProperties(state_directory=path, store_lock_timeout_seconds=value)
        else:
            FileRuntimeStore(path, lock_timeout_s=value)
    assert not path.exists()


@pytest.mark.parametrize("entry", ["properties", "file-store"])
def test_zero_storage_wait_limit_is_accepted(tmp_path, entry):
    if entry == "properties":
        assert RuntimeProperties(store_lock_timeout_seconds=0).store_lock_timeout_seconds == 0
    else:
        backend = FileRuntimeStore(tmp_path / "state", lock_timeout_s=0)
        assert backend.lock_timeout_s == 0
        backend.close()
