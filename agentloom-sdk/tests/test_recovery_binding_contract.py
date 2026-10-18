"""Recovery rejects semantic configuration changes before dispatching any work."""

from dataclasses import asdict, replace

import pytest

from agentloom import (
    AgentDefinition,
    InfrastructureError,
    LocalEnvironment,
    ModelOptions,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RequestError,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    VariableSpec,
    WorkspaceBinding,
)
from agentloom.configuration.json_documents import canonical_document
from agentloom.runtime.recovery import validate_recovery


class OptionsModel(ScriptedModel):
    """The test adapter accepts the custom JSON parameters used by this matrix."""

    def validate_options(self, options):
        pass


def program(runtime, definition, role, *, model_options=None):
    def respond(request):
        if request.role_key == "planner" and role == "worker":
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "go"}),)
                )
            return ModelResponse("done")
        return ModelResponse("true" if definition.output_schema else "done")

    options = {
        "runtime_engine": runtime,
        "workspace": WorkspaceBinding("workspace"),
        "model": OptionsModel(respond),
        "model_options": model_options,
        "definition": definition,
    }
    if role == "worker":
        options.update(
            definition=AgentDefinition("planner", "Delegate then summarize."),
            executors={"worker": definition},
        )
    return (ReactAgent if role == "react" else PlanAgent)(**options)


async def suspend(store, definition, role, *, model_options=None):
    def checkpoint(command, state):
        if command.startswith("decision:" if role == "worker" else "environment:"):
            raise RuntimeError("Stop before the target role is created")

    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        agent = program(runtime, definition, role, model_options=model_options)
        handle = await agent.start("go", run_id="recovery")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
        state = runtime._load(handle.run_id)
        assert all(t["role_key"] != role for t in state["tasks"].values())
        return state


class NoPreparation(LocalEnvironment):
    def prepare(self, *args, **kwargs):
        pytest.fail("Rejected recovery must not prepare an environment")


@pytest.mark.parametrize("role", ["react", "planner", "worker"])
@pytest.mark.parametrize("location", ["const", "enum", "variable", "model_options"])
async def test_boolean_and_number_are_distinct_recovery_bindings(runtime_store, role, location):
    original = AgentDefinition("agent", "Mode: {{mode}}", "Perform work", variables={"mode": True})
    changed = original
    before_options = after_options = None
    if location == "const":
        original = replace(original, output_schema={"const": True})
        changed = replace(original, output_schema={"const": 1})
    elif location == "enum":
        original = replace(original, output_schema={"enum": [{"flags": [False, True]}]})
        changed = replace(original, output_schema={"enum": [{"flags": [0, 1]}]})
    elif location == "variable":
        changed = replace(original, variables={"mode": 1})
    else:
        before_options = ModelOptions(parameters={"custom": {"flags": [True, False]}})
        after_options = ModelOptions(parameters={"custom": {"flags": [1, 0]}})
    saved = await suspend(runtime_store, original, role, model_options=before_options)
    async with RuntimeEngine(store=runtime_store, environment=NoPreparation()) as runtime:
        agent = program(runtime, changed, role, model_options=after_options)
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await agent.start(run_id="recovery")
        assert not agent.model.requests
        assert canonical_document(runtime._load("recovery")) == canonical_document(saved)


@pytest.mark.parametrize("role", ["react", "planner", "worker"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_variable_spec_and_dictionary_default_cannot_replace_each_other(
    runtime_store, role, reverse
):
    spec = VariableSpec({"type": "string"}, default="SAFE", required=False)
    original = AgentDefinition("agent", "Mode: {{mode}}", "Perform work", variables={"mode": spec})
    changed = replace(original, variables={"mode": asdict(spec)})
    if reverse:
        original, changed = changed, original
    assert original.render() != changed.render()
    saved = await suspend(runtime_store, original, role)
    async with RuntimeEngine(store=runtime_store, environment=NoPreparation()) as runtime:
        agent = program(runtime, changed, role)
        assert agent.program_key != program(runtime, original, role).program_key
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await agent.start(run_id="recovery")
        assert not agent.model.requests
        assert canonical_document(runtime._load("recovery")) == canonical_document(saved)


@pytest.mark.parametrize(
    "default",
    [1.0, {"schema": {}, "default": "SAFE", "required": False}, VariableSpec({}, "SAFE", False)],
)
@pytest.mark.parametrize("role", ["react", "worker"])
async def test_original_variable_types_and_dictionary_order_can_resume(
    runtime_store, role, default
):
    definition = AgentDefinition(
        "agent", "Mode: {{mode}}", "Perform work", variables={"mode": default, "other": "value"}
    )
    await suspend(runtime_store, definition, role)
    reordered = replace(definition, variables=dict(reversed(list(definition.variables.items()))))
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = program(runtime, reordered, role)
        result = await agent.ainvoke(run_id="recovery")
        assert result.status == "succeeded"
        request = next(r for r in agent.model.requests if r.role_key == role)
        assert request.messages[0]["content"] == definition.render()


async def test_integral_number_default_cannot_turn_into_integer_default(runtime_store):
    definition = AgentDefinition("agent", "Value {{v}}", variables={"v": 1.0})
    await suspend(runtime_store, definition, "react")
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = program(runtime, replace(definition, variables={"v": 1}), "react")
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await agent.start(run_id="recovery")
        assert not agent.model.requests


def legacy_manifest(store):
    with store.transaction("default") as tx:
        state = tx.get("runs", "recovery")
        del state["bindings"]["binding_version"]
        del state["bindings"]["variable_contracts"]
        tx.put("runs", "recovery", state)


@pytest.mark.parametrize("role", ["react", "worker"])
@pytest.mark.parametrize("default", ["SAFE", {"flags": [True, 1]}])
async def test_unambiguous_legacy_binding_can_resume_without_rewriting_history(
    runtime_store, role, default
):
    definition = AgentDefinition("a", "Mode: {{mode}}", "Work", variables={"mode": default})
    saved = await suspend(runtime_store, definition, role)
    legacy_manifest(runtime_store)
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = program(runtime, definition, role)
        result = await agent.ainvoke(run_id="recovery")
        assert result.status == "succeeded"
        state = runtime._load(result.run_id)
        assert "binding_version" not in state["bindings"]
        assert state["prompt_contract"] == saved["prompt_contract"]
        validate_recovery(state)


@pytest.mark.parametrize("role", ["react", "worker"])
@pytest.mark.parametrize(
    "default",
    [VariableSpec({}, "SAFE", False), {"schema": {}, "default": "SAFE", "required": False}, 1, 1.0],
)
async def test_ambiguous_legacy_variables_are_not_guessed_from_current_program(
    runtime_store, role, default
):
    definition = AgentDefinition("a", "Mode: {{mode}}", "Work", variables={"mode": default})
    await suspend(runtime_store, definition, role)
    legacy_manifest(runtime_store)
    async with RuntimeEngine(store=runtime_store, environment=NoPreparation()) as runtime:
        agent = program(runtime, definition, role)
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH") as caught:
            await agent.start(run_id="recovery")
        assert caught.value.field_path == f"bindings.definitions.{role}.variables.mode"
        assert not agent.model.requests


async def test_legacy_schema_comparison_still_separates_booleans_and_numbers(runtime_store):
    definition = AgentDefinition("a", "Return true", output_schema={"const": True})
    await suspend(runtime_store, definition, "react")
    legacy_manifest(runtime_store)
    async with RuntimeEngine(store=runtime_store) as runtime:
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await program(runtime, replace(definition, output_schema={"const": 1}), "react").start(
                run_id="recovery"
            )
        assert (
            await program(runtime, definition, "react").ainvoke(run_id="recovery")
        ).status == "succeeded"


@pytest.mark.parametrize(
    "corruption", ["version", "missing_contracts", "missing_role", "missing_definitions"]
)
async def test_incomplete_versioned_binding_never_falls_back_to_legacy(runtime_store, corruption):
    definition = AgentDefinition("a", "Mode: {{mode}}", variables={"mode": "SAFE"})
    await suspend(runtime_store, definition, "react")
    with runtime_store.transaction("default") as tx:
        state = tx.get("runs", "recovery")
        bindings = state["bindings"]
        if corruption == "version":
            bindings["binding_version"] = True
        elif corruption == "missing_contracts":
            del bindings["variable_contracts"]
        elif corruption == "missing_role":
            del bindings["variable_contracts"]["react"]
        else:
            del bindings["definitions"]
        tx.put("runs", "recovery", state)
    async with RuntimeEngine(store=runtime_store, environment=NoPreparation()) as runtime:
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await program(runtime, definition, "react").start(run_id="recovery")


async def test_persisted_policy_cannot_exchange_false_and_zero(runtime_store):
    state = await suspend(runtime_store, AgentDefinition("a", "Go"), "react")
    state["policy"]["parallel_enabled"] = 0
    with pytest.raises(InfrastructureError, match="PERSISTENCE_INVARIANT_BROKEN") as caught:
        validate_recovery(state)
    assert caught.value.field_path == "policy"
