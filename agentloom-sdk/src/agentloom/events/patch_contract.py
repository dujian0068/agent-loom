"""Persist the pure validation inputs needed to settle a saved Hook on stop."""

from ..configuration import ContextPolicy, ModelOptions, digest, json_copy
from ..configuration.actions import action_contract
from ..context.budget import ContextBudget
from ..errors import AgentLoomError, InfrastructureError, error_record
from ..types import ModelDescriptor


def patch_contract(runner, role):
    program = runner.program
    return {
        "schema_version": 1,
        "actions": action_contract(program, role, runner.tools(role)),
        "outputs": {
            name: json_copy(entry.spec.output_schema)
            for name, entry in program.catalogs[role].tools.items()
        },
        "model_output_limit": program.model.descriptor.max_output_tokens,
        "input_budget": program.budget.input,
        "parameters": json_copy(program.model_options.parameters),
    }


def validate_parameter_patch(result, model):
    """Save the adapter's offline feature check before the result/apply boundary."""
    patch = result.get("payload_patch")
    if not isinstance(patch, dict) or "parameters" not in patch:
        return None
    parameters = patch["parameters"]
    diagnostic = None
    try:
        if not hasattr(model, "validate_options"):
            raise AgentLoomError("MODEL_FEATURE_UNSUPPORTED")
        model.validate_options(ModelOptions(parameters=parameters))
    except InfrastructureError:
        raise
    except Exception as exc:
        diagnostic = error_record(exc)
    return {"hash": digest(parameters), "error": diagnostic}


def legacy_patch_contract(state, op):
    """Recover old pure schemas from bindings and the role's frozen model view.

    Older checkpoints lack an adapter parameter-validation receipt. Those edits
    fail closed with a diagnostic; a saved deny still remains authoritative.
    """
    bindings = state["bindings"]
    role = op["role_key"]
    catalog = bindings["capabilities"][role]
    requests = [
        item["request"]
        for item in state["operations"].values()
        if item["event_type"] == "model.generate"
        and item["purpose"] == "business"
        and item["task_id"] == op["task_id"]
    ]
    definitions = {
        name: {"definition": definition, "variable_specs": []}
        for name, definition in bindings["definitions"].items()
        if name not in {"react", "planner"}
    }
    # The old manifest flattened VariableSpec and ordinary dict defaults alike.
    # Never guess their meaning when validating a delegated variable edit.
    for snapshot in definitions.values():
        snapshot["legacy_variables"] = True
    descriptor = ModelDescriptor(**bindings["model_descriptor"])
    options = ModelOptions(**bindings["model_options"])
    budget = ContextBudget.resolve(descriptor, ContextPolicy(**bindings["context_policy"]), options)
    return {
        "schema_version": 1,
        "actions": {
            "tools": requests[-1]["tools"] if requests else [],
            "executors": definitions,
            "skills": {name: entry["metadata"] for name, entry in catalog["skills"].items()},
        },
        "outputs": {name: spec["output_schema"] for name, spec in catalog["tools"].items()},
        "model_output_limit": descriptor.max_output_tokens,
        "input_budget": budget.input,
        "parameters": json_copy(options.parameters),
    }
