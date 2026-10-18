"""One request, one registered handler. Observation never executes work."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..errors import AgentLoomError, ConfigurationError


@runtime_checkable
class EventHandler(Protocol):
    async def handle(self, event, context): ...


@runtime_checkable
class EventBus(Protocol):
    async def request(self, event, context=None): ...


@runtime_checkable
class HandlerRegistry(Protocol):
    def register_handler(self, event_type, schema_version, binding): ...
    def resolve(self, event_type, schema_version=1): ...
    def freeze(self, required_manifest=()): ...


@dataclass(frozen=True)
class HandlerBinding:
    handler: EventHandler


class EventBusImpl:
    def __init__(self):
        self._handlers = {}
        self._frozen = False

    def register_handler(self, event_type, schema_version, binding):
        if self._frozen:
            raise ConfigurationError("REGISTRY_FROZEN")
        self._validate_key(event_type, schema_version)
        key = (event_type, schema_version)
        if key in self._handlers:
            raise ConfigurationError("DUPLICATE_HANDLER")
        if not isinstance(binding, HandlerBinding) or not isinstance(binding.handler, EventHandler):
            raise ConfigurationError("HANDLER_INVALID")
        self._handlers[key] = binding

    def resolve(self, event_type, schema_version=1):
        self._validate_key(event_type, schema_version)
        binding = self._handlers.get((event_type, schema_version))
        return (binding,) if binding is not None else ()

    @staticmethod
    def _validate_key(event_type, schema_version):
        if not isinstance(event_type, str) or not event_type.strip():
            raise ConfigurationError("EVENT_TYPE_INVALID")
        if type(schema_version) is not int or schema_version < 1:
            raise ConfigurationError("EVENT_VERSION_INVALID")

    def freeze(self, required_manifest=()):
        visited, active, registries = set(), set(), []

        def visit(bus):
            if id(bus) in active:
                raise ConfigurationError("HANDLER_CYCLE")
            if id(bus) in visited:
                return
            active.add(id(bus))
            for binding in bus._handlers.values():
                if isinstance(binding.handler, EventBusImpl):
                    visit(binding.handler)
            active.remove(id(bus))
            visited.add(id(bus))
            registries.append(bus)

        visit(self)
        for event_type, version in required_manifest:
            if not self.resolve(event_type, version):
                raise ConfigurationError("REQUIRED_HANDLER_MISSING")
        for bus in registries:
            bus._frozen = True

    async def request(self, event, context=None):
        if not self._frozen:
            raise ConfigurationError("REGISTRY_NOT_FROZEN")
        bindings = self.resolve(event.event_type, getattr(event, "schema_version", 1))
        if not bindings:
            raise AgentLoomError("HANDLER_NOT_FOUND")
        return await bindings[0].handler.handle(event, context)

    async def handle(self, event, context):
        return await self.request(event, context)
