import asyncio
import json
from uuid import uuid4

import pytest
from conftest import binding, tool

from agentloom import (
    Agent,
    AgentDefinition,
    AgentLoomError,
    AgentNode,
    CapabilityBinding,
    CapabilityRegistry,
    ConfigurationError,
    ContextPolicy,
    ExecutionPolicy,
    ModelDescriptor,
    ModelOptions,
    ModelResponse,
    Node,
    PlanAgent,
    ProviderEvent,
    RequestError,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
    WaitTimeout,
)


async def test_public_identity_replay_and_observation(build):
    async with RuntimeEngine() as runtime:
        agent = build(runtime, lambda r: ModelResponse("answer"))
        assert isinstance(agent, Agent) and isinstance(AgentNode(agent), Node)
        run_id, input_id = str(uuid4()), str(uuid4())
        handle = await agent.start("question", run_id=run_id, input_id=input_id)
        result = await handle.wait(timeout_s=2)
        assert result.output.value == "answer" and result.usage.loops_consumed == 1
        assert result.usage.input_tokens is None and not result.usage.token_counts_complete
        await (await agent.start("question", run_id=run_id, input_id=input_id)).wait()
        assert len(agent.model.requests) == 1
        events = [event async for event in handle.events()]
        assert [e.seq for e in events] == list(range(1, len(events) + 1))
        assert events[-1].type == "run.completed"
        assert [event async for event in handle.events(after_seq=events[-1].seq)] == []
        with pytest.raises(RequestError, match="INPUT_CONFLICT"):
            await agent.start("different", run_id=run_id)
        with pytest.raises(RequestError, match="AGENT_INSTANCE_BOUND"):
            await agent.start("new question")
        with pytest.raises(RequestError, match="EVENT_CURSOR_AHEAD"):
            _ = [event async for event in handle.events(after_seq=10000)]
        assert "context" not in json.dumps(await handle.snapshot())


@pytest.mark.parametrize("parallel,peak_expected", [(False, 1), (True, 2)])
async def test_plan_private_roles_and_stable_all_settled_batch(build, parallel, peak_expected):
    active, peak, finished = 0, 0, []

    async def model(request):
        nonlocal active, peak
        feedback = [m for m in request.messages if m["role"] == "tool"]
        if request.role_key == "planner":
            if not feedback:
                return ModelResponse(
                    tool_calls=tuple(
                        ToolCall("delegate_task", {"executor": "worker", "task": x})
                        for x in ("slow", "fail", "fast")
                    )
                )
            assert len(finished) == 3
            assert [json.loads(m["content"])["status"] for m in feedback] == [
                "succeeded",
                "failed",
                "succeeded",
            ]
            return ModelResponse("combined")
        assert request.messages[0]["content"] == "PRIVATE WORKER"
        assert "PLANNER SECRET" not in json.dumps(request.messages)
        word = request.messages[-1]["content"]
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03 if word == "slow" else 0.005)
            if word == "fail":
                raise AgentLoomError("DOMAIN_FAILURE")
            return ModelResponse(word)
        finally:
            active -= 1
            finished.append(word)

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            cls=PlanAgent,
            definition=AgentDefinition("planner", "PLANNER SECRET"),
            executors={"worker": AgentDefinition("worker", "PRIVATE WORKER", "Process one task")},
            execution_policy=ExecutionPolicy(parallel_enabled=parallel),
        )
        result = await agent.ainvoke("do three tasks")
        assert result.status == "succeeded" and peak == peak_expected
        assert result.usage.loops_consumed == 5
        state = runtime._load(result.run_id)
        assert (
            len(state["tasks"])
            == len({t["context"]["context_id"] for t in state["tasks"].values()})
            == 4
        )
        assert all(
            o["target_ref"] in state["tasks"] and o["permit"]
            for o in state["operations"].values()
            if o["event_type"] == "executor.delegate"
        )


async def test_plan_does_not_invent_executor(build):
    async with RuntimeEngine() as runtime:
        agent = build(runtime, lambda r: ModelResponse("done"), cls=PlanAgent)
        await agent.ainvoke("question")
        assert "delegate_task" not in [t["name"] for t in agent.model.requests[0].tools]


@pytest.mark.parametrize("limit", [1, 100])
async def test_exact_last_loop_and_format_correction_budget(build, limit):
    calls = 0

    def model(request):
        nonlocal calls
        calls += 1
        return ModelResponse('{"ok":true}' if calls == limit else "invalid")

    async with RuntimeEngine() as runtime:
        definition = AgentDefinition(
            "a", "SYSTEM", output_schema={"type": "object", "required": ["ok"]}
        )
        agent = build(
            runtime, model, definition=definition, execution_policy=ExecutionPolicy(loop_max=limit)
        )
        result = await agent.ainvoke("question")
        assert result.output.value == {"ok": True} and result.usage.loops_consumed == limit
        other = build(
            runtime,
            lambda r: ModelResponse("invalid"),
            definition=definition,
            execution_policy=ExecutionPolicy(loop_max=limit),
        )
        result = await other.ainvoke("question")
        assert result.status == "budget_exhausted" and result.usage.loops_reserved == 0
        assert len(other.model.requests) == limit


async def test_sdk_retry_is_separate_from_new_model_decision(build):
    attempts = 0

    async def unavailable(arguments, context):
        nonlocal attempts
        attempts += 1
        raise AgentLoomError("TEMPORARY_RESET", category="transport", retryable_transport=True)

    def model(request):
        feedback = [m for m in request.messages if m["role"] == "tool"]
        return (
            ModelResponse("stop")
            if len(feedback) == 2
            else ModelResponse(tool_calls=(ToolCall("echo", {}),))
        )

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            definition=AgentDefinition("a", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(unavailable),)),
            execution_policy=ExecutionPolicy(retry=RetryPolicy(True, 2, 0)),
        )
        result = await agent.ainvoke("question")
        ops = [
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "capability.invoke"
        ]
        assert result.status == "succeeded" and attempts == 4 and len(ops) == 2
        assert all(len(o["attempts"]) == 2 for o in ops)


async def test_stop_and_cancelled_wait_are_distinct(build):
    started, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        started.set()
        await release.wait()
        return ModelResponse("done")

    async with RuntimeEngine() as runtime:
        h = await build(runtime, model).start("question", session_id="s")
        await started.wait()
        with pytest.raises(WaitTimeout):
            await h.wait(timeout_s=0.01)
        waiter = asyncio.create_task(h.wait())
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await h.snapshot())["status"] == "running"
        with pytest.raises(RequestError, match="SESSION_BUSY"):
            await build(runtime, model).start("new", session_id="s")
        receipt = await h.stop(command_id="stop")
        assert receipt["status_at_commit"] == "stopping"
        result = await h.wait(timeout_s=2)
        assert result.status == "cancelled" and result.usage.loops_reserved == 0
        assert await h.stop(command_id="stop") == receipt
        with pytest.raises(RequestError, match="IDEMPOTENCY_CONFLICT"):
            await h.stop(command_id="stop", reason="different")


async def test_stop_before_start_and_invalid_identity(build):
    async with RuntimeEngine() as runtime:
        await runtime.runs.stop("future", command_id="stop")
        with pytest.raises(RequestError, match="RUN_ADMISSION_CLOSED"):
            await build(runtime, lambda r: ModelResponse("bad")).start("question", run_id="future")
        with pytest.raises(RequestError, match="RUN_NOT_FOUND"):
            await build(runtime, lambda r: ModelResponse("bad")).start(run_id="missing")
        with pytest.raises(RequestError, match="INVALID_ID_COMBINATION"):
            await build(runtime, lambda r: ModelResponse("bad")).start(
                "question", input_id="only-input"
            )


async def test_supplement_cutoff_delivery_and_carry(build):
    started, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        started.set()
        await release.wait()
        return ModelResponse("first")

    async with RuntimeEngine() as runtime:
        handle = await build(runtime, model).start("original", session_id="s")
        await started.wait()
        receipt = await runtime.inputs.append(
            handle.run_id, input_id="supplement", content="UNSEEN CONSTRAINT"
        )
        assert receipt["delivery_state"] == "pending"
        release.set()
        await handle.wait()
        receipt = await runtime.inputs.append(
            handle.run_id, input_id="supplement", content="UNSEEN CONSTRAINT"
        )
        assert receipt["delivery_state"] == "pending_next_run"
        with pytest.raises(RequestError, match="RUN_ENDED"):
            await runtime.inputs.append(handle.run_id, input_id="too-late", content="new")
        next_agent = build(runtime, lambda r: ModelResponse("second"))
        await next_agent.ainvoke("next", session_id="s")
        assert "UNSEEN CONSTRAINT" in json.dumps(next_agent.model.requests[0].messages)
        receipt = await runtime.inputs.append(
            handle.run_id, input_id="supplement", content="UNSEEN CONSTRAINT"
        )
        assert receipt["delivery_state"] == "delivered" and receipt["run_id"] == handle.run_id


@pytest.mark.parametrize(
    "mode,code",
    [("heartbeat", "MODEL_FIRST_OUTPUT_TIMEOUT"), ("partial", "RESET"), ("root", "RUN_TIMEOUT")],
)
async def test_stream_timeouts_and_incomplete_material(build, mode, code):
    class Model:
        descriptor = ModelDescriptor("test", 32768)

        async def stream_once(self, request, context):
            if mode == "partial":
                yield ProviderEvent("text_delta", "incomplete")
                raise AgentLoomError("RESET", category="transport", retryable_transport=True)
            for _ in range(200):
                await asyncio.sleep(0.01)
                yield ProviderEvent("heartbeat")
            yield ProviderEvent("completed", ModelResponse("late").to_dict())

    async with RuntimeEngine() as runtime:
        policy = (
            ExecutionPolicy(run_timeout_seconds=1)
            if mode == "root"
            else ExecutionPolicy(model_first_output_timeout_seconds=1)
        )
        result = await build(runtime, None, model=Model(), execution_policy=policy).ainvoke(
            "question"
        )
        assert result.error["code"] == code and result.output is None
        if mode == "partial":
            assert result.partial_results == ()
            assert runtime._load(result.run_id)["partial_results"][0]["text"] == "incomplete"


async def test_first_output_removes_only_first_output_timer(build):
    class Model:
        descriptor = ModelDescriptor("test", 32768)

        async def stream_once(self, request, context):
            yield ProviderEvent("text_delta", "early")
            await asyncio.sleep(1.05)
            yield ProviderEvent("completed", ModelResponse("complete").to_dict())

    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            None,
            model=Model(),
            execution_policy=ExecutionPolicy(model_first_output_timeout_seconds=1),
        ).ainvoke("question")
        assert result.status == "succeeded"


def test_configuration_is_local_and_deeply_immutable(tmp_path, build):
    variables, schema = {"name": "before"}, {"type": "object"}
    definition = AgentDefinition("a", "Hello {{name}}", variables=variables, input_schema=schema)
    variables["name"], schema["type"] = "after", "array"
    with pytest.raises(TypeError):
        definition.variables["name"] = "tampered"
    assert definition.variables["name"] == "before" and definition.input_schema["type"] == "object"
    runtime = RuntimeEngine()
    agent = build(runtime, lambda r: ModelResponse("done"), definition=definition)
    assert not (tmp_path / "workspace").exists() and not agent.model.requests


@pytest.mark.parametrize(
    "kwargs",
    [
        {"loop_max": True},
        {"loop_max": 0},
        {"run_timeout_seconds": float("nan")},
        {"executor_max_parallel": 129},
        {"run_timeout_seconds": None},
        {"model_first_output_timeout_seconds": None},
        {"tool_timeout_seconds": None},
        {"run_timeout_seconds": 0.5},
        {"model_first_output_timeout_seconds": 0.5},
        {"model_stream_idle_timeout_seconds": 0.5},
        {"tool_timeout_seconds": 0.5},
    ],
)
def test_policy_rejects_invalid_numbers(kwargs):
    with pytest.raises(ConfigurationError):
        ExecutionPolicy(**kwargs)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CapabilityBinding("b", "tool", "bad.name", "a"),
        lambda: CapabilityBinding("b", "tool", "good", "a", access_scope={"read_only": "false"}),
        lambda: CapabilityBinding("b", "tool", "good", "a", access_scope={"paths": "allowed"}),
        lambda: CapabilityBinding("b", "tool", "good", "a", access_scope={"paths": ["../outside"]}),
        lambda: ModelOptions(parameters=[]),
        lambda: AgentDefinition("a", "SYSTEM", capabilities=[{}]),
    ],
)
def test_invalid_assembly_fails_locally(factory):
    with pytest.raises(ConfigurationError):
        factory()


async def test_output_reserve_is_independent_of_provider_output_cap(build):
    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse("done"),
            context_policy=ContextPolicy(output_reserve_tokens=8192),
        )
        result = await agent.ainvoke("question")
        assert result.status == "succeeded"
        assert agent.model.requests[0].max_output_tokens == 4096


async def test_parallel_usage_is_complete_when_all_attempts_report_counts(build):
    async def model(request):
        usage = {"input_tokens": 10, "output_tokens": 2}
        if request.role_key == "planner" and len(request.messages) == 2:
            return ModelResponse(
                tool_calls=tuple(
                    ToolCall("delegate_task", {"executor": "worker", "task": str(i)})
                    for i in range(3)
                ),
                usage=usage,
            )
        await asyncio.sleep(0.01)
        return ModelResponse("done", usage=usage)

    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            model,
            cls=PlanAgent,
            executors={"worker": AgentDefinition("w", "SYS", "work")},
            execution_policy=ExecutionPolicy(parallel_enabled=True),
        ).ainvoke("question")
        assert result.usage.token_counts_complete
        assert result.usage.input_tokens == 50 and result.usage.output_tokens == 10
