"""Serializable node handoff contracts. No runtime clients enter these values."""

from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ..configuration.json_documents import copy_document, document_digest, freeze_document
from ..errors import InfrastructureError, RequestError

PHASES = frozenset(
    {
        "prepare_context",
        "prepare_model",
        "compact_context",
        "call_model",
        "interpret_response",
        "wait_batch",
        "consume_results",
        "validate_output",
        "completed",
    }
)


@dataclass(frozen=True)
class PayloadRef:
    """Owner-bound JSON material; the coordinator persists its immutable version."""

    owner_id: str
    value: Any = field(repr=False)
    content_hash: str = field(init=False)

    def __post_init__(self):
        if not isinstance(self.owner_id, str) or not self.owner_id:
            raise RequestError("NODE_REFERENCE_INVALID")
        object.__setattr__(self, "value", freeze_document(self.value))
        object.__setattr__(self, "content_hash", document_digest(self.value))

    @classmethod
    def from_dict(cls, saved, *, owner_id):
        """Restore an inline reference without wrapping its envelope as material.

        The envelope, owner and digest must match; a null value is still a
        present reference. No other reference kind is supported by this codec.
        """
        if (
            not isinstance(saved, Mapping)
            or set(saved) != {"kind", "owner_id", "content_hash", "value"}
            or saved["kind"] != "inline"
            or saved["owner_id"] != owner_id
        ):
            raise RequestError("NODE_REFERENCE_INVALID")
        ref = cls(saved["owner_id"], saved["value"])
        if ref.content_hash != saved["content_hash"]:
            raise RequestError("NODE_REFERENCE_INVALID")
        return ref

    def to_dict(self):
        return {
            "kind": "inline",
            "owner_id": self.owner_id,
            "content_hash": self.content_hash,
            "value": copy_document(self.value),
        }


@dataclass(frozen=True)
class ContinuationRef(PayloadRef):
    revision: int = 0


@dataclass(frozen=True)
class ReactContinuationV1:
    phase: str
    role_key: str
    role_binding_ref: str
    context_revision_ref: dict
    role_loop_index: int = 0
    protocol_version: int = 1
    current_loop_ticket_id: str | None = None
    model_operation_id: str | None = None
    pending_batch_id: str | None = None
    pending_operation_refs: tuple[str, ...] = ()
    compaction_ref: str | None = None
    supplement_delivery_watermark: int = 0
    projection_ref: str | None = None
    accepted_decision_ref: str | None = None
    validation_feedback_ref: str | None = None
    final_output_ref: str | None = None

    def __post_init__(self):
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise InfrastructureError("NODE_PROTOCOL_VERSION_UNSUPPORTED")
        if self.phase not in PHASES or type(self.role_loop_index) is not int:
            raise InfrastructureError("CONTINUATION_INVALID")
        object.__setattr__(self, "context_revision_ref", freeze_document(self.context_revision_ref))
        object.__setattr__(self, "pending_operation_refs", tuple(self.pending_operation_refs))

    def to_dict(self):
        return {name: copy_document(getattr(self, name)) for name in self.__dataclass_fields__}

    def advance(self, **changes):
        return replace(self, **changes)


@dataclass(frozen=True)
class InvocationScopeRef:
    run_id: str
    graph_id: str
    task_id: str
    role_key: str


@dataclass(frozen=True)
class NodeInput:
    task_id: str
    input_ref: PayloadRef
    resume_input_ref: PayloadRef | None = None


@dataclass(frozen=True)
class WaitCondition:
    kind: Literal["operation_completed", "batch_settled", "budget_available", "child_completed"]
    ref_id: str
    generation: int = 1
    observed_revision: int = 0

    def to_dict(self):
        return dict(vars(self))


@dataclass(frozen=True)
class PreparedEffect:
    """A registered effect name and JSON body, never a callable mutation."""

    kind: str
    body: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "body", freeze_document(self.body))

    def to_dict(self):
        return {"kind": self.kind, "body": copy_document(self.body)}


@dataclass(frozen=True)
class PreparedTaskWrite(PreparedEffect):
    """A registered task write committed with the unique TaskResult."""


@dataclass(frozen=True)
class RuntimeErrorRecord:
    code: str
    message: str
    scope_kind: Literal["run", "invocation", "task"]
    scope_id: str
    source_operation_id: str | None = None
    details_ref: PayloadRef | None = None

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "source_operation_id": self.source_operation_id,
            "details_ref": self.details_ref.to_dict() if self.details_ref else None,
        }


@dataclass(frozen=True)
class Progress:
    continuation: ContinuationRef
    effects: tuple[PreparedEffect, ...] = ()


@dataclass(frozen=True)
class Wait:
    continuation: ContinuationRef
    condition: WaitCondition
    resume_input_ref: PayloadRef
    effects: tuple[PreparedEffect, ...] = ()


@dataclass(frozen=True)
class Complete:
    output_ref: PayloadRef
    continuation: ContinuationRef
    writes: tuple[PreparedTaskWrite, ...] = ()


@dataclass(frozen=True)
class Fail:
    error: RuntimeErrorRecord | dict
    partial_result_ref: PayloadRef | None = None
    accounting: dict | None = None

    def __post_init__(self):
        object.__setattr__(
            self,
            "error",
            freeze_document(
                self.error.to_dict() if isinstance(self.error, RuntimeErrorRecord) else self.error
            ),
        )


@dataclass(frozen=True)
class CommitReceipt:
    command_id: str
    fingerprint: str
    committed_at: float
    run_revision: int
    scope_versions: tuple[dict, ...]
    entity_versions: tuple[dict, ...]
    allocated_refs: dict
    public_event_seqs: tuple[int, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "scope_versions", freeze_document(self.scope_versions))
        object.__setattr__(self, "entity_versions", freeze_document(self.entity_versions))
        object.__setattr__(self, "allocated_refs", freeze_document(self.allocated_refs))
        object.__setattr__(self, "public_event_seqs", tuple(self.public_event_seqs))


@dataclass(frozen=True)
class ActivationReceipt:
    task_id: str
    activation_id: str
    status: str
    commit_receipt: CommitReceipt


@dataclass(frozen=True)
class NodeContext:
    """Services restricted to one activation; it neither owns nor exposes a Runner."""

    scope: InvocationScopeRef
    activation_id: str
    lease: Any
    context_read_authority: Any
    control: Any
    model_port: Any
    capability_port: Any
    context_port: Any
    environment_port: Any
    delegation_port: Any = None

    @property
    def task_id(self):
        return self.scope.task_id


NodeOutcome = Progress | Wait | Complete | Fail
ActivationStream = AsyncGenerator[NodeOutcome, CommitReceipt]


def continuation_ref(task):
    return ContinuationRef(task["task_id"], task["continuation"], task.get("node_revision", 0))


def validate_binding(input, continuation, context):
    context.control.check()
    if (
        not isinstance(input, NodeInput)
        or input.task_id != context.task_id
        or input.input_ref.owner_id != context.task_id
        or continuation is not None
        and continuation.owner_id != context.task_id
    ):
        raise RequestError("NODE_BINDING_MISMATCH")
