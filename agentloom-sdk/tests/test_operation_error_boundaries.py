"""Malformed exception DTOs are safe failures, never retry or completion evidence."""

import asyncio

import pytest
from conftest import assert_finished
from test_attempt_completion import operation, tool_model, tool_options

from agentloom import (
    AgentLoomError,
    ExecutionPolicy,
    InfrastructureError,
    ModelDescriptor,
    RetryPolicy,
    RuntimeEngine,
    ToolResult,
)
from agentloom.capabilities.invocation import normalize_completion, normalize_error_completion
from agentloom.errors import LeaseLost, error_record
from agentloom.operation_errors import normalize_operation_error
from agentloom.runtime.completion import failed_result


@pytest.mark.parametrize(
    "field,value",
    [
        ("code", ""),
        ("code", False),
        ("message", ""),
        ("message", "\ud800"),
        ("category", "infrastructure"),
        ("phase", "commit"),
        ("external_outcome", "invalid"),
        ("retryable_transport", 1),
        ("retry_after_at", True),
        ("retry_after_at", "tomorrow"),
        ("retry_after_at", float("nan")),
        ("retry_after_at", float("inf")),
        ("retry_after_at", 2**53),
        ("field_path", []),
    ],
)
def test_exception_and_completion_paths_share_the_error_contract(field, value):
    error = AgentLoomError("FAILED")
    setattr(error, field, value)
    raw = failed_result(error)
    assert raw["error"]["code"] == "ADAPTER_ERROR_INVALID"
    assert raw["error"]["category"] == "protocol" and raw["execution_fact"] == "unknown"
    assert normalize_operation_error(raw["error"]) == raw["error"]
    for completion in (
        lambda: normalize_error_completion(error),
        lambda: normalize_completion(
            {
                "status": "failed",
                "error": error.to_dict(),
                "content": [],
                "metadata": {},
                "artifacts": [],
            }
        ),
    ):
        with pytest.raises(AgentLoomError) as caught:
            completion()
        assert caught.value.code == "CAPABILITY_RESULT_INVALID"
        assert caught.value.execution_fact == "completed"


@pytest.mark.parametrize(
    "outcome,fact", [("known", "unknown"), ("unknown", "unknown"), ("not_started", "not_started")]
)
def test_valid_exception_keeps_retry_and_outcome_semantics(outcome, fact):
    error = AgentLoomError(
        "THROTTLED",
        category="rate_limit",
        phase="receive",
        external_outcome=outcome,
        retryable_transport=True,
        retry_after_at=123.5,
        field_path="arguments.path",
    )
    raw = failed_result(error)
    assert raw["error"] == error.to_dict() and raw["execution_fact"] == fact


@pytest.mark.parametrize("provider_code", ["invalid_request", -32602])
def test_provider_error_code_can_be_text_or_a_json_rpc_number(provider_code):
    value = {**AgentLoomError("FAILED").to_dict(), "provider_code": provider_code}
    assert normalize_operation_error(value) == value


@pytest.mark.parametrize(
    "error", [InfrastructureError("STORE_FAILED"), LeaseLost(), asyncio.CancelledError()]
)
def test_control_and_storage_exceptions_are_never_normalized_as_attempt_failures(error):
    with pytest.raises(type(error)) as caught:
        failed_result(error)
    assert caught.value is error


@pytest.mark.parametrize("shape", ["raises", "wrong-type", "extra-field"])
def test_exception_serializer_cannot_escape_the_record_boundary(shape):
    class InvalidError(AgentLoomError):
        def to_dict(self):
            if shape == "raises":
                raise ValueError("provider secret must not escape")
            return [] if shape == "wrong-type" else {**super().to_dict(), "extra": "secret"}

    record = error_record(InvalidError("FAILED"))
    assert record["code"] == "ADAPTER_ERROR_INVALID"
    assert "secret" not in str(record)


def test_rejected_error_cannot_assert_completion_through_private_sidecars():
    error = AgentLoomError("FAILED", category="invalid")
    error.execution_fact = "completed"
    error.result_material = {"untrusted": True}
    raw = failed_result(error)
    assert raw["error"]["code"] == "ADAPTER_ERROR_INVALID"
    assert raw["execution_fact"] == "unknown" and raw["result"] is None


@pytest.mark.parametrize("shape", ["cycle", "depth"])
def test_exception_sidecars_cannot_poison_attempt_storage(shape):
    value = []
    if shape == "cycle":
        value.append(value)
    else:
        for _ in range(200):
            value = [value]
    error = AgentLoomError("FAILED")
    error.execution_fact = "completed"
    error.result_material = value
    error.tool_result = value
    error.invalid_tool_result = {"value": value}
    raw = failed_result(error)
    assert raw["execution_fact"] == "completed" and raw["result"] is None
    assert raw["error"]["code"] == "ADAPTER_ERROR_INVALID"
    assert "tool_result" not in raw and "invalid_tool_result" not in raw


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize(
    "sidecar",
    [
        {"status": "unknown"},
        {"status": "failed", "content": [], "metadata": {}, "artifacts": [], "error": None},
        {"status": "succeeded", "content": [{}], "metadata": {}, "artifacts": [], "error": None},
        {"status": [], "content": [], "metadata": {}, "artifacts": [], "error": None},
    ],
)
def test_error_sidecar_uses_complete_tool_contract_without_changing_facts(completed, sidecar):
    error = AgentLoomError("FAILED")
    if completed:
        error.execution_fact = "completed"
    error.tool_result = sidecar
    error.invalid_tool_result = {"wrong_key": None}
    raw = failed_result(error)
    assert raw["error"]["code"] == "FAILED"
    assert raw["execution_fact"] == ("completed" if completed else "unknown")
    assert "tool_result" not in raw and "invalid_tool_result" not in raw


def test_infrastructure_error_from_sidecar_serialization_propagates():
    class BrokenReceipt(ToolResult):
        def to_dict(self):
            raise InfrastructureError("RECEIPT_STORAGE_FAILED")

    error = AgentLoomError("FAILED")
    error.tool_result = BrokenReceipt()
    with pytest.raises(InfrastructureError, match="RECEIPT_STORAGE_FAILED"):
        failed_result(error)


async def test_invalid_exception_is_delivered_once_without_retry(runtime_store, build):
    calls = []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(1)
            raise AgentLoomError("THROTTLED", category="rate_limit", retryable_transport=1)

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            tool_model,
            **tool_options(Native(), kind="tool"),
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
            ),
        ).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "succeeded")
        op = operation(state, "capability.invoke")
        assert len(calls) == len(op["attempts"]) == 1
        assert op["raw"]["error"]["code"] == "ADAPTER_ERROR_INVALID"
        assert op["raw"]["execution_fact"] == "unknown"


async def test_invalid_model_exception_finishes_with_safe_error(runtime_store, build):
    class Model:
        descriptor = ModelDescriptor("invalid-error", 32768)

        async def stream_once(self, request, context):
            raise AgentLoomError("FAILED", retry_after_at=float("nan"))
            yield

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, None, model=Model()).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "failed")
        op = operation(state, "model.generate")
        assert op["raw"]["error"]["code"] == "ADAPTER_ERROR_INVALID"
        assert op["raw"]["execution_fact"] == "unknown"
