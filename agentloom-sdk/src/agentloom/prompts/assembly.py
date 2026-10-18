"""Freeze generated instructions once per Run, before any role is activated."""

from ..configuration.json_documents import canonical_document
from ..configuration.models import VariableSpec
from ..configuration.variables import default_variable_schema
from . import templates

PROMPT_RECORD_KIND = "sdk_instructions"
PROMPT_CONTRACT_VERSION = 1


def profile_for_role(mode, role):
    return "react" if mode == "react" else "planner" if role == "planner" else "executor"


def create_prompt_contract(program):
    roles = {}
    for role, definition in program.definitions.items():
        profile = profile_for_role(program.mode, role)
        content = templates.ROLES[profile] + "\n\n" + templates.COMMON
        if profile == "planner":
            content += "\n\n" + (
                templates.DELEGATION if program.executors else templates.LOCAL_EXECUTION
            )
        if profile == "planner" and any(
            d.input_schema is not None or d.variables for d in program.executors.values()
        ):
            content += "\n\n" + _executor_inputs(program.executors)
        if definition.output_schema is not None:
            content += (
                "\n\nFinal output must be one JSON value matching this schema, without Markdown "
                "fences or extra prose. This requirement applies to the final answer, not "
                "intermediate tool calls.\n" + canonical_document(definition.output_schema)
            )
        roles[role] = {
            "profile": profile,
            "template_version": templates.TEMPLATE_VERSION,
            "content": content,
        }
    return {"schema_version": PROMPT_CONTRACT_VERSION, "roles": roles}


def _executor_inputs(executors):
    contracts = {
        key: {
            "task_schema": definition.input_schema
            if definition.input_schema is not None
            else {"type": "string", "minLength": 1},
            "variables": {
                name: {"schema": spec.schema, "required": spec.required}
                if isinstance(spec, VariableSpec)
                else {"schema": default_variable_schema(spec), "required": False}
                for name, spec in definition.variables.items()
            },
        }
        for key, definition in executors.items()
    }
    return (
        "Executor input contracts for delegate_task: task must satisfy task_schema; "
        "variables must satisfy each declared schema. Supply required variables; optional "
        "variables may be omitted to use the executor's private defaults. Never send "
        "undeclared variables.\n" + canonical_document(contracts)
    )
