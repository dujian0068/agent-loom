"""Central model and budget validation shared by all construction entry points."""

import inspect

import httpx

from ..context import ContextBudget
from ..errors import ConfigurationError
from ..types import ModelDescriptor


def validate_connection(
    *, model, api_key, context_window_tokens, base_url, max_output_tokens, client
):
    """Validate connection values locally; adapters only consume the normalized result."""
    if not isinstance(model, str) or not model.strip():
        raise ConfigurationError("MODEL_CONFIGURATION_INVALID", field_path="model.name")
    if api_key is not None and (
        not isinstance(api_key, str) or not api_key or any(c in api_key for c in "\r\n")
    ):
        raise ConfigurationError("MODEL_CONFIGURATION_INVALID", field_path="model.api_key")
    try:
        url = httpx.URL(base_url) if isinstance(base_url, str) else None
    except httpx.InvalidURL:
        url = None
    if (
        url is None
        or url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ConfigurationError("MODEL_CONFIGURATION_INVALID", field_path="model.base_url")
    if client is not None and not callable(getattr(client, "stream", None)):
        raise ConfigurationError("MODEL_CLIENT_INVALID", field_path="model.client")
    return ModelDescriptor(model, context_window_tokens, max_output_tokens), str(url).rstrip("/")


def validate_model_adapter(model):
    validator = getattr(model, "validate_options", None)
    if (
        not callable(getattr(model, "stream_once", None))
        or not isinstance(getattr(model, "descriptor", None), ModelDescriptor)
        or hasattr(model, "validate_options")
        and (
            not callable(validator)
            or inspect.iscoroutinefunction(validator)
            or inspect.iscoroutinefunction(getattr(validator, "__call__", None))
        )
    ):
        raise ConfigurationError("MODEL_ADAPTER_INVALID", field_path="model.adapter")


def resolve_model_budget(model, properties):
    """Validate only supplied metadata/options; never discover or invoke a model."""
    validate_model_adapter(model)
    budget = ContextBudget.resolve(
        model.descriptor, properties.context_policy, properties.model_options
    )
    if hasattr(model, "validate_options"):
        result = model.validate_options(properties.model_options)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ConfigurationError("MODEL_ADAPTER_INVALID", field_path="model.adapter")
    elif properties.model_options.parameters:
        raise ConfigurationError("MODEL_FEATURE_UNSUPPORTED")
    return budget
