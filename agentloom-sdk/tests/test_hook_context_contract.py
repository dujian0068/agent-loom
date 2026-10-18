import json

import pytest
from conftest import binding, tool

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistry,
    ContextPolicy,
    DirectorySkillLoader,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    RuntimeEngine,
    ScriptedModel,
    SkillPackage,
    SkillRegistration,
    ToolCall,
)


async def test_hook_durable_order_raw_preservation_and_expired_reader(build):
    readers, observed = [], []

    async def first(event, context):
        readers.append(context.reader)
        system = context.reader.messages()[0]
        return HookDecision(context_patch={system["record_id"]: "EFFECTIVE SYSTEM"})

    async def second(event, context):
        observed.append(context.reader.messages()[0]["message"]["content"])
        return HookDecision()

    async def echo(args, context):
        return "ok"

    def model(request):
        assert request.messages[0]["content"] == "EFFECTIVE SYSTEM"
        return (
            ModelResponse("done")
            if any(m["role"] == "tool" for m in request.messages)
            else ModelResponse(tool_calls=(ToolCall("echo", {}),))
        )

    hooks = HookSet(
        (
            HookBinding("first", "model.generate", "request", "first"),
            HookBinding("second", "model.generate", "request", "second", 1),
        ),
        {"first": FunctionHookHandler(first), "second": FunctionHookHandler(second)},
    )
    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            definition=AgentDefinition("a", "RAW SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(echo),)),
            hooks=hooks,
        )
        result = await agent.ainvoke("PROTECTED QUESTION")
        assert result.status == "succeeded" and observed == ["EFFECTIVE SYSTEM", "EFFECTIVE SYSTEM"]
        state = runtime._load(result.run_id)
        records = state["tasks"][state["root_task_id"]]["context"]["records"]
        assert records[0]["raw"]["content"] == "RAW SYSTEM"
        assert records[0]["message"]["content"] == "EFFECTIVE SYSTEM"
        with pytest.raises(AgentLoomError, match="HOOK_READ_PORT_EXPIRED"):
            readers[0].messages()


async def test_valid_deny_survives_invalid_patch_and_stops_chain(build):
    seen = []

    async def fail(event, context):
        seen.append("failure")
        raise ValueError("not a transport signal")

    async def deny(event, context):
        seen.append("deny")
        protected = next(
            r
            for r in context.reader.messages()
            if r["protected"] and r["message"]["role"] == "user"
        )
        return HookDecision("deny", context_patch={protected["record_id"]: "lost question"})

    async def skipped(event, context):
        seen.append("skipped")
        return HookDecision()

    async def forbidden(args, context):
        pytest.fail("Denied tool must not be called")

    hooks = HookSet(
        tuple(
            HookBinding(key, "capability.invoke", "request", key, index)
            for index, key in enumerate(("fail", "deny", "skipped"))
        ),
        {
            "fail": FunctionHookHandler(fail),
            "deny": FunctionHookHandler(deny),
            "skipped": FunctionHookHandler(skipped),
        },
    )
    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            lambda r: (
                ModelResponse("done")
                if any(m["role"] == "tool" for m in r.messages)
                else ModelResponse(tool_calls=(ToolCall("echo", {}),))
            ),
            definition=AgentDefinition("a", "SYS", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(forbidden),)),
            hooks=hooks,
        )
        result = await agent.ainvoke("original")
        assert result.status == "succeeded" and seen == ["failure", "deny"]
        op = next(
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "capability.invoke"
        )
        assert op["response"]["status"] == "blocked" and op["attempts"] == []
        assert op["hooks"]["request:deny"]["diagnostic"]["code"] == "HOOK_PROTECTED_INPUT"


async def test_compaction_is_resumable_separate_budget_and_preserves_raw(build):
    async def verbose(args, context):
        return "资料" * 6000

    def model(request):
        if request.purpose == "compaction":
            assert not request.tools
            return ModelResponse("The tool returned reference material.")
        if any(m["role"] == "tool" for m in request.messages) or any(
            "Earlier execution summary:" in m["content"] for m in request.messages
        ):
            assert "CURRENT QUESTION" in json.dumps(request.messages)
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            None,
            model=ScriptedModel(model, context_window_tokens=8192, max_output_tokens=512),
            definition=AgentDefinition("a", "SYS", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(verbose),)),
            context_policy=ContextPolicy(compaction_prompt_addendum="Keep file paths."),
        )
        result = await (await agent.start("CURRENT QUESTION")).wait(timeout_s=10)
        assert result.status == "succeeded", result.error
        assert result.usage.compaction_model_attempts > 1 and result.usage.loops_consumed == 2
        state = runtime._load(result.run_id)
        context = state["tasks"][state["root_task_id"]]["context"]
        assert any("资料" * 100 in r["raw"]["content"] for r in context["records"])
        assert all(e["cursor"] == len(e["chunks"]) for e in context["episodes"].values())


async def test_protected_input_cannot_be_discarded_for_window(build):
    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            None,
            model=ScriptedModel(
                lambda r: ModelResponse("must not run"),
                context_window_tokens=2048,
                max_output_tokens=256,
            ),
        )
        result = await agent.ainvoke("PROTECTED" * 500)
        assert result.error["code"] == "CONTEXT_PROTECTED_INPUT_TOO_LARGE"
        assert not agent.model.requests and result.usage.loops_consumed == 0


async def test_skill_manifest_arguments_authorization_and_effective_result(tmp_path, build):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: research\ndescription: Read research\n---\nInput=$ARGUMENTS\nSecond=$2",
        encoding="utf-8",
    )
    package = SkillPackage.from_directory(root)
    loader = DirectorySkillLoader()
    result = loader.read_entry(package, raw_arguments='a   "b c"')
    assert 'Input=a   "b c"' in result["text"] and "Second=b c" in result["text"]
    assert (
        result["rendered_arguments_hash"]
        != loader.read_entry(package, arguments=["a", "b c"])["rendered_arguments_hash"]
    )
    with pytest.raises(AgentLoomError, match="SKILL_ARGUMENTS_INVALID"):
        loader.read_entry(package, raw_arguments="a", arguments=["a"])

    async def after(event, context):
        return HookDecision(
            payload_patch={"result": {**event.payload["result"], "text": "EFFECTIVE SKILL"}}
        )

    def model(request):
        if not any(m["role"] == "tool" for m in request.messages):
            assert not any(t["name"] == "exec" for t in request.tools)
            return ModelResponse(
                tool_calls=(ToolCall("skill_read", {"name": "research", "arguments": ["hello"]}),)
            )
        assert "EFFECTIVE SKILL" in json.dumps(request.messages)
        return ModelResponse("done")

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            definition=AgentDefinition(
                "a",
                "SYS",
                capabilities=(CapabilityBinding("research", "skill", "research", "research"),),
            ),
            capabilities=CapabilityRegistry(
                skills=(SkillRegistration("research", package, loader),)
            ),
            hooks=HookSet(
                (HookBinding("after", "skill.read", "response", "after"),),
                {"after": FunctionHookHandler(after)},
            ),
        )
        result = await agent.ainvoke("read the skill")
        assert result.status == "succeeded"
        op = next(
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "skill.read"
        )
        assert op["attempts"] == [] and "Input=hello" in op["raw"]["result"]["text"]
    (root / "SKILL.md").write_text("changed")
    with pytest.raises(AgentLoomError, match="SKILL_CONTENT_CHANGED"):
        loader.read_entry(package)


@pytest.mark.parametrize(
    "decision,patch,expected",
    [
        ("allow", {"messages": None}, "succeeded"),
        ("deny", {"messages": None}, "failed"),
        ("deny", {"anything": float("nan")}, "failed"),
        ("deny", {"anything": 2**60}, "failed"),
    ],
)
async def test_malformed_hook_patch_fails_open_but_preserves_explicit_deny(
    build, decision, patch, expected
):
    hooks = HookSet(
        (HookBinding("bad", "model.generate", "request", "h"),),
        {"h": FunctionHookHandler(lambda e, c: HookDecision(decision, payload_patch=patch))},
    )
    async with RuntimeEngine() as runtime:
        agent = build(runtime, lambda r: ModelResponse("done"), hooks=hooks)
        result = await agent.ainvoke("question")
        assert result.status == expected
        assert len(agent.model.requests) == (1 if decision == "allow" else 0)
