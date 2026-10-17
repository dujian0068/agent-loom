"""Native capability adapters and immutable role catalogs obey the public contract."""

import json
from dataclasses import FrozenInstanceError

import pytest
from conftest import assert_finished

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityAdapter,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilityRequest,
    CapabilitySpec,
    ContentBlock,
    ExecutionPolicy,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    PlanAgent,
    RequestError,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
    ToolInvocationContext,
    ToolResult,
)


def registration(adapter, **options):
    spec = CapabilitySpec(
        resource_key="resource",
        capability_key="member",
        kind="hosted",
        model_name="suggested",
        description="test",
        handler_ref="adapter",
        **options,
    )
    return CapabilityRegistry(tools=(CapabilityRegistration("adapter", adapter, (spec,)),))


def definition(name="visible"):
    return AgentDefinition(
        "agent",
        "SYSTEM",
        "work",
        capabilities=(CapabilityBinding("binding", "tool", name, "adapter", "member"),),
    )


def responder(request):
    return (
        ModelResponse("done")
        if any(m["role"] == "tool" for m in request.messages)
        else ModelResponse(tool_calls=(ToolCall("visible", {"value": "original"}),))
    )


def operation(runtime, result):
    return next(
        op
        for op in runtime._load(result.run_id)["operations"].values()
        if op["event_type"] == "capability.invoke"
    )


async def test_native_request_context_result_and_effective_hook_projection(runtime_store, build):
    called = []

    class Native:
        async def invoke_once(self, request, context):
            assert isinstance(request, CapabilityRequest) and isinstance(
                context, ToolInvocationContext
            )
            assert request.resource_key == "resource" and request.capability_key == "member"
            assert request.arguments == {"value": "effective"}
            assert request.operation_id == context.operation_id
            assert context.attempt_no == 1 and context.lease_epoch > 0
            assert context.config == context.binding_config == {}
            assert context.task_id == context.node_task_id
            context.check_active()
            for value in [request.arguments, context.binding_config, context.authorization_ref]:
                with pytest.raises(TypeError):
                    value["bad"] = True
            with pytest.raises(FrozenInstanceError):
                request.resource_key = "other"
            called.append((request, context))
            return ToolResult(
                content=(
                    ContentBlock("json", json_ref={"kind": "inline", "value": {"value": "RAW"}}),
                ),
                metadata={"private_transport": "RAW_METADATA"},
            )

    def hook(event, context):
        return (
            HookDecision(payload_patch={"arguments": {"value": "effective"}})
            if event.stage == "request"
            else HookDecision(payload_patch={"result": {"value": "PUBLIC"}})
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        native = Native()
        assert isinstance(native, CapabilityAdapter)
        agent = build(
            runtime,
            responder,
            definition=definition(),
            capabilities=registration(native),
            hooks=HookSet(
                (
                    HookBinding("before", "capability.invoke", "request", "h"),
                    HookBinding("after", "capability.invoke", "response", "h"),
                ),
                {"h": FunctionHookHandler(hook)},
            ),
        )
        result = await agent.ainvoke("go")
        assert_finished(runtime, result.run_id, "succeeded")
        assert len(called) == 1
        op = operation(runtime, result)
        assert op["raw"]["result"] == {"value": "RAW"}
        assert op["response"]["result"] == {"value": "PUBLIC"}
        assert op["attempts"][0]["tool_result"]["metadata"]["private_transport"] == "RAW_METADATA"
        model_feedback = json.dumps(agent.model.requests[-1].messages)
        assert "PUBLIC" in model_feedback and "RAW" not in model_feedback
        assert "tool_result" not in op["response"]
        await (await runtime.runs.attach(result.run_id)).wait()
        assert len(called) == 1


@pytest.mark.parametrize("status", ["failed", "unknown"])
async def test_native_failure_preserves_outcome_and_does_not_retry_business_error(
    runtime_store, build, status
):
    called = []

    class Native:
        async def invoke_once(self, request, context):
            called.append(1)
            error = AgentLoomError(
                "BUSINESS_ERROR",
                category="business",
                external_outcome="unknown" if status == "unknown" else "known",
            )
            return ToolResult(
                status=status,
                error=error.to_dict(),
                content=(ContentBlock("text", text="failure detail"),),
            )

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            responder,
            definition=definition(),
            capabilities=registration(Native()),
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
            ),
        ).ainvoke("go")
        assert_finished(runtime, result.run_id, "succeeded")
        op = operation(runtime, result)
        assert len(called) == 1 and op["status"] == "failed"
        assert (
            op["raw"]["error"]["code"] == "BUSINESS_ERROR"
            and op["raw"]["result"] == "failure detail"
        )
        assert op["attempts"][0]["status"] == status
        assert op["raw"]["execution_fact"] == ("unknown" if status == "unknown" else "completed")


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "CAPABILITY_RESULT_INVALID"),
        ({"status": []}, "CAPABILITY_RESULT_INVALID"),
        ({"error": {"code": "bad"}}, "CAPABILITY_RESULT_INVALID"),
        ({"status": "unknown"}, "CAPABILITY_RESULT_INVALID"),
        ({"content": [{"type": "text", "text": 42}]}, "CAPABILITY_RESULT_INVALID"),
        (
            {"content": [{"type": "json", "json_ref": {"kind": "blob", "blob_id": "forged"}}]},
            "CAPABILITY_CONTENT_REFERENCE_UNSUPPORTED",
        ),
        ({"artifacts": [{"path": "/private"}]}, "CAPABILITY_ARTIFACT_UNSUPPORTED"),
    ],
)
async def test_native_result_validation_is_terminal_and_never_reinvokes(
    runtime_store, build, payload, code
):
    calls = []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(1)
            return (
                {}
                if not payload
                else {
                    "status": "succeeded",
                    "content": [],
                    "error": None,
                    "artifacts": [],
                    "metadata": {},
                    **payload,
                }
            )

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, responder, definition=definition(), capabilities=registration(Native())
        ).ainvoke("go")
        assert_finished(runtime, result.run_id, "succeeded")
        op = operation(runtime, result)
        assert op["status"] == "failed" and op["raw"]["error"]["code"] == code
        assert len(calls) == 1
        assert "invalid_tool_result" in op["attempts"][0]
        assert op["raw"]["result"] is None and op["raw"]["execution_fact"] == "completed"


async def test_type_error_never_falls_back_to_legacy_invocation(runtime_store, build):
    calls = []

    class Both:
        async def invoke_once(self, request, context):
            calls.append("once")
            raise TypeError("raised after external effect")

        async def invoke(self, member, arguments, context):
            calls.append("legacy")
            return "wrong"

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, responder, definition=definition(), capabilities=registration(Both())
        ).ainvoke("go")
        assert calls == ["once"] and operation(runtime, result)["status"] == "failed"


async def test_legacy_business_status_dictionary_is_not_a_tool_result(runtime_store, build):
    class Legacy:
        async def invoke(self, member, arguments, context):
            return {"status": "failed", "error": "a business field"}

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, responder, definition=definition(), capabilities=registration(Legacy())
        ).ainvoke("go")
        op = operation(runtime, result)
        assert op["status"] == "succeeded" and op["raw"]["result"]["status"] == "failed"


async def test_bound_catalogs_are_local_immutable_and_never_expand_grants(build):
    class Native:
        async def invoke_once(self, request, context):
            pytest.fail("catalog query must not execute adapters")

    source = registration(Native())
    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse("done"),
            cls=PlanAgent,
            definition=AgentDefinition("planner", "P"),
            executors={"worker": definition("worker_visible")},
            capabilities=source,
        )
        # Executor definitions need an explicit description.
        catalog = agent.capabilities.for_role(agent.program_key, "worker")
        assert catalog is agent.capabilities.for_node_binding(agent.program_key, "worker")
        assert catalog.program_key == agent.program_key and catalog.binding_key == "worker"
        spec = agent.capabilities.resolve(catalog, "worker_visible")
        assert spec.model_name == "worker_visible" and spec.resource_key == "resource"
        assert catalog.capabilities == (spec,) and catalog.model_name_to_key == {
            "worker_visible": "member"
        }
        assert agent.capabilities.for_role(agent.program_key, "planner").capabilities == ()
        with pytest.raises(RequestError, match="CAPABILITY_NOT_FOUND"):
            agent.capabilities.resolve(catalog, "suggested")
        with pytest.raises(RequestError, match="CAPABILITY_CATALOG_NOT_FOUND"):
            source.for_role(agent.program_key, "worker")
        other = build(
            runtime, lambda r: ModelResponse("done"), definition=definition(), capabilities=source
        )
        with pytest.raises(RequestError, match="CAPABILITY_CATALOG_FORBIDDEN"):
            other.capabilities.resolve(catalog, "worker_visible")
        with pytest.raises(TypeError):
            catalog.tools["other"] = catalog.tools["worker_visible"]
        with pytest.raises(TypeError):
            spec.input_schema["required"] = ("injected",)
        assert source._entries["adapter"].members[0].model_name == "suggested"


async def test_native_output_schema_failure_retains_original_envelope(runtime_store, build):
    class Native:
        async def invoke_once(self, request, context):
            return ToolResult.from_value({"bad": "preserved"})

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            responder,
            definition=definition(),
            capabilities=registration(
                Native(), output_schema={"type": "object", "required": ["required"]}
            ),
        ).ainvoke("go")
        op = operation(runtime, result)
        assert (
            op["status"] == "failed"
            and op["raw"]["error"]["code"] == "CAPABILITY_OUTPUT_SCHEMA_INVALID"
        )
        assert op["raw"]["result"] == {"bad": "preserved"}
        assert op["attempts"][0]["tool_result"]["content"][0]["json_ref"]["value"] == {
            "bad": "preserved"
        }
        assert op["raw"]["execution_fact"] == "completed"
        assert_finished(runtime, result.run_id, "succeeded")


async def test_native_dictionary_envelope_preserves_multiple_blocks(runtime_store, build):
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "json", "json_ref": {"kind": "inline", "value": {"second": 2}}},
    ]

    class Native:
        async def invoke_once(self, request, context):
            return {
                "status": "succeeded",
                "content": blocks,
                "error": None,
                "artifacts": [],
                "metadata": {},
            }

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime, responder, definition=definition(), capabilities=registration(Native())
        ).ainvoke("go")
        op = operation(runtime, result)
        assert op["raw"]["result"] == {"content": blocks}
        assert op["attempts"][0]["tool_result"]["content"] == blocks


def test_skill_routes_are_separate_from_ordinary_tool_resolution(tmp_path):
    from agentloom import DirectorySkillLoader, SkillPackage, SkillRegistration

    (tmp_path / "SKILL.md").write_text("---\nname: sample\ndescription: sample\n---\nBODY")
    source = CapabilityRegistry(
        skills=(
            SkillRegistration(
                "skills", SkillPackage.from_directory(tmp_path), DirectorySkillLoader()
            ),
        )
    )
    roles = {
        "react": AgentDefinition(
            "a", "SYSTEM", capabilities=(CapabilityBinding("s", "skill", "guide", "skills"),)
        )
    }
    registry = source.bind_program("program", roles)
    catalog = registry.for_role("program", "react")
    assert catalog.capabilities == () and catalog.model_name_to_key == {}
    assert catalog.skill_entries[0]["name"] == "guide"
    assert {item["event_type"] for item in catalog.skill_calls} == {
        "skill.list",
        "skill.read",
        "skill.resource.read",
    }
    with pytest.raises(RequestError, match="CAPABILITY_NOT_FOUND"):
        registry.resolve(catalog, "skill_read")
    with pytest.raises(RequestError, match="CAPABILITY_CATALOG_NOT_FOUND"):
        registry.for_role("another-program", "react")
