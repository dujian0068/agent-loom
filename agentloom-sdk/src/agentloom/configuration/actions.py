"""Serializable action admission rules shared by live calls and saved Hook patches."""

from ..errors import AgentLoomError
from .definitions import definition_snapshot, restore_definition
from .json_documents import copy_document as json_copy
from .schemas import validate_schema


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
