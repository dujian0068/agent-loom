"""Immutable parameter groups; no clients, runtime state or filesystem access."""

import math
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigurationError
from .models import ContextPolicy, ExecutionPolicy, ModelOptions, WorkspaceBinding, number

DEFAULT_STATE_DIRECTORY = ".agentloom/state"
DEFAULT_STORE_LOCK_TIMEOUT_SECONDS = 10.0


class _Unset:
    def __repr__(self):
        return "UNSET"


UNSET = _Unset()


def store_lock_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigurationError("STORE_LOCK_TIMEOUT_INVALID")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ConfigurationError("STORE_LOCK_TIMEOUT_INVALID") from exc
    if not math.isfinite(converted):
        raise ConfigurationError("STORE_LOCK_TIMEOUT_INVALID")
    return converted


@dataclass(frozen=True, kw_only=True)
class RuntimeProperties:
    """Host parameters, independent of every Agent/Run and borrowed service.

    state_directory and store_lock_timeout_seconds configure only the default
    FileRuntimeStore. An explicitly injected store uses its own configuration.
    Relative paths are fixed at construction, without creating directories.
    """

    namespace: str = "default"
    state_directory: Path | str = DEFAULT_STATE_DIRECTORY
    store_lock_timeout_seconds: float = DEFAULT_STORE_LOCK_TIMEOUT_SECONDS
    lease_seconds: float = 30
    cleanup_retry_seconds: float = 30

    def __post_init__(self):
        if not isinstance(self.namespace, str) or not self.namespace.strip():
            raise ConfigurationError("STORAGE_NAMESPACE_INVALID")
        number(self.lease_seconds, "lease_seconds", 0, 3600, open_low=True)
        number(self.cleanup_retry_seconds, "cleanup_retry_seconds", 0, 3600, open_low=True)
        if (
            not isinstance(self.state_directory, (str, Path))
            or not str(self.state_directory)
            or "\0" in str(self.state_directory)
        ):
            raise ConfigurationError("STATE_DIRECTORY_INVALID", field_path="state_directory")
        object.__setattr__(self, "state_directory", Path(self.state_directory).absolute())
        object.__setattr__(
            self, "store_lock_timeout_seconds", store_lock_timeout(self.store_lock_timeout_seconds)
        )


@dataclass(frozen=True, kw_only=True)
class AgentProperties:
    """Fixed Agent parameters shared as defaults or replaced for one assembly.

    workspace is required. Optional None values select SDK defaults. Reuses the
    existing policy value objects; input, variables and Run state do not belong here.
    """

    workspace: WorkspaceBinding
    execution_policy: ExecutionPolicy | None = None
    context_policy: ContextPolicy | None = None
    model_options: ModelOptions | None = None

    def __post_init__(self):
        if not isinstance(self.workspace, WorkspaceBinding):
            raise ConfigurationError("WORKSPACE_BINDING_REQUIRED")
        for name, expected in (
            ("execution_policy", ExecutionPolicy),
            ("context_policy", ContextPolicy),
            ("model_options", ModelOptions),
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected):
                raise ConfigurationError("INVALID_CONFIGURATION", field_path=expected.__name__)
            object.__setattr__(self, name, expected() if value is None else value)


def resolve_properties(value, expected, **legacy):
    """Adapt legacy keyword constructors once; never silently mix both sources."""
    supplied = {key: item for key, item in legacy.items() if item is not UNSET}
    if value is not None:
        if not isinstance(value, expected):
            raise ConfigurationError("INVALID_CONFIGURATION", field_path=expected.__name__)
        if supplied:
            raise ConfigurationError("PARAMETERS_CONFLICT", field_path=", ".join(sorted(supplied)))
        return value
    if expected is AgentProperties and "workspace" not in supplied:
        raise ConfigurationError("WORKSPACE_BINDING_REQUIRED")
    return expected(**supplied)
