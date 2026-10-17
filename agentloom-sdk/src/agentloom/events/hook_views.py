"""Immutable values at the user Hook boundary, independent of dispatch events."""

from dataclasses import InitVar, dataclass

from ..configuration.json_documents import freeze_document as freeze
from ..configuration.json_values import FrozenDict


class _ReadView(FrozenDict):
    """A JSON-compatible read-only DTO supporting both keys and attributes."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        raise TypeError("Hook views are immutable")

    def __delattr__(self, key):
        raise TypeError("Hook views are immutable")


class HookRecordView(_ReadView):
    def __init__(
        self, record_id, context_seq, role, message_kind, effective_payload, visible_fields
    ):
        super().__init__(
            freeze(
                dict(
                    record_id=record_id,
                    context_seq=context_seq,
                    role=role,
                    message_kind=message_kind,
                    effective_payload=effective_payload,
                    visible_fields=visible_fields,
                )
            )
        )


class HookRecordPage(_ReadView):
    def __init__(self, records, next_seq):
        super().__init__(records=tuple(records), next_seq=next_seq)


class HookContext(_ReadView):
    """Trusted metadata fixed when a Hook invocation is prepared."""

    def __init__(
        self,
        hook_invocation_id,
        operation_id,
        phase,
        scope_ref,
        deadline_at,
        input_revision,
        input_context_refs,
        mapping_version,
        *,
        run_id=None,
        task_id=None,
        role_key=None,
        purpose=None,
        context_ref=None,
    ):
        super().__init__(
            freeze(
                dict(
                    hook_invocation_id=hook_invocation_id,
                    operation_id=operation_id,
                    phase=phase,
                    scope_ref=scope_ref,
                    deadline_at=deadline_at,
                    input_revision=input_revision,
                    input_context_refs=input_context_refs,
                    mapping_version=mapping_version,
                    run_id=run_id,
                    task_id=task_id,
                    role_key=role_key,
                    purpose=purpose,
                    context_ref=context_ref,
                )
            )
        )


@dataclass(frozen=True)
class HookInput:
    event_type: str
    stage: str
    payload_ref: dict
    context_refs: tuple
    _context: InitVar[HookContext | None] = None

    def __post_init__(self, _context):
        object.__setattr__(self, "payload_ref", freeze(self.payload_ref))
        object.__setattr__(self, "context_refs", freeze(self.context_refs))
        object.__setattr__(self, "_metadata", _context)

    # Compatibility accessors for the initial Python API; all are read-only
    # projections of the same fixed input, never the live Operation.
    @property
    def payload(self):
        return self.payload_ref["value"]

    @property
    def operation_id(self):
        return self._metadata.operation_id

    @property
    def role_key(self):
        return self._metadata.role_key

    @property
    def purpose(self):
        return self._metadata.purpose


@dataclass(frozen=True)
class HookCallContext:
    reader: object
    metadata: HookContext
