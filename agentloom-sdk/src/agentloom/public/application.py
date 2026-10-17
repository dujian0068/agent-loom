"""Startup/lifecycle facade; execution stays in RuntimeEngine and AgentBuilder."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ..configuration import AgentDefinition, AgentProperties
from ..errors import ConfigurationError, InfrastructureError
from .agents import PlanAgent, ReactAgent
from .builder import AgentBuilder
from .properties import PropertiesConfig


class AgentLoom:
    """Open one host from PropertiesConfig and build independent Agents from it.

    Construction is local and side-effect-free; async entry creates/opens the
    RuntimeEngine. Use async with AgentLoom(config) as app, then app.build(...).
    app.runtime exposes the existing query/control services. Closing suspends
    local advancement and preserves checkpoints; it does not issue user stops.
    Injected services remain caller-owned. Reopening needs a new facade instance.
    """

    def __init__(self, properties: PropertiesConfig):
        if not isinstance(properties, PropertiesConfig):
            raise ConfigurationError("PROPERTIES_CONFIG_REQUIRED")
        self._properties = properties
        self._runtime = None
        self._builder = None
        self._status = "created"

    @property
    def properties(self) -> PropertiesConfig:
        """The immutable startup configuration for this instance."""
        return self._properties

    @property
    def runtime(self):
        """The created host (also observable after close); unavailable before entry."""
        if self._runtime is None:
            raise InfrastructureError("RUNTIME_CLOSED")
        return self._runtime

    @property
    def builder(self) -> AgentBuilder:
        """The bound builder while this host is open; derive per-Agent overrides here."""
        if self._status != "open":
            raise InfrastructureError("RUNTIME_CLOSED")
        return self._builder

    async def __aenter__(self) -> AgentLoom:
        """Create/open the host once; preserve the primary error if startup fails.

        Raises ConfigurationError for invalid assembly and InfrastructureError
        for storage/startup failures or reuse of this facade. On failed startup,
        only created host resources are closed; injected dependencies stay open.
        """
        from ..runtime import RuntimeEngine

        if self._status != "created":
            raise InfrastructureError("RUNTIME_ALREADY_OPENED")
        self._status = "opening"
        services = self.properties.services
        try:
            runtime = RuntimeEngine(
                properties=self.properties.runtime,
                store=services.store,
                environment=services.environment,
                checkpoint_callback=services.checkpoint_callback,
            )
            self._runtime = runtime
            self._builder = AgentBuilder(
                properties=self.properties.defaults,
                model=services.model,
                capabilities=services.capabilities,
                hooks=services.hooks,
                runtime_engine=runtime,
            )
            await runtime.__aenter__()
        except BaseException as primary:
            try:
                await self.aclose()
            except Exception as cleanup:
                primary.add_note(f"Startup cleanup also failed: {type(cleanup).__name__}")
            raise
        self._status = "open"
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            await self.aclose()
        except Exception as cleanup:
            if exc is None:
                raise
            exc.add_note(f"Host cleanup also failed: {type(cleanup).__name__}")

    async def aclose(self):
        """Idempotently suspend local work; close only the host-owned file store.

        The same runtime shutdown behavior applies to previously built Agents.
        An injected store/model/environment is not closed. Storage close failures
        propagate and may be retried; accepted Runs remain recoverable.
        """
        if self._status == "closed":
            return
        self._status = "closing"
        if self._runtime is not None:
            await self._runtime.aclose()
        self._status = "closed"

    def build(
        self,
        definition: AgentDefinition,
        *,
        executors: Mapping[str, AgentDefinition] | None = None,
        mode: Literal["react", "plan"] | None = None,
        properties: AgentProperties | None = None,
    ) -> ReactAgent | PlanAgent:
        """Build through the current host; optionally replace this Agent's parameters.

        Mode/Executor selection and errors match AgentBuilder.build. A supplied
        AgentProperties replaces the whole defaults group; partial overrides use
        app.builder.with_options(...). Neither form changes startup parameters or
        previously built Agents. Raises InfrastructureError unless the host is open.
        """
        builder = self.builder
        if properties is not None:
            builder = builder.with_options(properties=properties)
        return builder.build(definition, executors=executors, mode=mode)
