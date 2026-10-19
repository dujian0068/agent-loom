"""Shared continuation invariants at the commit and recovery boundaries."""

from collections.abc import Mapping

from ..errors import RequestError
from .contracts import PHASES, PayloadRef, ReactContinuationV1

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
REFERENCE_FIELDS = (
    "current_loop_ticket_id",
    "model_operation_id",
    "pending_batch_id",
    "compaction_ref",
    "projection_ref",
    "accepted_decision_ref",
    "validation_feedback_ref",
    "final_output_ref",
)


def _require(condition, field):
    if not condition:
        raise RequestError("NODE_OUTCOME_INVALID", category="protocol", field_path=field)


def validate_payload(ref, task_id):
    _require(isinstance(ref, PayloadRef), "node.payload_ref")
    try:
        PayloadRef.from_dict(ref.to_dict(), owner_id=task_id)
    except RequestError as exc:
        raise RequestError("NODE_OUTCOME_INVALID", field_path="node.payload_ref") from exc


def validate_continuation(value, task, *, terminal=None, expected_round=None, state=None):
    """Validate a proposal or a committed continuation against its task facts.

    A proposal is checked before effects, using their declared round advance,
    and again against the resulting transaction before any version is published.
    Recovery applies the same rules to the committed task and reference owners.
    """
    _require(isinstance(value, Mapping), "node.continuation")
    _require(not (value.keys() - ReactContinuationV1.__dataclass_fields__.keys()), "node.fields")
    _require(
        type(value.get("protocol_version")) is int and value["protocol_version"] == 1,
        "node.protocol_version",
    )
    _require(isinstance(value.get("phase"), str) and value["phase"] in PHASES, "node.phase")
    _require(
        value.get("role_key") == task["role_key"]
        and value.get("role_binding_ref") == task["role_key"],
        "node.continuation_binding",
    )
    round_index = task["round"] if expected_round is None else expected_round
    _require(
        type(value.get("role_loop_index")) is int
        and value["role_loop_index"] >= 0
        and value["role_loop_index"] == round_index,
        "node.loop_index",
    )
    context = value.get("context_revision_ref")
    _require(
        isinstance(context, Mapping)
        and set(context) == {"context_id", "revision"}
        and context["context_id"] == task["context"]["context_id"]
        and type(context["revision"]) is int
        and 0 <= context["revision"] <= task["context"]["revision"],
        "node.context_binding",
    )
    terminal = task["status"] in TERMINAL if terminal is None else terminal
    _require((value["phase"] == "completed") == terminal, "node.terminal")
    _require(
        type(value.get("supplement_delivery_watermark", 0)) is int
        and value.get("supplement_delivery_watermark", 0) >= 0,
        "node.supplement_delivery_watermark",
    )
    for field in REFERENCE_FIELDS:
        ref = value.get(field)
        _require(ref is None or isinstance(ref, str) and bool(ref), "node." + field)
    pending = value.get("pending_operation_refs", ())
    _require(
        isinstance(pending, (list, tuple))
        and all(isinstance(ref, str) and ref for ref in pending)
        and len(set(pending)) == len(pending),
        "node.pending_operation_refs",
    )
    if state is not None:
        for field in ("model_operation_id", "current_loop_ticket_id", "accepted_decision_ref"):
            ref = value.get(field)
            if ref is not None:
                op = state["operations"].get(ref)
                _require(
                    op is not None
                    and op["task_id"] == task["task_id"]
                    and op["event_type"] == "model.generate",
                    "node." + field,
                )
        for ref in pending:
            op = state["operations"].get(ref)
            _require(
                op is not None and op["task_id"] == task["task_id"],
                "node.pending_operation_refs",
            )
        if value.get("pending_batch_id") is not None:
            batch = state.get("batches", {}).get(value["pending_batch_id"])
            _require(
                batch is not None and batch["owner_task_id"] == task["task_id"],
                "node.pending_batch_id",
            )
