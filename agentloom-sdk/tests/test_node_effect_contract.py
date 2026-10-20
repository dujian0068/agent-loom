"""Invalid proposals fail their task atomically, without poisoning recovery."""

from dataclasses import replace

import pytest

from agentloom import AgentDefinition, AgentNode, ModelResponse, PlanAgent, RuntimeEngine, ToolCall
from agentloom.configuration.json_documents import copy_document
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


@pytest.mark.parametrize("child", [False, True], ids=["root", "executor"])
async def test_rejected_decision_releases_reservation_before_task_failure(
    runtime_store, build, monkeypatch, child
):
    original = AgentNode.activate
    rejected = []

    async def activate(self, input, continuation, context):
        stream = original(self, input, continuation, context)
        try:
            outcome = await anext(stream)
            while True:
                if isinstance(outcome, Progress):
                    decision = [e for e in outcome.effects if e.kind == "accept_model"]
                    if decision and (not child or input.task_id in child_ids):
                        rejected.append(input.task_id)
                        outcome = replace(outcome, effects=(decision[0], decision[0]))
                receipt = yield outcome
                outcome = await stream.asend(receipt)
        except StopAsyncIteration:
            return
        finally:
            await stream.aclose()

    child_ids = set()

    def checkpoint(command, state):
        child_ids.update(t["task_id"] for t in state["tasks"].values() if t["parent_operation"])

    def respond(request):
        if (
            child
            and request.role_key == "planner"
            and not any(m["role"] == "tool" for m in request.messages)
        ):
            return ModelResponse(
                tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "go"}),)
            )
        return ModelResponse("done")

    monkeypatch.setattr(AgentNode, "activate", activate)
    options = (
        {
            "cls": PlanAgent,
            "executors": {"worker": AgentDefinition(prompt="SYS", description="work")},
        }
        if child
        else {}
    )
    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as runtime:
        result = await build(runtime, respond, **options).ainvoke("go")
        assert result.status == ("succeeded" if child else "failed")
        state = runtime._load(result.run_id)
        assert len(rejected) == 1
        failed = state["tasks"][rejected[0]]
        assert failed["error"]["code"] == "NODE_EFFECT_INVALID"
        model = state["operations"][failed["continuation"]["model_operation_id"]]
        assert model["status"] == "succeeded" and model["ticket"] == "released"
        assert model["raw"]["result"]["text"] == "done"
        assert "accepted_record_id" not in model
        assert not any(r["message"]["role"] == "assistant" for r in failed["context"]["records"])
        assert state["usage"]["loops_reserved"] == 0
        assert state["usage"]["loops_consumed"] == (2 if child else 0)
        assert state["usage"]["business_model_attempts"] == (3 if child else 1)
        validate_recovery(state)
    async with RuntimeEngine(store=runtime_store) as runtime:
        reopened = await build(
            runtime, lambda r: pytest.fail("A rejected decision must never be rerun"), **options
        ).ainvoke(run_id=result.run_id)
        assert reopened.status == result.status


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


async def test_decision_acceptance_is_persisted_separately_from_legacy_ticket(
    runtime_store,
    build,
):
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: ModelResponse("done")).ainvoke("go")
        state = runtime._load(result.run_id)
    task = state["tasks"][state["root_task_id"]]
    op = next(o for o in state["operations"].values() if o["purpose"] == "business")
    assert op["accepted_record_id"] in {r["record_id"] for r in task["context"]["records"]}
    validate_recovery(state)
    effect = {"kind": "accept_model", "body": {"operation_id": op["operation_id"]}}
    task["continuation"].update(
        phase="interpret_response",
        model_operation_id=op["operation_id"],
        accepted_decision_ref=None,
    )
    before = copy_document(state)
    with pytest.raises(RequestError, match="NODE_EFFECT_INVALID"):
        apply_effect(state, task, effect)
    assert state == before
    # A supported old response commit consumed the ticket before acceptance.
    # It still needs exactly one decision/context commit after upgrading.
    accepted = op.pop("accepted_record_id")
    task["context"]["records"] = [
        r for r in task["context"]["records"] if r["record_id"] != accepted
    ]
    consumed = state["usage"]["loops_consumed"]
    apply_effect(state, task, effect)
    assert state["usage"]["loops_consumed"] == consumed
    assert op["accepted_record_id"] != accepted
    with pytest.raises(RequestError, match="NODE_EFFECT_INVALID"):
        apply_effect(state, task, effect)


async def test_conflicting_revisions_within_a_proposal_fail_without_requeue(
    runtime_store,
    build,
    monkeypatch,
):
    activations = []

    class Node:
        async def activate(self, input, continuation, context):
            activations.append(input.task_id)
            revision = context.context_port.snapshot()["context"]["revision"]
            yield Progress(
                ContinuationRef(
                    input.task_id,
                    {
                        **continuation.value,
                        "role_loop_index": 1,
                    },
                    continuation.revision,
                ),
                (
                    PreparedEffect(
                        "format_feedback",
                        {"message": "ROLLBACK", "expected_context_revision": revision},
                    ),
                    PreparedEffect("prepare_context", {"expected_context_revision": revision}),
                ),
            )

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: pytest.fail("No model call")).ainvoke("go")
        assert result.error["code"] == "NODE_EFFECT_INVALID"
        assert len(activations) == 1
        state = runtime._load(result.run_id)
        assert "ROLLBACK" not in str(state["tasks"][state["root_task_id"]]["context"])
        validate_recovery(state)
