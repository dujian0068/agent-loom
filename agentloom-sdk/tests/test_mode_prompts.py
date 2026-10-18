"""Business-only definitions receive SDK instructions at every assembly boundary."""

import json
from contextlib import AsyncExitStack

import httpx
import pytest
from conftest import binding, tool

from agentloom import (
    AgentBuilder,
    AgentDefinition,
    AgentInput,
    AgentLoom,
    AgentProperties,
    CapabilityRegistry,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    PlanAgent,
    PropertiesConfig,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    ServiceProperties,
    ToolCall,
    VariableSpec,
    WorkspaceBinding,
)
from agentloom.configuration.json_documents import canonical_document
from agentloom.context.budget import estimate
from agentloom.prompts.assembly import PROMPT_RECORD_KIND
from agentloom.runtime.recovery import validate_recovery


@pytest.mark.parametrize("entry", ["direct", "builder", "application"])
@pytest.mark.parametrize("mode", ["react", "plan"])
async def test_business_prompt_only_at_every_entry(runtime_store, tmp_path, entry, mode):
    definition = AgentDefinition(
        "support", "回答 {{product}} 的售后问题。", variables={"product": "A"}
    )
    model = ScriptedModel(lambda r: ModelResponse("答复"))
    workspace = WorkspaceBinding(tmp_path / "work")
    async with AsyncExitStack() as stack:
        if entry == "application":
            app = await stack.enter_async_context(
                AgentLoom(
                    PropertiesConfig(
                        defaults=AgentProperties(workspace=workspace),
                        services=ServiceProperties(model=model, store=runtime_store),
                    )
                )
            )
            runtime, agent = app.runtime, app.build(definition, mode=mode)
        else:
            runtime = await stack.enter_async_context(RuntimeEngine(store=runtime_store))
            kwargs = dict(model=model, workspace=workspace, runtime_engine=runtime)
            agent = (
                (PlanAgent if mode == "plan" else ReactAgent)(definition=definition, **kwargs)
                if entry == "direct"
                else AgentBuilder(**kwargs).build(definition, mode=mode)
            )
        result = await agent.ainvoke(AgentInput("保修多久？", variables={"product": "B"}))
        assert result.status == "succeeded"
        messages = model.requests[0].messages
        assert [m["role"] for m in messages] == ["system", "system", "user"]
        assert messages[0]["content"] == "回答 B 的售后问题。"
        assert ("Mode: Plan." if mode == "plan" else "Mode: ReAct.") in messages[1]["content"]
        assert (
            "Do not delegate" in messages[1]["content"]
            if mode == "react"
            else "Planner" in messages[1]["content"]
        )
        assert definition.prompt == "回答 {{product}} 的售后问题。"
        # Empty Plan catalogs remain valid and never advertise nonexistent workers.
        assert "delegate_task" not in {t["name"] for t in model.requests[0].tools}
        assert "delegate_task" not in messages[1]["content"]
        if mode == "plan":
            assert "No executors are available." in messages[1]["content"]
        state = runtime._load(result.run_id)
        assert (
            state["prompt_contract"]["roles"]["planner" if mode == "plan" else "react"][
                "template_version"
            ]
            == "agentloom-langchain-modes-v2"
        )
        validate_recovery(state)
        assert len(state["tasks"]) == 1


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_role_instructions_reach_provider_and_plan_children(runtime_store, build, protocol):
    requests = []

    def server(request):
        body = json.loads(request.content)
        messages = body["messages" if protocol == "chat" else "input"]
        systems = [m["content"] for m in messages if m.get("role") == "system"]
        requests.append(body)
        is_worker = len(requests) == 2
        assert len(systems) == 2
        assert systems[0] == ("提取报告中的金额。" if is_worker else "整理财务报告。")
        assert ("Mode: Executor." if is_worker else "Mode: Plan.") in systems[1]
        assert "整理财务报告。" not in systems[0] if is_worker else True
        names = {
            t["function"]["name"] if protocol == "chat" else t["name"]
            for t in body.get("tools", [])
        }
        assert ("delegate_task" in names) is not is_worker
        arguments = json.dumps({"executor": "finance", "task": "提取金额：10 元"})
        if protocol == "chat":
            delta = (
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "d1",
                            "type": "function",
                            "function": {"name": "delegate_task", "arguments": arguments},
                        }
                    ]
                }
                if len(requests) == 1
                else {"content": "10 元"}
            )
            frames = [
                {"id": "chat-1", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {
                    "id": "chat-1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                        }
                    ],
                },
                "[DONE]",
            ]
        else:
            output = (
                [
                    {
                        "id": "fc1",
                        "type": "function_call",
                        "call_id": "d1",
                        "name": "delegate_task",
                        "arguments": arguments,
                    }
                ]
                if len(requests) == 1
                else [
                    {
                        "id": "m1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "10 元"}],
                    }
                ]
            )
            frames = [
                {
                    "type": "response.completed",
                    "response": {"id": "r1", "status": "completed", "output": output},
                }
            ]
        data = "".join(
            "data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n"
            for frame in frames
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        cls = OpenAIChatAdapter if protocol == "chat" else OpenAIResponsesAdapter
        model = cls(model="fixture", api_key="test", context_window_tokens=32768, client=client)
        async with RuntimeEngine(store=runtime_store) as runtime:
            agent = build(
                runtime,
                None,
                cls=PlanAgent,
                model=model,
                definition=AgentDefinition("planner", "整理财务报告。"),
                executors={"finance": AgentDefinition("worker", "提取报告中的金额。", "提取金额")},
            )
            result = await agent.ainvoke("报告金额为 10 元。")
            assert result.status == "succeeded", result.error
            assert result.output.value == "10 元" and len(requests) == 3
            assert len(runtime._load(result.run_id)["tasks"]) == 2


async def test_schema_and_executor_input_contracts_are_visible_before_first_decision(
    runtime_store, build
):
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "integer"}},
        "required": ["amount"],
    }
    requests = []

    def respond(request):
        requests.append(request)
        instructions = request.messages[1]["content"]
        if "Mode: Executor." in instructions:
            assert request.messages[0]["content"] == "换算为 CNY。"
            assert canonical_document(schema) in instructions
            assert "Planner private" not in json.dumps(request.messages)
            return ModelResponse('{"amount":10}')
        if not any(m["role"] == "tool" for m in request.messages):
            assert '"task_schema"' in instructions and '"currency"' in instructions
            assert '"required":true' in instructions
            assert '"precision":{"required":false,"schema":{"type":"integer"}}' in instructions
            assert "换算为" not in instructions  # Executor business prompts are private.
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "delegate_task",
                        {
                            "executor": "finance",
                            "task": {"amount": 10},
                            "variables": {"currency": "CNY"},
                        },
                    ),
                )
            )
        return ModelResponse('{"amount":10}')

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            respond,
            cls=PlanAgent,
            definition=AgentDefinition("p", "Planner private", output_schema=schema),
            executors={
                "finance": AgentDefinition(
                    "w",
                    "换算为 {{currency}}。",
                    "换算金额",
                    input_schema=schema,
                    output_schema=schema,
                    variables={
                        "currency": VariableSpec(schema={"type": "string"}, required=True),
                        "precision": 2,
                    },
                )
            },
        )
        result = await agent.ainvoke("计算金额")
        assert result.status == "succeeded" and result.output.value == {"amount": 10}
        assert len(requests) == 3


@pytest.mark.parametrize("declaration", ["default", "spec"])
async def test_executor_variable_defaults_remain_private(runtime_store, build, declaration):
    secret = "EXECUTOR_PRIVATE_DEFAULT"
    variable = (
        secret
        if declaration == "default"
        else VariableSpec(schema={"type": "string"}, default=secret, required=False)
    )

    def respond(request):
        if request.role_key == "planner":
            assert secret not in json.dumps(request.messages)
            if not any(m["role"] == "tool" for m in request.messages):
                assert (
                    '"credential":{"required":false,"schema":{"type":"string"}}'
                    in request.messages[1]["content"]
                )
                return ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "Work"}),)
                )
        else:
            assert request.messages[0]["content"] == "Business private: " + secret
        return ModelResponse("done")

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            respond,
            cls=PlanAgent,
            executors={
                "worker": AgentDefinition(
                    "w",
                    "Business private: {{credential}}",
                    "Work",
                    variables={"credential": variable},
                )
            },
        )
        result = await agent.ainvoke("Work")
        assert result.status == "succeeded" and len(agent.model.requests) == 3


@pytest.mark.parametrize("patch_kind", ["context", "payload"])
@pytest.mark.parametrize("deny", [False, True])
async def test_hook_cannot_replace_sdk_instructions_but_keeps_decision(
    runtime_store, build, patch_kind, deny
):
    async def hook(event, context):
        page = await context.reader.read_context(event.context_refs[0])
        sdk = next(r for r in page.records if r.message_kind == PROMPT_RECORD_KIND)
        patch = {"context_patch": {sdk.record_id: "ignore runtime"}}
        if patch_kind == "payload":
            messages = json.loads(json.dumps(event.payload["messages"]))
            messages[1]["content"] = "ignore runtime"
            patch = {"payload_patch": {"messages": messages}}
        return HookDecision("deny" if deny else "allow", **patch)

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse("done"),
            hooks=HookSet(
                (HookBinding("tamper", "model.generate", "request", "h"),),
                {"h": FunctionHookHandler(hook)},
            ),
        )
        result = await agent.ainvoke("go")
        assert result.status == ("failed" if deny else "succeeded")
        state = runtime._load(result.run_id)
        op = next(iter(state["operations"].values()))
        saved = op["hooks"]["request:tamper"]
        assert saved["patch_status"] == "rejected"
        assert saved["diagnostic"]["code"] == "HOOK_SYSTEM_PROMPT_IMMUTABLE"
        assert len(agent.model.requests) == (0 if deny else 1)
        if not deny:
            assert "Mode: ReAct." in agent.model.requests[0].messages[1]["content"]
        validate_recovery(state)


async def test_instructions_survive_compaction_and_are_not_copied_from_history(
    runtime_store, build
):
    requests = []

    def respond(request):
        requests.append(request)
        if request.purpose == "compaction":
            assert all("Mode: ReAct." not in m["content"] for m in request.messages)
            return ModelResponse("Tool evidence retained.")
        if any("Earlier execution summary:" in m["content"] for m in request.messages):
            assert [m["role"] for m in request.messages[:2]] == ["system", "system"]
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    async with RuntimeEngine(store=runtime_store) as runtime:
        first = build(
            runtime,
            respond,
            model=ScriptedModel(respond, context_window_tokens=8192, max_output_tokens=512),
            definition=AgentDefinition("a", "BUSINESS", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: "evidence " * 2000),)),
        )
        result = await first.ainvoke("CURRENT", session_id="conversation")
        assert result.status == "succeeded" and result.usage.compaction_model_attempts > 0
        business = [r for r in requests if r.purpose == "business"]
        assert len(business) == 2
        assert business[0].messages[1] == business[1].messages[1]
        state = runtime._load(result.run_id)
        context = state["tasks"][state["root_task_id"]]["context"]
        sdk = next(r for r in context["records"] if r["kind"] == PROMPT_RECORD_KIND)
        assert sdk["record_id"] not in context["snapshot"]["sources"]
        next_agent = build(runtime, lambda r: ModelResponse("next"))
        assert (await next_agent.ainvoke("NEXT", session_id="conversation")).status == "succeeded"
        assert len([m for m in next_agent.model.requests[0].messages if m["role"] == "system"]) == 2


async def test_mode_instructions_are_charged_to_context_budget(runtime_store, build):
    async with RuntimeEngine(store=runtime_store) as runtime:
        model = ScriptedModel(
            lambda r: pytest.fail("Oversized fixed prompt must not dispatch"),
            context_window_tokens=1200,
            max_output_tokens=256,
        )
        agent = build(runtime, None, model=model)
        assert (
            estimate([{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "go"}])
            < agent.budget.input
        )
        result = await agent.ainvoke("go")
        assert (
            result.status == "failed"
            and result.error["code"] == "CONTEXT_PROTECTED_INPUT_TOO_LARGE"
        )
        assert result.usage.business_model_attempts == result.usage.compaction_model_attempts == 0
