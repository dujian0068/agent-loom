import asyncio
import json

import pytest

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionHookHandler,
    FunctionToolAdapter,
    HookBinding,
    HookDecision,
    HookMatcher,
    HookSet,
    MemoryRuntimeStore,
    ModelDescriptor,
    ModelResponse,
    PlanAgent,
    ProviderEvent,
    ReactAgent,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)


@pytest.fixture(params=("memory", "file"))
def store(request, tmp_path):
    backend = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield backend
    backend.close()


def agent(runtime, root, responder, *, function=None, hooks=None, cls=ReactAgent, **kwargs):
    capabilities = CapabilityRegistry()
    bindings = ()
    if function is not None:
        capabilities = CapabilityRegistry(
            tools=(
                CapabilityRegistration(
                    "echo",
                    FunctionToolAdapter(function),
                    (CapabilitySpec("echo", "echo", "Echo", {"type": "object"}),),
                ),
            )
        )
        bindings = (CapabilityBinding("echo", "tool", "echo", "echo", "echo"),)
    return cls(
        definition=AgentDefinition("a", "SYSTEM", capabilities=bindings),
        model=kwargs.pop("model", ScriptedModel(responder)),
        workspace=WorkspaceBinding(root),
        runtime_engine=runtime,
        capabilities=capabilities,
        hooks=hooks,
        **kwargs,
    )


async def test_tool_request_hook_updates_effective_history(tmp_path, store):
    executed, requests = [], []

    def echo(arguments, context):
        executed.append(arguments)
        return "ok"

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return ModelResponse(tool_calls=(ToolCall("echo", {"path": "old.txt"}),))
        return ModelResponse("done")

    hooks = HookSet(
        (HookBinding("rewrite", "capability.invoke", "request", "h"),),
        {
            "h": FunctionHookHandler(
                lambda e, c: HookDecision(payload_patch={"arguments": {"path": "new.txt"}})
            )
        },
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(runtime, tmp_path, respond, function=echo, hooks=hooks).ainvoke("go")
        assert result.status == "succeeded"
        assert executed == [{"path": "new.txt"}]
        messages = requests[1].messages
        call = next(m for m in messages if m.get("tool_calls"))["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"path": "new.txt"}
        state = runtime._load(result.run_id)
        context = state["tasks"][state["root_task_id"]]["context"]
        record = next(r for r in context["records"] if r["message"].get("tool_calls"))
        raw = record["raw"]["tool_calls"][0]
        assert json.loads(raw["function"]["arguments"]) == {"path": "old.txt"}
        assert raw["id"] == call["id"]


async def test_exhausted_loop_does_not_call_compaction_model(tmp_path, store):
    requests = []

    def respond(request):
        requests.append(request)
        if request.purpose == "compaction":
            return ModelResponse("summarized")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    model = ScriptedModel(respond, context_window_tokens=8192, max_output_tokens=512)
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            None,
            model=model,
            function=lambda a, c: "x" * 10000,
            execution_policy=ExecutionPolicy(loop_max=1),
        ).ainvoke("go")
        assert result.status == "budget_exhausted"
        assert result.usage.business_model_attempts == 1
        assert result.usage.compaction_model_attempts == 0, [r.purpose for r in requests]


async def test_oversized_fixed_hook_patch_fails_open(tmp_path, store):
    def oversized(event, context):
        system = context.reader.messages()[0]
        return HookDecision(context_patch={system["record_id"]: "x" * 10000})

    hooks = HookSet(
        (HookBinding("oversized", "model.generate", "request", "h"),),
        {"h": FunctionHookHandler(oversized)},
    )
    model = ScriptedModel(
        lambda r: ModelResponse("done"),
        context_window_tokens=2048,
        max_output_tokens=256,
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(runtime, tmp_path, None, model=model, hooks=hooks).ainvoke("go")
        assert result.status == "succeeded", result.error
        assert model.requests[0].messages[0]["content"] == "SYSTEM"


async def test_hook_change_invalidates_summary_for_current_request(tmp_path, store):
    business = []
    patched = []

    def respond(request):
        if request.purpose == "compaction":
            return ModelResponse("OLD_SUMMARY")
        business.append(request)
        if len(business) <= 2:
            return ModelResponse(tool_calls=(ToolCall("echo", {"round": len(business)}),))
        return ModelResponse("done")

    def revise(event, context):
        records = context.reader.messages()
        if not patched and any(
            "Earlier execution summary:" in r["message"]["content"] for r in records
        ):
            old_tool = next(r for r in records if r["message"]["role"] == "tool")
            patched.append(old_tool["record_id"])
            return HookDecision(context_patch={old_tool["record_id"]: "CORRECTED_FACT"})
        return HookDecision()

    hooks = HookSet(
        (
            HookBinding(
                "revise",
                "model.generate",
                "request",
                "h",
                matcher=HookMatcher(purposes=("business",)),
            ),
        ),
        {"h": FunctionHookHandler(revise)},
    )
    model = ScriptedModel(respond, context_window_tokens=8192, max_output_tokens=512)
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            None,
            model=model,
            hooks=hooks,
            function=lambda a, c: "x" * 10000 if a["round"] == 1 else "ok",
        ).ainvoke("go")
        assert result.status == "succeeded", result.error
        assert patched and len(business) == 3
        assert "CORRECTED_FACT" in json.dumps(business[-1].messages), business[-1].messages
        assert "OLD_SUMMARY" not in json.dumps(business[-1].messages)


async def test_carried_supplement_keeps_acceptance_order(tmp_path, store):
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        started.set()
        await release.wait()
        return ModelResponse("done")

    async with RuntimeEngine(store=store) as runtime:
        first = agent(runtime, tmp_path, respond)
        handle = await first.start("QUESTION_1", session_id="session")
        await started.wait()
        await runtime.inputs.append(
            handle.run_id, input_id="supplement", content="OLDER_SUPPLEMENT"
        )
        release.set()
        assert (await handle.wait()).status == "succeeded"
        model = ScriptedModel(lambda r: ModelResponse("done"))
        second = agent(runtime, tmp_path, None, model=model)
        assert (await second.ainvoke("NEWER_QUESTION", session_id="session")).status == "succeeded"
        inputs = [m["content"] for m in model.requests[0].messages if m["role"] == "user"]
        assert inputs.index("OLDER_SUPPLEMENT") < inputs.index("NEWER_QUESTION"), inputs


async def test_model_first_output_timeout_retries_when_enabled(tmp_path, store):
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.sleep(1.2)
        return ModelResponse("done")

    policy = ExecutionPolicy(
        model_first_output_timeout_seconds=1,
        retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(runtime, tmp_path, respond, execution_policy=policy).ainvoke("go")
        assert result.status == "succeeded", result.error
        assert result.usage.business_model_attempts == 2
        assert result.usage.loops_consumed == 1
        assert calls[0].operation_id == calls[1].operation_id
        assert calls[0].messages == calls[1].messages


async def test_tool_timeout_retries_when_enabled(tmp_path, store):
    calls = []

    async def echo(arguments, context):
        calls.append(context.attempt_id)
        if len(calls) == 1:
            await asyncio.sleep(1.2)
        return "done"

    def respond(request):
        if any(m["role"] == "tool" for m in request.messages):
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    policy = ExecutionPolicy(
        tool_timeout_seconds=1,
        retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime, tmp_path, respond, function=echo, execution_policy=policy
        ).ainvoke("go")
        assert result.status == "succeeded"
        assert result.usage.tool_attempts == 2, result.usage
        assert len(set(calls)) == 2


async def test_parallel_argument_patches_preserve_sibling_calls(tmp_path, store):
    arrived, rewritten, requests = [], [], []
    barrier = asyncio.Event()

    async def patch(event, context):
        args = dict(event.payload["arguments"])
        arrived.append(args["path"])
        if len(arrived) == 2:
            barrier.set()
        await barrier.wait()
        return HookDecision(payload_patch={"arguments": {"path": "new-" + args["path"]}})

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return ModelResponse(
                tool_calls=tuple(ToolCall("echo", {"path": p}) for p in ("a", "b"))
            )
        return ModelResponse("done")

    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            respond,
            function=lambda a, c: rewritten.append(a["path"]) or "ok",
            hooks=HookSet(
                (HookBinding("rewrite", "capability.invoke", "request", "h"),),
                {"h": FunctionHookHandler(patch)},
            ),
            execution_policy=ExecutionPolicy(parallel_enabled=True),
        ).ainvoke("go")
        assert result.status == "succeeded"
        assert sorted(rewritten) == ["new-a", "new-b"]
        calls = next(m["tool_calls"] for m in requests[-1].messages if m.get("tool_calls"))
        assert [json.loads(c["function"]["arguments"])["path"] for c in calls] == ["new-a", "new-b"]


@pytest.mark.parametrize("cut", ("hook-result:", "hook-apply:"))
async def test_argument_patch_recovery_reuses_saved_hook_and_effective_history(
    tmp_path, store, cut
):
    reached = asyncio.Event()
    requests, hooks_called, executed = [], [], []

    def checkpoint(command, state):
        if command.startswith(cut):
            reached.set()
            raise asyncio.CancelledError

    def rewrite(event, context):
        hooks_called.append(event.operation_id)
        return HookDecision(payload_patch={"arguments": {"path": "new.txt"}})

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return ModelResponse(tool_calls=(ToolCall("echo", {"path": "old.txt"}),))
        return ModelResponse("done")

    def build(runtime):
        return agent(
            runtime,
            tmp_path,
            respond,
            function=lambda a, c: executed.append(a) or "ok",
            hooks=HookSet(
                (HookBinding("rewrite", "capability.invoke", "request", "h"),),
                {"h": FunctionHookHandler(rewrite)},
            ),
        )

    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        await build(runtime).start("go", run_id="hook-recovery")
        await asyncio.wait_for(reached.wait(), 2)
    assert executed == [] and len(hooks_called) == 1
    async with RuntimeEngine(store=store) as runtime:
        result = await (await build(runtime).start(run_id="hook-recovery")).wait(timeout_s=3)
        assert result.status == "succeeded"
        assert len(hooks_called) == 1 and executed == [{"path": "new.txt"}]
        call = next(m["tool_calls"][0] for m in requests[-1].messages if m.get("tool_calls"))
        assert json.loads(call["function"]["arguments"]) == executed[0]


async def test_oversized_payload_patch_is_atomic_and_visible_to_next_hook(tmp_path, store):
    observed = []

    def oversized(event, context):
        messages = [dict(m) for m in event.payload["messages"]]
        messages[0]["content"] = "x" * 10000
        return HookDecision(payload_patch={"messages": messages, "max_output_tokens": 1})

    def observe(event, context):
        observed.append(
            (event.payload["messages"][0]["content"], event.payload["max_output_tokens"])
        )
        assert context.reader.messages()[0]["message"]["content"] == "SYSTEM"
        return HookDecision()

    model = ScriptedModel(
        lambda r: ModelResponse("done"), context_window_tokens=2048, max_output_tokens=256
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            None,
            model=model,
            hooks=HookSet(
                (
                    HookBinding("oversized", "model.generate", "request", "bad"),
                    HookBinding("observe", "model.generate", "request", "next", 1),
                ),
                {"bad": FunctionHookHandler(oversized), "next": FunctionHookHandler(observe)},
            ),
        ).ainvoke("go")
        assert result.status == "succeeded" and observed == [("SYSTEM", 256)]
        op = next(iter(runtime._load(result.run_id)["operations"].values()))
        assert (
            op["hooks"]["request:oversized"]["diagnostic"]["code"] == "HOOK_FIXED_INPUT_TOO_LARGE"
        )


async def test_large_compressible_hook_patch_keeps_normal_compaction(tmp_path, store):
    business = []

    def respond(request):
        if request.purpose == "compaction":
            return ModelResponse("Tool returned expanded facts.")
        business.append(request)
        if len(business) == 1:
            return ModelResponse(tool_calls=(ToolCall("echo", {}),))
        return ModelResponse("done")

    hooks = HookSet(
        (HookBinding("expand", "capability.invoke", "response", "h"),),
        {
            "h": FunctionHookHandler(
                lambda e, c: HookDecision(payload_patch={"result": "x" * 10000})
            )
        },
    )
    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            None,
            model=ScriptedModel(respond, context_window_tokens=8192, max_output_tokens=512),
            function=lambda a, c: "raw",
            hooks=hooks,
        ).ainvoke("go")
        assert result.status == "succeeded" and result.usage.compaction_model_attempts > 0
        op = next(
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "capability.invoke"
        )
        assert op["raw"]["result"] == "raw" and op["response"]["result"] == "x" * 10000
        assert op["hooks"]["response:expand"]["diagnostic"] is None


async def test_stream_idle_timeout_retries_with_one_stage_chain(tmp_path, store):
    attempts, stages = [], []

    class Model:
        descriptor = ModelDescriptor("idle-test", 32768, 1024)

        async def stream_once(self, request, context):
            attempts.append(context.attempt_id)
            if len(attempts) == 1:
                yield ProviderEvent("text_delta", "incomplete")
                await asyncio.sleep(2)
            yield ProviderEvent("completed", ModelResponse("done").to_dict())

    def hook(event, context):
        stages.append(event.stage)
        return HookDecision()

    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            None,
            model=Model(),
            hooks=HookSet(
                tuple(
                    HookBinding(stage, "model.generate", stage, "h")
                    for stage in ("request", "response")
                ),
                {"h": FunctionHookHandler(hook)},
            ),
            execution_policy=ExecutionPolicy(
                model_stream_idle_timeout_seconds=1,
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
            ),
        ).ainvoke("go")
        assert result.status == "succeeded" and result.output.value == "done"
        assert result.usage.loops_consumed == 1 and result.usage.business_model_attempts == 2
        assert len(set(attempts)) == 2 and stages == ["request", "response"]
        assert (
            len(result.partial_results) == 1 and result.partial_results[0]["text"] == "incomplete"
        )


@pytest.mark.parametrize("target", ("model", "tool"))
async def test_root_deadline_never_becomes_a_retryable_attempt_timeout(tmp_path, store, target):
    calls = []

    async def slow(*args):
        calls.append(1)
        await asyncio.sleep(5)
        return ModelResponse("late") if target == "model" else "late"

    async with RuntimeEngine(store=store) as runtime:
        result = await agent(
            runtime,
            tmp_path,
            slow
            if target == "model"
            else lambda r: ModelResponse(tool_calls=(ToolCall("echo", {}),)),
            function=slow if target == "tool" else None,
            execution_policy=ExecutionPolicy(
                run_timeout_seconds=1,
                retry=RetryPolicy(enabled=True, max_attempts=3, delay_seconds=0),
            ),
        ).ainvoke("go")
        assert result.status == "timed_out" and result.error["code"] == "RUN_TIMEOUT"
        assert len(calls) == 1 and result.usage.loops_reserved == 0


async def test_reserved_loop_capacity_blocks_compaction_until_settled(tmp_path, store):
    release, returned_tool = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(request)
        if request.role_key == "planner":
            return ModelResponse(
                tool_calls=tuple(
                    ToolCall("delegate_task", {"executor": role, "task": "work"})
                    for role in ("blocker", "worker")
                )
            )
        if request.role_key == "blocker":
            await release.wait()
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    def large_tool(args, context):
        returned_tool.set()
        return "x" * 10000

    async with RuntimeEngine(store=store) as runtime:
        handle = await agent(
            runtime,
            tmp_path,
            None,
            cls=PlanAgent,
            model=ScriptedModel(respond, context_window_tokens=8192, max_output_tokens=512),
            function=large_tool,
            executors={
                "blocker": AgentDefinition("blocker", "wait", "wait"),
                "worker": AgentDefinition(
                    "worker",
                    "work",
                    "work",
                    capabilities=(CapabilityBinding("echo", "tool", "echo", "echo", "echo"),),
                ),
            },
            execution_policy=ExecutionPolicy(parallel_enabled=True, loop_max=3),
        ).start("go")
        await asyncio.wait_for(returned_tool.wait(), 2)
        await asyncio.sleep(0.03)
        assert runtime._load(handle.run_id)["status"] == "running"
        assert all(r.purpose == "business" for r in requests)
        release.set()
        result = await handle.wait(timeout_s=3)
        assert result.status == "budget_exhausted"
        assert result.usage.loops_consumed == 3 and result.usage.compaction_model_attempts == 0


async def test_supplements_keep_order_and_identity_across_two_transfers(tmp_path, store):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        entered.set()
        await release.wait()
        return ModelResponse("done")

    async with RuntimeEngine(store=store) as runtime:
        first = await agent(runtime, tmp_path, delayed).start("Q1", session_id="s")
        await asyncio.wait_for(entered.wait(), 2)
        for content in ("A", "B"):
            await runtime.inputs.append(first.run_id, input_id=content, content=content)
        release.set()
        await first.wait()
        entered.clear()
        release.clear()
        second = await agent(runtime, tmp_path, delayed).start("Q2", session_id="s")
        await asyncio.wait_for(entered.wait(), 2)
        await second.stop(command_id="stop-before-delivery")
        assert (await second.wait()).status == "cancelled"

        model = ScriptedModel(lambda r: ModelResponse("done"))
        third = await agent(runtime, tmp_path, None, model=model).ainvoke("Q3", session_id="s")
        assert third.status == "succeeded"
        selected = [
            m["content"] for m in model.requests[0].messages if m["content"] in {"A", "B", "Q3"}
        ]
        assert selected == ["A", "B", "Q3"]
        state = runtime._load(third.run_id)
        assert [i["session_seq"] for i in state["inputs"]] == [2, 3, 5]
        assert all(i["delivery_state"] == "delivered" for i in state["inputs"])
        history = await runtime.sessions.history("s")
        assert [r["run_id"] for r in history["items"]] == [
            first.run_id,
            second.run_id,
            third.run_id,
        ]
        for content in ("A", "B"):
            receipt = await runtime.inputs.append(first.run_id, input_id=content, content=content)
            assert receipt["delivery_state"] == "delivered" and receipt["run_id"] == first.run_id
