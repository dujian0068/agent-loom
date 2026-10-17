"""Usage accounting stays canonical and preserves unknown/partial statistics."""

import pytest
from conftest import assert_finished, binding, tool

from agentloom import (
    AgentDefinition,
    CapabilityRegistry,
    ConfigurationError,
    ModelResponse,
    RuntimeEngine,
    ToolCall,
)
from agentloom.configuration.json_values import MAX_JSON_INTEGER
from agentloom.runtime.recovery import validate_recovery


@pytest.mark.parametrize("overflow", [False, True])
async def test_token_sum_boundary_is_atomic_and_does_not_strand_session(
    runtime_store, build, overflow
):
    def model(request):
        if any(m["role"] == "tool" for m in request.messages):
            return ModelResponse("done", usage={"input_tokens": 1, "output_tokens": 1})
        return ModelResponse(
            tool_calls=(ToolCall("echo", {}),),
            usage={
                "input_tokens": MAX_JSON_INTEGER if overflow else MAX_JSON_INTEGER - 1,
                "output_tokens": 0,
            },
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            model,
            definition=AgentDefinition("test", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: "ok"),)),
        ).ainvoke("question", session_id="usage")
        state = assert_finished(runtime, result.run_id, "failed" if overflow else "succeeded")
        assert result.usage.input_tokens == MAX_JSON_INTEGER
        assert result.usage.output_tokens == (0 if overflow else 1)
        assert result.usage.token_counts_complete is not overflow
        if overflow:
            assert result.error["code"] == "MODEL_USAGE_OVERFLOW"
            op = next(o for o in state["operations"].values() if o["raw"] and o["raw"]["error"])
            assert op["raw"]["result"]["text"] == "done"
            assert op["raw"]["execution_fact"] == "completed"
            assert len(op["attempts"]) == 1 and op["attempts"][0]["usage_accounted"] is False
        validate_recovery(state)
        assert (
            await build(runtime, lambda r: ModelResponse("next")).ainvoke(
                "next",
                session_id="usage",
            )
        ).status == "succeeded"
    async with RuntimeEngine(store=runtime_store) as runtime:
        reopened = await (await runtime.runs.attach(result.run_id)).wait(timeout_s=2)
        assert reopened.usage == result.usage and reopened.status == result.status


@pytest.mark.parametrize(
    "statistics",
    [
        {"input_tokens": 3},
        {"output_tokens": 4},
        {"input_tokens": None, "output_tokens": 4},
    ],
)
async def test_partial_known_counts_survive_without_inventing_missing_values(
    runtime_store, build, statistics
):
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: ModelResponse("done", usage=statistics)).ainvoke(
            "question"
        )
        assert result.status == "succeeded" and not result.usage.token_counts_complete
        assert result.usage.input_tokens == statistics.get("input_tokens")
        assert result.usage.output_tokens == statistics.get("output_tokens")
        validate_recovery(runtime._load(result.run_id))


def test_stores_reject_noncanonical_integer_before_commit(runtime_store):
    with pytest.raises(ConfigurationError, match="INVALID_CANONICAL_JSON"):
        with runtime_store.transaction("default") as tx:
            tx.put("probe", "valid", {"count": 1})
            tx.put("probe", "bad", {"count": MAX_JSON_INTEGER + 1})
    with runtime_store.transaction("default") as tx:
        assert tx.get("probe", "valid") is None and tx.get("probe", "bad") is None
