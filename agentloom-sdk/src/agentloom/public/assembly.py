"""Local dependency resolution shared by direct constructors and AgentBuilder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRegistry
from ..configuration import AgentProperties
from ..configuration.properties import UNSET, resolve_properties
from ..context import ContextBudget
from ..errors import ConfigurationError
from ..events import HookSet
from ..types import ModelDescriptor

if TYPE_CHECKING:
    from ..runtime import RuntimeEngine
    from ..types import ModelAdapter


class AgentParameterView:
    """Compatibility accessors backed by one fixed AgentProperties instance."""

    properties: AgentProperties

    @property
    def workspace(self):
        return self.properties.workspace

    @property
    def execution_policy(self):
        return self.properties.execution_policy

    @property
    def context_policy(self):
        return self.properties.context_policy

    @property
    def model_options(self):
        return self.properties.model_options


def validate_model_adapter(model):
    if (
        not callable(getattr(model, "stream_once", None))
        or not isinstance(getattr(model, "descriptor", None), ModelDescriptor)
        or hasattr(model, "validate_options")
        and not callable(model.validate_options)
    ):
        raise ConfigurationError("MODEL_ADAPTER_INVALID")


def resolve_model_budget(model, properties):
    """Validate only supplied metadata/options; never discover or invoke a model."""
    validate_model_adapter(model)
    budget = ContextBudget.resolve(
        model.descriptor, properties.context_policy, properties.model_options
    )
    if hasattr(model, "validate_options"):
        model.validate_options(properties.model_options)
    elif properties.model_options.parameters:
        raise ConfigurationError("MODEL_FEATURE_UNSUPPORTED")
    return budget


@dataclass(frozen=True, init=False, kw_only=True, eq=False, repr=False)
class AgentAssembly(AgentParameterView):
    """Internal borrowed dependencies; no Agent, run state or resource ownership."""

    model: ModelAdapter
    runtime_engine: RuntimeEngine
    properties: AgentProperties
    capabilities: CapabilityRegistry
    hooks: HookSet
    budget: ContextBudget = field(init=False)

    def __init__(
        self,
        *,
        model,
        runtime_engine,
        properties=None,
        workspace=UNSET,
        capabilities=None,
        hooks=None,
        execution_policy=UNSET,
        context_policy=UNSET,
        model_options=UNSET,
    ):
        validate_model_adapter(model)
        properties = resolve_properties(
            properties,
            AgentProperties,
            workspace=workspace,
            execution_policy=execution_policy,
            context_policy=context_policy,
            model_options=model_options,
        )
        if not callable(getattr(runtime_engine, "open_run", None)):
            raise ConfigurationError("RUNTIME_ENGINE_REQUIRED")
        for name, value, expected in (
            ("capabilities", capabilities, CapabilityRegistry),
            ("hooks", hooks, HookSet),
        ):
            if value is not None and not isinstance(value, expected):
                raise ConfigurationError("INVALID_CONFIGURATION", field_path=expected.__name__)
            object.__setattr__(self, name, expected() if value is None else value)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "runtime_engine", runtime_engine)
        object.__setattr__(self, "properties", properties)
        object.__setattr__(self, "hooks", HookSet(self.hooks.bindings, self.hooks.handlers))
        object.__setattr__(self, "budget", resolve_model_budget(model, properties))

    def _agent_kwargs(self):
        """Legacy keyword projection; parameters are never separately stored."""
        return {
            "model": self.model,
            "runtime_engine": self.runtime_engine,
            "workspace": self.workspace,
            "capabilities": self.capabilities,
            "hooks": self.hooks,
            "execution_policy": self.execution_policy,
            "context_policy": self.context_policy,
            "model_options": self.model_options,
        }

    def _constructor_kwargs(self):
        return {
            "model": self.model,
            "runtime_engine": self.runtime_engine,
            "properties": self.properties,
            "capabilities": self.capabilities,
            "hooks": self.hooks,
        }
