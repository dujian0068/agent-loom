"""Definition-based assembly preserves isolation, authorization and recovery bindings."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from conftest import assert_finished, binding, tool

from agentloom import (
    AgentBuilder,
    AgentDefinition,
    AgentInput,
    CapabilityRegistry,
    ConfigurationError,
    ContextPolicy,
    ExecutionPolicy,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookMatcher,
    HookSet,
    InfrastructureError,
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
from agentloom.configuration.assembly import recovery_manifest


def make_builder(tmp_path, **options):
    if "runtime_engine" not in options:
        options["runtime_engine"] = RuntimeEngine()
    defaults = {
        "model": ScriptedModel(lambda request: ModelResponse("done")),
        "workspace": WorkspaceBinding(tmp_path / "work"),
    }
    return AgentBuilder(**(defaults | options))


def test_build_is_local_and_requires_no_open_runtime(tmp_path):
    def unexpected(*args):
        pytest.fail("build performed execution or discovery")

    model = ScriptedModel(unexpected)
    model.discover = unexpected
    hooks = HookSet(
        (HookBinding("h", "model.generate", "request", "h"),),
        {"h": FunctionHookHandler(unexpected)},
    )
    runtime = RuntimeEngine()
    before = set(tmp_path.rglob("*"))
    builder = make_builder(tmp_path, model=model, hooks=hooks, runtime_engine=runtime)
    first = builder.build(AgentDefinition("a", "SYSTEM"))
    second = builder.with_options().build(first.definition)
    assert set(tmp_path.rglob("*")) == before
    assert first.context is second.context is None
    assert not model.requests
    assert builder.runtime_engine._status == "created"
    with pytest.raises(FrozenInstanceError):
        builder.model = ScriptedModel(unexpected)
    assert first is not second and first._binding_lock is not second._binding_lock
    assert first.program_key == second.program_key


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, ReactAgent),
        ({"mode": "react"}, ReactAgent),
        ({"mode": "plan"}, PlanAgent),
        ({"executors": {}}, PlanAgent),
        ({"mode": "plan", "executors": {}}, PlanAgent),
    ],
)
def test_explicit_and_inferred_modes_keep_direct_recovery_identity(tmp_path, options, expected):
    builder = make_builder(tmp_path)
    definition = AgentDefinition("a", "SYSTEM")
    built = builder.build(definition, **options)
    direct = expected(
        definition=definition,
        model=builder.model,
        workspace=builder.workspace,
        runtime_engine=builder.runtime_engine,
    )
    assert type(built) is expected
    assert recovery_manifest(built) == recovery_manifest(direct)
    assert built.program_key == direct.program_key


@pytest.mark.parametrize(
    "options,code",
    [
        ({"mode": "workflow"}, "AGENT_MODE_INVALID"),
        ({"mode": []}, "AGENT_MODE_INVALID"),
        ({"mode": "react", "executors": {}}, "EXECUTORS_UNSUPPORTED"),
        ({"executors": []}, "EXECUTORS_INVALID"),
        ({"executors": {"planner": AgentDefinition("a", "SYS", "work")}}, "EXECUTOR_KEY_INVALID"),
        ({"executors": {"worker": AgentDefinition("a", "SYS")}}, "EXECUTOR_DEFINITION_INVALID"),
    ],
)
def test_invalid_build_does_not_poison_reusable_builder(tmp_path, options, code):
    builder = make_builder(tmp_path)
    with pytest.raises(ConfigurationError, match=code):
        builder.build(AgentDefinition("a", "SYS"), **options)
    assert type(builder.build(AgentDefinition("a", "SYS"))) is ReactAgent
    assert not builder.model.requests


@pytest.mark.parametrize(
    "options,code",
    [
        ({"model": None}, "MODEL_ADAPTER_INVALID"),
        ({"workspace": "work"}, "WORKSPACE_BINDING_REQUIRED"),
        ({"runtime_engine": None}, "RUNTIME_ENGINE_REQUIRED"),
        ({"capabilities": {}}, "INVALID_CONFIGURATION"),
        ({"hooks": []}, "INVALID_CONFIGURATION"),
        ({"execution_policy": {}}, "INVALID_CONFIGURATION"),
        ({"context_policy": {}}, "INVALID_CONFIGURATION"),
        ({"model_options": {}}, "INVALID_CONFIGURATION"),
        ({"model_options": ModelOptions(parameters={"unknown": 1})}, "MODEL_FEATURE_UNSUPPORTED"),
    ],
)
def test_builder_and_derivation_reuse_constructor_validation(tmp_path, options, code):
    builder = make_builder(tmp_path)
    with pytest.raises(ConfigurationError, match=code):
        make_builder(tmp_path, **options)
    with pytest.raises(ConfigurationError, match=code):
        builder.with_options(**options)
    with pytest.raises(ConfigurationError, match=code):
        ReactAgent(definition=AgentDefinition("a", "SYS"), **(builder._agent_kwargs() | options))


def test_derivation_replaces_and_resets_without_mutating_prior_agents(tmp_path):
    registry = CapabilityRegistry(tools=(tool(lambda arguments, context: "value"),))
    hooks = HookSet(
        (HookBinding("h", "model.generate", "request", "h"),),
        {"h": FunctionHookHandler(lambda event, context: HookDecision())},
    )
    builder = make_builder(
        tmp_path,
        capabilities=registry,
        hooks=hooks,
        execution_policy=ExecutionPolicy(loop_max=3),
        context_policy=ContextPolicy(recent_runs=0),
        model_options=ModelOptions(max_output_tokens=2048),
    )
    definition = AgentDefinition("a", "SYS", capabilities=(binding(),))
    first = builder.build(definition)
    other = builder.with_options(execution_policy=ExecutionPolicy(loop_max=4))
    assert other.capabilities is builder.capabilities is registry
    assert other.build(definition).execution_policy.loop_max == 4
    assert builder.execution_policy.loop_max == first.execution_policy.loop_max == 3
    reset = other.with_options(
        capabilities=None,
        hooks=None,
        execution_policy=None,
        context_policy=None,
        model_options=None,
    )
    clean = reset.build(AgentDefinition("clean", "SYS"))
    assert clean.execution_policy == ExecutionPolicy()
    assert clean.context_policy == ContextPolicy() and clean.model_options == ModelOptions()
    assert not clean.catalogs["react"].tools and not clean.hooks.bindings
    with pytest.raises(ConfigurationError, match="CAPABILITY_NOT_FOUND"):
        reset.build(definition)
    assert builder.build(definition).program_key == first.program_key
    with pytest.raises(ConfigurationError, match="CONTEXT_BUDGET_INVALID"):
        builder.with_options(
            model=ScriptedModel(lambda r: ModelResponse(), context_window_tokens=2048)
        )
    small = builder.with_options(
        model=ScriptedModel(lambda r: ModelResponse(), context_window_tokens=2048),
        model_options=None,
    )
    assert small.build(definition).budget.window == 2048
    for keyword in ("mode", "executors", "definition", "budget", "typo"):
        with pytest.raises(TypeError, match="Unknown AgentBuilder options"):
            builder.with_options(**{keyword: None})


def test_build_validates_definitions_and_role_matchers(tmp_path):
    builder = make_builder(tmp_path)
    with pytest.raises(ConfigurationError, match="DEFINITION_REQUIRED"):
        builder.build({"name": "agent", "prompt": "SYSTEM"})
    only_worker = builder.with_options(
        hooks=HookSet(
            (
                HookBinding(
                    "h",
                    "model.generate",
                    "request",
                    "h",
                    matcher=HookMatcher(role_keys=("worker",)),
                ),
            ),
            {"h": FunctionHookHandler(lambda event, context: HookDecision("deny"))},
        )
    )
    definition = AgentDefinition("a", "SYS")
    with pytest.raises(ConfigurationError, match="HOOK_MATCHER_ROLE_UNKNOWN"):
        only_worker.build(definition)
    plan = only_worker.build(
        definition, executors={"worker": AgentDefinition("w", "WORKER", "work")}
    )
    assert plan.hooks.bindings[0].matcher.role_keys == ("worker",)
    assert not builder.build(definition).hooks.bindings


async def test_derived_settings_cannot_rebind_an_active_run(tmp_path, runtime_store):
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        started.set()
        await release.wait()
        return ModelResponse("done")

    async with RuntimeEngine(store=runtime_store) as runtime:
        builder = make_builder(tmp_path, runtime_engine=runtime, model=ScriptedModel(respond))
        definition = AgentDefinition("a", "SYS")
        handle = await builder.build(definition).start("go")
        await asyncio.wait_for(started.wait(), timeout=2)
        changed = builder.with_options(execution_policy=ExecutionPolicy(loop_max=1))
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await changed.build(definition).start(run_id=handle.run_id)
        attached = await builder.build(definition).start(run_id=handle.run_id)
        release.set()
        results = await asyncio.gather(handle.wait(timeout_s=2), attached.wait(timeout_s=2))
        assert results[0].output == results[1].output
        assert len(builder.model.requests) == 1


async def test_repeated_builds_are_independent_runs_and_preserve_structured_definition(
    tmp_path, runtime_store
):
    model = ScriptedModel(lambda r: ModelResponse('{"ok":true}'))
    definition = AgentDefinition(
        "a",
        "SYSTEM {{label}}",
        variables={"label": VariableSpec({"type": "string"})},
        input_schema={"type": "object", "required": ["question"]},
        output_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        builder = make_builder(tmp_path, runtime_engine=runtime, model=model)
        first, second = builder.build(definition), builder.build(definition)
        results = await asyncio.gather(
            first.ainvoke(AgentInput({"question": "one"}, variables={"label": "ONE"})),
            second.ainvoke(AgentInput({"question": "two"}, variables={"label": "TWO"})),
        )
        assert results[0].run_id != results[1].run_id
        assert first.context is not second.context
        for result in results:
            assert result.output.value == {"ok": True}
            assert_finished(runtime, result.run_id, "succeeded")
        assert {r.messages[0]["content"] for r in model.requests} == {"SYSTEM ONE", "SYSTEM TWO"}
        assert definition.prompt == "SYSTEM {{label}}"
        with pytest.raises(RequestError, match="AGENT_INSTANCE_BOUND"):
            await first.start(AgentInput({"question": "new"}, variables={"label": "THREE"}))
        assert (
            await builder.build(definition).ainvoke(
                AgentInput({"question": "new"}, variables={"label": "THREE"}),
                session_id=results[0].session_id,
            )
        ).status == "succeeded"
        calls = len(model.requests)
        replay = await builder.build(definition).ainvoke(run_id=results[0].run_id)
        assert replay.output == results[0].output and len(model.requests) == calls


async def test_plan_builder_keeps_role_grants_hooks_and_executor_definitions_isolated(
    tmp_path, runtime_store
):
    tool_calls, hook_calls = [], []

    def work(arguments, context):
        tool_calls.append(context.role_key)
        return "PRIVATE RESULT"

    registry = CapabilityRegistry(tools=(tool(work),))

    def hook(event, context):
        hook_calls.append(context.metadata.role_key)
        return HookDecision()

    def respond(request):
        feedback = any(m["role"] == "tool" for m in request.messages)
        names = {tool["name"] for tool in request.tools}
        if request.role_key == "planner":
            assert "echo" not in names and "delegate_task" in names
            return (
                ModelResponse("combined")
                if feedback
                else ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "work"}),)
                )
            )
        assert request.messages[0]["content"] == "WORKER"
        assert "echo" in names and "delegate_task" not in names
        return (
            ModelResponse("finished")
            if feedback
            else ModelResponse(tool_calls=(ToolCall("echo", {}),))
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        builder = make_builder(
            tmp_path,
            runtime_engine=runtime,
            model=ScriptedModel(respond),
            capabilities=registry,
            hooks=HookSet(
                (HookBinding("h", "model.generate", "request", "h"),),
                {"h": FunctionHookHandler(hook)},
            ),
        )
        definitions = {"worker": AgentDefinition("w", "WORKER", "work", capabilities=(binding(),))}
        plan = builder.build(AgentDefinition("planner", "PLANNER"), executors=definitions)
        definitions.clear()
        assert "worker" in plan.executors
        result = await plan.ainvoke("go")
        assert_finished(runtime, result.run_id, "succeeded")
        assert result.output.value == "combined" and tool_calls == ["worker"]
        assert hook_calls == ["planner", "worker", "worker", "planner"]
        assert builder.capabilities is registry
        with pytest.raises(RequestError, match="CAPABILITY_CATALOG_NOT_FOUND"):
            registry.for_role(plan.program_key, "worker")
        plain = builder.build(AgentDefinition("plain", "SYSTEM"))
        assert not plain.catalogs["react"].tools and not plain.executors
        with pytest.raises(RequestError, match="CAPABILITY_CATALOG_NOT_FOUND"):
            plain.capabilities.for_role(plan.program_key, "worker")


async def test_runtime_ownership_and_closed_host_errors_remain_explicit(tmp_path, runtime_store):
    runtime = RuntimeEngine(store=runtime_store)
    builder = make_builder(tmp_path, runtime_engine=runtime)
    definition = AgentDefinition("a", "SYS")
    with pytest.raises(InfrastructureError, match="RUNTIME_CLOSED"):
        await builder.build(definition).start("go")
    async with runtime:
        assert (await builder.build(definition).ainvoke("go")).status == "succeeded"
    with pytest.raises(InfrastructureError, match="RUNTIME_CLOSED"):
        await builder.build(definition).start("go")
    async with RuntimeEngine(store=runtime_store) as next_runtime:
        rebound = builder.with_options(runtime_engine=next_runtime)
        assert (await rebound.build(definition).ainvoke("go")).status == "succeeded"
