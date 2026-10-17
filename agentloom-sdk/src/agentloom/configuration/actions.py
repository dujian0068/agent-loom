"""Serializable action admission rules shared by live calls and saved Hook patches."""

from dataclasses import asdict

from ..errors import AgentLoomError
from .json_values import json_copy
from .models import AgentDefinition, VariableSpec
from .schemas import validate_schema


def definition_snapshot(definition):
    return {
        "definition": asdict(definition),
        "variable_specs": [
            key for key, value in definition.variables.items() if isinstance(value, VariableSpec)
        ],
    }


def restore_definition(snapshot):
    value = snapshot["definition"]
    variables = dict(value["variables"])
    if snapshot.get("legacy_variables") and any(
        isinstance(v, dict) and set(v) == {"schema", "default", "required"}
        for v in variables.values()
    ):
        raise AgentLoomError("HOOK_VARIABLE_VALIDATION_UNAVAILABLE")
    for key in snapshot["variable_specs"]:
        variables[key] = VariableSpec(**variables[key])
    return AgentDefinition(
        value["name"],
        value["prompt"],
        value["description"],
        variables=variables,
        input_schema=value["input_schema"],
        output_schema=value["output_schema"],
    )


def action_contract(program, role, tools):
    return json_copy(
        {
            "tools": tools,
            "executors": {name: definition_snapshot(d) for name, d in program.executors.items()},
            "skills": {
                name: entry.registration.package.metadata
                for name, entry in program.catalogs[role].skills.items()
            },
        }
    )


def validate_action_contract(contract, name, arguments):
    tool = next((item for item in contract["tools"] if item["name"] == name), None)
    if tool is None:
        raise AgentLoomError("CAPABILITY_NOT_FOUND", category="permission")
    if not isinstance(arguments, dict):
        raise AgentLoomError("TOOL_ARGUMENTS_INVALID", category="validation")
    validate_schema(arguments, tool["parameters"])
    if name == "delegate_task":
        definition = restore_definition(contract["executors"][arguments["executor"]])
        value = arguments["task"]
        validate_schema(value, definition.input_schema)
        if definition.input_schema is None and (not isinstance(value, str) or not value.strip()):
            raise AgentLoomError("EXECUTOR_INPUT_INVALID", category="validation")
        definition.render(arguments.get("variables"))
    elif name in {"skill_read", "skill_read_resource"}:
        metadata = contract["skills"].get(arguments["name"])
        if metadata is None or metadata.get("disable-model-invocation"):
            raise AgentLoomError("SKILL_NOT_AUTHORIZED", category="permission")
        if name == "skill_read":
            from ..capabilities.skills import parse_arguments

            parse_arguments(arguments.get("raw_arguments"), arguments.get("arguments"))
