"""Pure action admission checks, before Operation/Attempt or child creation."""

from ..configuration import validate_schema
from ..errors import AgentLoomError


def validate_action(runner, role, name, arguments):
    tool = next((item for item in runner.tools(role) if item["name"] == name), None)
    if tool is None:
        raise AgentLoomError("CAPABILITY_NOT_FOUND", category="permission")
    if not isinstance(arguments, dict):
        raise AgentLoomError("TOOL_ARGUMENTS_INVALID", category="validation")
    validate_schema(arguments, tool["parameters"])
    if name == "delegate_task":
        definition = runner.program.executors[arguments["executor"]]
        value = arguments["task"]
        validate_schema(value, definition.input_schema)
        if definition.input_schema is None and (not isinstance(value, str) or not value.strip()):
            raise AgentLoomError("EXECUTOR_INPUT_INVALID", category="validation")
        definition.render(arguments.get("variables"))
    elif name in {"skill_read", "skill_read_resource"}:
        entry = runner.program.catalogs[role].skills.get(arguments["name"])
        if entry is None or entry.registration.package.metadata.get("disable-model-invocation"):
            raise AgentLoomError("SKILL_NOT_AUTHORIZED", category="permission")
        if name == "skill_read":
            from ..capabilities.skills import parse_arguments

            parse_arguments(arguments.get("raw_arguments"), arguments.get("arguments"))
