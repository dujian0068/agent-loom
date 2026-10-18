"""Classify final candidate errors separately from unusable output schemas."""

import json

from ..configuration import json_copy, validate_schema
from ..errors import ConfigurationError, RequestError


def validate_output(candidate, schema):
    """Return a valid value, or a correctable RequestError for invalid model data.

    Schema configuration failures propagate from validate_schema. Canonical JSON
    constraints belong to the candidate boundary, even though the shared copier
    uses ConfigurationError when called by configuration builders.
    """
    value = candidate
    if schema is not None:
        try:
            value = json_copy(json.loads(candidate))
        except (ValueError, RecursionError, ConfigurationError) as exc:
            raise RequestError("OUTPUT_SCHEMA_INVALID", category="validation") from exc
    validate_schema(value, schema, code="OUTPUT_SCHEMA_INVALID")
    if schema is None and not value.strip():
        raise RequestError("OUTPUT_EMPTY", category="validation")
    return value
