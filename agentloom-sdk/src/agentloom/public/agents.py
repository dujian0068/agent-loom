"""Public Agent facade and construction-time assembly; no execution on construction."""

import asyncio
import re
from types import MappingProxyType

from ..capabilities import CapabilityRegistry
from ..configuration import (
    AgentDefinition,
    ContextPolicy,
    ExecutionPolicy,
    ModelOptions,
    WorkspaceBinding,
)
from ..context import ContextBudget
from ..errors import ConfigurationError, RequestError
from ..events import HookSet


class ReactAgent:
    """Immutable Agent assembly; the first accepted start binds it to one Run."""

    mode = "react"

    def __setattr__(self, name, value):
        if not name.startswith("_") and name in self.__dict__:
            raise AttributeError("Agent assembly is immutable; construct a new Agent")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        definition,
        model,
        workspace,
        runtime_engine,
        capabilities=None,
        hooks=None,
        execution_policy=None,
        context_policy=None,
        model_options=None,
    ):
        if not isinstance(definition, AgentDefinition):
            raise ConfigurationError("DEFINITION_REQUIRED")
        if not callable(getattr(model, "stream_once", None)) or not hasattr(model, "descriptor"):
            raise ConfigurationError("MODEL_ADAPTER_INVALID")
        if not isinstance(workspace, WorkspaceBinding):
            raise ConfigurationError("WORKSPACE_BINDING_REQUIRED")
        if not callable(getattr(runtime_engine, "open_run", None)):
            raise ConfigurationError("RUNTIME_ENGINE_REQUIRED")
        for value, expected in (
            (capabilities, CapabilityRegistry),
            (hooks, HookSet),
            (execution_policy, ExecutionPolicy),
            (context_policy, ContextPolicy),
            (model_options, ModelOptions),
        ):
            if value is not None and not isinstance(value, expected):
                raise ConfigurationError("INVALID_CONFIGURATION", field_path=expected.__name__)
        self.definition, self.model, self.workspace, self.runtime_engine = (
            definition,
            model,
            workspace,
            runtime_engine,
        )
        self.capabilities = capabilities or CapabilityRegistry()
        original_hooks = hooks or HookSet()
        self.hooks = HookSet(original_hooks.bindings, original_hooks.handlers)
        self.execution_policy = execution_policy or ExecutionPolicy()
        self.context_policy = context_policy or ContextPolicy()
        self.model_options = model_options or ModelOptions()
        self.budget = ContextBudget.resolve(
            model.descriptor, self.context_policy, self.model_options
        )
        if hasattr(model, "validate_options"):
            model.validate_options(self.model_options)
        elif self.model_options.parameters:
            raise ConfigurationError("MODEL_FEATURE_UNSUPPORTED")
        self.executors = MappingProxyType({})
        role = "planner" if self.mode == "plan" else "react"
        self.definitions = MappingProxyType({role: definition})
        self.catalogs = MappingProxyType({role: self.capabilities.resolve_definition(definition)})
        if self.mode == "react":
            self._validate_hook_matchers()
        self._bound_run = None
        self._binding_lock = asyncio.Lock()
        self._context = None

    @property
    def context(self):
        return self._context

    async def _activate(self, node_context):
        # Local imports keep the public facade independent of runtime assembly.
        from ..runtime.agent_loop import AgentLoop

        return await AgentLoop(self, node_context).run()

    async def start(self, input=None, *, session_id=None, run_id=None, input_id=None):
        """Accept or reopen a Run and return its observation/control RunHandle.

        Args:
            input: Nonempty text or AgentInput matching the definition's schema
                and variables. Omit only when reopening an existing run_id.
            session_id: Conversation identity; omitted for a new isolated session,
                or inferred from an existing Run. One active Run per session.
            run_id: Durable execution identity. Omit to generate a new identity;
                reuse it to resume matching work or read its terminal result.
            input_id: Initial input deduplication identity. Requires both input
                and run_id; generated when omitted for a new Run.

        Returns:
            RunHandle after durable acceptance, without waiting for execution.
            A terminal Run is observed without invoking the model again.

        Raises:
            RequestError: Invalid input/IDs, a busy session, conflicting replay
                or recovery bindings, input schema/variable validation failure,
                or reuse of this instance for another Run.
            ConfigurationError: Input material cannot be represented as finite,
                canonical JSON.
            InfrastructureError: Runtime is closed/on another event loop, or
                durable storage is unavailable.

        After the first successful call, this instance requires the same explicit
        run_id on every call, including after completion. Create a fresh Agent
        for each new Run. A reopened nonterminal Run requires its original assembly.
        """
        async with self._binding_lock:
            if self._bound_run is not None and (run_id is None or str(run_id) != self._bound_run):
                raise RequestError("AGENT_INSTANCE_BOUND")
            handle = await self.runtime_engine.open_run(
                self, input, session_id=session_id, run_id=run_id, input_id=input_id
            )
            self._bound_run = handle.run_id
            return handle

    async def ainvoke(self, input=None, **ids):
        """Start/reopen and wait for a RunResult, including failed business outcomes.

        Args:
            input: Text or AgentInput, or None when reopening an existing Run.
            **ids: session_id, run_id and input_id, with the contracts in start().

        Returns:
            RunResult once business execution is terminal; file cleanup may still
            be pending. Inspect status/error for business failure, stop or deadline.

        Raises:
            RequestError, ConfigurationError, InfrastructureError: As in start()
                or RunHandle.wait(). Cancellation of this coroutine cancels only
                the local wait, not the accepted Run.

        Uses the same instance binding as start(). There is no local wait timeout;
        use start() then handle.wait(timeout_s=...) to bound observation time. The
        Run's execution deadline remains governed by ExecutionPolicy.
        """
        return await (await self.start(input, **ids)).wait()

    async def astream(self, input=None, *, after_seq=0, **ids):
        """Yield committed RunEvents in sequence through the durable terminal event.

        Args:
            input: Text or AgentInput, or None when reopening an existing Run.
            after_seq: Exclusive nonnegative event cursor; 0 replays from the start.
            **ids: session_id, run_id and input_id, with the contracts in start().

        Yields:
            RunEvent records, not provider token deltas. A terminal Run can be
            replayed without executing the model again.

        Raises:
            RequestError: Invalid/ahead cursor, or a start() admission error.
            ConfigurationError, InfrastructureError: As in start(), or a runtime,
                storage or local execution-driver failure while observing.

        Acceptance begins on first iteration and binds this Agent as start() does.
        Ending/cancelling iteration does not stop the Run. Use RunHandle.stop() for
        that; this stream has no independent observation timeout.
        """
        handle = await self.start(input, **ids)
        async for event in handle.events(after_seq=after_seq):
            yield event

    def bind_role(self, role):
        if self.mode == "react" and role == "react":
            return self
        definition = self.definitions.get(role)
        if definition is None:
            raise ConfigurationError("RECOVERY_BINDING_MISSING", field_path=role)
        agent = ReactAgent(
            definition=definition,
            model=self.model,
            workspace=self.workspace,
            runtime_engine=self.runtime_engine,
            capabilities=self.capabilities,
            execution_policy=self.execution_policy,
            context_policy=self.context_policy,
            model_options=self.model_options,
        )
        # Plan matchers are validated against the entire role catalog once.
        object.__setattr__(agent, "hooks", self.hooks)
        return agent

    def _validate_hook_matchers(self):
        keys = {
            b.binding_key
            for definition in self.definitions.values()
            for b in definition.capabilities
        }
        for binding in self.hooks.bindings:
            if binding.matcher.role_keys and set(binding.matcher.role_keys) - set(self.definitions):
                raise ConfigurationError("HOOK_MATCHER_ROLE_UNKNOWN")
            if (
                binding.matcher.capability_binding_keys
                and set(binding.matcher.capability_binding_keys) - keys
            ):
                raise ConfigurationError("HOOK_MATCHER_CAPABILITY_UNKNOWN")


class PlanAgent(ReactAgent):
    mode = "plan"

    def __init__(self, *, executors=None, **kwargs):
        super().__init__(**kwargs)
        executors = dict(executors or {})
        for key, definition in executors.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", key)
                or key in {"react", "planner"}
            ):
                raise ConfigurationError("EXECUTOR_KEY_INVALID")
            if (
                not isinstance(definition, AgentDefinition)
                or not isinstance(definition.description, str)
                or not definition.description.strip()
            ):
                raise ConfigurationError("EXECUTOR_DEFINITION_INVALID")
        object.__setattr__(self, "executors", MappingProxyType(executors))
        object.__setattr__(
            self, "definitions", MappingProxyType({"planner": self.definition, **executors})
        )
        object.__setattr__(
            self,
            "catalogs",
            MappingProxyType(
                {
                    key: self.capabilities.resolve_definition(definition)
                    for key, definition in self.definitions.items()
                }
            ),
        )
        self._validate_hook_matchers()


class AgentNode:
    def __init__(self, agent):
        if not isinstance(agent, (ReactAgent, PlanAgent)):
            raise ConfigurationError("AGENT_BINDING_UNSUPPORTED")
        self.agent = agent

    async def start(self, input=None, **ids):
        return await self.agent.start(input, **ids)

    async def ainvoke(self, input=None, **ids):
        return await self.agent.ainvoke(input, **ids)

    def astream(self, input=None, **ids):
        return self.agent.astream(input, **ids)

    async def activate(self, input, continuation, node_context):
        # Internal activation is scoped to an existing task, never public start().
        return await self.agent._activate(node_context)
