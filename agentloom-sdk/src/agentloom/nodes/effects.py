"""Registered node effects. Context and batch consumption share one commit."""

from ..configuration.json_documents import canonical_document, copy_document
from ..context import append
from ..context.results import project_result
from ..errors import InfrastructureError, RequestError
from ..runtime.batches import freeze_batch


def assistant_message(response):
    message = {"role": "assistant", "content": response["text"]}
    if response.get("provider_state"):
        message["provider_state"] = response["provider_state"]
    if response["tool_calls"]:
        message["tool_calls"] = [
            {
                "id": c["call_id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": c["arguments"]
                    if isinstance(c["arguments"], str)
                    else canonical_document(c["arguments"]),
                },
            }
            for c in response["tool_calls"]
        ]
    return message


def apply_effect(state, task, effect):
    kind, body = effect["kind"], effect["body"]
    expected = body.get("expected_context_revision")
    if expected is not None and expected != task["context"]["revision"]:
        raise RequestError("COMMIT_CONFLICT")
    if kind == "prepare_context":
        task["stage"] = "call_model"
        if task["task_id"] == state["root_task_id"]:
            for item in sorted(state["inputs"], key=lambda i: i["session_seq"]):
                if item.get("record_id") is None and item["delivery_state"] == "pending":
                    entry = append(
                        task["context"],
                        {"role": "user", "content": item["text"]},
                        protected=True,
                        fixed=True,
                    )
                    item["record_id"] = entry["record_id"]
    elif kind == "begin_model":
        op = copy_document(body["operation"])
        if op["task_id"] != task["task_id"] or op["event_type"] != "model.generate":
            raise RequestError("NODE_EFFECT_INVALID")
        if op["operation_id"] in state["operations"]:
            raise RequestError("IDEMPOTENCY_CONFLICT")
        state["operations"][op["operation_id"]] = op
    elif kind == "accept_model":
        op = state["operations"][body["operation_id"]]
        if op["task_id"] != task["task_id"] or op["status"] != "succeeded":
            raise RequestError("NODE_EFFECT_INVALID")
        consume_ticket(state, op, "decision")
        candidate = op["response"]["result"]
        append(
            task["context"],
            assistant_message(candidate),
            raw=assistant_message(op["raw"]["result"]),
        )
        task.update(
            stage="batch" if candidate["tool_calls"] else "validate",
            calls=copy_document(candidate["tool_calls"]),
            candidate=candidate["text"],
        )
        if task["calls"]:
            freeze_batch(state, task, op)
    elif kind == "consume_batch":
        batch = state["batches"][body["batch_id"]]
        if (
            batch["owner_task_id"] != task["task_id"]
            or batch["delivery_state"] != "unconsumed"
            or any(m["status"] != "settled" for m in batch["members"])
        ):
            raise InfrastructureError("BATCH_CONSUMPTION_INVALID")
        for member in batch["members"]:
            call, response = member["call"], member["response"]
            op = state["operations"].get(response.get("operation_id"))
            append(
                task["context"],
                {
                    "role": "tool",
                    "tool_call_id": call["call_id"],
                    "content": canonical_document(
                        response
                        if op is None or op["event_type"] == "result.read"
                        else project_result(response, state["run_id"])
                    ),
                },
                raw={
                    "role": "tool",
                    "tool_call_id": call["call_id"],
                    "content": canonical_document((op["raw"] if op else None) or response),
                },
            )
        batch["delivery_state"] = "consumed"
        task.setdefault("consumed_result_ids", []).append(batch["batch_id"])
        task.update(stage="prepare", round=task["round"] + 1, calls=[])
    elif kind == "format_feedback":
        append(task["context"], {"role": "user", "content": body["message"]}, kind="format")
        task.update(stage="prepare", round=task["round"] + 1)
    else:
        raise RequestError("NODE_EFFECT_INVALID")


def consume_ticket(state, op, kind):
    if op["ticket"] == "consumed":
        if op.get("consumption_kind") != kind:
            raise InfrastructureError("LOOP_CONSUMPTION_INVALID")
        return  # supported legacy checkpoints already consumed at response commit
    if op["ticket"] != "reserved":
        raise InfrastructureError("LOOP_TICKET_REQUIRED")
    op.update(ticket="consumed", consumption_kind=kind)
    state["usage"]["loops_reserved"] -= 1
    state["usage"]["loops_consumed"] += 1
