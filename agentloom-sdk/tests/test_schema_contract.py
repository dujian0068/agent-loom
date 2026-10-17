"""Schema locations, offline references and runtime error containment."""

import pytest

from agentloom import AgentDefinition, ConfigurationError, RequestError
from agentloom.configuration import check_schema, validate_schema


@pytest.mark.parametrize(
    "schema,value",
    [
        ({"enum": [{"$ref": "https://example.invalid/x"}]}, {"$ref": "https://example.invalid/x"}),
        ({"const": {"$dynamicRef": "literal"}}, {"$dynamicRef": "literal"}),
        ({"properties": {"$ref": {"type": "string"}}}, {"$ref": "value"}),
        ({"default": {"$ref": "data"}, "examples": [{"$ref": "also data"}]}, {}),
        ({"$defs": {"n": {"$anchor": "number", "type": "integer"}}, "$ref": "#number"}, 1),
        ({"$defs": {"a/b~c": {"type": "integer"}}, "$ref": "#/$defs/a~1b~0c"}, 1),
        (
            {
                "$defs": {
                    "node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/node"}}}
                },
                "$ref": "#/$defs/node",
            },
            {"child": {}},
        ),
        (
            {
                "$id": "https://example.invalid/root",
                "$defs": {
                    "inner": {
                        "$id": "inner",
                        "$defs": {"n": {"type": "integer"}},
                        "$ref": "#/$defs/n",
                    }
                },
                "$ref": "#/$defs/inner",
            },
            3,
        ),
        (
            {
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {"child": {"$dynamicRef": "#node"}},
            },
            {"child": {}},
        ),
        (True, {"anything": [1, None]}),
    ],
)
def test_schema_data_and_resolvable_local_scopes(schema, value):
    definition = AgentDefinition("schema", "SYSTEM", input_schema=schema)
    validate_schema(value, definition.input_schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#missing"},
        {"properties": {"value": {"$ref": "#/$defs/missing"}}},
        {"$defs": {"unused": {"$ref": "#missing"}}},
        {"definitions": {"old": {"$ref": "#missing"}}, "$ref": "#/definitions/old"},
        {"$dynamicRef": "#missing"},
        {"$ref": "#/const", "const": 1},
        {"$ref": "https://example.invalid/schema"},
        {"items": {"$ref": "other.json"}},
        {"type": "not-a-type"},
    ],
)
def test_invalid_schema_rejected_before_execution(schema):
    with pytest.raises(ConfigurationError):
        check_schema(schema)


def test_runtime_reference_failure_is_distinct_from_instance_mismatch():
    with pytest.raises(ConfigurationError, match="INVALID_SCHEMA"):
        validate_schema({}, {"$ref": "#/$defs/missing"}, code="OUTPUT_SCHEMA_INVALID")
    with pytest.raises(ConfigurationError, match="INVALID_SCHEMA"):
        validate_schema({}, {"$ref": "https://example.invalid/unavailable"})
    with pytest.raises(RequestError, match="OUTPUT_SCHEMA_INVALID"):
        validate_schema("not an integer", {"type": "integer"}, code="OUTPUT_SCHEMA_INVALID")
    with pytest.raises(RequestError):
        validate_schema({}, False)


def test_recursive_schema_that_cannot_evaluate_fails_with_sdk_error():
    check_schema({"$ref": "#"})
    with pytest.raises(ConfigurationError, match="INVALID_SCHEMA"):
        validate_schema({}, {"$ref": "#"})
