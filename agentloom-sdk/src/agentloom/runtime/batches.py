"""Pure batch planning and durable scheduling predicates; no workers or I/O."""

from uuid import NAMESPACE_URL, uuid5

from ..configuration.json_documents import copy_document
from ..errors import InfrastructureError

TASK_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def stable_id(*parts):
    return str(uuid5(NAMESPACE_URL, ":".join(str(p) for p in parts)))


def ready(state, task):
    state.setdefault("graph", {}).setdefault("ready_sequence", 0)
    state["graph"]["ready_sequence"] += 1
    task.update(status="ready", ready_seq=state["graph"]["ready_sequence"], wait_condition=None)


def satisfied(state, condition):
    kind, ref = condition["kind"], condition["ref_id"]
    if condition["generation"] != 1:
        raise InfrastructureError("WAIT_GENERATION_INVALID")
    if kind == "operation_completed":
        op = state["operations"].get(ref)
        return op is not None and op["status"] != "pending"
    if kind == "child_completed":
        return ref in state["tasks"] and state["tasks"][ref]["status"] in TASK_TERMINAL
    if kind == "batch_settled":
        batch = state.get("batches", {}).get(ref)
        return batch is not None and all(m["status"] == "settled" for m in batch["members"])
    if kind == "budget_available":
        return (
            state["usage"]["loops_consumed"] + state["usage"]["loops_reserved"]
            < state["policy"]["loop_max"]
            or not state["usage"]["loops_reserved"]
        )
    raise InfrastructureError("WAIT_CONDITION_INVALID")


def wake_waiters(state):
    for task in state["tasks"].values():
        condition = task.get("wait_condition")
        if task["status"] == "waiting" and condition and satisfied(state, condition):
            ready(state, task)


def freeze_batch(state, task, model_op):
    if not isinstance(model_op, dict) or (
        model_op.get("task_id") != task["task_id"]
        or model_op.get("event_type") != "model.generate"
        or model_op.get("ticket") != "consumed"
        or model_op.get("consumption_kind") != "decision"
        or not isinstance(model_op.get("operation_id"), str)
    ):
        raise InfrastructureError("BATCH_DECISION_TICKET_REQUIRED")
    batch_id = stable_id(task["task_id"], "batch", task["round"])
    batch = {
        "batch_id": batch_id,
        "owner_task_id": task["task_id"],
        "generation": 1,
        "decision_operation_id": model_op["operation_id"],
        "delivery_state": "unconsumed",
        "policy": {k: state["policy"][k] for k in ("parallel_enabled", "executor_max_parallel")},
        "members": [
            {
                "ordinal": i,
                "call": copy_document(call),
                "status": "queued",
                "admission_token": None,
                "operation_id": None,
                "response": None,
            }
            for i, call in enumerate(task["calls"])
        ],
    }
    state.setdefault("batches", {})[batch_id] = batch
    return batch_id


def executor_slots(state):
    active = sum(
        t["parent_operation"] is not None and t["status"] not in TASK_TERMINAL
        for t in state["tasks"].values()
    )
    reserved = sum(
        m["admission_token"] is not None
        for b in state.get("batches", {}).values()
        for m in b["members"]
    )
    return active + reserved


def eligible_members(state, batch):
    pending = [m for m in batch["members"] if m["status"] != "settled"]
    if not batch["policy"]["parallel_enabled"]:
        pending = pending[:1]
    capacity = batch["policy"]["executor_max_parallel"] - executor_slots(state)
    result = []
    for member in pending:
        if member["status"] != "queued":
            continue
        if member["call"]["name"] == "delegate_task":
            if capacity <= 0:
                continue
            capacity -= 1
        result.append(member["ordinal"])
    return result


def settle_operation(state, op_id, response):
    for batch in state.get("batches", {}).values():
        for member in batch["members"]:
            if member["operation_id"] == op_id:
                member.update(
                    status="settled", response=copy_document(response), admission_token=None
                )
    wake_waiters(state)
