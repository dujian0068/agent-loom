"""One startup contract, protocol selection, immutable reads and unnamed definitions."""

import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from agentloom import (
    AgentBuilder,
    AgentDefinition,
    AgentLoom,
    ChatCompletionsAdapter,
    ConfigurationError,
    ModelResponse,
    PropertiesConfig,
    ResponsesAdapter,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    VariableSpec,
)
from agentloom.configuration.definitions import definition_snapshot, restore_definition


def values(tmp_path, **changes):
    return {
        "workspace.root": tmp_path / "work",
        "runtime.state_directory": tmp_path / "state",
        "model.adapter": ScriptedModel(lambda r: ModelResponse("done")),
        **changes,
    }


@pytest.mark.parametrize("key", ["modle.name", "runtime.unknown", "workspace", 4, None])
def test_unknown_keys_fail_before_resources_or_default_fallback(tmp_path, key):
    source = values(tmp_path)
    with pytest.raises(ConfigurationError, match="PROPERTY_UNKNOWN") as caught:
        PropertiesConfig(source | {key: "SECRET_SENTINEL"})
    assert caught.value.field_path == (key if isinstance(key, str) else "properties")
    assert "SECRET_SENTINEL" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []
    p = PropertiesConfig(source)
    with pytest.raises(ConfigurationError, match="PROPERTY_UNKNOWN"):
        p.with_options({key: 1})
    with pytest.raises(ConfigurationError, match="PROPERTY_UNKNOWN"):
        p[key]


@pytest.mark.parametrize(
    "key,value",
    [
        ("runtime.namespace", " "),
        ("runtime.lease_seconds", True),
        ("runtime.state_directory", ""),
        ("runtime.store_lock_timeout_seconds", -1),
        ("workspace.root", "bad\0path"),
        ("workspace.persistent_sandbox", 1),
        ("execution.loop_max", False),
        ("execution.parallel_enabled", 1),
        ("execution.tool_timeout_seconds", "10"),
        ("execution.retry.enabled", 1),
        ("execution.retry.max_attempts", 0),
        ("execution.retry.delay_seconds", float("inf")),
        ("context.recent_runs", True),
        ("context.trigger_ratio", 1.0),
        ("context.compaction_prompt_addendum", []),
        ("model.parameters", []),
        ("model.max_output_tokens", 0),
        ("files.provider", "invalid name"),
        ("files.options", {"secret": "SECRET_SENTINEL"}),
        ("services.capabilities", {}),
        ("services.hooks", []),
        ("services.store", object()),
        ("services.environment", object()),
        ("services.checkpoint_callback", False),
        ("model.adapter", object()),
    ],
)
def test_parameter_types_and_constraints_report_public_key(tmp_path, key, value):
    with pytest.raises(ConfigurationError) as caught:
        PropertiesConfig(values(tmp_path, **{key: value}))
    assert caught.value.field_path == key
    assert "SECRET_SENTINEL" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("source", [None, [], "runtime.namespace=one"])
def test_configuration_requires_a_mapping(source):
    with pytest.raises(ConfigurationError, match="PROPERTIES_INVALID"):
        PropertiesConfig(source)


def test_empty_configuration_reports_required_model_parameter():
    with pytest.raises(ConfigurationError, match="PROPERTY_REQUIRED") as caught:
        PropertiesConfig()
    assert caught.value.field_path == "model.name"


@pytest.mark.parametrize("key", ["model.parameters", "files.options"])
@pytest.mark.parametrize("value", [float("nan"), object(), {1: "invalid key"}])
def test_non_json_options_report_the_exact_parameter(tmp_path, key, value):
    with pytest.raises(ConfigurationError) as caught:
        PropertiesConfig(values(tmp_path, **{key: {"setting": value}}))
    assert caught.value.field_path == key


@pytest.mark.parametrize("shape", ["method", "callable", "awaitable_result"])
def test_asynchronous_option_validation_is_rejected_at_startup(tmp_path, shape):
    calls = []

    async def validate(options):
        calls.append(options)

    class Validator:
        async def __call__(self, options):
            await validate(options)

    model = ScriptedModel(lambda r: ModelResponse("unused"))
    model.validate_options = {
        "method": validate,
        "callable": Validator(),
        "awaitable_result": lambda options: validate(options),
    }[shape]
    with pytest.raises(ConfigurationError, match="MODEL_ADAPTER_INVALID") as caught:
        PropertiesConfig(values(tmp_path, **{"model.adapter": model}))
    assert caught.value.field_path == "model.adapter"
    assert not calls and not list(tmp_path.iterdir())


def connection_values(tmp_path, **changes):
    source = values(tmp_path)
    source.pop("model.adapter")
    return source | {
        "model.name": "vendor-local-model",
        "model.base_url": "http://vendor.test/v1/",
        "model.context_window_tokens": 8192,
        **changes,
    }


@pytest.mark.parametrize("key", ["name", "base_url", "context_window_tokens"])
def test_model_connection_has_no_invented_provider_or_metadata(tmp_path, key):
    source = connection_values(tmp_path)
    source.pop("model." + key)
    with pytest.raises(ConfigurationError, match="PROPERTY_REQUIRED") as caught:
        PropertiesConfig(source)
    assert caught.value.field_path == "model." + key


@pytest.mark.parametrize(
    "key,value",
    [
        ("model.protocol", "vendor"),
        ("model.protocol", []),
        ("model.name", 1),
        ("model.base_url", 42),
        ("model.base_url", "https://"),
        ("model.base_url", "file:///tmp/model"),
        ("model.base_url", "https://user:SECRET_SENTINEL@vendor.test"),
        ("model.api_key", "invalid\nSECRET_SENTINEL"),
        ("model.context_window_tokens", "8192"),
        ("model.output_token_limit", 0),
        ("model.client", object()),
    ],
)
def test_model_connection_validation_uses_prefixed_fields(tmp_path, key, value):
    with pytest.raises(ConfigurationError) as caught:
        PropertiesConfig(connection_values(tmp_path, **{key: value}))
    assert caught.value.field_path == key
    assert "SECRET_SENTINEL" not in str(caught.value)


def test_group_conflicts_and_cross_parameter_constraints_precede_startup(tmp_path):
    p = PropertiesConfig(values(tmp_path))
    with pytest.raises(ConfigurationError, match="PARAMETERS_CONFLICT"):
        PropertiesConfig(values(tmp_path), runtime=p.runtime)
    with pytest.raises(ConfigurationError, match="PARAMETERS_CONFLICT"):
        PropertiesConfig(connection_values(tmp_path, **{"model.adapter": p.services.model}))
    with pytest.raises(ConfigurationError, match="PARAMETERS_CONFLICT"):
        p.with_options({"runtime.namespace": "other"}, runtime=p.runtime)
    with pytest.raises(ConfigurationError, match="CONTEXT_BUDGET_INVALID"):
        p.with_options({"model.max_output_tokens": 100000})
    with pytest.raises(ConfigurationError, match="WORKSPACE_ROOT_REQUIRED"):
        PropertiesConfig(values(tmp_path, **{"workspace.root": None}))
    with pytest.raises(ConfigurationError, match="FILE_ENVIRONMENT_CONFLICT"):
        from agentloom import LocalEnvironment

        PropertiesConfig(
            values(
                tmp_path,
                **{
                    "files.provider": "remote",
                    "services.environment": LocalEnvironment(),
                },
            )
        )
    assert list(tmp_path.iterdir()) == []


def test_defaults_snapshots_and_derivation_are_isolated(tmp_path, monkeypatch):
    options = {"temperature": 0.2}
    source = connection_values(tmp_path, **{"model.parameters": options})
    p = PropertiesConfig(source)
    source["runtime.namespace"] = "mutated"
    options["temperature"] = 1
    assert p["runtime.namespace"] == "default"
    assert p["model.parameters"] == {"temperature": 0.2}
    assert p["execution.loop_max"] == 100
    with pytest.raises(FrozenInstanceError):
        p.runtime = None
    with pytest.raises(TypeError):
        p["model.parameters"]["temperature"] = 1
    monkeypatch.chdir(tmp_path)
    derived = p.with_options(
        {
            "execution.loop_max": 2,
            "execution.retry.enabled": True,
            "model.parameters": {"temperature": 0.7},
        }
    )
    assert derived["workspace.root"] == p["workspace.root"]
    assert derived["runtime.state_directory"] == p["runtime.state_directory"]
    assert derived["execution.loop_max"] == 2 and p["execution.loop_max"] == 100
    assert derived["execution.retry.enabled"] and not p["execution.retry.enabled"]
    assert derived["model.parameters"] == {"temperature": 0.7}
    assert p.with_options() is p and p.with_options({}) is p


def test_model_source_replacement_is_explicit_and_preserves_common_options(tmp_path):
    p = PropertiesConfig(connection_values(tmp_path, **{"model.max_output_tokens": 512}))
    injected = ScriptedModel(lambda r: ModelResponse("done"))
    q = p.with_options({"model.adapter": injected})
    assert q.services.model is injected and q["model.max_output_tokens"] == 512
    with pytest.raises(ConfigurationError, match="PROPERTY_UNAVAILABLE"):
        q["model.base_url"]
    r = q.with_options(
        {
            "model.protocol": "responses",
            "model.name": "another-model",
            "model.base_url": "https://another-vendor.test/v1",
            "model.context_window_tokens": 8192,
        }
    )
    assert isinstance(r.services.model, ResponsesAdapter)
    assert isinstance(p.services.model, ChatCompletionsAdapter)
    assert r["model.max_output_tokens"] == 512


async def test_startup_and_multiple_role_builds_validate_parameters_only_once(tmp_path):
    class Model(ScriptedModel):
        validations = 0

        def validate_options(self, options):
            self.validations += 1
            assert self.validations == 1, "An execution module revalidated startup parameters"
            assert not options.parameters

    model = Model(lambda r: ModelResponse("done"))
    p = PropertiesConfig(values(tmp_path, **{"model.adapter": model}))
    async with AgentLoom(p) as app:
        first = app.build(AgentDefinition(prompt="SYS"))
        plan = app.build(
            AgentDefinition(prompt="PLAN"),
            executors={
                "worker": AgentDefinition(prompt="WORK", description="Work on the assigned task"),
            },
        )
        worker = plan.bind_role("worker")
        assert first.properties is worker.properties is p.defaults
        assert first.budget is worker.budget is p._budget
        assert (await first.ainvoke("go")).status == "succeeded"
        assert (await plan.ainvoke("go")).status == "succeeded"
        assert app.build(first.definition).budget is p._budget
    assert model.validations == 1


@pytest.mark.parametrize(
    "protocol,adapter,path",
    [
        ("chat_completions", ChatCompletionsAdapter, "/v1/chat/completions"),
        ("responses", ResponsesAdapter, "/v1/responses"),
    ],
)
@pytest.mark.parametrize("api_key", [None, "SECRET_SENTINEL"])
async def test_configured_protocol_executes_on_any_endpoint_and_borrows_client(
    tmp_path,
    runtime_store,
    protocol,
    adapter,
    path,
    api_key,
):
    requests = []

    def server(request):
        requests.append(request)
        if protocol == "chat_completions":
            frames = [
                {
                    "id": "c",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                "[DONE]",
            ]
        else:
            frames = [
                {
                    "type": "response.completed",
                    "response": {
                        "id": "r",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}],
                            }
                        ],
                    },
                }
            ]
        content = "".join(
            "data: " + (f if isinstance(f, str) else json.dumps(f)) + "\n\n" for f in frames
        )
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        p = PropertiesConfig(
            connection_values(
                tmp_path,
                **{
                    "model.protocol": protocol,
                    "model.client": client,
                    "model.api_key": api_key,
                    "services.store": runtime_store,
                },
            )
        )
        assert type(p.services.model) is adapter
        assert "SECRET_SENTINEL" not in repr(p)
        async with AgentLoom(p) as app:
            definition = AgentDefinition(prompt="Answer accurately")
            result = await app.build(definition).ainvoke("go", run_id="one")
            assert result.status == "succeeded" and result.output.value == "done"
            state = app.runtime._load("one")
            assert "SECRET_SENTINEL" not in json.dumps(state)
            assert "vendor.test" not in json.dumps(state["bindings"])
        assert not client.is_closed
        async with AgentLoom(p) as reopened:
            assert (await reopened.build(definition).ainvoke(run_id="one")).output == result.output
        assert len(requests) == 1
        assert requests[0].url.host == "vendor.test" and requests[0].url.path == path
        assert requests[0].headers.get("authorization") == (
            "Bearer " + api_key if api_key is not None else None
        )


async def test_unnamed_executor_routes_by_binding_and_recovers(tmp_path, runtime_store):
    def respond(request):
        if request.role_key == "planner" and not any(m["role"] == "tool" for m in request.messages):
            return ModelResponse(
                tool_calls=(ToolCall("delegate_task", {"executor": "research", "task": "go"}),)
            )
        return ModelResponse("done")

    model = ScriptedModel(respond)
    p = PropertiesConfig(
        values(tmp_path, **{"model.adapter": model, "services.store": runtime_store})
    )
    planner = AgentDefinition(prompt="Plan work")
    worker = AgentDefinition(prompt="Do work", description="Research facts")
    for _ in range(2):
        async with AgentLoom(p) as app:
            agent = app.build(planner, executors={"research": worker})
            result = (
                await agent.ainvoke("go", run_id="plan")
                if not model.requests
                else await agent.ainvoke(run_id="plan")
            )
            assert result.status == "succeeded"
    assert [r.role_key for r in model.requests] == ["planner", "research", "planner"]


def test_optional_name_keeps_variable_snapshots_and_positional_compatibility():
    d = AgentDefinition(
        prompt="Mode: {{mode}}",
        variables={
            "mode": VariableSpec({"type": "string"}, "safe", required=False),
        },
    )
    restored = restore_definition(definition_snapshot(d))
    assert restored.name is None and restored.render() == "Mode: safe"
    assert replace(d, name="display").name == "display"
    assert AgentDefinition("display", "SYS", "description").prompt == "SYS"
    with pytest.raises(TypeError, match="requires prompt"):
        AgentDefinition()
    for invalid in ("", " ", True, []):
        with pytest.raises(ConfigurationError, match="INVALID_DEFINITION"):
            AgentDefinition(name=invalid, prompt="SYS")


def test_legacy_builder_reuses_its_validated_parameters(tmp_path):
    class Model(ScriptedModel):
        count = 0

        def validate_options(self, options):
            self.count += 1

    model = Model(lambda r: ModelResponse())
    p = PropertiesConfig(values(tmp_path, **{"model.adapter": model}))
    builder = AgentBuilder(model=model, properties=p.defaults, runtime_engine=RuntimeEngine())
    before = model.count
    for _ in range(3):
        builder.build(AgentDefinition(prompt="SYS"))
    assert model.count == before
