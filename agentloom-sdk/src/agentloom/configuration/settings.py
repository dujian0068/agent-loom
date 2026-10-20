"""The startup parameter catalog and compiler. Runtime modules read its output.

Defaults/types/constraints come from the existing immutable value contracts;
the catalog alone decides which fields are configurable and their public keys.
No environment variables, directories, network calls or provider clients open
here. Service references are borrowed, while value parameters are frozen.
"""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType

from ..errors import ConfigurationError
from .files import FileProperties
from .model_validation import resolve_model_budget
from .models import ContextPolicy, ExecutionPolicy, ModelOptions, RetryPolicy, WorkspaceBinding
from .properties import AgentProperties, RuntimeProperties
from .services import ServiceProperties


@dataclass(frozen=True, repr=False)
class ModelConnectionParameters:
    """Protocol connection values; optional auth supports local compatible servers."""

    protocol: str = "chat_completions"
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    context_window_tokens: int | None = None
    output_token_limit: int | None = None
    client: object | None = None


# Each prefix projects an existing value contract; exclusions compose nested
# contracts or keep live dependencies under an explicit injection key.
PARAMETER_GROUPS = MappingProxyType(
    {
        "runtime": (RuntimeProperties, frozenset()),
        "workspace": (WorkspaceBinding, frozenset()),
        "execution": (ExecutionPolicy, frozenset({"retry"})),
        "execution.retry": (RetryPolicy, frozenset()),
        "context": (ContextPolicy, frozenset()),
        "model": (ModelOptions, frozenset()),
        "files": (FileProperties, frozenset()),
        "services": (ServiceProperties, frozenset({"model"})),
    }
)
CONNECTION_KEYS = frozenset(f"model.{f.name}" for f in fields(ModelConnectionParameters))
PARAMETER_KEYS = (
    frozenset(
        f"{prefix}.{f.name}"
        for prefix, (cls, excluded) in PARAMETER_GROUPS.items()
        for f in fields(cls)
        if f.name not in excluded
    )
    | CONNECTION_KEYS
    | {"model.adapter"}
)


def checked_values(values):
    if not isinstance(values, Mapping):
        raise ConfigurationError("PROPERTIES_INVALID", field_path="properties")
    for key in values:
        if not isinstance(key, str) or key not in PARAMETER_KEYS:
            raise ConfigurationError(
                "PROPERTY_UNKNOWN",
                "Unknown startup parameter",
                field_path=key if isinstance(key, str) else "properties",
            )
    return dict(values)


def group_values(prefix, value):
    _, excluded = PARAMETER_GROUPS[prefix]
    return {
        f"{prefix}.{f.name}": getattr(value, f.name)
        for f in fields(value)
        if f.name not in excluded
    }


def construct(prefix, values, **extra):
    cls, excluded = PARAMETER_GROUPS[prefix]
    options = {
        f.name: values[f"{prefix}.{f.name}"]
        for f in fields(cls)
        if f.name not in excluded and f"{prefix}.{f.name}" in values
    }
    try:
        return cls(**(options | extra))
    except ConfigurationError as exc:
        path = exc.field_path
        aliases = {
            "STORAGE_NAMESPACE_INVALID": "runtime.namespace",
            "STATE_DIRECTORY_INVALID": "runtime.state_directory",
            "STORE_LOCK_TIMEOUT_INVALID": "runtime.store_lock_timeout_seconds",
            "WORKSPACE_ROOT_INVALID": "workspace.root",
            "MODEL_PARAMETERS_INVALID": "model.parameters",
            "RUNTIME_STORE_INVALID": "services.store",
            "ENVIRONMENT_INVALID": "services.environment",
            "CHECKPOINT_CALLBACK_INVALID": "services.checkpoint_callback",
            "MODEL_ADAPTER_INVALID": "model.adapter",
        }
        if exc.code in {"INVALID_JSON", "INVALID_CANONICAL_JSON"}:
            path = {"model": "model.parameters", "files": "files.options"}.get(prefix, path)
        if path not in PARAMETER_KEYS:
            path = {"CapabilityRegistry": "capabilities", "HookSet": "hooks"}.get(path, path)
            if path and path.startswith(prefix.rsplit(".", 1)[-1] + "."):
                path = path.split(".", 1)[1]
            candidate = f"{prefix}.{path}" if path else prefix
            path = aliases.get(exc.code, candidate)
        raise ConfigurationError(exc.code, "Invalid startup parameter", field_path=path) from exc


@dataclass(frozen=True, repr=False)
class ResolvedProperties:
    runtime: RuntimeProperties
    defaults: AgentProperties
    services: ServiceProperties
    files: FileProperties
    budget: object
    values: Mapping


def resolve_groups(*, runtime, defaults, services, files, connection=None):
    """Compile legacy groups and the flat entry into the same fixed parameters."""
    for name, value, cls in (
        ("runtime", runtime, RuntimeProperties),
        ("defaults", defaults, AgentProperties),
        ("services", services, ServiceProperties),
        ("files", files, FileProperties),
    ):
        if not isinstance(value, cls):
            raise ConfigurationError("INVALID_CONFIGURATION", field_path=name)
    if files.provider == "local" and defaults.workspace.root is None:
        raise ConfigurationError("WORKSPACE_ROOT_REQUIRED", field_path="workspace.root")
    if files.provider != "local" and services.environment is not None:
        raise ConfigurationError("FILE_ENVIRONMENT_CONFLICT", field_path="services.environment")
    try:
        budget = resolve_model_budget(services.model, defaults)
    except ConfigurationError as exc:
        raise ConfigurationError(
            exc.code,
            "Incompatible model or context parameters",
            field_path=exc.field_path
            or ("model.parameters" if exc.code == "MODEL_FEATURE_UNSUPPORTED" else "model"),
        ) from exc
    values = {}
    for prefix, group in (
        ("runtime", runtime),
        ("workspace", defaults.workspace),
        ("execution", defaults.execution_policy),
        ("execution.retry", defaults.execution_policy.retry),
        ("context", defaults.context_policy),
        ("model", defaults.model_options),
        ("files", files),
        ("services", services),
    ):
        values.update(group_values(prefix, group))
    values.update({"model.adapter": services.model} if connection is None else connection)
    return ResolvedProperties(runtime, defaults, services, files, budget, MappingProxyType(values))


def resolve_configuration(values):
    """Validate every key/value and combination before any host resources open."""
    values = checked_values(values)
    runtime = construct("runtime", values)
    workspace = construct("workspace", values)
    retry = construct("execution.retry", values)
    execution = construct("execution", values, retry=retry)
    context = construct("context", values)
    options = construct("model", values)
    files = construct("files", values)
    model, connection = resolve_model(values)
    services = construct("services", values, model=model)
    return resolve_groups(
        runtime=runtime,
        defaults=AgentProperties(
            workspace=workspace,
            execution_policy=execution,
            context_policy=context,
            model_options=options,
        ),
        services=services,
        files=files,
        connection=connection,
    )


def resolve_model(values):
    """Select a protocol implementation or an explicitly borrowed adapter."""
    from ..models import ChatCompletionsAdapter, ResponsesAdapter

    if "model.adapter" in values and values["model.adapter"] is not None:
        if CONNECTION_KEYS & values.keys():
            raise ConfigurationError("PARAMETERS_CONFLICT", field_path="model.adapter")
        return values["model.adapter"], None
    connection = ModelConnectionParameters(
        **{
            key.removeprefix("model."): value
            for key, value in values.items()
            if key in CONNECTION_KEYS
        }
    )
    adapters = {"chat_completions": ChatCompletionsAdapter, "responses": ResponsesAdapter}
    if not isinstance(connection.protocol, str) or connection.protocol not in adapters:
        raise ConfigurationError("MODEL_PROTOCOL_UNSUPPORTED", field_path="model.protocol")
    for key in ("name", "base_url", "context_window_tokens"):
        if getattr(connection, key) is None:
            raise ConfigurationError("PROPERTY_REQUIRED", field_path=f"model.{key}")
    try:
        model = adapters[connection.protocol](
            model=connection.name,
            base_url=connection.base_url,
            api_key=connection.api_key,
            context_window_tokens=connection.context_window_tokens,
            max_output_tokens=connection.output_token_limit,
            client=connection.client,
        )
    except ConfigurationError as exc:
        path = exc.field_path or "model"
        if path == "model.max_output_tokens":
            path = "model.output_token_limit"
        if path not in PARAMETER_KEYS:
            path = f"model.{path}"
        raise ConfigurationError(exc.code, "Invalid model connection", field_path=path) from exc
    normalized = {f"model.{f.name}": getattr(connection, f.name) for f in fields(connection)}
    normalized["model.base_url"] = model.base_url
    return model, normalized
