"""Node handshakes and durable graph boundaries, exercised through the real driver."""

import asyncio
from dataclasses import dataclass

import pytest

from agentloom import (
    AgentDefinition,
    InfrastructureError,
    ModelResponse,
    PlanAgent,
    RuntimeEngine,
    ToolCall,
)
from agentloom.errors import ConfigurationError, RequestError
from agentloom.events import EventBusImpl, HandlerBinding
from agentloom.nodes import Complete, ContinuationRef, PayloadRef, Progress
from agentloom.runtime.agent_engine import AgentGraphEngine
from agentloom.runtime.handlers import BusinessEvent


@pytest.mark.parametrize("versions", [(1, 2), (2,)])
async def test_event_version_is_an_exact_route(versions):
    @dataclass
    class Handler:
        version: int

        async def handle(self, event, context):
            return self.version

    bus = EventBusImpl()
    for version in versions:
        bus.register_handler("test", version, HandlerBinding(Handler(version)))
    bus.freeze()
    assert await bus.request(BusinessEvent("test", "op", {}, schema_version=2)) == 2
    assert isinstance(bus.resolve("test", 2), tuple)
    with pytest.raises(Exception, match="HANDLER_NOT_FOUND"):
        await bus.request(BusinessEvent("test", "op", {}, schema_version=3))


@pytest.mark.parametrize("version", [True, False, 0, -1, 1.0, "1", None])
def test_event_version_rejects_ambiguous_types(version):
    with pytest.raises(ConfigurationError, match="EVENT_VERSION_INVALID"):
        EventBusImpl().register_handler("test", version, HandlerBinding(object()))


def test_registry_cycle_failure_does_not_partially_freeze():
    root, child = EventBusImpl(), EventBusImpl()
    root.register_handler("child", 1, HandlerBinding(child))
    child.register_handler("root", 1, HandlerBinding(root))
    with pytest.raises(ConfigurationError, match="HANDLER_CYCLE"):
        root.freeze()
    assert not root._frozen and not child._frozen


async def test_progress_receipt_precedes_continuation_and_ports_expire(
    runtime_store, build, monkeypatch
):
    observations = []

    class Node:
        async def activate(self, input, continuation, context):
            assert input.input_ref.value["content"] == "hello"
            assert continuation.owner_id == input.task_id == context.task_id
            assert not hasattr(context, "driver")
            observations.append(context)
            next_value = {**continuation.value, "phase": "prepare_model"}
            receipt = yield Progress(
                ContinuationRef(context.task_id, next_value, continuation.revision)
            )
            assert receipt.allocated_refs["task_id"] == context.task_id
            committed = context.context_port.snapshot()
            assert committed["continuation"]["phase"] == "prepare_model"
            assert receipt.entity_versions[0]["node_revision"] == committed["node_revision"]
            yield Complete(
                PayloadRef(context.task_id, "from-node"),
                ContinuationRef(
                    context.task_id,
                    {**next_value, "phase": "completed"},
                    committed["node_revision"],
                ),
            )
            pytest.fail("Driver must close after Complete, not resume the generator")

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: pytest.fail("No model required")).ainvoke("hello")
        assert result.output.value == "from-node"
        state = runtime._load(result.run_id)
        assert len(state["task_results"]) == 1
        with pytest.raises(RequestError, match="NODE_ACTIVATION_CLOSED"):
            observations[0].context_port.snapshot()


@pytest.mark.parametrize(
    "mode,code",
    [
        ("missing", "NODE_MISSING_TERMINAL_OUTCOME"),
        ("invalid", "NODE_OUTCOME_INVALID"),
        ("exception", "NODE_IMPLEMENTATION_FAILED"),
    ],
)
async def test_node_contract_failures_are_sanitized_task_results(
    runtime_store, build, monkeypatch, mode, code
):
    class Node:
        async def activate(self, input, continuation, context):
            if mode == "exception":
                raise ValueError("secret provider credential")
            if mode == "invalid":
                yield {"complete": True}

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: pytest.fail("No model required")).ainvoke("hello")
        assert result.status == "failed" and result.error["code"] == code
        assert "secret" not in str(result)
        state = runtime._load(result.run_id)
        assert len(state["task_results"]) == 1
        assert all(a["status"] == "completed" for a in state["activations"].values())


async def test_model_wait_has_no_live_node_activation(runtime_store, build):
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        entered.set()
        await release.wait()
        return ModelResponse("done")

    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, model).start("hello")
        await asyncio.wait_for(entered.wait(), 3)
        state = runtime._load(handle.run_id)
        root = state["tasks"][state["root_task_id"]]
        assert (
            root["status"] == "waiting" and root["wait_condition"]["kind"] == "operation_completed"
        )
        assert all(a["status"] != "running" for a in state["activations"].values())
        assert root["continuation"]["phase"] == "call_model"
        release.set()
        assert (await handle.wait(timeout_s=3)).status == "succeeded"


async def test_decision_ticket_context_and_batch_share_receipt(runtime_store, build):
    seen = []

    def checkpoint(command, state):
        if command.startswith(("complete:", "decision:")):
            models = [
                o for o in state["operations"].values() if o["event_type"] == "model.generate"
            ]
            if models:
                op = models[0]
                seen.append((command.split(":")[0], op["ticket"], len(state.get("batches", {}))))

    def model(request):
        if request.role_key == "planner" and not any(m["role"] == "tool" for m in request.messages):
            return ModelResponse(
                tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "work"}),)
            )
        return ModelResponse("done")

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as runtime:
        result = await build(
            runtime, model, cls=PlanAgent, executors={"worker": AgentDefinition("w", "SYS", "work")}
        ).ainvoke("hello")
        assert result.status == "succeeded"
        assert seen[:2] == [("complete", "reserved", 0), ("decision", "consumed", 1)]
        state = runtime._load(result.run_id)
        assert all(
            m["admission_token"] is None for b in state["batches"].values() for m in b["members"]
        )
        assert all(b["delivery_state"] == "consumed" for b in state["batches"].values())
        assert all(o["raw"] is not None for o in state["operations"].values() if o["target_ref"])


async def test_scope_replay_and_corruption_are_checked_before_execution(runtime_store, build):
    class Cut(Exception):
        pass

    def checkpoint(command, state):
        if command.startswith("decision:"):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as runtime:
        handle = await build(runtime, lambda r: ModelResponse("saved")).start(
            "hello", run_id="journal"
        )
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
    restored = runtime_store.load_run_consistently("default", "journal")
    assert restored["tasks"][restored["root_task_id"]]["candidate"] == "saved"
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", restored["graph_id"])
        assert head["scope_commit_seq"] > 1
        node_id = head["node_refs"][restored["root_task_id"]]
        checkpoint = tx.get("node_checkpoints", node_id)
        checkpoint["activation_id"] = "tampered"
        tx.put("node_checkpoints", node_id, checkpoint)
    async with RuntimeEngine(store=runtime_store) as recovered:
        handle = await build(
            recovered, lambda r: pytest.fail("Corruption must precede dispatch")
        ).start(run_id="journal")
        with pytest.raises(InfrastructureError, match="CHECKPOINT_CORRUPT"):
            await handle.wait(timeout_s=3)


@pytest.mark.parametrize("invalid", ["continuation", "output", "effects", "wait_scope"])
async def test_invalid_outcome_fields_are_protocol_failures(
    runtime_store, build, monkeypatch, invalid
):
    from agentloom.nodes import Wait, WaitCondition

    class Node:
        async def activate(self, input, continuation, context):
            if invalid == "continuation":
                yield Progress(None)
            elif invalid == "output":
                yield Complete(None, continuation)
            elif invalid == "effects":
                yield Progress(continuation, (lambda state: None,))
            else:
                yield Wait(
                    continuation,
                    WaitCondition("operation_completed", "foreign"),
                    PayloadRef(context.task_id, {}),
                )

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: pytest.fail("No model dispatch")).ainvoke("hello")
        assert result.status == "failed" and result.error["code"] == "NODE_OUTCOME_INVALID"
