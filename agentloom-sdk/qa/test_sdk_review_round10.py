"""Incremental review of a5ec1bc's node driver and checkpoint protocol.

These are independent contract assertions, not xfails. They use the real runtime
and both stores, with deterministic local nodes/models and no LLM credentials.
The supplied 1764-case baseline is reused; run this file alone for this audit.
"""

import json
from pathlib import Path

import pytest

from agentloom import (
    AgentDefinition,
    AgentNode,
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
    PlanAgent,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)
from agentloom.nodes import Complete, ContinuationRef, PayloadRef, Progress, Wait, WaitCondition
from agentloom.runtime.agent_engine import AgentGraphEngine


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


def build(runtime, responder=None, **kwargs):
    kwargs.setdefault("definition", AgentDefinition("review10", "SYSTEM"))
    kwargs.setdefault("workspace", WorkspaceBinding(Path.cwd() / "workspace"))
    kwargs.setdefault("model", ScriptedModel(responder or (lambda r: ModelResponse("done"))))
    return ReactAgent(runtime_engine=runtime, **kwargs)


@pytest.mark.parametrize(
    "payload",
    [{"operation_id": "saved", "generation": 1}, None, {}, [], False, 0, ""],
    ids=["reference", "null", "empty_object", "empty_array", "false", "zero", "empty_text"],
)
async def test_wait_preserves_resume_payload_and_hash(store, monkeypatch, payload):
    """runtime.md §3: both wait paths preserve the same durable resume reference."""
    observed = []

    class Node:
        async def activate(self, input, continuation, context):
            if continuation.value["phase"] == "prepare_context":
                yield Wait(
                    ContinuationRef(
                        input.task_id,
                        {**continuation.value, "phase": "prepare_model"},
                        continuation.revision,
                    ),
                    WaitCondition("budget_available", context.scope.run_id),
                    PayloadRef(input.task_id, payload),
                )
                return
            observed.append(input.resume_input_ref)
            yield Complete(
                PayloadRef(input.task_id, "done"),
                ContinuationRef(
                    input.task_id,
                    {**continuation.value, "phase": "completed"},
                    continuation.revision,
                ),
            )

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=store) as runtime:
        result = await (await build(runtime).start("go")).wait(timeout_s=5)
        assert result.status == "succeeded", result.error
        expected = PayloadRef(observed[0].owner_id, payload)
        print(
            json.dumps(
                {
                    "case": "wait_payload",
                    "expected": expected.to_dict(),
                    "actual": observed[0].to_dict(),
                },
                ensure_ascii=False,
            )
        )
        assert observed[0].to_dict() == expected.to_dict()


async def test_builtin_model_wait_delivers_its_saved_reference(store, monkeypatch):
    """Observe the real ReAct node's pending-operation wait, without replacing it."""
    activate = AgentNode.activate
    observations = []

    def observe(self, input, continuation, context):
        if input.resume_input_ref is not None:
            observations.append(
                (
                    input.resume_input_ref.to_dict(),
                    context.context_port.snapshot()["resume_input_ref"],
                )
            )
        return activate(self, input, continuation, context)

    monkeypatch.setattr(AgentNode, "activate", observe)
    async with RuntimeEngine(store=store) as runtime:
        result = await (await build(runtime).start("go")).wait(timeout_s=5)
        assert result.status == "succeeded"
        assert observations
        for actual, saved in observations:
            print(json.dumps({"case": "builtin_resume", "actual": actual, "saved": saved}))
            assert actual == saved


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "completed"),
        ("phase", "unknown_phase"),
        ("protocol_version", 2),
        ("role_key", "foreign_executor"),
        ("role_loop_index", -1),
    ],
    ids=[
        "completed_progress",
        "unknown_phase",
        "unsupported_version",
        "foreign_role",
        "negative_loop",
    ],
)
async def test_invalid_progress_is_rejected_before_commit(store, monkeypatch, field, value):
    """runtime.md §3 requires phase/binding/version validation at the commit gate."""
    receipts = []

    class Node:
        async def activate(self, input, continuation, context):
            invalid = {**continuation.value, field: value}
            receipts.append(
                (yield Progress(ContinuationRef(input.task_id, invalid, continuation.revision)))
            )
            current = context.context_port.snapshot()
            yield Complete(
                PayloadRef(input.task_id, "accepted-invalid-progress"),
                ContinuationRef(
                    input.task_id,
                    {**continuation.value, "phase": "completed"},
                    current["node_revision"],
                ),
            )

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=store) as runtime:
        result = await (await build(runtime).start("go")).wait(timeout_s=5)
        print(
            json.dumps(
                {
                    "case": "invalid_progress",
                    "field": field,
                    "value": value,
                    "received_commit_receipts": len(receipts),
                    "status": result.status,
                    "error": result.error,
                },
                ensure_ascii=False,
            )
        )
        assert result.status == "failed" and result.error["code"] == "NODE_OUTCOME_INVALID"
        assert not receipts


async def test_rejected_progress_does_not_leave_unrecoverable_checkpoint(store, monkeypatch):
    """A malformed phase must become a task error before it can poison takeover."""
    cuts = []

    class Node:
        async def activate(self, input, continuation, context):
            yield Progress(
                ContinuationRef(
                    input.task_id,
                    {**continuation.value, "phase": "completed"},
                    continuation.revision,
                )
            )
            yield Complete(
                PayloadRef(input.task_id, "unexpected"),
                ContinuationRef(
                    input.task_id,
                    continuation.value,
                    context.context_port.snapshot()["node_revision"],
                ),
            )

    def checkpoint(command, state):
        if command.startswith("progress:"):
            cuts.append(command)
            raise RuntimeError("simulated exit after an invalid progress commit")

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        handle = await build(runtime).start("go", run_id="invalid-progress-recovery")
        try:
            result = await handle.wait(timeout_s=5)
        except InfrastructureError:
            assert cuts
            state = runtime._load(handle.run_id)
            root = state["tasks"][state["root_task_id"]]
            print(
                json.dumps(
                    {
                        "case": "invalid_progress_checkpoint",
                        "status": root["status"],
                        "phase": root["continuation"]["phase"],
                    }
                )
            )
        else:
            assert result.status == "failed" and result.error["code"] == "NODE_OUTCOME_INVALID"
            return
    async with RuntimeEngine(store=store) as recovered:
        result = await (await build(recovered).start(run_id="invalid-progress-recovery")).wait(
            timeout_s=5
        )
        assert result.status == "failed" and result.error["code"] == "NODE_OUTCOME_INVALID"


async def test_generator_cleanup_cannot_erase_committed_complete(store, monkeypatch):
    """Once Complete is committed, generator cleanup cannot lose its receipt/result."""

    class Node:
        async def activate(self, input, continuation, context):
            try:
                yield Complete(
                    PayloadRef(input.task_id, "durable-output"),
                    ContinuationRef(
                        input.task_id,
                        {**continuation.value, "phase": "completed"},
                        continuation.revision,
                    ),
                )
            finally:
                raise ValueError("local cleanup failed")

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime).start("go")
        try:
            result = await handle.wait(timeout_s=5)
        except InfrastructureError as exc:
            state = runtime._load(handle.run_id)
            root = state["tasks"][state["root_task_id"]]
            print(
                json.dumps(
                    {
                        "case": "close_after_complete",
                        "error": exc.code,
                        "run_status": state["status"],
                        "task_status": root["status"],
                        "task_result": root["result"],
                        "result_count": len(state["task_results"]),
                    }
                )
            )
            raise
        assert result.status == "succeeded" and result.output.value == "durable-output"


async def checkpointed_run(store):
    class Cut(Exception):
        pass

    def checkpoint(command, state):
        if command.startswith("decision:") and state["tasks"][state["root_task_id"]]["round"] == 2:
            raise Cut()

    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse("not-json"),
            definition=AgentDefinition("review10", "SYS", output_schema={"type": "object"}),
        )
        handle = await agent.start("go", run_id="round10-checkpoint")
        with pytest.raises(InfrastructureError, match="COMMIT_ACKNOWLEDGEMENT_FAILED"):
            await handle.wait(timeout_s=5)
    return store.load("default", handle.run_id)


@pytest.mark.parametrize("damage", ["missing_blob", "missing_hash", "null_ref"])
async def test_malformed_latest_snapshot_falls_back_to_valid_ancestor(store, damage):
    """checkpoints.md §5: load the most recent valid snapshot and replay its deltas."""
    state = await checkpointed_run(store)
    with store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        snapshot = tx.get("graph_checkpoints", head["head_checkpoint_id"])
        assert snapshot["previous_checkpoint_id"] is not None
        if damage == "missing_blob":
            tx.delete("checkpoint_versions", snapshot["engine_state_ref"]["blob_id"])
        elif damage == "missing_hash":
            del snapshot["engine_state_ref"]["sha256"]
        else:
            snapshot["engine_state_ref"] = None
        tx.put("graph_checkpoints", head["head_checkpoint_id"], snapshot)
    restored = store.load_run_consistently("default", state["run_id"])
    assert restored["tasks"] == state["tasks"]
    assert restored["operations"] == state["operations"]
    assert restored["lease"] == state["lease"]


@pytest.mark.parametrize("cut", [None, "admit:", "delegate-start:", "validate:", "consume:"])
async def test_plan_incremental_commit_boundaries_recover_without_duplicate_tools(store, cut):
    """Positive controls: actual Plan nodes survive the new scheduler commit boundaries."""
    effects, cuts = [], []

    def responder(request):
        feedback = [m for m in request.messages if m["role"] == "tool"]
        if request.role_key == "planner":
            if not feedback:
                return ModelResponse(
                    tool_calls=tuple(
                        ToolCall("delegate_task", {"executor": role, "task": "work"})
                        for role in ("worker_a", "worker_b")
                    )
                )
            return ModelResponse("all done")
        if not feedback:
            return ModelResponse(tool_calls=(ToolCall("probe", {"role": request.role_key}),))
        return ModelResponse("worker done")

    def tool(arguments, context):
        effects.append(arguments["role"])
        return "done"

    def checkpoint(command, state):
        if cut is not None and not cuts and command.startswith(cut):
            cuts.append(command)
            raise RuntimeError("simulated process exit after durable commit")

    def plan(runtime):
        return PlanAgent(
            runtime_engine=runtime,
            definition=AgentDefinition("planner", "SYS"),
            workspace=WorkspaceBinding(Path.cwd() / "workspace"),
            model=ScriptedModel(responder),
            capabilities=CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "probe",
                        FunctionToolAdapter(tool),
                        (CapabilitySpec("probe", "probe", "Local probe", {"type": "object"}),),
                    ),
                )
            ),
            executors={
                role: AgentDefinition(
                    role,
                    "SYS",
                    "worker",
                    capabilities=(CapabilityBinding("probe", "tool", "probe", "probe", "probe"),),
                )
                for role in ("worker_a", "worker_b")
            },
            execution_policy=ExecutionPolicy(parallel_enabled=True, executor_max_parallel=1),
        )

    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        handle = await plan(runtime).start("go", run_id="plan-boundary")
        if cut is None:
            result = await handle.wait(timeout_s=10)
        else:
            with pytest.raises(InfrastructureError):
                await handle.wait(timeout_s=10)
            assert len(cuts) == 1
    if cut is not None:
        async with RuntimeEngine(store=store) as recovered:
            result = await (await plan(recovered).start(run_id="plan-boundary")).wait(timeout_s=10)
    state = store.load_run_consistently("default", result.run_id)
    print(
        json.dumps(
            {
                "case": "plan_boundary",
                "cut": cut,
                "effects": effects,
                "status": result.status,
                "task_count": len(state["tasks"]),
            }
        )
    )
    assert result.status == "succeeded" and result.output.value == "all done"
    assert sorted(effects) == ["worker_a", "worker_b"]
    assert len(state["tasks"]) == len(state["task_results"]) == 3
    assert state["usage"]["loops_reserved"] == 0
    assert all(b["delivery_state"] == "consumed" for b in state["batches"].values())
    assert all(
        m["admission_token"] is None for b in state["batches"].values() for m in b["members"]
    )
