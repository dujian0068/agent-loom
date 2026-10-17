"""Public transport values and structural interfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable
from uuid import uuid4

from .capabilities.contracts import CapabilityAdapter as CapabilityAdapter
from .configuration import freeze, number
from .errors import ConfigurationError, RequestError


def new_id():
    return str(uuid4())


@dataclass(frozen=True)
class AgentInput:
    content: Any
    variables: dict = field(default_factory=dict)
    artifacts: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "content", freeze(self.content))
        object.__setattr__(self, "variables", freeze(self.variables))


@dataclass(frozen=True)
class ModelDescriptor:
    provider_model_id: str
    context_window_tokens: int
    max_output_tokens: int | None = None
    supported_features: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.provider_model_id, str) or not self.provider_model_id:
            raise ConfigurationError("MODEL_CONFIGURATION_INVALID")
        number(self.context_window_tokens, "context_window_tokens", 1, 10**9, integer=True)
        if self.max_output_tokens is not None:
            number(self.max_output_tokens, "model.max_output_tokens", 1, 10**9, integer=True)
        object.__setattr__(self, "supported_features", tuple(self.supported_features))


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Any
    call_id: str = field(default_factory=new_id)

    def __post_init__(self):
        object.__setattr__(self, "arguments", freeze(self.arguments))

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict | None = None
    finish_reason: str | None = None
    provider_state: dict = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(self, "usage", freeze(self.usage))
        object.__setattr__(self, "provider_state", freeze(self.provider_state))

    def to_dict(self):
        value = {
            "text": self.text,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "usage": self.usage,
            "finish_reason": self.finish_reason,
        }
        if self.provider_state:
            value["provider_state"] = self.provider_state
        return value


@dataclass(frozen=True)
class ProviderEvent:
    kind: str
    data: Any = None
    provider_index: int | None = None
    provider_call_id: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict, ...]
    tools: tuple[dict, ...]
    role_key: str
    purpose: str = "business"
    operation_id: str = ""
    max_output_tokens: int | None = None
    parameters: dict = field(default_factory=dict)

    def __post_init__(self):
        from .configuration.json_documents import freeze_document

        for name in ("messages", "tools", "parameters"):
            object.__setattr__(self, name, freeze_document(getattr(self, name)))


@dataclass(frozen=True)
class FinalOutput:
    kind: str
    value: Any
    source_ref: Any = None

    def __post_init__(self):
        object.__setattr__(self, "value", freeze(self.value))
        object.__setattr__(self, "source_ref", freeze(self.source_ref))


@dataclass(frozen=True)
class RunUsage:
    loops_consumed: int = 0
    loops_reserved: int = 0
    business_model_attempts: int = 0
    compaction_model_attempts: int = 0
    tool_attempts: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_counts_complete: bool = False


@dataclass(frozen=True)
class RunResult:
    run_id: str
    session_id: str | None
    status: str
    output: FinalOutput | None
    error: dict | None
    partial_results: tuple
    usage: RunUsage
    started_at: float | None
    ended_at: float
    cleanup_state: str
    schema_version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "error", freeze(self.error))
        object.__setattr__(self, "partial_results", freeze(self.partial_results))


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    run_id: str
    seq: int
    type: str
    payload: dict
    committed_at: float
    schema_version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "payload", freeze(self.payload))


@dataclass(frozen=True)
class CallStageEvent:
    operation_id: str
    event_type: str
    stage: str
    payload: dict
    purpose: str = "business"
    role_key: str | None = None
    source: str = "agent-loop"


@runtime_checkable
class Agent(Protocol):
    async def start(self, input=None, **ids): ...
    async def ainvoke(self, input=None, **ids): ...
    def astream(self, input=None, **ids): ...


@runtime_checkable
class Node(Protocol):
    async def activate(self, input, continuation, node_context): ...


@dataclass(frozen=True)
class NodeContext:
    task_id: str
    driver: Any


@dataclass(frozen=True)
class AgentContext:
    context_id: str
    task_id: str
    _reader: Callable | None = field(default=None, repr=False, compare=False)

    def snapshot(self):
        """Read the owning task's committed context; never expose other roles."""
        if self._reader is None:
            raise RequestError("CONTEXT_NOT_ATTACHED")
        task = self._reader()
        if task is None or task["context"]["context_id"] != self.context_id:
            raise RequestError("CONTEXT_NOT_FOUND")
        from .configuration.json_documents import freeze_document

        return freeze_document(
            {
                **task["context"],
                "task_id": self.task_id,
                "progress": {key: task[key] for key in ("stage", "round", "calls", "status")},
            }
        )

    @property
    def messages(self):
        from .configuration.json_documents import freeze_document
        from .context.records import materialize

        return freeze_document([record["message"] for record in materialize(self.snapshot())])

    @property
    def revision(self):
        return self.snapshot()["revision"]

    @property
    def progress(self):
        return self.snapshot()["progress"]


@runtime_checkable
class ModelAdapter(Protocol):
    descriptor: ModelDescriptor

    def stream_once(self, request, context): ...
