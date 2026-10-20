"""Unified startup parameters keep dependency ownership and Run recovery unchanged."""

import asyncio
import json
from dataclasses import FrozenInstanceError, replace

import pytest
from conftest import assert_finished, binding, tool

from agentloom import (
    AgentBuilder,
    AgentDefinition,
    AgentLoom,
    AgentProperties,
    CapabilityRegistry,
    ConfigurationError,
    ContextPolicy,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    InfrastructureError,
    LocalEnvironment,
    ModelOptions,
    ModelResponse,
    PlanAgent,
    PropertiesConfig,
    ReactAgent,
    RequestError,
    RuntimeEngine,
    RuntimeProperties,
    ScriptedModel,
    ServiceProperties,
    ToolCall,
    WorkspaceBinding,
)
from agentloom.configuration.assembly import recovery_manifest


def config(tmp_path, *, model=None, store=None, **host):
    return PropertiesConfig(
        runtime=RuntimeProperties(state_directory=tmp_path / "state", **host),
        defaults=AgentProperties(workspace=WorkspaceBinding(tmp_path / "work")),
        services=ServiceProperties(
            model=model if model is not None else ScriptedModel(lambda r: ModelResponse("done")),
            store=store,
        ),
    )


def test_startup_configuration_is_local_frozen_and_does_not_open_services(tmp_path):
    def unexpected(*args):
        pytest.fail("configuration invoked a service")

    model = ScriptedModel(unexpected)
    model.discover = unexpected
    before = set(tmp_path.rglob("*"))
    parameters = config(tmp_path, model=model)
    app = AgentLoom(parameters)
    assert set(tmp_path.rglob("*")) == before
    assert app.properties is parameters and not model.requests
    for obj, attribute, value in (
        (parameters, "defaults", None),
        (parameters.runtime, "namespace", "changed"),
        (parameters.defaults, "workspace", None),
        (parameters.defaults.execution_policy, "loop_max", 1),
        (parameters.services, "model", None),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attribute, value)
    with pytest.raises(AttributeError):
        app.properties = parameters
    with pytest.raises(InfrastructureError, match="RUNTIME_CLOSED"):
        app.build(AgentDefinition("a", "SYSTEM"))
    with pytest.raises(InfrastructureError, match="RUNTIME_CLOSED"):
        _ = app.runtime


@pytest.mark.parametrize(
    "values,code",
    [
        ({"namespace": " "}, "STORAGE_NAMESPACE_INVALID"),
        ({"lease_seconds": True}, "INVALID_CONFIGURATION"),
        ({"lease_seconds": 0}, "INVALID_CONFIGURATION"),
        ({"cleanup_retry_seconds": float("nan")}, "INVALID_CONFIGURATION"),
        ({"state_directory": ""}, "STATE_DIRECTORY_INVALID"),
        ({"state_directory": "bad\0path"}, "STATE_DIRECTORY_INVALID"),
        ({"store_lock_timeout_seconds": -1}, "STORE_LOCK_TIMEOUT_INVALID"),
        ({"store_lock_timeout_seconds": float("inf")}, "STORE_LOCK_TIMEOUT_INVALID"),
        ({"store_lock_timeout_seconds": True}, "STORE_LOCK_TIMEOUT_INVALID"),
    ],
)
def test_runtime_parameters_reject_invalid_values_locally(tmp_path, values, code):
    before = set(tmp_path.rglob("*"))
    with pytest.raises(ConfigurationError, match=code):
        RuntimeProperties(**values)
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "values,code",
    [
        ({"model": None}, "MODEL_ADAPTER_INVALID"),
        ({"capabilities": {}}, "INVALID_CONFIGURATION"),
        ({"hooks": []}, "INVALID_CONFIGURATION"),
        ({"store": object()}, "RUNTIME_STORE_INVALID"),
        ({"environment": object()}, "ENVIRONMENT_INVALID"),
        ({"checkpoint_callback": False}, "CHECKPOINT_CALLBACK_INVALID"),
    ],
)
def test_injected_service_contracts_are_checked_without_calling_them(values, code):
    with pytest.raises(ConfigurationError, match=code):
        ServiceProperties(**({"model": ScriptedModel(lambda r: ModelResponse())} | values))


def test_model_metadata_and_async_checkpoint_callback_are_rejected():
    model = ScriptedModel(lambda r: ModelResponse())
    model.descriptor = None
    with pytest.raises(ConfigurationError, match="MODEL_ADAPTER_INVALID"):
        ServiceProperties(model=model)

    async def callback(command, state):
        pytest.fail("callback must not be executed during validation")

    with pytest.raises(ConfigurationError, match="CHECKPOINT_CALLBACK_INVALID"):
        ServiceProperties(
            model=ScriptedModel(lambda r: ModelResponse()), checkpoint_callback=callback
        )


@pytest.mark.parametrize("group", ["runtime", "defaults", "services"])
def test_invalid_groups_and_root_derivation_do_not_mutate_source(tmp_path, group):
    original = config(tmp_path)
    with pytest.raises(ConfigurationError, match="INVALID_CONFIGURATION"):
        original.with_options(**{group: None})
    with pytest.raises(TypeError, match="Unknown PropertiesConfig options"):
        original.with_options(namespace="hidden")
    derived = original.with_options(runtime=replace(original.runtime, namespace="another"))
    assert derived.runtime.namespace == "another" and original.runtime.namespace == "default"
    assert derived.defaults is original.defaults and derived.services is original.services


def test_cross_group_budget_validation_precedes_host_creation(tmp_path):
    original = config(tmp_path)
    large = replace(original.defaults, model_options=ModelOptions(max_output_tokens=2048))
    parameters = original.with_options(defaults=large)
    with pytest.raises(ConfigurationError, match="CONTEXT_BUDGET_INVALID"):
        parameters.with_options(
            services=replace(
                parameters.services,
                model=ScriptedModel(lambda r: ModelResponse(), context_window_tokens=2048),
            )
        )
    assert not (tmp_path / "state").exists()


async def test_grouped_parameters_and_legacy_constructors_have_one_recovery_contract(
    tmp_path, runtime_store
):
    parameters = config(tmp_path, store=runtime_store)
    async with AgentLoom(parameters) as app:
        definition = AgentDefinition("a", "SYS")
        built = app.build(definition)
        direct = ReactAgent(
            definition=definition,
            model=parameters.services.model,
            workspace=parameters.defaults.workspace,
            runtime_engine=app.runtime,
        )
        grouped = ReactAgent(
            definition=definition,
            model=parameters.services.model,
            properties=parameters.defaults,
            runtime_engine=app.runtime,
        )
        assert (
            built.properties is grouped.properties is app.builder.properties is parameters.defaults
        )
        assert app.runtime.properties is parameters.runtime
        assert built.execution_policy is parameters.defaults.execution_policy
        assert built.context_policy is parameters.defaults.context_policy
        assert built.model_options is parameters.defaults.model_options
        assert built.workspace is parameters.defaults.workspace
        assert recovery_manifest(built) == recovery_manifest(direct) == recovery_manifest(grouped)
        assert built.program_key == direct.program_key == grouped.program_key
        with pytest.raises(AttributeError):
            app.runtime.namespace = "other"
        with pytest.raises(AttributeError):
            app.runtime.properties = RuntimeProperties()
        with pytest.raises(AttributeError):
            built.execution_policy = ExecutionPolicy(loop_max=1)
        result = await built.ainvoke("go")
        assert_finished(app.runtime, result.run_id, "succeeded")
        before = len(parameters.services.model.requests)
        assert (await direct.ainvoke(run_id=result.run_id)).output == result.output
        assert len(parameters.services.model.requests) == before


def test_mixed_parameter_sources_fail_before_creating_default_storage(tmp_path):
    parameters = config(tmp_path)
    with pytest.raises(ConfigurationError, match="PARAMETERS_CONFLICT"):
        RuntimeEngine(properties=parameters.runtime, namespace="default")
    assert not (tmp_path / "state").exists()
    runtime = RuntimeEngine(properties=parameters.runtime, store=parameters.services.store)
    for constructor in (AgentBuilder, ReactAgent):
        args = {"definition": AgentDefinition("a", "SYS")} if constructor is ReactAgent else {}
        with pytest.raises(ConfigurationError, match="PARAMETERS_CONFLICT"):
            constructor(
                **args,
                properties=parameters.defaults,
                model=parameters.services.model,
                runtime_engine=runtime,
                execution_policy=None,
            )
    runtime.store.close()


async def test_single_agent_overrides_are_frozen_and_do_not_change_startup_or_active_run(
    tmp_path, runtime_store
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await release.wait()
        return ModelResponse("done")

    parameters = config(tmp_path, model=ScriptedModel(respond), store=runtime_store)
    async with AgentLoom(parameters) as app:
        definition = AgentDefinition("a", "SYS")
        changed = replace(parameters.defaults, execution_policy=ExecutionPolicy(loop_max=2))
        agent = app.build(definition, properties=changed)
        derived = app.builder.with_options(context_policy=ContextPolicy(recent_runs=0))
        assert derived.properties is not app.builder.properties
        assert derived.properties.workspace is parameters.defaults.workspace
        assert derived.context_policy.recent_runs == 0
        assert app.builder.context_policy.recent_runs == 2
        handle = await agent.start("go")
        await asyncio.wait_for(entered.wait(), timeout=2)
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await app.build(definition).start(run_id=handle.run_id)
        attached = await app.build(definition, properties=changed).start(run_id=handle.run_id)
        release.set()
        assert (await attached.wait(timeout_s=2)).status == "succeeded"
        assert len(parameters.services.model.requests) == 1
        assert parameters.defaults.execution_policy.loop_max == 100
        with pytest.raises(ConfigurationError, match="PARAMETERS_CONFLICT"):
            app.builder.with_options(properties=changed, execution_policy=ExecutionPolicy())


async def test_plan_authorization_and_hooks_are_resolved_from_startup_services(
    tmp_path, runtime_store
):
    called = []

    def respond(request):
        feedback = any(m["role"] == "tool" for m in request.messages)
        names = {item["name"] for item in request.tools}
        if request.role_key == "planner":
            assert "echo" not in names
            return (
                ModelResponse("combined")
                if feedback
                else ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "go"}),)
                )
            )
        assert "echo" in names
        return (
            ModelResponse("worker result")
            if feedback
            else ModelResponse(tool_calls=(ToolCall("echo", {}),))
        )

    def hook(event, context):
        called.append(context.metadata.role_key)
        return HookDecision()

    parameters = config(tmp_path, model=ScriptedModel(respond), store=runtime_store)
    registry = CapabilityRegistry(tools=(tool(lambda arguments, context: "result"),))
    parameters = parameters.with_options(
        services=replace(
            parameters.services,
            capabilities=registry,
            hooks=HookSet(
                (HookBinding("h", "model.generate", "request", "h"),),
                {"h": FunctionHookHandler(hook)},
            ),
        )
    )
    async with AgentLoom(parameters) as app:
        plan = app.build(
            AgentDefinition("p", "PLANNER"),
            executors={"worker": AgentDefinition("w", "WORKER", "work", capabilities=(binding(),))},
        )
        assert isinstance(plan, PlanAgent)
        assert plan.bind_role("worker").properties is parameters.defaults
        result = await plan.ainvoke("go")
        assert result.output.value == "combined"
        assert called == ["planner", "worker", "worker", "planner"]
        assert parameters.services.capabilities is registry
        assert app.build(AgentDefinition("empty", "SYS")).catalogs["react"].tools == {}
        state = assert_finished(app.runtime, result.run_id, "succeeded")
        assert not {"services", "runtime", "properties"} & state["bindings"].keys()


async def test_default_file_parameters_ownership_and_facade_lifecycle(tmp_path):
    parameters = config(tmp_path, namespace="custom", store_lock_timeout_seconds=0.25)
    app = AgentLoom(parameters)
    assert not (tmp_path / "state").exists()
    async with app:
        runtime = app.runtime
        assert isinstance(runtime.store, FileRuntimeStore)
        assert runtime.store.path == parameters.runtime.state_directory
        assert runtime.store.lock_timeout_s == 0.25
        result = await app.build(AgentDefinition("a", "SYS")).ainvoke("go", run_id="fixed")
        assert runtime.store.load("custom", "fixed")["run_id"] == result.run_id
        with pytest.raises(InfrastructureError, match="RUNTIME_ALREADY_OPENED"):
            await app.__aenter__()
    assert runtime.store._closed
    await app.aclose()
    with pytest.raises(InfrastructureError, match="RUNTIME_CLOSED"):
        app.build(AgentDefinition("a", "SYS"))
    with pytest.raises(InfrastructureError, match="RUNTIME_ALREADY_OPENED"):
        await app.__aenter__()
    async with AgentLoom(parameters) as reopened:
        calls = len(parameters.services.model.requests)
        assert (
            await reopened.build(AgentDefinition("a", "SYS")).ainvoke(run_id="fixed")
        ).output == result.output
        assert len(parameters.services.model.requests) == calls


async def test_multiple_facades_share_injected_services_without_sharing_parameters_or_runs(
    tmp_path, runtime_store
):
    first = config(tmp_path, namespace="one", store=runtime_store)
    second = first.with_options(
        runtime=replace(first.runtime, namespace="two"),
        services=replace(first.services, model=ScriptedModel(lambda r: ModelResponse("second"))),
    )
    definition = AgentDefinition("a", "SYS")
    async with AgentLoom(second) as right:
        async with AgentLoom(first) as left:
            one, two = await asyncio.gather(
                left.build(definition).ainvoke("FIRST_PRIVATE", run_id="same"),
                right.build(definition).ainvoke("SECOND_PRIVATE", run_id="same"),
            )
            assert one.output.value == "done" and two.output.value == "second"
            assert "FIRST_PRIVATE" not in json.dumps(right.runtime._load("same"))
            assert "SECOND_PRIVATE" not in json.dumps(left.runtime._load("same"))
        assert runtime_store.load("one", "same")["status"] == "succeeded"
        assert (await right.build(definition).ainvoke("still open")).output.value == "second"
    assert runtime_store.load("two", "same")["status"] == "succeeded"


async def test_startup_failure_closes_created_store_and_retains_primary_error(
    tmp_path, monkeypatch
):
    async def fail_open(runtime):
        raise InfrastructureError("STARTUP_FAILED")

    monkeypatch.setattr(RuntimeEngine, "__aenter__", fail_open)
    parameters = config(tmp_path, model=ScriptedModel(lambda r: pytest.fail("model called")))
    app = AgentLoom(parameters)
    with pytest.raises(InfrastructureError, match="STARTUP_FAILED"):
        await app.__aenter__()
    assert app.runtime.store._closed
    assert not parameters.services.model.requests
    with pytest.raises(InfrastructureError, match="RUNTIME_CLOSED"):
        _ = app.builder


async def test_shutdown_failure_preserves_body_error_and_can_be_retried(tmp_path, runtime_store):
    app = AgentLoom(config(tmp_path, store=runtime_store))
    primary = ValueError("body error")
    calls = 0
    with pytest.raises(ValueError) as caught:
        async with app:
            close = app.runtime.aclose

            async def fail_once():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("PRIVATE CLEANUP TEXT")
                await close()

            app.runtime.aclose = fail_once
            raise primary
    assert caught.value is primary
    assert primary.__notes__ == ["Host cleanup also failed: OSError"]
    await app.aclose()
    assert calls == 2
    assert app.runtime._status == "closed"


async def test_startup_cleanup_failure_retains_primary_and_can_be_retried(tmp_path, monkeypatch):
    async def fail_open(runtime):
        raise InfrastructureError("STARTUP_FAILED")

    monkeypatch.setattr(RuntimeEngine, "__aenter__", fail_open)
    close = RuntimeEngine.aclose
    calls = 0

    async def fail_once(runtime):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("PRIVATE CLOSE ERROR")
        await close(runtime)

    monkeypatch.setattr(RuntimeEngine, "aclose", fail_once)
    app = AgentLoom(config(tmp_path))
    with pytest.raises(InfrastructureError, match="STARTUP_FAILED") as caught:
        await app.__aenter__()
    assert caught.value.__notes__ == ["Startup cleanup also failed: OSError"]
    assert not app.runtime.store._closed
    await app.aclose()
    assert calls == 2 and app.runtime.store._closed


async def test_injected_model_and_environment_remain_caller_owned(tmp_path):
    class Model(ScriptedModel):
        async def aclose(self):
            pytest.fail("injected model was closed")

    class Environment(LocalEnvironment):
        def close(self):
            pytest.fail("injected environment was closed")

    parameters = config(tmp_path, model=Model(lambda r: ModelResponse("done")))
    environment = Environment()
    parameters = parameters.with_options(
        services=replace(parameters.services, environment=environment)
    )
    async with AgentLoom(parameters) as app:
        assert app.runtime.environment is environment
        assert (await app.build(AgentDefinition("a", "SYS")).ainvoke("go")).status == "succeeded"
    assert app.runtime.store._closed


async def test_facade_close_preserves_run_and_reopen_does_not_repeat_unknown_attempt(
    tmp_path, runtime_store
):
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.Event().wait()
        return ModelResponse("unreachable")

    parameters = config(tmp_path, store=runtime_store, model=ScriptedModel(slow))
    definition = AgentDefinition("a", "SYS")
    async with AgentLoom(parameters) as app:
        await app.build(definition).start("go", run_id="recover")
        await asyncio.wait_for(entered.wait(), timeout=2)
    state = runtime_store.load("default", "recover")
    assert state["status"] == "running"
    assert not state.get("control")
    reopened = parameters.with_options(
        services=replace(
            parameters.services,
            model=ScriptedModel(lambda r: pytest.fail("ambiguous call was repeated")),
        )
    )
    async with AgentLoom(reopened) as app:
        result = await (await app.build(definition).start(run_id="recover")).wait(timeout_s=2)
        assert result.status == "failed" and result.error["code"] == "CALL_OUTCOME_UNKNOWN"
        assert_finished(app.runtime, result.run_id, "failed")
