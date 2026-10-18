"""Variable type inference shared by validation and model-visible input contracts."""

from .json_values import json_copy


def default_variable_schema(default):
    """Expose the accepted JSON type without disclosing a private default value."""
    kind = {
        type(None): "null",
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }[type(json_copy(default))]
    return {"type": kind}
