"""Reference round trips, invalid proposals and cleanup failures at real commits."""

import pytest

from agentloom import AgentNode, InfrastructureError, ModelResponse, RuntimeEngine
from agentloom.errors import AgentLoomError, RequestError
from agentloom.nodes import (
    Complete,
    ContinuationRef,
    Fail,
    PayloadRef,
    PreparedEffect,
    Progress,
    Wait,
    WaitCondition,
)
from agentloom.nodes.commands import RuntimeCommit
from agentloom.runtime.agent_engine import AgentGraphEngine
from agentloom.runtime.recovery import validate_recovery
from agentloom.storage.commit import CommitCoordinator


@pytest.mark.parametrize("damage", ["kind", "owner", "missing", "hash", "typed_value", "extra"])
def test_reference_codec_rejects_malformed_or_changed_material(damage):
    saved = PayloadRef("task", True).to_dict()
    if damage == "kind":
        saved["kind"] = "blob"
    elif damage == "owner":
        saved["owner_id"] = "another-task"
    elif damage == "missing":
        del saved["content_hash"]
    elif damage == "hash":
        saved["content_hash"] = "invalid"
    elif damage == "typed_value":
        saved["value"] = 1  # Python equality must not validate a saved boolean digest.
    else:
        saved["unknown"] = "field"
    with pytest.raises(RequestError, match="NODE_REFERENCE_INVALID"):
        PayloadRef.from_dict(saved, owner_id="task")


@pytest.mark.parametrize("mode", ["already_ready", "waiting"])
async def test_resume_reference_survives_takeover(runtime_store, build, monkeypatch, mode):
    observed, cuts = [], []
    activate = AgentNode.activate

    def observe(self, input, continuation, context):
        if input.resume_input_ref is not None:
            observed.append(input.resume_input_ref.to_dict())
        return activate(self, input, continuation, context)

    class ReadyNode:
        async def activate(self, input, continuation, context):
            if continuation.value["phase"] == "prepare_context":
                yield Wait(
                    ContinuationRef(
                        input.task_id,
                        {**continuation.value, "phase": "prepare_model"},
                        continuation.revision,
                    ),
                    WaitCondition("budget_available", context.scope.run_id),
                    PayloadRef(input.task_id, None),
                )
                return
            observed.append(input.resume_input_ref.to_dict())
            yield Complete(
                PayloadRef(input.task_id, "done"),
                ContinuationRef(
                    input.task_id,
                    {**continuation.value, "phase": "completed"},
                    continuation.revision,
                ),
            )

    if mode == "already_ready":
        monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: ReadyNode())
    else:
        monkeypatch.setattr(AgentNode, "activate", observe)

    def cut(command, state):
        if command.startswith("wait:") and not cuts:
            cuts.append(command)
            raise RuntimeError("exit immediately after wait commit")

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=cut) as runtime:
        handle = await build(runtime, lambda r: ModelResponse("done")).start("go", run_id="resume")
        with pytest.raises(InfrastructureError, match="COMMIT_ACKNOWLEDGEMENT_FAILED"):
            await handle.wait(timeout_s=5)
    state = runtime_store.load("default", "resume")
    root = state["tasks"][state["root_task_id"]]
    assert root["status"] == ("ready" if mode == "already_ready" else "waiting")
    saved = root["resume_input_ref"]
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await (
            await build(runtime, lambda r: ModelResponse("done")).start(run_id="resume")
        ).wait(timeout_s=5)
        assert result.status == "succeeded"
    assert observed and all(ref == saved for ref in observed)


@pytest.mark.parametrize(
    "case",
    [
        "bool_version",
        "float_version",
        "bool_loop",
        "future_loop",
        "foreign_binding",
        "foreign_context",
        "bool_context_revision",
        "future_context_revision",
        "negative_context_revision",
        "unknown_field",
        "missing_protocol",
        "missing_phase",
        "missing_model",
        "missing_batch",
        "invalid_pending",
        "negative_watermark",
        "complete_nonterminal",
        "wait_terminal",
    ],
)
async def test_continuation_rejection_rolls_back_effects_and_is_recoverable(
    runtime_store, build, monkeypatch, case
):
    receipts = []

    class Node:
        async def activate(self, input, continuation, context):
            value = {
                **continuation.value,
                "context_revision_ref": dict(continuation.value["context_revision_ref"]),
                "role_loop_index": 1,
            }
            if case in {"bool_version", "float_version"}:
                value["protocol_version"] = True if case == "bool_version" else 1.0
            elif case in {"bool_loop", "future_loop"}:
                value["role_loop_index"] = True if case == "bool_loop" else 2
            elif case == "foreign_binding":
                value["role_binding_ref"] = "foreign-role"
            elif case == "foreign_context":
                value["context_revision_ref"]["context_id"] = "foreign-context"
            elif case in {
                "bool_context_revision",
                "future_context_revision",
                "negative_context_revision",
            }:
                value["context_revision_ref"]["revision"] = {
                    "bool_context_revision": True,
                    "future_context_revision": 999,
                    "negative_context_revision": -1,
                }[case]
            elif case == "unknown_field":
                value["extra"] = "unknown"
            elif case.startswith("missing_") and case in {"missing_protocol", "missing_phase"}:
                del value["protocol_version" if case == "missing_protocol" else "phase"]
            elif case == "missing_model":
                value["model_operation_id"] = "uncommitted-operation"
            elif case == "missing_batch":
                value["pending_batch_id"] = "uncommitted-batch"
            elif case == "invalid_pending":
                value["pending_operation_refs"] = "not-a-reference-list"
            elif case == "negative_watermark":
                value["supplement_delivery_watermark"] = -1
            elif case == "wait_terminal":
                value["phase"] = "completed"
            ref = ContinuationRef(input.task_id, value, continuation.revision)
            effects = (PreparedEffect("format_feedback", {"message": "MUST_NOT_COMMIT"}),)
            if case == "complete_nonterminal":
                outcome = Complete(PayloadRef(input.task_id, "bad"), ref, effects)
            elif case == "wait_terminal":
                outcome = Wait(
                    ref,
                    WaitCondition("budget_available", context.scope.run_id),
                    PayloadRef(input.task_id, None),
                    effects,
                )
            else:
                outcome = Progress(ref, effects)
            receipts.append((yield outcome))

    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, lambda r: pytest.fail("Invalid node must not call model")
        ).ainvoke("go")
        assert result.status == "failed" and result.error["code"] == "NODE_OUTCOME_INVALID"
        assert not receipts
        state = runtime_store.load_run_consistently("default", result.run_id)
        validate_recovery(state)
        task = state["tasks"][state["root_task_id"]]
        assert task["round"] == 0 and "MUST_NOT_COMMIT" not in str(task["context"])


@pytest.mark.parametrize("mode", ["complete", "wait", "fail", "commit_cut", "diagnostic_failure"])
async def test_cleanup_preserves_committed_facts_and_primary_failure(
    runtime_store, build, monkeypatch, mode, caplog
):
    activations = []

    class Node:
        async def activate(self, input, continuation, context):
            activations.append(context.activation_id)
            try:
                if mode == "wait" and continuation.value["phase"] == "prepare_context":
                    yield Wait(
                        ContinuationRef(
                            input.task_id,
                            {**continuation.value, "phase": "prepare_model"},
                            continuation.revision,
                        ),
                        WaitCondition("budget_available", context.scope.run_id),
                        PayloadRef(input.task_id, False),
                    )
                elif mode == "fail":
                    yield Fail(AgentLoomError("EXPECTED_NODE_FAILURE").to_dict())
                else:
                    yield Complete(
                        PayloadRef(input.task_id, "durable-output"),
                        ContinuationRef(
                            input.task_id,
                            {**continuation.value, "phase": "completed"},
                            continuation.revision,
                        ),
                    )
            finally:
                raise ValueError("PRIVATE_CLEANUP_MATERIAL")

    def cut(command, state):
        if mode == "commit_cut" and command.startswith("validate:"):
            raise RuntimeError("primary acknowledgement lost")

    if mode == "diagnostic_failure":
        commit = CommitCoordinator.commit

        def reject(self, command, *args, **kwargs):
            if (
                isinstance(command, RuntimeCommit)
                and command.command_kind == "RecordActivationCleanup"
            ):
                raise InfrastructureError("DIAGNOSTIC_WRITE_FAILED")
            return commit(self, command, *args, **kwargs)

        monkeypatch.setattr(CommitCoordinator, "commit", reject)
    monkeypatch.setattr(AgentGraphEngine, "_node", lambda self, task_id: Node())
    async with RuntimeEngine(store=runtime_store, checkpoint_callback=cut) as runtime:
        handle = await build(runtime, lambda r: ModelResponse("unused")).start("go", run_id="close")
        if mode == "commit_cut":
            with pytest.raises(InfrastructureError, match="COMMIT_ACKNOWLEDGEMENT_FAILED"):
                await handle.wait(timeout_s=5)
        else:
            result = await handle.wait(timeout_s=5)
            assert result.status == ("failed" if mode == "fail" else "succeeded")
            if mode == "fail":
                assert result.error["code"] == "EXPECTED_NODE_FAILURE"
            else:
                assert result.output.value == "durable-output"
        state = runtime_store.load_run_consistently("default", "close")
        assert len(state["task_results"]) == 1
        if mode != "diagnostic_failure":
            assert all(
                a["cleanup_error"]["code"] == "NODE_GENERATOR_CLOSE_FAILED"
                for a in state["activations"].values()
            )
        assert "PRIVATE_CLEANUP_MATERIAL" not in str(state) + caplog.text
    count = len(activations)
    async with RuntimeEngine(store=runtime_store) as recovered:
        result = await (
            await build(recovered, lambda r: ModelResponse("unused")).start(run_id="close")
        ).wait(timeout_s=5)
        assert result.status == ("failed" if mode == "fail" else "succeeded")
    assert len(activations) == count
