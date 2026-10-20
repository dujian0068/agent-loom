"""Startup service contracts; live references never enter checkpoints."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from ..capabilities import CapabilityRegistry
from ..errors import ConfigurationError
from ..events import HookSet
from .model_validation import validate_model_adapter

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
