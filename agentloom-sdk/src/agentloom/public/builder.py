"""Reusable definition-to-Agent assembly without executing or owning dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from ..capabilities import CapabilityRegistry
from ..configuration import (
    AgentDefinition,
    AgentProperties,
    ContextPolicy,
    ExecutionPolicy,
    ModelOptions,
    WorkspaceBinding,
)
from ..errors import ConfigurationError
from ..events import HookSet
from .agents import PlanAgent, ReactAgent
from .assembly import AgentAssembly

if TYPE_CHECKING:
    from ..runtime import RuntimeEngine
    from ..types import ModelAdapter


class _AssemblyOverrides(TypedDict, total=False):
    properties: AgentProperties
    model: ModelAdapter
    workspace: WorkspaceBinding
    runtime_engine: RuntimeEngine
    capabilities: CapabilityRegistry | None
    hooks: HookSet | None
    execution_policy: ExecutionPolicy | None
    context_policy: ContextPolicy | None
    model_options: ModelOptions | None


@dataclass(frozen=True, init=False, kw_only=True, eq=False, repr=False)
class AgentBuilder(AgentAssembly):
    """Configure shared dependencies once, then build a fresh Agent per definition.

    Args:
        model: ModelAdapter with explicit descriptor and local option validation.
        properties: Immutable AgentProperties. Alternatively use the legacy
            workspace/execution_policy/context_policy/model_options keywords;
            mixing the two parameter sources is rejected.
        workspace: Legacy WorkspaceBinding; required when properties is omitted.
        runtime_engine: Borrowed execution host. Open/close it in the caller;
            building is allowed before it is opened.
        capabilities: Immutable registered resources selected by each definition.
            Defaults to an empty registry, never grants every registered resource.
        hooks: Fixed HookSet, or None for no hooks.
        execution_policy, context_policy, model_options: Fixed defaults shared by
            built Agents. None selects the corresponding SDK default.

    Raises:
        ConfigurationError: Invalid dependencies, options or model window/budget,
            using the same validation as direct Agent construction.

    This immutable factory can be reused for many Runs. It neither opens/closes
    dependencies nor calls discovery, models, tools or Hooks. Shared adapters and
    handlers remain caller-owned and must support the caller's concurrency.
    """

    def build(
        self,
        definition: AgentDefinition,
        *,
        executors: Mapping[str, AgentDefinition] | None = None,
        mode: Literal["react", "plan"] | None = None,
    ) -> ReactAgent | PlanAgent:
        """Assemble a new Agent locally; no Run is created until start/ainvoke.

        Args:
            definition: Existing immutable behavior definition. Used as the root
                ReAct definition or Planner definition, without changing it.
            executors: Executor key -> definition. A supplied mapping (even {})
                selects Plan mode when mode is omitted. Copied by PlanAgent;
                registered capabilities remain separately authorized per role.
            mode: Explicit react/plan, or None to infer from executors. Explicit
                plan with no executors is valid; react rejects any executor map.

        Returns:
            A fresh ReactAgent or PlanAgent with independent instance binding,
            locks and context, using the normal execution/recovery contract.

        Raises:
            ConfigurationError: Invalid mode, conflicting executor map, or any
                definition, capability, Hook or assembly error from constructors.
                A failed build does not change this builder or previous Agents.

        Identical assembly has the same recovery fingerprint as direct
        construction. New questions require another build; reopening uses a new
        build with the original configuration and start(run_id=...).
        """
        if mode not in (None, "react", "plan"):
            raise ConfigurationError("AGENT_MODE_INVALID", field_path="mode")
        resolved_mode = mode or ("plan" if executors is not None else "react")
        if resolved_mode == "react":
            if executors is not None:
                raise ConfigurationError("EXECUTORS_UNSUPPORTED", field_path="executors")
            return ReactAgent._from_assembly(definition, self)
        return PlanAgent._from_assembly(definition, self, executors=executors)

    def with_options(self, **overrides: Unpack[_AssemblyOverrides]) -> AgentBuilder:
        """Return an independently validated builder with selected replacements.

        Accepts the same dependency keywords as the constructor. Omitted fields
        are inherited; None resets optional fields to SDK defaults. Replacement
        is whole-value (registries, Hooks and policies are not merged), allowing
        callers to remove grants/Hooks explicitly. Required dependencies cannot
        be cleared. Previously built Agents and the source builder are unchanged.
        properties replaces the entire parameter group; individual parameter
        overrides derive a new group. Supplying both forms is rejected.

        Raises:
            ConfigurationError: Invalid replacement or incompatible combined
                configuration, including a new model's window/output limits.
            TypeError: Unknown keyword; definition/mode/executors belong to build.
        """
        unknown = overrides.keys() - (self._agent_kwargs().keys() | {"properties"})
        if unknown:
            raise TypeError(f"Unknown AgentBuilder options: {', '.join(sorted(unknown))}")
        parameter_names = {"workspace", "execution_policy", "context_policy", "model_options"}
        parameters = {key: overrides.pop(key) for key in parameter_names & overrides.keys()}
        if "properties" in overrides:
            if parameters:
                raise ConfigurationError("PARAMETERS_CONFLICT", field_path="properties")
            if not isinstance(overrides["properties"], AgentProperties):
                raise ConfigurationError("INVALID_CONFIGURATION", field_path="AgentProperties")
        elif parameters:
            overrides["properties"] = replace(self.properties, **parameters)
        return replace(self, **overrides)
