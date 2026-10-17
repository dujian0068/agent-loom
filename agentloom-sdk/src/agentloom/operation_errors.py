"""Shared OperationError validation, independent of execution-fact decisions."""

from .configuration.json_values import json_copy

ERROR_CATEGORIES = frozenset(
    {
        "validation",
        "permission",
        "not_found",
        "conflict",
        "transport",
        "rate_limit",
        "timeout",
        "business",
        "protocol",
        "cancelled",
        "unknown",
    }
)
ERROR_PHASES = frozenset({"prepare", "connect", "send", "receive", "execute", "after"})
EXTERNAL_OUTCOMES = frozenset({"not_started", "known", "unknown"})
_REQUIRED = frozenset({"code", "message", "category", "phase", "external_outcome"})
_OPTIONAL = frozenset({"provider_code", "retryable_transport", "retry_after_at", "field_path"})


def normalize_operation_error(value):
    """Return a detached canonical DTO or raise ValueError/ConfigurationError."""
    value = json_copy(value)
    if (
        not isinstance(value, dict)
        or not _REQUIRED <= value.keys()
        or value.keys() - (_REQUIRED | _OPTIONAL)
        or not all(isinstance(value[k], str) and value[k] for k in _REQUIRED)
        or value["category"] not in ERROR_CATEGORIES
        or value["phase"] not in ERROR_PHASES
        or value["external_outcome"] not in EXTERNAL_OUTCOMES
        or type(value.get("retryable_transport", False)) is not bool
        or value.get("retry_after_at") is not None
        and type(value["retry_after_at"]) not in (int, float)
        or value.get("field_path") is not None
        and not isinstance(value["field_path"], str)
        or value.get("provider_code") is not None
        and type(value["provider_code"]) not in (str, int)
    ):
        raise ValueError("Invalid OperationError")
    return value
