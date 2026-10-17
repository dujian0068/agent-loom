import asyncio
import importlib.util
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from conftest import binding, tool

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ContextPolicy,
    DirectorySkillLoader,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionHookHandler,
    FunctionToolAdapter,
    HookBinding,
    HookDecision,
    HookMatcher,
    HookSet,
    InfrastructureError,
    LocalEnvironment,
    McpToolAdapter,
    MemoryRuntimeStore,
    ModelDescriptor,
    ModelResponse,
    PlanAgent,
    ProviderEvent,
    RequestError,
    RuntimeEngine,
    ScriptedModel,
    SkillPackage,
    SkillRegistration,
    ToolCall,
    WorkspaceBinding,
)


async def test_default_files_persist_between_engines_and_memory_is_explicit(tmp_path, build):
    async with RuntimeEngine() as first:
        assert isinstance(first.store, FileRuntimeStore)
        assert first.store.path == (tmp_path / ".agentloom/state").resolve()
        result = await build(first, lambda r: ModelResponse("saved")).ainvoke(
            "question", run_id="restart"
        )
        assert result.output.value == "saved"
    with pytest.raises(InfrastructureError, match="STORE_CLOSED"):
        first.store.load("default", "restart")
    async with RuntimeEngine() as second:
        agent = build(second, lambda r: pytest.fail("Completed calls must not be repeated"))
        result = await (await agent.start(run_id="restart")).wait()
        assert result.output.value == "saved"
    assert list((tmp_path / ".agentloom/state").rglob("manifest.json"))
    memory = MemoryRuntimeStore()
    async with RuntimeEngine(store=memory) as runtime:
        assert runtime.store is memory
        await build(runtime, lambda r: ModelResponse("ephemeral")).ainvoke("q", run_id="memory")
    assert memory.load("default", "memory")["status"] == "succeeded"
    assert importlib.util.find_spec("agentloom.storage.sqlite") is None


async def test_planner_context_is_readonly_and_excludes_private_executor_context(build):
    def model(request):
        if request.role_key == "worker":
            return ModelResponse("public result")
        if any(m["role"] == "tool" for m in request.messages):
            return ModelResponse("final")
        return ModelResponse(
            tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "do work"}),)
        )

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            cls=PlanAgent,
            executors={"worker": AgentDefinition("worker", "PRIVATE PROMPT", "work")},
        )
        assert agent.context is None
        result = await agent.ainvoke("question")
        context = agent.context
        assert context.task_id == runtime._load(result.run_id)["root_task_id"]
        assert context.progress["stage"] == "completed" and context.revision > 1
        assert "PRIVATE PROMPT" not in json.dumps(context.snapshot())
        assert context.messages[-1]["content"] == "final"
        with pytest.raises(TypeError):
            context.messages[-1]["content"] = "overwrite"


async def test_executor_slot_is_acquired_before_creating_child(build):
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        if request.role_key == "worker":
            entered.set()
            await release.wait()
            return ModelResponse("done")
        if any(m["role"] == "tool" for m in request.messages):
            return ModelResponse("finished")
        return ModelResponse(
            tool_calls=tuple(
                ToolCall("delegate_task", {"executor": "worker", "task": str(n)}) for n in range(3)
            )
        )

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            cls=PlanAgent,
            executors={"worker": AgentDefinition("worker", "work", "one")},
            execution_policy=ExecutionPolicy(parallel_enabled=True, executor_max_parallel=1),
        )
        handle = await agent.start("question")
        await asyncio.wait_for(entered.wait(), 2)
        state = runtime._load(handle.run_id)
        assert len(state["tasks"]) == 2
        delegates = [
            op for op in state["operations"].values() if op["event_type"] == "executor.delegate"
        ]
        assert sum(op["target_ref"] is not None for op in delegates) == 1
        assert all(op["permit"] is None for op in delegates if op["target_ref"] is None)
        release.set()
        assert (await handle.wait()).status == "succeeded"


async def test_invalid_tool_arguments_settle_locally_without_attempt_or_hook(build):
    registration = CapabilityRegistration(
        "echo",
        FunctionToolAdapter(lambda a, c: pytest.fail("Invalid args must not execute")),
        (CapabilitySpec("echo", "echo", "echo", {"type": "object", "required": ["text"]}),),
    )

    def model(request):
        if any(m["role"] == "tool" for m in request.messages):
            feedback = json.loads(request.messages[-1]["content"])
            assert feedback["execution_fact"] == "not_started" and feedback["operation_id"] is None
            return ModelResponse("corrected")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            model,
            definition=AgentDefinition("a", "SYS", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(registration,)),
            hooks=HookSet(
                (HookBinding("never", "capability.invoke", "request", "never"),),
                {"never": FunctionHookHandler(lambda e, c: pytest.fail("No Operation"))},
            ),
        ).ainvoke("question")
        state = runtime._load(result.run_id)
        assert result.usage.tool_attempts == 0 and result.usage.loops_consumed == 2
        assert all(op["event_type"] == "model.generate" for op in state["operations"].values())
        assert len(state["tasks"][state["root_task_id"]]["local_outcomes"]) == 1


async def test_root_model_failure_releases_unconsumed_loop_ticket(build):
    def fail(request):
        raise AgentLoomError("MODEL_UNAVAILABLE")

    async with RuntimeEngine() as runtime:
        result = await build(runtime, fail).ainvoke("question")
        assert result.status == "failed"
        assert result.usage.loops_consumed == result.usage.loops_reserved == 0
        assert result.usage.business_model_attempts == 1
        op = next(iter(runtime._load(result.run_id)["operations"].values()))
        assert op["ticket"] == "released"


@pytest.mark.parametrize("limit,status", [(2, "budget_exhausted"), (3, "succeeded")])
async def test_executor_model_deny_consumes_feedback_loop_without_attempt(build, limit, status):
    def model(request):
        assert request.role_key == "planner"
        if any(m["role"] == "tool" for m in request.messages):
            return ModelResponse("handled failure")
        return ModelResponse(
            tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "do work"}),)
        )

    hooks = HookSet(
        (
            HookBinding(
                "deny",
                "model.generate",
                "request",
                "deny",
                matcher=HookMatcher(role_keys=("worker",)),
            ),
        ),
        {"deny": FunctionHookHandler(lambda e, c: HookDecision("deny"))},
    )
    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            model,
            cls=PlanAgent,
            hooks=hooks,
            executors={"worker": AgentDefinition("worker", "work", "one")},
            execution_policy=ExecutionPolicy(loop_max=limit),
        ).ainvoke("question")
        assert result.status == status and result.usage.loops_consumed == limit
        denied = next(
            op
            for op in runtime._load(result.run_id)["operations"].values()
            if op["role_key"] == "worker"
        )
        assert denied["attempts"] == [] and denied["permit"] is None
        assert denied["ticket"] == "consumed" and denied["consumption_kind"] == "error_feedback"


async def test_hook_cannot_publish_invalid_tool_result_or_change_to_unauthorized_tool(build):
    def model(request):
        if any(m["role"] == "tool" for m in request.messages):
            assert json.loads(request.messages[-1]["content"])["result"] == {"ok": True}
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    def redirect(event, context):
        result = json.loads(json.dumps(event.payload["result"]))
        if result["tool_calls"]:
            result["tool_calls"][0]["name"] = "unregistered"
        return HookDecision(payload_patch={"result": result})

    registration = CapabilityRegistration(
        "echo",
        FunctionToolAdapter(lambda a, c: {"ok": True}),
        (
            CapabilitySpec(
                "echo", "echo", "echo", output_schema={"type": "object", "required": ["ok"]}
            ),
        ),
    )
    hooks = HookSet(
        (
            HookBinding("redirect", "model.generate", "response", "redirect"),
            HookBinding("invalid", "capability.invoke", "response", "invalid"),
        ),
        {
            "redirect": FunctionHookHandler(redirect),
            "invalid": FunctionHookHandler(
                lambda e, c: HookDecision(payload_patch={"result": "bad"})
            ),
        },
    )
    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            model,
            definition=AgentDefinition("a", "SYS", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(registration,)),
            hooks=hooks,
        ).ainvoke("question")
        assert result.status == "succeeded" and result.usage.tool_attempts == 1
        codes = [
            h["diagnostic"]["code"]
            for op in runtime._load(result.run_id)["operations"].values()
            for h in op["hooks"].values()
            if h["diagnostic"]
        ]
        assert "CAPABILITY_NOT_FOUND" in codes and "HOOK_RESULT_SCHEMA_INVALID" in codes


async def test_invalid_terminal_frame_does_not_mark_supplement_delivered(build):
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        descriptor = ModelDescriptor("test", 32768)

        async def stream_once(self, request, context):
            entered.set()
            await release.wait()
            yield ProviderEvent("completed", {"text": 42, "tool_calls": []})

    async with RuntimeEngine() as runtime:
        handle = await build(runtime, None, model=Model()).start("question", session_id="session")
        await entered.wait()
        await runtime.inputs.append(handle.run_id, input_id="extra", content="KEEP THIS")
        release.set()
        assert (await handle.wait()).error["code"] == "MODEL_PROTOCOL_ERROR"
        state = runtime._load(handle.run_id)
        assert state["inputs"][1]["delivery_state"] == "pending_next_run"
        assert next(iter(state["operations"].values()))["attempts"][0]["first_output_at"] is None
        next_agent = build(runtime, lambda r: ModelResponse("done"))
        await next_agent.ainvoke("next", session_id="session")
        assert "KEEP THIS" in json.dumps(next_agent.model.requests[0].messages)


async def test_changed_workspace_rejected_before_preparing_new_environment(tmp_path, build):
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.sleep(60)

    async with RuntimeEngine() as runtime:
        await build(runtime, slow).start("question", run_id="recover")
        await entered.wait()
    other = tmp_path / "other-workspace"
    async with RuntimeEngine() as runtime:
        agent = build(runtime, slow, workspace=WorkspaceBinding(other))
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await agent.start(run_id="recover")
        assert not other.exists()


async def test_skill_catalog_uses_authorized_visible_alias(tmp_path, build):
    root = tmp_path / "skill"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: original\ndescription: guide\n---\nSkill body")
    registration = SkillRegistration(
        "pkg", SkillPackage.from_directory(root), DirectorySkillLoader()
    )

    def model(request):
        feedback = [m for m in request.messages if m["role"] == "tool"]
        if not feedback:
            return ModelResponse(tool_calls=(ToolCall("skill_list", {}),))
        if len(feedback) == 1:
            entry = json.loads(feedback[0]["content"])["result"]["entries"][0]
            assert entry["name"] == "alias" and entry["entry_name"] == "original"
            return ModelResponse(tool_calls=(ToolCall("skill_read", {"name": entry["name"]}),))
        assert "Skill body" in json.loads(feedback[-1]["content"])["result"]["text"]
        return ModelResponse("done")

    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            model,
            definition=AgentDefinition(
                "a", "SYS", capabilities=(CapabilityBinding("skill", "skill", "alias", "pkg"),)
            ),
            capabilities=CapabilityRegistry(skills=(registration,)),
        ).ainvoke("question")
        assert result.status == "succeeded" and result.usage.tool_attempts == 0


async def test_executor_compaction_failure_ends_run_instead_of_returning_tool_feedback(build):
    def model(request):
        if request.purpose == "compaction":
            raise AgentLoomError("SUMMARY_UNAVAILABLE")
        if request.role_key == "planner":
            assert not any(m["role"] == "tool" for m in request.messages)
            return ModelResponse(
                tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "do work"}),)
            )
        if not any(m["role"] == "tool" for m in request.messages):
            return ModelResponse(tool_calls=(ToolCall("echo", {}),))
        pytest.fail("Business model must not run after compaction fails")

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            None,
            cls=PlanAgent,
            model=ScriptedModel(model, context_window_tokens=8000),
            executors={
                "worker": AgentDefinition("worker", "work", "one", capabilities=(binding(),))
            },
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: "x" * 12000),)),
            context_policy=ContextPolicy(),
        )
        result = await agent.ainvoke("question")
        assert result.status == "failed" and result.error["code"] == "COMPACTION_FAILED"


async def test_terminal_commit_rechecks_deadline_even_when_monitor_cannot_run(build):
    def delay(command, state):
        if command.startswith("validate:"):
            time.sleep(1.05)

    async with RuntimeEngine(checkpoint_callback=delay) as runtime:
        result = await build(
            runtime,
            lambda r: ModelResponse("too late"),
            execution_policy=ExecutionPolicy(run_timeout_seconds=1),
        ).ainvoke("question")
        assert result.status == "timed_out" and result.output is None


async def test_mcp_repeated_catalog_cursor_fails_instead_of_looping():
    class Session:
        calls = 0

        async def list_tools(self, cursor=None):
            self.calls += 1
            return SimpleNamespace(tools=[], nextCursor="repeated")

    session = Session()

    class Adapter(McpToolAdapter):
        @asynccontextmanager
        async def _session(self):
            yield session

    with pytest.raises(AgentLoomError, match="MCP_CATALOG_CURSOR_INVALID"):
        await Adapter(command="unused").discover()
    assert session.calls == 2


def test_directory_shrink_reports_changed_version_before_cursor_range(tmp_path):
    environment = LocalEnvironment()
    scope = environment.prepare(WorkspaceBinding(tmp_path / "files"), "s", "r", 1)
    for index in range(4):
        environment.write(scope, str(index), "content")
    cursor = environment.list_page(scope, limit=3)["next_cursor"]
    for index in range(3):
        version = environment.stat(scope, str(index))
        environment.delete(scope, str(index), version["etag"])
    with pytest.raises(AgentLoomError, match="DIRECTORY_CHANGED"):
        environment.list_page(scope, limit=3, cursor=cursor)
