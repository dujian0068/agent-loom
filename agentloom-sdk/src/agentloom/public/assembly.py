"""Local dependency resolution shared by direct constructors and AgentBuilder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRegistry
from ..configuration import AgentProperties
from ..configuration.model_validation import resolve_model_budget
from ..configuration.properties import UNSET, resolve_properties
from ..configuration.services import ServiceProperties
from ..context import ContextBudget
from ..errors import ConfigurationError
from ..events import HookSet

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
        services = ServiceProperties(model=model, capabilities=capabilities, hooks=hooks)
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
        for name in ("capabilities", "hooks"):
            object.__setattr__(self, name, getattr(services, name))
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "runtime_engine", runtime_engine)
        object.__setattr__(self, "properties", properties)
        object.__setattr__(self, "budget", resolve_model_budget(model, properties))

    @classmethod
    def _from_configuration(cls, configuration, runtime_engine):
        """Bind a validated startup snapshot without rerunning parameter validators."""
        assembly = object.__new__(cls)
        for key, value in (
            ("properties", configuration.defaults),
            ("model", configuration.services.model),
            ("capabilities", configuration.services.capabilities),
            ("hooks", configuration.services.hooks),
            ("budget", configuration._budget),
            ("runtime_engine", runtime_engine),
        ):
            object.__setattr__(assembly, key, value)
        return assembly

    def _bind_services(self, capabilities, hooks):
        """Project already-bound role services; fixed parameters/budget stay shared."""
        bound = object.__new__(AgentAssembly)
        for key, value in (
            self._constructor_kwargs()
            | {
                "capabilities": capabilities,
                "hooks": hooks,
                "budget": self.budget,
            }
        ).items():
            object.__setattr__(bound, key, value)
        return bound

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
