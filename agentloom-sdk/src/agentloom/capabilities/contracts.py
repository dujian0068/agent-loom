"""Single-attempt capability extension contracts; no dispatch or retry policy."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..configuration import freeze
from ..errors import ConfigurationError


@dataclass(frozen=True)
class CapabilityRequest:
    operation_id: str
    resource_key: str
    capability_key: str
    arguments: object

    def __post_init__(self):
        object.__setattr__(self, "arguments", freeze(self.arguments))


@dataclass(frozen=True)
class ToolInvocationContext:
    """Bound attempt services, valid only during this invocation.

    report_completion, when supplied by the runtime, accepts a returned ToolResult
    (or error=AgentLoomError) synchronously before adapter-owned resource teardown.
    It validates and commits the outcome under the current lease. It does not
    authorize further work or change stop/deadline semantics; errors propagate.
    A valid error= report is committed and then raised. Its external_outcome
    determines completed/unknown/not_started; execution exceptions are separate.
    The first saved outcome is fixed and the callback expires when dispatch ends.
    """

    content_scope_ref: object
    run_id: str
    graph_execution_id: str
    node_task_id: str | None
    invocation_id: str | None
    session_id: str | None
    role_key: str | None
    execution_binding_ref: object
    binding_config: object
    workspace_scope_ref: object
    operation_id: str
    attempt_id: str
    attempt_no: int
    lease_epoch: int
    deadline_at: float
    authorization_ref: object
    cancellation: object
    workspace: object = field(repr=False)
    check_active: object = field(repr=False)
    spec: object = field(default=None, repr=False)
    report_diagnostic: object = field(default=None, repr=False)
    report_completion: object = field(default=None, repr=False)
    environment_port: object = field(default=None, repr=False)

    def __post_init__(self):
        for key in (
            "content_scope_ref",
            "execution_binding_ref",
            "binding_config",
            "workspace_scope_ref",
            "authorization_ref",
        ):
            object.__setattr__(self, key, freeze(getattr(self, key)))

    @property
    def config(self):
        return self.binding_config

    @property
    def task_id(self):
        return self.node_task_id


@dataclass(frozen=True)
class ContentBlock:
    type: str
    text: str | None = None
    json_ref: object = None
    artifact_ref: object = None

    def __post_init__(self):
        fields = {"text": self.text, "json": self.json_ref, "artifact_ref": self.artifact_ref}
        if (
            not isinstance(self.type, str)
            or self.type not in fields
            or any(value is not None for key, value in fields.items() if key != self.type)
            or self.type == "text"
            and not isinstance(self.text, str)
            or self.type != "text"
            and not isinstance(fields[self.type], dict)
        ):
            raise ConfigurationError("CAPABILITY_RESULT_INVALID", category="protocol")
        object.__setattr__(self, "json_ref", freeze(self.json_ref))
        object.__setattr__(self, "artifact_ref", freeze(self.artifact_ref))

    def to_dict(self):
        key = {"text": "text", "json": "json_ref", "artifact_ref": "artifact_ref"}.get(self.type)
        return {"type": self.type, **({key: getattr(self, key)} if key else {})}


@dataclass(frozen=True)
class ToolResult:
    status: str = "succeeded"
    content: tuple = ()
    error: object = None
    artifacts: tuple = ()
    metadata: object = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self,
            "content",
            freeze(
                [
                    block.to_dict() if isinstance(block, ContentBlock) else block
                    for block in self.content
                ]
            ),
        )
        for key in ("error", "artifacts", "metadata"):
            object.__setattr__(self, key, freeze(getattr(self, key)))

    @classmethod
    def from_value(cls, value):
        """Wrap a legacy JSON/text result without interpreting business keys as status."""
        block = (
            ContentBlock("text", text=value)
            if isinstance(value, str)
            else ContentBlock("json", json_ref={"kind": "inline", "value": value})
        )
        return cls(content=(block,))

    def to_dict(self):
        return {
            key: getattr(self, key)
            for key in ("status", "content", "error", "artifacts", "metadata")
        }


@runtime_checkable
class CapabilityAdapter(Protocol):
    async def invoke_once(
        self, request: CapabilityRequest, context: ToolInvocationContext
    ) -> ToolResult: ...


class ValueToolAdapter:
    """Bridge SDK adapters' existing value-returning implementation to ToolResult."""

    async def invoke_once(self, request, context):
        from .invocation import result_from_value

        value = await self.invoke(request.capability_key, request.arguments, context)
        return result_from_value(value)
