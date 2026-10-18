"""Independent SDK contract regressions, including previously reproduced defects.

Run explicitly with:
  .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_audit.py
Original failures are archived in evidence/; current assertions follow docs/sdk.
"""

import asyncio
import hashlib
import json
import time

import httpx
import pytest

from agentloom import (
    AgentDefinition,
    AgentInput,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ConfigurationError,
    ContextPolicy,
    DirectorySkillLoader,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionHookHandler,
    FunctionToolAdapter,
    HookBinding,
    HookDecision,
    HookSet,
    LocalEnvironment,
    MemoryRuntimeStore,
    ModelResponse,
    OpenAIChatAdapter,
    PlanAgent,
    ReactAgent,
    RequestError,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    SkillPackage,
    SkillRegistration,
    ToolCall,
    WorkspaceBinding,
)


@pytest.fixture(autouse=True)
def isolated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    backend = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield backend
    backend.close()


def build(runtime, responder=None, *, cls=ReactAgent, **kwargs):
    kwargs.setdefault("definition", AgentDefinition("audit", "SYSTEM"))
    kwargs.setdefault("model", ScriptedModel(responder or (lambda r: ModelResponse("done"))))
    kwargs.setdefault("workspace", WorkspaceBinding("workspace"))
    kwargs.setdefault("execution_policy", ExecutionPolicy(run_timeout_seconds=10))
    return cls(runtime_engine=runtime, **kwargs)


def show_run_evidence(runtime, run_id):
    state = runtime._load(run_id)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": state["status"],
                "terminal_seq": state["terminal_seq"],
                "deadline_at": state["deadline_at"],
                "observed_at": time.time(),
                "operations": [
                    {key: op[key] for key in ("operation_id", "status", "phase", "ticket")}
                    for op in state["operations"].values()
                ],
            },
            sort_keys=True,
        )
    )


def assert_quiescent(runtime, run_id):
    state = runtime._load(run_id)
    assert state["status"] == "failed" and state["terminal_seq"] is not None
    assert state["usage"]["loops_reserved"] == 0
    assert all(
        op["status"] != "pending" and op["ticket"] != "reserved"
        for op in state["operations"].values()
    )
    assert len([e for e in state["events"] if e["type"] == "run.completed"]) == 1


POLICY_RANGES = [
    (ExecutionPolicy, "loop_max", 1, 100000),
    (ExecutionPolicy, "run_timeout_seconds", 1, 86400),
    (ExecutionPolicy, "executor_max_parallel", 1, 128),
    (ExecutionPolicy, "model_first_output_timeout_seconds", 1, 3600),
    (ExecutionPolicy, "model_stream_idle_timeout_seconds", 1, 3600),
    (ExecutionPolicy, "tool_timeout_seconds", 1, 3600),
    (RetryPolicy, "max_attempts", 1, 20),
    (RetryPolicy, "delay_seconds", 0, 300),
    (ContextPolicy, "recent_runs", 0, 100),
]


@pytest.mark.parametrize("factory,field,lower,upper", POLICY_RANGES)
def test_numeric_policy_boundaries(factory, field, lower, upper):
    for value in (lower, upper):
        assert getattr(factory(**{field: value}), field) == value
    invalid = (lower - 1, upper + 1, True, "1", float("nan"), float("inf"), -float("inf"))
    if field != "model_stream_idle_timeout_seconds":
        invalid += (None,)
    for value in invalid:
        with pytest.raises(ConfigurationError):
            factory(**{field: value})


def test_context_and_retry_special_boundaries():
    assert ExecutionPolicy(model_stream_idle_timeout_seconds=None)
    assert ContextPolicy(trigger_ratio=0.000001, safety_margin_tokens=0)
    for value in (None, 0, 1, -0.1, 1.1, True, float("nan"), float("inf")):
        with pytest.raises(ConfigurationError):
            ContextPolicy(trigger_ratio=value)
    for factory, field in ((ExecutionPolicy, "parallel_enabled"), (RetryPolicy, "enabled")):
        for value in (None, 0, 1, "false", []):
            with pytest.raises(ConfigurationError):
                factory(**{field: value})


@pytest.mark.parametrize("value", ["", " ", "\t\n", None, 0, False, {}, []])
async def test_no_schema_rejects_empty_and_nontext_input(store, value):
    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime)
        with pytest.raises(RequestError):
            await agent.start(AgentInput(value), run_id="bad-input")
        assert agent.model.requests == []
        assert store.load(runtime.namespace, "bad-input") is None


@pytest.mark.parametrize("value", [None, "", 0, False, {}, []])
async def test_explicit_json_schema_preserves_falsy_values(store, value):
    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse(json.dumps(value)),
            definition=AgentDefinition("json", "SYSTEM", input_schema={}, output_schema={}),
        )
        result = await (await agent.start(AgentInput(value))).wait(timeout_s=4)
        assert result.status == "succeeded"
        assert json.loads(json.dumps(result.output.value)) == value
        assert result.usage.loops_consumed == 1


async def test_same_session_and_run_id_admission(store):
    started, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        started.set()
        await release.wait()
        return ModelResponse("done")

    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime, model)
        handle = await agent.start("question", run_id="same", session_id="s")
        await asyncio.wait_for(started.wait(), 4)
        attached = await build(runtime, model).start("question", run_id="same", session_id="s")
        assert attached.run_id == handle.run_id
        with pytest.raises(RequestError, match="INPUT_CONFLICT"):
            await build(runtime).start("different", run_id="same")
        with pytest.raises(RequestError, match="SESSION_BUSY"):
            await build(runtime).start("new question", run_id="other", session_id="s")
        with pytest.raises(RequestError, match="AGENT_INSTANCE_BOUND"):
            await agent.start("question")
        release.set()
        assert (await handle.wait(timeout_s=4)).status == "succeeded"
        assert len(agent.model.requests) == 1


@pytest.mark.parametrize("bad_task", [None, "", " \n", 0, {}, []])
async def test_invalid_delegate_hook_patch_fails_open(store, bad_task):
    """AUD-01: invalid optional patches must preserve the original valid call."""
    children = []

    def model(request):
        if request.role_key == "worker":
            children.append(request)
            return ModelResponse("child result")
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        return ModelResponse(
            tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "valid task"}),)
        )

    def patch(event, context):
        return HookDecision(payload_patch={"arguments": {"executor": "worker", "task": bad_task}})

    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            model,
            cls=PlanAgent,
            executors={"worker": AgentDefinition("worker", "WORKER", "worker description")},
            hooks=HookSet(
                (HookBinding("patch", "executor.delegate", "request", "patch"),),
                {"patch": FunctionHookHandler(patch)},
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=4)
        op = next(
            op
            for op in runtime._load(result.run_id)["operations"].values()
            if op["event_type"] == "executor.delegate"
        )
        assert len(children) == 1, op["response"]
        assert op["request"]["arguments"]["task"] == "valid task"
        assert op["hooks"]["request:patch"]["diagnostic"] is not None


@pytest.mark.parametrize("schema_field", ["input_schema", "output_schema"])
def test_dangling_local_schema_reference_rejected_at_construction(schema_field):
    """AUD-02: unresolved local references must not reach running executions."""
    with pytest.raises(ConfigurationError):
        definition = AgentDefinition(
            "broken", "SYSTEM", **{schema_field: {"$ref": "#/$defs/missing"}}
        )
        # The contract requires rejection before assembly completes, not in one
        # particular value-object constructor.
        build(RuntimeEngine(store=MemoryRuntimeStore()), definition=definition)


async def test_dangling_output_schema_does_not_strand_running_execution(store, monkeypatch):
    """AUD-02: inject a legacy invalid schema to independently verify runtime containment."""
    from agentloom.configuration import models

    async with RuntimeEngine(store=store) as runtime:
        with monkeypatch.context() as injection:
            injection.setattr(models, "check_schema", lambda schema: None)
            definition = AgentDefinition(
                "broken", "SYSTEM", output_schema={"$ref": "#/$defs/missing"}
            )
            agent = build(runtime, lambda r: ModelResponse("{}"), definition=definition)
        handle = await agent.start(
            "question", run_id="dangling-output", session_id="schema-session"
        )
        try:
            result = await handle.wait(timeout_s=4)
        except Exception:
            show_run_evidence(runtime, handle.run_id)
            raise
        assert result.status == "failed"
        assert result.error["code"] == "INVALID_SCHEMA"
        assert len(agent.model.requests) == 1, "A broken schema is not a correctable model answer"
        assert_quiescent(runtime, result.run_id)
        assert (
            await build(runtime).ainvoke("next", session_id="schema-session")
        ).status == "succeeded"


@pytest.mark.parametrize(
    "schema",
    [
        {"enum": [{"$ref": "https://example.invalid/resource"}]},
        {"const": {"$ref": "https://example.invalid/resource"}},
        {"type": "object", "properties": {"$ref": {"type": "string"}}},
    ],
)
def test_ref_named_property_inside_enum_is_literal_data(schema):
    """AUD-03: the local-reference rule applies to schema keywords, not enum data."""
    assert AgentDefinition("literal", "SYSTEM", input_schema=schema)


@pytest.mark.parametrize("usage", [True, [1], "invalid"])
async def test_malformed_model_usage_does_not_strand_running_execution(store, usage):
    """AUD-04: malformed provider material must fail as a normal model operation."""
    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime, lambda r: ModelResponse("answer", usage=usage))
        handle = await agent.start("question", run_id="malformed-usage", session_id="usage-session")
        try:
            result = await handle.wait(timeout_s=4)
        except Exception:
            show_run_evidence(runtime, handle.run_id)
            raise
        assert result.status == "failed"
        # The explicit mapping is now documented in capabilities.md §1.1.
        assert result.error["code"] == "MODEL_PROTOCOL_ERROR"
        assert result.error["category"] == "protocol"
        assert_quiescent(runtime, result.run_id)
        assert (
            await build(runtime).ainvoke("next", session_id="usage-session")
        ).status == "succeeded"


@pytest.mark.parametrize("command_id", [None, "", " "])
async def test_stop_rejects_missing_or_blank_command_id(store, command_id):
    """AUD-05: invalid identities must be rejected before they mutate admission."""
    async with RuntimeEngine(store=store) as runtime:
        with pytest.raises(RequestError):
            receipt = await runtime.runs.stop("not-yet-started", command_id=command_id)
            print(receipt)
        with store.transaction(runtime.namespace) as tx:
            assert tx.get("admission", "not-yet-started") is None


@pytest.mark.parametrize("input_id", [None, "", " "])
async def test_append_rejects_missing_or_blank_input_id(store, input_id):
    """AUD-05: null must not silently become the global input ID 'None'."""
    started, release = asyncio.Event(), asyncio.Event()

    async def model(request):
        started.set()
        await release.wait()
        return ModelResponse("answer")

    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime, model)
        handle = await agent.start("question")
        await asyncio.wait_for(started.wait(), 4)
        try:
            with pytest.raises(RequestError):
                receipt = await runtime.inputs.append(
                    handle.run_id, input_id=input_id, content="supplement"
                )
                print(receipt)
        finally:
            release.set()
            await handle.wait(timeout_s=4)


@pytest.mark.parametrize("cursor", [None, "0", [], {}])
async def test_event_read_rejects_bad_cursor_with_sdk_error(store, cursor):
    """AUD-06: invalid public pagination input must not leak Python TypeError."""
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime).start("question")
        await handle.wait(timeout_s=4)
        with pytest.raises(RequestError):
            await runtime.events.read(handle.run_id, after_seq=cursor)


@pytest.mark.parametrize("unsupported", ["allowed-tools", "disallowed-tools", "model"])
def test_direct_skill_package_enforces_supported_metadata(tmp_path, unsupported):
    """AUD-07: explicit prepared manifests need the same profile validation."""
    root = tmp_path / "skill"
    root.mkdir()
    text = f"---\nname: audit\ndescription: audit skill\n{unsupported}: forbidden\n---\nBody"
    (root / "SKILL.md").write_text(text)
    runtime = RuntimeEngine(store=MemoryRuntimeStore())
    with pytest.raises(ConfigurationError, match="SKILL_PROFILE_UNSUPPORTED"):
        package = SkillPackage(
            root,
            {"name": "audit", "description": "audit skill", unsupported: "forbidden"},
            {"SKILL.md": hashlib.sha256(text.encode()).hexdigest()},
            "audit",
        )
        build(
            runtime,
            definition=AgentDefinition(
                "a", "SYSTEM", capabilities=(CapabilityBinding("skill", "skill", "audit", "audit"),)
            ),
            capabilities=CapabilityRegistry(
                skills=(SkillRegistration("audit", package, DirectorySkillLoader()),)
            ),
        )


async def test_invalid_custom_tool_output_keeps_raw_evidence(store):
    """AUD-08: output schema failure must not discard an already executed result."""
    calls = []

    def execute(arguments, context):
        calls.append(context.operation_id)
        return {"receipt": "external-side-effect-123", "count": "invalid"}

    def model(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("handled tool failure")
        return ModelResponse(tool_calls=(ToolCall("custom", {}),))

    registration = CapabilityRegistration(
        "custom",
        FunctionToolAdapter(execute),
        (
            CapabilitySpec(
                "custom",
                "custom",
                "audit tool",
                output_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            ),
        ),
    )
    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            model,
            definition=AgentDefinition(
                "a",
                "SYSTEM",
                capabilities=(CapabilityBinding("custom", "tool", "custom", "custom"),),
            ),
            capabilities=CapabilityRegistry(tools=(registration,)),
        )
        result = await (await agent.start("question")).wait(timeout_s=4)
        op = next(
            op
            for op in runtime._load(result.run_id)["operations"].values()
            if op["event_type"] == "capability.invoke"
        )
        assert len(calls) == 1
        assert op["raw"]["status"] == "failed"
        print({"operation_id": op["operation_id"], "raw": op["raw"]})
        assert op["raw"]["result"] == {"receipt": "external-side-effect-123", "count": "invalid"}


def chat_frame(delta=None, finish=None):
    return {"id": "audit", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def sse_response(frames):
    data = "".join(
        "data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n"
        for frame in frames
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=data)


async def test_http_retry_after_is_respected(store):
    """AUD-09: server retry delay is part of the documented retry contract."""
    times = []

    def server(request):
        times.append(time.monotonic())
        if len(times) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "limited"})
        return sse_response([chat_frame({"content": "done"}), chat_frame(finish="stop"), "[DONE]"])

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine(store=store) as runtime,
    ):
        agent = build(
            runtime,
            model=OpenAIChatAdapter(
                model="audit",
                api_key="audit-placeholder",
                context_window_tokens=32768,
                client=client,
            ),
            execution_policy=ExecutionPolicy(
                run_timeout_seconds=10,
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=4)
        assert result.status == "succeeded" and len(times) == 2
        assert times[1] - times[0] >= 0.9, f"retried after only {times[1] - times[0]:.4f}s"


async def test_provider_context_length_error_has_stable_error_code(store):
    """AUD-10: explicit provider error codes must not be collapsed to HTTP status."""

    def server(request):
        return httpx.Response(
            400,
            json={"error": {"type": "invalid_request_error", "code": "context_length_exceeded"}},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine(store=store) as runtime,
    ):
        agent = build(
            runtime,
            model=OpenAIChatAdapter(
                model="audit",
                api_key="audit-placeholder",
                context_window_tokens=32768,
                client=client,
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=4)
        assert result.status == "failed"
        assert result.error["code"] == "MODEL_CONTEXT_LENGTH_EXCEEDED"


async def test_interrupted_model_retains_nontext_partial_material(store):
    """AUD-11: reasoning / incomplete tool calls are audit material, never decisions."""
    executed = []

    def server(request):
        return sse_response(
            [
                chat_frame({"reasoning_content": "PARTIAL_REASONING"}),
                chat_frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "partial-call",
                                "function": {
                                    "name": "custom",
                                    "arguments": '{"marker":"PARTIAL_ARGUMENT',
                                },
                            }
                        ]
                    }
                ),
            ]
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine(store=store) as runtime,
    ):
        agent = build(
            runtime,
            model=OpenAIChatAdapter(
                model="audit",
                api_key="audit-placeholder",
                context_window_tokens=32768,
                client=client,
            ),
            definition=AgentDefinition(
                "a",
                "SYSTEM",
                capabilities=(CapabilityBinding("custom", "tool", "custom", "custom"),),
            ),
            capabilities=CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "custom",
                        FunctionToolAdapter(lambda a, c: executed.append(a)),
                        (CapabilitySpec("custom", "custom", "custom"),),
                    ),
                )
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=4)
        state = runtime._load(result.run_id)
        assert result.status == "failed" and executed == []
        evidence = json.dumps(
            {"operations": state["operations"], "partial_results": state["partial_results"]}
        )
        assert "PARTIAL_REASONING" in evidence and "PARTIAL_ARGUMENT" in evidence


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf"), 2**53, -(2**53), object()]
)
def test_noncanonical_input_rejected(value):
    with pytest.raises(ConfigurationError):
        AgentInput(value)


async def test_json_number_unicode_and_snapshot_immutability(store):
    source = {"numbers": [-(2**53 - 1), 2**53 - 1, 0], "text": "中文🙂\u0000"}
    supplied = AgentInput(source)
    source["numbers"].append(999)
    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse('{"ok":true}'),
            definition=AgentDefinition("json", "SYSTEM", input_schema={}, output_schema={}),
        )
        handle = await agent.start(supplied)
        result = await handle.wait(timeout_s=4)
        assert result.status == "succeeded"
        content = json.loads(
            next(m["content"] for m in agent.model.requests[0].messages if m["role"] == "user")
        )
        assert content["numbers"] == [-(2**53 - 1), 2**53 - 1, 0]
        assert content["text"] == "中文🙂\u0000"
        snapshot = await handle.snapshot()
        snapshot["output"]["value"]["ok"] = False
        assert (await handle.snapshot())["output"]["value"]["ok"] is True


async def test_slow_cleanup_renews_its_lease(store):
    """AUD-12: an individual cleanup operation may outlive one lease interval."""

    class SlowCleanup(LocalEnvironment):
        calls = 0

        def cleanup(self, scope):
            self.calls += 1
            time.sleep(0.4)
            return super().cleanup(scope)

    environment = SlowCleanup()
    async with RuntimeEngine(store=store, environment=environment, lease_seconds=0.25) as runtime:
        agent = build(runtime)
        handle = await agent.start("question")
        result = await handle.wait(timeout_s=4)
        assert result.status == "succeeded"
        terminal_seq = (await handle.snapshot())["terminal_seq"]
        until = time.monotonic() + 1.6
        while time.monotonic() < until:
            snapshot = await handle.snapshot()
            if snapshot["cleanup_state"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        assert (await handle.snapshot())["terminal_seq"] == terminal_seq
        assert len(agent.model.requests) == 1
        assert snapshot["cleanup_state"] == "succeeded", {
            "cleanup_state": snapshot["cleanup_state"],
            "cleanup_calls": environment.calls,
            "job": runtime._load(handle.run_id)["cleanup_job"],
        }


async def test_unowned_expired_execution_is_finalized_without_agent_reconstruction(store):
    """AUD-13: root deadline scanning must also cover a run with no active owner."""
    started = asyncio.Event()

    async def model(request):
        started.set()
        await asyncio.Event().wait()
        return ModelResponse("unreachable")

    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime, model, execution_policy=ExecutionPolicy(run_timeout_seconds=1))
        handle = await agent.start(
            "question", run_id="orphan-deadline", session_id="orphan-session"
        )
        await asyncio.wait_for(started.wait(), 4)
    async with RuntimeEngine(store=store) as runtime:
        handle = await runtime.runs.attach("orphan-deadline")
        try:
            result = await handle.wait(timeout_s=2.2)
        except Exception:
            show_run_evidence(runtime, handle.run_id)
            with pytest.raises(RequestError, match="SESSION_BUSY"):
                await build(runtime).start("next question", session_id="orphan-session")
            raise
        assert result.status == "timed_out" and result.error["code"] == "RUN_TIMEOUT"


async def test_artifact_input_is_explicitly_unimplemented(store):
    """Gap inventory: an existing public field is not an implemented content pipeline."""
    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime)
        with pytest.raises(RequestError, match="ARTIFACT_INPUT_UNSUPPORTED"):
            await agent.start(AgentInput("question", artifacts=({"artifact_id": "prepared"},)))
        assert agent.model.requests == []
