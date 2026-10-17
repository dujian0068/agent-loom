"""Independent follow-up contract probes for revision 220ebd4.

Run explicitly: .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_rereview.py
Assertions describe required behavior; confirmed defects remain ordinary failures.
"""

import asyncio
import hashlib
import json
import time

import httpx
import pytest

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ConfigurationError,
    DirectorySkillLoader,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionToolAdapter,
    LocalEnvironment,
    MemoryRuntimeStore,
    ModelResponse,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
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


def build(runtime, responder=None, **kwargs):
    kwargs.setdefault("definition", AgentDefinition("review", "SYSTEM"))
    kwargs.setdefault("model", ScriptedModel(responder or (lambda r: ModelResponse("done"))))
    kwargs.setdefault("workspace", WorkspaceBinding("workspace"))
    kwargs.setdefault("execution_policy", ExecutionPolicy(run_timeout_seconds=10))
    return ReactAgent(runtime_engine=runtime, **kwargs)


def evidence(runtime, run_id, **extra):
    state = runtime._load(run_id)
    print(
        json.dumps(
            {
                "status": state["status"],
                "terminal_seq": state["terminal_seq"],
                "usage": state["usage"],
                "maintenance": state.get("maintenance", {}),
                **extra,
            },
            sort_keys=True,
        )
    )
    return state


def sse(frames):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(
            "data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n"
            for frame in frames
        ),
    )


@pytest.mark.parametrize(
    "empty_usage",
    [None, {}, {"prompt_tokens": None, "completion_tokens": None}],
    ids=["null", "empty-object", "null-counters"],
)
async def test_empty_usage_after_known_usage_preserves_counts(store, empty_usage):
    frames = [
        {"choices": [{"index": 0, "delta": {"content": "answer"}}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": empty_usage},
        "[DONE]",
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sse(frames))
    ) as client:
        async with RuntimeEngine(store=store) as runtime:
            model = OpenAIChatAdapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            result = await build(runtime, model=model).ainvoke("question")
            evidence(runtime, result.run_id)
            assert result.status == "succeeded"
            assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 2)
            assert result.usage.token_counts_complete


@pytest.mark.parametrize("control", ["stop", "deadline"])
@pytest.mark.parametrize("revoke_delay", [0.02, 0.35], ids=["fast-revoke", "slow-revoke"])
async def test_orphan_finalizes_when_revocation_exceeds_lease(store, control, revoke_delay):
    entered = asyncio.Event()

    async def blocking_model(request):
        entered.set()
        await asyncio.Event().wait()

    async with RuntimeEngine(store=store) as initial:
        await build(
            initial,
            blocking_model,
            execution_policy=ExecutionPolicy(run_timeout_seconds=1),
        ).start("question", run_id="slow-revoke", session_id="slow-revoke-session")
        await asyncio.wait_for(entered.wait(), 2)

    class SlowRevoke(LocalEnvironment):
        calls = 0

        def revoke(self, scope):
            self.calls += 1
            time.sleep(revoke_delay)
            return super().revoke(scope)

    environment = SlowRevoke()
    async with RuntimeEngine(
        store=store,
        environment=environment,
        lease_seconds=0.15,
        cleanup_retry_seconds=0.05,
    ) as runtime:
        if control == "stop":
            await runtime.runs.stop("slow-revoke", command_id="stop")
        try:
            result = await (await runtime.runs.attach("slow-revoke")).wait(timeout_s=2.4)
        except Exception:
            evidence(runtime, "slow-revoke", revoke_calls=environment.calls)
            with pytest.raises(RequestError, match="SESSION_BUSY"):
                await build(runtime).start("next", session_id="slow-revoke-session")
            raise
        assert result.status == ("cancelled" if control == "stop" else "timed_out")
        assert (
            await build(runtime).ainvoke("next", session_id="slow-revoke-session")
        ).status == "succeeded"


@pytest.mark.parametrize("overflow", [False, True], ids=["max-safe-total", "overflow-total"])
async def test_usage_aggregation_at_canonical_integer_boundary_does_not_strand_run(store, overflow):
    safe_max = 2**53 - 1

    def model(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done", usage={"input_tokens": 1, "output_tokens": 1})
        return ModelResponse(
            tool_calls=(ToolCall("echo", {}),),
            usage={"input_tokens": safe_max if overflow else safe_max - 1, "output_tokens": 0},
        )

    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            model,
            definition=AgentDefinition(
                "review",
                "SYSTEM",
                capabilities=(CapabilityBinding("echo", "tool", "echo", "echo", "echo"),),
            ),
            capabilities=CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "echo",
                        FunctionToolAdapter(lambda args, ctx: {"ok": True}),
                        (CapabilitySpec("echo", "echo", "echo"),),
                    ),
                ),
            ),
        )
        handle = await agent.start("question", session_id="overflow-session")
        try:
            result = await handle.wait(timeout_s=3)
        except Exception:
            evidence(runtime, handle.run_id)
            with pytest.raises(RequestError, match="SESSION_BUSY"):
                await build(runtime).start("next", session_id="overflow-session")
            raise
        assert result.status in {"succeeded", "failed"}
        if not overflow:
            assert result.status == "succeeded"
            assert result.usage.input_tokens == safe_max
        state = evidence(runtime, handle.run_id)
        assert state["terminal_seq"] is not None
        assert state["usage"]["loops_reserved"] == 0
        assert all(op["status"] != "pending" for op in state["operations"].values())
        assert (
            await build(runtime).ainvoke("next", session_id="overflow-session")
        ).status == "succeeded"


@pytest.mark.parametrize("kind", ["response.failed", "error"])
async def test_responses_explicit_context_error_mapping_in_both_event_shapes(store, kind):
    error = {"code": "context_length_exceeded", "message": "Request exceeds context window"}
    frames = [
        {
            "type": "response.failed",
            "response": {"id": "response-1", "status": "failed", "error": error},
        }
        if kind == "response.failed"
        else {"type": "error", **error, "param": None, "sequence_number": 1}
    ]
    calls = []

    def server(request):
        calls.append(request)
        return sse(frames)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        async with RuntimeEngine(store=store) as runtime:
            model = OpenAIResponsesAdapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            result = await build(
                runtime,
                model=model,
                execution_policy=ExecutionPolicy(
                    run_timeout_seconds=10,
                    retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
                ),
            ).ainvoke("question")
            evidence(runtime, result.run_id, error=dict(result.error), requests=len(calls))
            assert result.status == "failed" and len(calls) == 1
            assert result.error["code"] == "MODEL_CONTEXT_LENGTH_EXCEEDED"
            assert result.error["category"] == "validation"


@pytest.mark.parametrize("body", ["Dynamic !`echo example`", "Session ${CLAUDE_SESSION_ID}"])
def test_prepared_skill_rejects_unsupported_body_like_directory_import(tmp_path, body):
    metadata = {"name": "test", "description": "test skill"}
    text = "---\nname: test\ndescription: test skill\n---\n" + body
    (tmp_path / "SKILL.md").write_text(text, encoding="utf-8")
    manifest = {"SKILL.md": hashlib.sha256(text.encode()).hexdigest()}
    with pytest.raises(ConfigurationError, match="SKILL_PROFILE_UNSUPPORTED"):
        SkillPackage.from_directory(tmp_path)

    # Prepared-package construction, Agent assembly and first read are all offered
    # as validation boundaries. None may silently accept unsupported semantics.
    with pytest.raises(AgentLoomError, match="SKILL_PROFILE_UNSUPPORTED"):
        package = SkillPackage(tmp_path, metadata, manifest, "test")
        loader = DirectorySkillLoader()
        build(
            RuntimeEngine(store=MemoryRuntimeStore()),
            definition=AgentDefinition(
                "review",
                "SYSTEM",
                capabilities=(CapabilityBinding("skill", "skill", "test", "test"),),
            ),
            capabilities=CapabilityRegistry(skills=(SkillRegistration("test", package, loader),)),
        )
        print(loader.read_entry(package)["text"])
