"""Closed node command vocabulary applied only by CommitCoordinator."""

from dataclasses import dataclass

from ..configuration.json_documents import copy_document, document_digest, freeze_document
from ..errors import InfrastructureError, RequestError
from ..runtime.batches import ready, satisfied, wake_waiters
from .contracts import CommitReceipt, PayloadRef
from .effect_validation import require_effect, validate_effect_set
from .validation import validate_continuation


@dataclass(frozen=True)
class RuntimeCommit:
    command_id: str
    command_kind: str
    run_id: str
    task_id: str
    activation_id: str
    expected_node_revision: int
    body: dict
    format_version: int = 1

    def __post_init__(self):
        if (
            type(self.format_version) is not int
            or self.format_version != 1
            or type(self.expected_node_revision) is not int
            or self.expected_node_revision < 0
            or not all(
                isinstance(value, str) and value
                for value in (
                    self.command_id,
                    self.command_kind,
                    self.run_id,
                    self.task_id,
                    self.activation_id,
                )
            )
            or self.command_kind
            not in {
                "ActivateTask",
                "ProgressNode",
                "WaitNode",
                "CompleteTask",
                "FailTask",
                "RecordActivationCleanup",
            }
            or not isinstance(self.body, dict)
        ):
            raise RequestError("RUNTIME_COMMIT_INVALID")
        object.__setattr__(self, "body", freeze_document(self.body))

    @property
    def fingerprint(self):
        return document_digest({name: getattr(self, name) for name in self.__dataclass_fields__})


def apply_node_command(state, command, apply_effect):
    if command.format_version != 1 or command.run_id != state["run_id"]:
        raise RequestError("RUNTIME_COMMIT_INVALID")
    task = state["tasks"][command.task_id]
    if task["node_revision"] != command.expected_node_revision:
        raise RequestError("COMMIT_CONFLICT")
    body = copy_document(command.body)
    kind = command.command_kind
    if kind == "RecordActivationCleanup":
        activation = state.get("activations", {}).get(command.activation_id)
        if activation is None or activation["task_id"] != command.task_id:
            raise RequestError("COMMIT_CONFLICT")
        if body != {"code": "NODE_GENERATOR_CLOSE_FAILED"}:
            raise RequestError("RUNTIME_COMMIT_INVALID")
        activation["cleanup_error"] = body
        return
    if kind == "ActivateTask":
        if task["status"] != "ready":
            raise RequestError("COMMIT_CONFLICT")
        task.update(
            status="running",
            activation_id=command.activation_id,
            activation_no=task["activation_no"] + 1,
        )
        state.setdefault("activations", {})[command.activation_id] = {
            "task_id": command.task_id,
            "status": "running",
            "lease_epoch": state["lease"]["epoch"],
            "start_node_revision": task["node_revision"],
        }
    else:
        if task["activation_id"] != command.activation_id or task["status"] != "running":
            raise RequestError("COMMIT_CONFLICT")
        if kind not in {"ProgressNode", "WaitNode", "CompleteTask", "FailTask"}:
            raise RequestError("RUNTIME_COMMIT_INVALID")
        effects = body.get("effects", [])
        if not isinstance(effects, list) or any(
            not isinstance(effect, dict)
            or set(effect) != {"kind", "body"}
            or not isinstance(effect["kind"], str)
            or not isinstance(effect["body"], dict)
            for effect in effects
        ):
            raise RequestError("NODE_OUTCOME_INVALID")
        validate_effect_set(effects)
        if kind != "FailTask":
            advance = sum(e["kind"] in {"consume_batch", "format_feedback"} for e in effects)
            validate_continuation(
                body.get("continuation"),
                task,
                terminal=kind == "CompleteTask",
                expected_round=task["round"] + advance,
            )
        elif effects:
            raise RequestError("NODE_OUTCOME_INVALID")
        if kind == "WaitNode":
            PayloadRef.from_dict(body.get("resume_input_ref"), owner_id=command.task_id)
        initial_revision = task["context"]["revision"]
        for effect in effects:
            expected = effect["body"].get("expected_context_revision")
            if task["context"]["revision"] != initial_revision and expected is not None:
                # A prior effect in this transaction changed the context. A stale
                # later expectation is an invalid proposal, not external contention.
                require_effect(expected == task["context"]["revision"])
            apply_effect(state, task, effect)
        if kind != "FailTask":
            validate_continuation(
                body["continuation"], task, terminal=kind == "CompleteTask", state=state
            )
            task["continuation"] = body["continuation"]
            task["continuation"]["context_revision_ref"] = {
                "context_id": task["context"]["context_id"],
                "revision": task["context"]["revision"],
            }
        if kind == "WaitNode":
            condition = body["condition"]
            task.update(
                status="waiting",
                wait_condition=condition,
                resume_input_ref=body["resume_input_ref"],
            )
            if satisfied(state, condition):
                ready(state, task)
        if kind in {"CompleteTask", "FailTask"}:
            if command.task_id in state.get("task_results", {}):
                raise RequestError("IDEMPOTENCY_CONFLICT")
            if body.get("partial_result_ref") is not None:
                state["partial_results"].append(
                    {
                        "task_id": command.task_id,
                        "partial_result_ref": body["partial_result_ref"],
                        "visibility": "internal",
                    }
                )
            if body.get("accounting") is not None:
                accounting = body["accounting"]
                if (
                    set(accounting) != {"operation_id", "consumption_kind"}
                    or accounting["operation_id"] != task["continuation"].get("model_operation_id")
                    or accounting["consumption_kind"] != "error_feedback"
                    or not task["parent_operation"]
                ):
                    raise RequestError("NODE_OUTCOME_INVALID")
            if kind == "FailTask":
                from .effects import settle_failed_task_model

                settle_failed_task_model(state, task)
            task.update(
                status="succeeded" if kind == "CompleteTask" else "failed",
                stage="completed",
                result=body.get("output"),
                error=body.get("error"),
                wait_condition=None,
            )
            task["continuation"]["phase"] = "completed"
            state.setdefault("task_results", {})[command.task_id] = {
                "task_id": command.task_id,
                "status": task["status"],
                "result": task["result"],
                "error": task["error"],
            }
            if task["parent_operation"]:
                from ..runtime.completion import failure, success

                op = state["operations"][task["parent_operation"]]
                raw = success(task["result"]) if kind == "CompleteTask" else failure(task["error"])
                op.update(raw=raw, response=copy_document(raw), phase="after")
        if kind != "ProgressNode":
            state["activations"][command.activation_id]["status"] = "completed"
    task["node_revision"] += 1
    wake_waiters(state)


def submit(coordinator, command, apply_effect):
    """Receipt replay never reapplies an effect, including after lost acknowledgements."""
    coordinator.commit(
        command.command_id,
        lambda s: apply_node_command(s, command, apply_effect),
        fingerprint=command.fingerprint,
        receipt_task_id=command.task_id,
    )
    with coordinator.store.transaction(coordinator.namespace) as tx:
        saved = tx.get("commits", command.run_id + ":" + command.command_id)
    if saved is None:
        raise InfrastructureError("COMMIT_RECEIPT_MISSING")
    # Store the exact receipt values, not a later task head, for idempotent replay.
    versions = saved.get("entity_versions") or [
        {"task_id": command.task_id, "node_revision": command.expected_node_revision + 1}
    ]
    return CommitReceipt(
        command.command_id,
        command.fingerprint,
        saved["committed_at"],
        saved["revision"],
        tuple(saved["scope_versions"]),
        tuple(versions),
        saved["allocated_refs"],
        tuple(saved["public_event_seqs"]),
    )
