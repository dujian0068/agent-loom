"""Selected entry-point discovery; never import unselected provider modules."""

import inspect
from dataclasses import dataclass
from importlib import metadata

from ..configuration.json_values import json_copy
from ..errors import AgentLoomError, ConfigurationError
from .contracts import FILE_OPERATIONS, WorkspaceBackend

ENTRY_POINT_GROUP = "agentloom.workspace_backends"
LOCAL_IMPLEMENTATION = "agentloom.workspace.local:LocalWorkspaceBackend"


@dataclass(frozen=True)
class ProviderDefinition:
    properties: object
    entry_point: object = None

    @property
    def manifest(self):
        # Preserve existing local checkpoints byte-for-byte.
        if self.properties.provider == "local":
            return None
        ep = self.entry_point
        return {
            "provider": self.properties.provider,
            "implementation": ep.value,
            "distribution": ep.dist.metadata["Name"],
            "version": ep.dist.version,
            "options_hash": self.properties.options_hash,
        }

    def create(self, environment):
        try:
            if self.properties.provider == "local":
                from .local import LocalWorkspaceBackend

                cls = LocalWorkspaceBackend
            else:
                cls = self.entry_point.load()
            _validate_class(cls)
            instance = cls(**json_copy(self.properties.options))
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError("FILE_PROVIDER_LOAD_FAILED", type(exc).__name__) from exc
        if not isinstance(instance, cls):
            raise ConfigurationError("FILE_PROVIDER_INVALID")
        if self.properties.provider == "local":
            instance.environment = environment
        return instance


def resolve_provider(properties):
    """Resolve installed metadata locally; loading/instantiation happens at startup."""
    try:
        selected = [
            ep
            for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)
            if ep.name == properties.provider
        ]
    except Exception as exc:
        raise ConfigurationError("FILE_PROVIDER_DISCOVERY_FAILED", type(exc).__name__) from exc
    if properties.provider == "local":
        if selected and (
            len(selected) != 1
            or selected[0].value != LOCAL_IMPLEMENTATION
            or selected[0].dist.metadata["Name"].lower().replace("_", "-") != "agentloom-sdk"
        ):
            raise ConfigurationError("FILE_PROVIDER_DUPLICATE")
        return ProviderDefinition(properties)
    if not selected:
        raise ConfigurationError("FILE_PROVIDER_NOT_FOUND")
    if len(selected) != 1:
        raise ConfigurationError("FILE_PROVIDER_DUPLICATE")
    ep = selected[0]
    if ep.dist is None or not ep.dist.metadata["Name"] or not ep.dist.version:
        raise ConfigurationError("FILE_PROVIDER_METADATA_INVALID")
    return ProviderDefinition(properties, ep)


def _validate_class(cls):
    if not inspect.isclass(cls) or not issubclass(cls, WorkspaceBackend) or inspect.isabstract(cls):
        raise ConfigurationError("FILE_PROVIDER_INTERFACE_INVALID")
    _validate_methods(cls, bound=False)


def validate_instance(instance):
    """Validate after the host owns the instance, so failed cleanup can be retried."""
    _validate_methods(instance, bound=True)


def _validate_methods(target, *, bound):
    for name in (*FILE_OPERATIONS, "prepare", "revoke", "cleanup", "close"):
        method = getattr(target, name, None)
        if (
            not callable(method)
            or inspect.iscoroutinefunction(method)
            or inspect.isasyncgenfunction(method)
            or inspect.isgeneratorfunction(method)
        ):
            raise ConfigurationError("FILE_PROVIDER_INTERFACE_INVALID", field_path=name)
        signature = inspect.signature(getattr(WorkspaceBackend, name))
        args, kwargs = [], {}
        for parameter in list(signature.parameters.values())[int(bound) :]:
            if parameter.kind == parameter.KEYWORD_ONLY:
                kwargs[parameter.name] = None
            else:
                args.append(None)
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("FILE_PROVIDER_INTERFACE_INVALID", field_path=name) from exc


def synchronous_result(value):
    """Reject an accidentally async implementation without running an event loop."""
    if inspect.isawaitable(value) or inspect.isasyncgen(value):
        if inspect.iscoroutine(value):
            value.close()
        raise AgentLoomError(
            "FILE_PROVIDER_ASYNC_RESULT", category="protocol", external_outcome="unknown"
        )
    return value
