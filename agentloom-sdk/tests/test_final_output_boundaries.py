"""Output correction uses business loops; schema defects remain configuration failures."""

import pytest

from agentloom import (
    AgentDefinition,
    ExecutionPolicy,
    ModelResponse,
    RetryPolicy,
    RuntimeEngine,
)
from agentloom.runtime.recovery import validate_recovery


@pytest.mark.parametrize("candidate", ["NaN", "[" * 1100 + "null" + "]" * 1100])
async def test_candidate_errors_consume_another_loop_not_a_transport_retry(
    runtime_store, build, candidate
):
    calls = []

    def respond(request):
        calls.append(request)
        return ModelResponse(candidate if len(calls) == 1 else "42")

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            respond,
            definition=AgentDefinition("a", "Return a number", output_schema={}),
            execution_policy=ExecutionPolicy(
                loop_max=2, retry=RetryPolicy(enabled=True, max_attempts=3, delay_seconds=0)
            ),
        ).ainvoke("go")
        assert result.status == "succeeded" and result.output.value == 42
        state = runtime._load(result.run_id)
        assert len(calls) == len(state["operations"]) == result.usage.loops_consumed == 2
        assert all(len(op["attempts"]) == 1 for op in state["operations"].values())
        records = state["tasks"][state["root_task_id"]]["context"]["records"]
        assert any(r["message"]["content"] == candidate for r in records)
        assert sum(r["kind"] == "format" for r in records) == 1
        validate_recovery(state)


async def test_output_correction_cannot_exceed_business_budget(runtime_store, build):
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda request: ModelResponse("NaN"),
            definition=AgentDefinition("a", "Return a number", output_schema={}),
            execution_policy=ExecutionPolicy(
                loop_max=1, retry=RetryPolicy(enabled=True, max_attempts=3, delay_seconds=0)
            ),
        )
        result = await agent.ainvoke("go")
        assert result.status == "budget_exhausted" and result.error["code"] == "LOOP_LIMIT_EXCEEDED"
        assert len(agent.model.requests) == result.usage.loops_consumed == 1
        assert result.usage.compaction_model_attempts == 0


async def test_unusable_schema_is_not_sent_back_for_model_correction(runtime_store, build):
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda request: ModelResponse("42"),
            definition=AgentDefinition("a", "Return a number", output_schema={"$ref": "#"}),
        )
        result = await agent.ainvoke("go")
        assert result.status == "failed" and result.error["code"] == "INVALID_SCHEMA"
        assert len(agent.model.requests) == 1
        state = runtime._load(result.run_id)
        assert not any(
            r["kind"] == "format"
            for r in state["tasks"][state["root_task_id"]]["context"]["records"]
        )
