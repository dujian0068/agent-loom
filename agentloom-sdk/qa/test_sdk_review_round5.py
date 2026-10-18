"""Independent fifth-round probes for durable JSON and exception boundaries.

Run explicitly: .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round5.py
The assertions describe safe outcomes; known defects are not skipped or xfailed.
"""

import json
import math
import sys

import pytest

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionToolAdapter,
    InfrastructureError,
    MemoryRuntimeStore,
    ModelResponse,
    ReactAgent,
    RequestError,
    RuntimeEngine,
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


def model(request):
    return (
        ModelResponse("done")
        if any(message["role"] == "tool" for message in request.messages)
        else ModelResponse(tool_calls=(ToolCall("work", {}),))
    )


def build(runtime, adapter):
    return ReactAgent(
        definition=AgentDefinition(
            "round5",
            "SYSTEM",
            capabilities=(CapabilityBinding("work", "tool", "work", "work", "work"),),
        ),
        workspace=WorkspaceBinding("workspace"),
        runtime_engine=runtime,
        model=ScriptedModel(model),
        capabilities=CapabilityRegistry(
            tools=(CapabilityRegistration("work", adapter, (CapabilitySpec("work", "work"),)),)
        ),
        execution_policy=ExecutionPolicy(run_timeout_seconds=10),
    )


def operation(runtime, run_id):
    return next(
        op
        for op in runtime._load(run_id)["operations"].values()
        if op["event_type"] == "capability.invoke"
    )


def nested_value(depth):
    value = None
    for _ in range(depth):
        value = [value]
    return value


def nested_adapter(kind, depth, calls):
    def value():
        calls.append(1)
        return nested_value(depth)

    class Legacy:
        async def invoke(self, member, arguments, context):
            return value()

    class Native:
        async def invoke_once(self, request, context):
            envelope = {
                "status": "succeeded",
                "error": None,
                "content": [{"type": "json", "json_ref": {"kind": "inline", "value": value()}}],
                "metadata": {},
                "artifacts": [],
            }
            if kind == "callback":
                context.report_completion(envelope)
            return envelope

    if kind == "function":
        return FunctionToolAdapter(lambda arguments, context: value())
    return Legacy() if kind == "legacy" else Native()


def compact_raw(op):
    if op["raw"] is None:
        return None
    return {key: op["raw"][key] for key in ("status", "error", "execution_fact")}


@pytest.mark.parametrize("kind", ["function", "legacy", "native", "callback"])
@pytest.mark.parametrize("depth", [0, 400, 465, 480, 600, 1010])
async def test_nested_return_must_finish_or_reject_before_durable_write(store, kind, depth):
    calls = []
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, nested_adapter(kind, depth, calls)).start("go")
        error = None
        result = None
        try:
            result = await handle.wait(timeout_s=3)
        except InfrastructureError as exc:
            error = exc.code
        state = runtime._load(handle.run_id)
        op = operation(runtime, handle.run_id)
        print(
            json.dumps(
                {
                    "run_id": handle.run_id,
                    "operation_id": op["operation_id"],
                    "depth": depth,
                    "recursion_limit": sys.getrecursionlimit(),
                    "kind": kind,
                    "driver_error": error,
                    "run_status": state["status"],
                    "raw": compact_raw(op),
                    "calls": len(calls),
                }
            )
        )
        assert error is None, "Accepted nesting must survive persistence and subsequent reads"
        assert result.status == "succeeded" and len(calls) == 1
        assert op["raw"]["execution_fact"] == "completed"
        if depth == 0:
            assert op["raw"]["status"] == "succeeded" and op["raw"]["result"] is None
        elif op["raw"]["status"] == "failed":
            assert op["raw"]["error"]["code"] == "CAPABILITY_RESULT_INVALID"
            assert op["raw"]["error"]["category"] == "protocol"
        else:
            # A safe implementation can support the depth or reject it as above.
            cursor = op["raw"]["result"]
            for _ in range(depth):
                assert isinstance(cursor, list) and len(cursor) == 1
                cursor = cursor[0]
            assert cursor is None


@pytest.mark.parametrize("kind", ["function", "native"])
async def test_saved_nested_result_remains_resumable_and_releases_session(store, kind):
    calls = []
    adapter = nested_adapter(kind, 465, calls)
    observations = []
    run_id = None
    for cycle in ("initial", "recovered"):
        async with RuntimeEngine(store=store) as runtime:
            handle = await build(runtime, adapter).start(
                "go" if run_id is None else None, run_id=run_id, session_id="nested"
            )
            run_id = handle.run_id
            error = None
            try:
                await handle.wait(timeout_s=3)
            except InfrastructureError as exc:
                error = exc.code
            state = runtime._load(run_id)
            observations.append(
                {
                    "run_id": run_id,
                    "cycle": cycle,
                    "error": error,
                    "status": state["status"],
                    "raw": compact_raw(operation(runtime, run_id)),
                }
            )
            if cycle == "recovered":
                try:
                    next_result = await build(
                        runtime, FunctionToolAdapter(lambda arguments, context: "ok")
                    ).ainvoke("next", session_id="nested")
                    observations[-1]["next_session_turn"] = next_result.status
                except RequestError as exc:
                    observations[-1]["next_session_turn"] = exc.code
    print(json.dumps({"cycles": observations, "calls": len(calls)}))
    assert len(calls) == 1, "Recovery must not repeat a tool with a saved completion"
    assert all(row["error"] is None and row["status"] == "succeeded" for row in observations), (
        "The stored result must remain usable after restarting RuntimeEngine"
    )
    assert observations[-1]["next_session_turn"] == "succeeded"


@pytest.mark.parametrize("channel", ["raised", "reported"])
@pytest.mark.parametrize("shape", ["valid", "nan", "bad-category", "empty-code", "bool-retryable"])
async def test_raised_error_fields_do_not_bypass_operation_error_validation(store, channel, shape):
    calls = []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(1)
            error = AgentLoomError(
                "" if shape == "empty-code" else "FAILED",
                category="outside-enum" if shape == "bad-category" else "business",
                retry_after_at=float("nan") if shape == "nan" else None,
                retryable_transport=1 if shape == "bool-retryable" else False,
            )
            if channel == "reported":
                context.report_completion(error=error)
            raise error

    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, Native()).ainvoke("go")
        op = operation(runtime, result.run_id)
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "operation_id": op["operation_id"],
                    "channel": channel,
                    "shape": shape,
                    "run_status": result.status,
                    "run_error": result.error,
                    "raw": compact_raw(op),
                }
            )
        )
        assert result.status == "succeeded", "A malformed tool error must remain a tool failure"
        assert len(calls) == 1 and op["status"] == "failed"
        raw = op["raw"]
        assert raw["status"] == "failed"
        error = raw["error"]
        assert isinstance(error["code"], str) and error["code"]
        assert isinstance(error["message"], str) and error["message"]
        assert error["category"] in {
            "validation",
            "permission",
            "not_found",
            "conflict",
            "transport",
            "rate_limit",
            "timeout",
            "business",
            "protocol",
            "cancelled",
            "unknown",
        }
        assert type(error["retryable_transport"]) is bool
        assert error.get("retry_after_at") is None or math.isfinite(error["retry_after_at"])
        if channel == "raised":
            assert raw["execution_fact"] == "unknown", "Raising does not prove a completed call"
        else:
            assert raw["execution_fact"] == "completed"
            if shape != "valid":
                assert error["code"] == "CAPABILITY_RESULT_INVALID"
