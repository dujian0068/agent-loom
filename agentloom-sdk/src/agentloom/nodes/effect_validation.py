"""Validate node proposals before deriving command IDs or touching durable state."""

from collections.abc import Mapping

from ..errors import RequestError
from ..runtime.batches import stable_id

_FIELDS = {
    "prepare_context": set(),
    "begin_model": {"operation"},
    "accept_model": {"operation_id"},
    "consume_batch": {"batch_id"},
    "format_feedback": {"message"},
}


def require_effect(condition):
    if not condition:
        raise RequestError("NODE_EFFECT_INVALID", category="protocol")


def validate_effect(effect):
    require_effect(isinstance(effect, Mapping) and set(effect) == {"kind", "body"})
    kind, body = effect["kind"], effect["body"]
    require_effect(isinstance(kind, str) and kind in _FIELDS and isinstance(body, Mapping))
    fields = _FIELDS[kind]
    require_effect(
        fields <= body.keys() and not body.keys() - fields - {"expected_context_revision"}
    )
    if "expected_context_revision" in body:
        require_effect(
            type(body["expected_context_revision"]) is int
            and body["expected_context_revision"] >= 0
        )
    for key in fields - {"operation"}:
        require_effect(isinstance(body[key], str) and bool(body[key]))
    if kind == "begin_model":
        op = body["operation"]
        require_effect(isinstance(op, Mapping))
        for key in ("operation_id", "task_id", "role_key", "purpose_key"):
            require_effect(isinstance(op.get(key), str) and bool(op[key]))


def validate_model_intent(state, task, op):
    """Only a pristine, task-owned model intent may enter the operation ledger."""
    required = {
        "operation_id",
        "task_id",
        "role_key",
        "purpose_key",
        "purpose",
        "event_type",
        "request",
        "request_hash",
        "response",
        "status",
        "phase",
        "raw",
        "stages",
        "hooks",
        "attempts",
        "ticket",
        "permit",
        "target_ref",
        "binding_key",
        "source_call_id",
        "next_attempt_at",
        "output_reserve",
    }
    require_effect(set(op) == required)
    require_effect(op["task_id"] == task["task_id"] and op["role_key"] == task["role_key"])
    require_effect(
        op["operation_id"] == stable_id(state["run_id"], task["task_id"], op["purpose_key"])
    )
    require_effect(op["event_type"] == "model.generate" and op["purpose"] == "business")
    require_effect(op["status"] == "pending" and op["phase"] == "before")
    require_effect(isinstance(op["request"], Mapping))
    request = op["request"]
    require_effect(
        set(request) == {"messages", "record_ids", "tools", "max_output_tokens", "parameters"}
    )
    require_effect(
        isinstance(request["messages"], list)
        and isinstance(request["record_ids"], list)
        and len(request["messages"]) == len(request["record_ids"])
    )
    require_effect(all(isinstance(ref, str) for ref in request["record_ids"]))
    require_effect(
        all(
            isinstance(message, Mapping) and isinstance(message.get("role"), str)
            for message in request["messages"]
        )
    )
    require_effect(
        isinstance(request["tools"], list) and isinstance(request["parameters"], Mapping)
    )
    require_effect(type(request["max_output_tokens"]) is int and request["max_output_tokens"] > 0)
    require_effect(op["stages"] == {} and op["hooks"] == {} and op["attempts"] == [])
    require_effect(type(op["output_reserve"]) is int and op["output_reserve"] > 0)
    require_effect(
        all(
            op[k] is None
            for k in (
                "request_hash",
                "response",
                "raw",
                "ticket",
                "permit",
                "target_ref",
                "binding_key",
                "source_call_id",
                "next_attempt_at",
            )
        )
    )
