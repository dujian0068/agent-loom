"""Invalid proposals fail their task atomically, without poisoning recovery."""

import pytest

from agentloom import AgentDefinition, ModelResponse, PlanAgent, RuntimeEngine, ToolCall
from agentloom.errors import InfrastructureError, RequestError
from agentloom.nodes import ContinuationRef, PreparedEffect, Progress
from agentloom.nodes.effects import apply_effect
from agentloom.runtime.agent_engine import AgentGraphEngine
from agentloom.runtime.recovery import validate_recovery


@pytest.mark.parametrize(
    "kind,body",
    [
        ("consume_batch", {}),
        ("consume_batch", {"batch_id": []}),
        ("consume_batch", {"batch_id": "missing"}),
        ("accept_model", {}),
        ("accept_model", {"operation_id": "missing"}),
        ("accept_model", {"operation_id": 1}),
        ("begin_model", {}),
        ("begin_model", {"operation": {}}),
        ("format_feedback", {"message": None}),
        ("prepare_context", {"expected_context_revision": True}),
        ("unknown", {}),
    ],
)
async def test_bad_effect_rolls_back_and_reopens(runtime_store, build, monkeypatch, kind, body):
    activations = []

    class Node:
        async def activate(self, input, continuation, context):
            activations.append(input.task_id)
            advance = 1 + int(kind in {"consume_batch", "format_feedback"})
            yield Progress(
                ContinuationRef(
                    input.task_id,
                    {**continuation.value, "role_loop_index": advance},
                    continuation.revision,
                ),
                (
                    PreparedEffect("format_feedback", {"message": "ROLLBACK_MARKER"}),
                    PreparedEffect(kind, body),
                ),
            )

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, lambda r: pytest.fail("Invalid node must not dispatch")
        ).ainvoke("go")
        assert result.status == "failed" and result.error["code"] == "NODE_EFFECT_INVALID"
        state = runtime._load(result.run_id)
        root = state["tasks"][state["root_task_id"]]
        assert root["round"] == 0 and len(state["task_results"]) == 1
        assert not state["operations"] and state["usage"]["loops_consumed"] == 0
        assert "ROLLBACK_MARKER" not in str(root["context"])
        validate_recovery(state)
    async with RuntimeEngine(store=runtime_store) as runtime:
        recovered = await (
            await build(runtime, lambda r: ModelResponse("unused")).start(run_id=result.run_id)
        ).wait(timeout_s=3)
        assert recovered.error["code"] == "NODE_EFFECT_INVALID" and len(activations) == 1


@pytest.mark.parametrize("damage", ["request", "role", "ticket", "identity", "attempts"])
async def test_malformed_model_intent_cannot_enter_ledger(
    runtime_store, build, monkeypatch, damage
):
    class Node:
        async def activate(self, input, continuation, context):
            _, effect = await context.model_port.prepare(0, context.context_port)
            op = dict(effect.body["operation"])
            if damage == "request":
                op["request"] = {}
            elif damage == "role":
                op["role_key"] = "foreign"
            elif damage == "ticket":
                op["ticket"] = "reserved"
            elif damage == "identity":
                op["operation_id"] = "invented"
            else:
                op["attempts"] = [{"attempt_id": "invented"}]
            yield Progress(continuation, (PreparedEffect("begin_model", {"operation": op}),))

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: pytest.fail("No model call allowed")).ainvoke("go")
        assert result.error["code"] == "NODE_EFFECT_INVALID"
        assert not runtime._load(result.run_id)["operations"]


async def test_invalid_executor_effect_does_not_cancel_planner_or_sibling(
    runtime_store, build, monkeypatch
):
    original = AgentGraphEngine._node

    class BadNode:
        async def activate(self, input, continuation, context):
            yield Progress(
                continuation, (PreparedEffect("accept_model", {"operation_id": "missing"}),)
            )

    monkeypatch.setattr(
        AgentGraphEngine,
        "_node",
        lambda self, task_id: (
            BadNode() if self.runner.task(task_id)["role_key"] == "bad" else original(self, task_id)
        ),
    )

    def model(request):
        if request.role_key == "planner" and not any(m["role"] == "tool" for m in request.messages):
            return ModelResponse(
                tool_calls=(
                    ToolCall("delegate_task", {"executor": "bad", "task": "one"}),
                    ToolCall("delegate_task", {"executor": "good", "task": "two"}),
                )
            )
        return ModelResponse("done")

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            model,
            cls=PlanAgent,
            executors={
                "bad": AgentDefinition("bad", "SYS", "one"),
                "good": AgentDefinition("good", "SYS", "two"),
            },
        ).ainvoke("go")
        assert result.status == "succeeded"
        tasks = {t["role_key"]: t for t in runtime._load(result.run_id)["tasks"].values()}
        assert tasks["bad"]["error"]["code"] == "NODE_EFFECT_INVALID"
        assert tasks["good"]["status"] == "succeeded"


@pytest.mark.parametrize("kind", ["accept_model", "consume_batch"])
def test_foreign_effect_reference_is_a_proposal_error(kind):
    task = {"task_id": "own", "context": {"revision": 0}}
    state = {
        "operations": {"foreign": {"task_id": "other"}},
        "batches": {"foreign": {"owner_task_id": "other"}},
    }
    body = {"operation_id" if kind == "accept_model" else "batch_id": "foreign"}
    with pytest.raises(RequestError, match="NODE_EFFECT_INVALID"):
        apply_effect(state, task, {"kind": kind, "body": body})


def test_legacy_batch_without_decision_has_explicit_recovery_error():
    with pytest.raises(InfrastructureError, match="BATCH_DECISION_TICKET_REQUIRED"):
        AgentGraphEngine._upgrade_task(
            {"run_id": "r", "operations": {}},
            {"task_id": "t", "stage": "batch", "round": 0, "parent_operation": None},
        )
