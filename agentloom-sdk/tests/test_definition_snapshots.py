"""Persisted action validation uses the same variable semantics as live definitions."""

import json

import pytest

from agentloom import AgentDefinition, AgentLoomError, RequestError, VariableSpec
from agentloom.configuration.definitions import (
    definition_snapshot,
    manifest_definition_snapshot,
    restore_definition,
    variable_contract,
)
from agentloom.configuration.json_documents import canonical_document


def serialized(value):
    return json.loads(canonical_document(value))


def definition():
    return AgentDefinition(
        "worker",
        "{{spec}} {{mapping}} {{decimal}} {{integer}}",
        variables={
            "spec": VariableSpec({"type": "string"}, "SAFE", False),
            "mapping": {"schema": {"type": "string"}, "default": "SAFE", "required": False},
            "decimal": 1.0,
            "integer": 1,
        },
    )


@pytest.mark.parametrize("source", ["action", "manifest"])
def test_variable_contract_survives_canonical_roundtrip(source):
    original = definition()
    snapshot = definition_snapshot(original)
    if source == "manifest":
        snapshot = manifest_definition_snapshot(
            serialized(
                {
                    "definitions": {"worker": snapshot["definition"]},
                    "variable_contracts": {"worker": variable_contract(original)},
                }
            ),
            "worker",
        )
    restored = restore_definition(serialized(snapshot))
    assert isinstance(restored.variables["spec"], VariableSpec)
    assert isinstance(restored.variables["mapping"], dict)
    assert type(restored.variables["decimal"]) is float
    assert type(restored.variables["integer"]) is int
    assert restored.render({"decimal": 1.5}) == original.render({"decimal": 1.5})
    with pytest.raises(RequestError, match="VARIABLE_TYPE_INVALID"):
        restored.render({"integer": 1.5})


def test_legacy_action_snapshot_retains_explicit_spec_evidence():
    original = AgentDefinition("a", "{{v}}", variables={"v": VariableSpec({}, "SAFE", False)})
    snapshot = definition_snapshot(original)
    del snapshot["variable_types"]
    assert restore_definition(serialized(snapshot)).render() == "SAFE"


def test_legacy_action_snapshot_cannot_guess_integral_number_default_type():
    snapshot = definition_snapshot(AgentDefinition("a", "{{v}}", variables={"v": 1.0}))
    del snapshot["variable_types"]
    with pytest.raises(AgentLoomError, match="HOOK_VARIABLE_VALIDATION_UNAVAILABLE"):
        restore_definition(serialized(snapshot))
