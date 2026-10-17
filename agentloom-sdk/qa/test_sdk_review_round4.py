"""Independent fourth-round probes for completion and invalid-value boundaries.

Run explicitly: .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round4.py
Assertions describe the documented behavior and are not marked xfail.
"""

import json

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
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    ToolResult,
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


def build(runtime, adapter, *, retry=False):
    return ReactAgent(
        definition=AgentDefinition(
            "round4",
            "SYSTEM",
            capabilities=(CapabilityBinding("work", "tool", "work", "work", "work"),),
        ),
        workspace=WorkspaceBinding("workspace"),
        runtime_engine=runtime,
        model=ScriptedModel(model),
        capabilities=CapabilityRegistry(
            tools=(CapabilityRegistration("work", adapter, (CapabilitySpec("work", "work"),)),)
        ),
        execution_policy=ExecutionPolicy(
            run_timeout_seconds=10,
            retry=RetryPolicy(enabled=retry, max_attempts=2, delay_seconds=0),
        ),
    )


def operation(runtime, run_id):
    return next(
        op
        for op in runtime._load(run_id)["operations"].values()
        if op["event_type"] == "capability.invoke"
    )


@pytest.mark.parametrize("channel", ["error", "result"], ids=["error-callback", "result-control"])
@pytest.mark.parametrize("retryable", [False, True], ids=["business", "transport"])
@pytest.mark.parametrize("outcome", ["known", "unknown", "not_started"])
async def test_known_error_completion_has_same_fact_and_retry_behavior_as_result(
    store, channel, retryable, outcome
):
    class Native:
        calls = 0

        async def invoke_once(self, request, context):
            self.calls += 1
            error = AgentLoomError(
                "KNOWN_RESPONSE",
                category="transport" if retryable else "business",
                phase="receive",
                external_outcome=outcome,
                retryable_transport=retryable,
            )
            if channel == "error":
                context.report_completion(error=error)
                raise error
            result = ToolResult(status="failed", error=error.to_dict())
            context.report_completion(result)
            return result

    adapter = Native()
    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, adapter, retry=True).ainvoke("go")
        op = operation(runtime, result.run_id)
        print(
            json.dumps(
                {"channel": channel, "outcome": outcome, "calls": adapter.calls, "raw": op["raw"]}
            )
        )
        assert result.status == "succeeded"
        expected_calls = 2 if retryable and outcome != "known" else 1
        assert adapter.calls == expected_calls, (
            "Callback syntax must not change completion or retry semantics"
        )
        assert op["raw"]["execution_fact"] == ("completed" if outcome == "known" else outcome)
        assert op["raw"]["error"]["code"] == "KNOWN_RESPONSE"


@pytest.mark.parametrize("channel", ["error", "result"], ids=["error-callback", "result-control"])
@pytest.mark.parametrize("malformed", ["empty-code", "bad-category", "nan-retry-after"])
async def test_error_completion_validates_error_dto_before_persistence(store, channel, malformed):
    class Native:
        calls = 0

        async def invoke_once(self, request, context):
            self.calls += 1
            error = AgentLoomError(
                "" if malformed == "empty-code" else "FAILED",
                category="outside-enum" if malformed == "bad-category" else "business",
                retry_after_at=float("nan") if malformed == "nan-retry-after" else None,
            )
            if channel == "error":
                context.report_completion(error=error)
                raise error
            # Use the public JSON-envelope form to exercise the runtime boundary.
            result = {
                "status": "failed",
                "error": error.to_dict(),
                "content": [],
                "artifacts": [],
                "metadata": {},
            }
            context.report_completion(result)
            return result

    adapter = Native()
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, adapter).start("go", session_id="invalid-error")
        infrastructure_error = None
        try:
            result = await handle.wait(timeout_s=2)
        except InfrastructureError as exc:
            infrastructure_error = exc.code
            result = None
        state = runtime._load(handle.run_id)
        op = operation(runtime, handle.run_id)
        session_busy = False
        if infrastructure_error is not None:
            with pytest.raises(RequestError, match="SESSION_BUSY"):
                await build(runtime, Native()).start("next", session_id="invalid-error")
            session_busy = True
        print(
            json.dumps(
                {
                    "channel": channel,
                    "malformed": malformed,
                    "status": state["status"],
                    "infrastructure_error": infrastructure_error,
                    "session_busy": session_busy,
                    "raw": op["raw"],
                }
            )
        )
        assert infrastructure_error is None, (
            "Invalid adapter data must not strand the Run as a storage failure"
        )
        assert result.status == "succeeded" and adapter.calls == 1
        assert op["raw"]["status"] == "failed"
        assert op["raw"]["error"]["code"] == "CAPABILITY_RESULT_INVALID"
        assert op["raw"]["error"]["category"] == "protocol"
        assert op["raw"]["execution_fact"] == "completed"


async def test_invalid_error_callback_requires_recovery_but_does_not_repeat_work(store):
    class Native:
        calls = 0

        async def invoke_once(self, request, context):
            self.calls += 1
            context.report_completion(error=AgentLoomError("FAILED", retry_after_at=float("nan")))
            raise AssertionError("Invalid completion must raise at the callback boundary")

    adapter = Native()
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, adapter).start("go", session_id="recovery")
        try:
            await handle.wait(timeout_s=2)
        except InfrastructureError as exc:
            # The current defect interrupts delivery; retain recovery as a control.
            assert exc.code == "ATTEMPT_COMPLETION_COMMIT_FAILED"
        run_id = handle.run_id
    async with RuntimeEngine(store=store) as recovered:
        result = await (await build(recovered, adapter).start(run_id=run_id)).wait(timeout_s=2)
        assert result.status == "succeeded" and adapter.calls == 1
        assert recovered._load(run_id)["usage"]["loops_reserved"] == 0


@pytest.mark.parametrize("adapter_kind", ["function", "legacy", "native"])
@pytest.mark.parametrize("shape", ["cycle-list", "cycle-dict", "nan-control"])
async def test_non_json_returned_container_keeps_completed_protocol_fact(
    store, adapter_kind, shape
):
    calls = []

    def value():
        calls.append(1)
        if shape == "cycle-list":
            result = []
            result.append(result)
            return result
        if shape == "cycle-dict":
            result = {}
            result["self"] = result
            return result
        return float("nan")

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
    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, adapter).ainvoke("go")
        op = operation(runtime, result.run_id)
        print(json.dumps({"kind": adapter_kind, "shape": shape, "raw": op["raw"]}))
        assert result.status == "succeeded" and len(calls) == 1
        assert op["raw"]["status"] == "failed"
        assert op["raw"]["execution_fact"] == "completed"
        assert op["raw"]["error"]["code"] == "CAPABILITY_RESULT_INVALID"
        assert op["raw"]["error"]["category"] == "protocol"
