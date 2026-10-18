"""One startup parameter entry, with fixed values and explicitly borrowed services."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable, TypedDict, Unpack

from ..capabilities import CapabilityRegistry
from ..configuration import AgentProperties, RuntimeProperties
from ..errors import ConfigurationError
from ..events import HookSet
from ..workspace.properties import FileProperties
from .assembly import resolve_model_budget, validate_model_adapter

if TYPE_CHECKING:
    from ..storage import RuntimeStore
    from ..types import ModelAdapter


@dataclass(frozen=True, kw_only=True, eq=False, repr=False)
class ServiceProperties:
    """Borrowed model, resource catalog and host services supplied at startup.

    model is required. None selects an empty capabilities/HookSet, the default
    file store or local environment. Only the host-created store is owned by the
    host. Adapter instances are shared references, not copied or closed here;
    they must support the caller's concurrency. Validation never invokes them.
    checkpoint_callback is the existing synchronous durable-boundary observer.
    """

    model: ModelAdapter
    capabilities: CapabilityRegistry | None = None
    hooks: HookSet | None = None
    store: RuntimeStore | None = None
    environment: object | None = None
    checkpoint_callback: Callable | None = field(default=None, repr=False)

    def __post_init__(self):
        validate_model_adapter(self.model)
        for name, expected in (("capabilities", CapabilityRegistry), ("hooks", HookSet)):
            value = getattr(self, name)
            if value is not None and not isinstance(value, expected):
                raise ConfigurationError("INVALID_CONFIGURATION", field_path=expected.__name__)
            object.__setattr__(self, name, expected() if value is None else value)
        object.__setattr__(self, "hooks", HookSet(self.hooks.bindings, self.hooks.handlers))
        for value, methods, code in (
            (self.store, ("transaction", "load", "read_path"), "RUNTIME_STORE_INVALID"),
            (self.environment, ("prepare", "revoke", "cleanup"), "ENVIRONMENT_INVALID"),
        ):
            if value is not None and not all(
                callable(getattr(value, method, None)) for method in methods
            ):
                raise ConfigurationError(code)
        if self.checkpoint_callback is not None and (
            not callable(self.checkpoint_callback)
            or inspect.iscoroutinefunction(self.checkpoint_callback)
            or inspect.iscoroutinefunction(getattr(self.checkpoint_callback, "__call__", None))
        ):
            raise ConfigurationError("CHECKPOINT_CALLBACK_INVALID")


class _PropertiesOverrides(TypedDict, total=False):
    runtime: RuntimeProperties
    defaults: AgentProperties
    services: ServiceProperties
    files: FileProperties


@dataclass(frozen=True, kw_only=True, eq=False, repr=False)
class PropertiesConfig:
    """Complete, immutable startup parameters for one AgentLoom instance.

    Args:
        runtime: Host parameters; defaults to local file persistence.
        defaults: Agent parameter defaults, including the required workspace.
        services: Borrowed adapters, including the required model.
        files: Installed file provider name and optional constructor parameters.

    Raises:
        ConfigurationError: Invalid group, unsupported model options or an
            incompatible model window/budget. No directories or clients open.

    Run input, variables, IDs, leases and messages remain execution-scoped. This
    object is not a global locator or a serialization format: live service refs
    never enter checkpoints. Built Agents retain their own fixed parameters.
    """

    defaults: AgentProperties
    services: ServiceProperties
    runtime: RuntimeProperties = field(default_factory=RuntimeProperties)
    files: FileProperties = field(default_factory=FileProperties)

    def __post_init__(self):
        for name, expected in (
            ("runtime", RuntimeProperties),
            ("defaults", AgentProperties),
            ("services", ServiceProperties),
            ("files", FileProperties),
        ):
            if not isinstance(getattr(self, name), expected):
                raise ConfigurationError("INVALID_CONFIGURATION", field_path=name)
        resolve_model_budget(self.services.model, self.defaults)

    def with_options(self, **overrides: Unpack[_PropertiesOverrides]) -> PropertiesConfig:
        """Return a validated copy with whole groups replaced, leaving this one fixed.

        Unspecified groups are inherited; None is invalid for a required group.
        Use dataclasses.replace on a group for selected fields, or a bound
        AgentBuilder.with_options for one Agent's overrides. Unknown keys raise
        TypeError; invalid combined parameters raise ConfigurationError.
        """
        unknown = overrides.keys() - {"runtime", "defaults", "services", "files"}
        if unknown:
            raise TypeError(f"Unknown PropertiesConfig options: {', '.join(sorted(unknown))}")
        return replace(self, **overrides)
