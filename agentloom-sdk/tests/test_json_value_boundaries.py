"""Invalid container graphs cannot escape the canonical JSON boundary."""

import sys
from collections import UserDict

import pytest

from agentloom import AgentLoomError, ConfigurationError
from agentloom.capabilities.invocation import result_from_value
from agentloom.configuration import canonical, freeze, json_copy


@pytest.mark.parametrize("convert", [json_copy, canonical, freeze])
@pytest.mark.parametrize("shape", ["list", "dict", "mixed"])
def test_circular_container_graphs_raise_configuration_errors(convert, shape):
    if shape == "dict":
        value = {}
        value["self"] = value
    else:
        value = []
        value.append(value if shape == "list" else (UserDict({"child": value}),))
    with pytest.raises(ConfigurationError, match="INVALID_JSON"):
        convert(value)


@pytest.mark.parametrize("convert", [json_copy, canonical, freeze])
def test_excessive_nesting_does_not_leak_recursion_error(convert):
    value = None
    for _ in range(sys.getrecursionlimit() + 10):
        value = [value]
    with pytest.raises(ConfigurationError, match="INVALID_JSON"):
        convert(value)


def test_freezing_overflow_after_json_copy_is_a_completed_protocol_failure():
    value = None
    for _ in range(sys.getrecursionlimit() * 3 // 4):
        value = [value]
    # Freezing needs more stack than validating a plain JSON value. Its own
    # boundary must also reject unsupported nesting before the value is returned.
    with pytest.raises(ConfigurationError, match="INVALID_JSON"):
        freeze(value)
    with pytest.raises(AgentLoomError) as caught:
        result_from_value(value)
    assert caught.value.code == "CAPABILITY_RESULT_INVALID"
    assert caught.value.execution_fact == "completed"


def test_shared_children_are_copied_as_values_not_rejected_as_cycles():
    child = UserDict({"nested": [1, None]})
    source = {"left": child, "right": (child, child)}
    copied = json_copy(source)
    assert copied == {"left": {"nested": [1, None]}, "right": [{"nested": [1, None]}] * 2}
    assert canonical(source) == canonical(copied)
    assert json_copy(freeze(source)) == copied
    copied["left"]["nested"].append(2)
    assert child["nested"] == copied["right"][0]["nested"] == [1, None]
