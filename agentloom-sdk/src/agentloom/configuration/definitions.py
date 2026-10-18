"""Preserve variable declaration semantics in recoverable Definition snapshots."""

from dataclasses import asdict

from ..errors import AgentLoomError
from .models import AgentDefinition, VariableSpec
from .variables import default_variable_schema


def variable_contract(definition):
    """Separate explicit specs from defaults, including integer/number inference.

    Canonical JSON can encode 1.0 as 1. The inferred default type must therefore
    travel separately, just like the VariableSpec discriminator.
    """
    return {
        "specs": sorted(
            key for key, value in definition.variables.items() if isinstance(value, VariableSpec)
        ),
        "defaults": {
            key: default_variable_schema(value)["type"]
            for key, value in definition.variables.items()
            if not isinstance(value, VariableSpec)
        },
    }


def definition_snapshot(definition):
    contract = variable_contract(definition)
    return {
        "definition": asdict(definition),
        "variable_specs": contract["specs"],
        "variable_types": contract["defaults"],
    }


def manifest_definition_snapshot(bindings, role):
    """Use saved type evidence only; old ambiguous declarations remain unproven."""
    snapshot = {"definition": bindings["definitions"][role], "variable_specs": []}
    contracts = bindings.get("variable_contracts")
    if contracts is None:
        snapshot["legacy_variables"] = True
    else:
        contract = contracts[role]
        snapshot.update(variable_specs=contract["specs"], variable_types=contract["defaults"])
    return snapshot


def ambiguous_legacy_variable(value):
    """Old JSON cannot distinguish a spec-shaped dict or an integral number default."""
    return (
        type(value) is int
        or type(value) is float
        and value.is_integer()
        or isinstance(value, dict)
        and set(value) == {"schema", "default", "required"}
    )


def restore_definition(snapshot):
    value = snapshot["definition"]
    variables = dict(value["variables"])
    if snapshot.get("legacy_variables") and any(
        ambiguous_legacy_variable(v) for v in variables.values()
    ):
        raise AgentLoomError("HOOK_VARIABLE_VALIDATION_UNAVAILABLE")
    if "variable_types" not in snapshot and any(
        type(value) in (int, float)
        and ambiguous_legacy_variable(value)
        and key not in snapshot["variable_specs"]
        for key, value in variables.items()
    ):
        raise AgentLoomError("HOOK_VARIABLE_VALIDATION_UNAVAILABLE")
    for key in snapshot["variable_specs"]:
        variables[key] = VariableSpec(**variables[key])
    for key, kind in snapshot.get("variable_types", {}).items():
        if kind == "number" and type(variables[key]) is int:
            variables[key] = float(variables[key])
    return AgentDefinition(
        value["name"],
        value["prompt"],
        value["description"],
        variables=variables,
        input_schema=value["input_schema"],
        output_schema=value["output_schema"],
    )
