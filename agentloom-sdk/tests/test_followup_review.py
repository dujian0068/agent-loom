import asyncio
import json
import time
from email.utils import formatdate

import httpx
import pytest

from agentloom import (
    AgentDefinition,
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    LocalEnvironment,
    MemoryRuntimeStore,
    ModelResponse,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    PlanAgent,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
)
from agentloom.models.http import retry_after_deadline


@pytest.fixture(params=("memory", "file"))
def store(request, tmp_path):
    backend = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield backend
    backend.close()


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_revocation_failure_isolated_durable_and_retried_after_restart(store, build, caplog):
    started = asyncio.Event()

    async def blocked(request):
        started.set()
        await asyncio.Event().wait()

    async with RuntimeEngine(store=store) as runtime:
        for run_id in ("a-bad", "b-healthy"):
            started.clear()
            await build(runtime, blocked).start("question", run_id=run_id)
            await started.wait()

    class FlakyEnvironment(LocalEnvironment):
        attempts = []

        def revoke(self, scope):
            if scope["run_id"] == "a-bad":
                self.attempts.append(time.time())
                if len(self.attempts) == 1:
                    raise OSError("private host path must not appear in diagnostics")
            return super().revoke(scope)

    environment = FlakyEnvironment()
    async with RuntimeEngine(
        store=store, environment=environment, cleanup_retry_seconds=0.6
    ) as runtime:
        for run_id in ("a-bad", "b-healthy"):
            await runtime.runs.stop(run_id, command_id="stop:" + run_id)
        await until(lambda: bool(environment.attempts))
        await asyncio.sleep(0.02)
        assert not runtime._cleanup.done(), "One revoke failure terminated maintenance"
        failed = await runtime.runs.get("a-bad")
        assert failed["status"] == "stopping"
        diagnostic = failed["maintenance"]["stop"]
        assert diagnostic["last_error"]["message"] == "OSError"
        assert diagnostic["failures"] == 1
        assert diagnostic["next_retry_at"] > time.time()
        await until(lambda: runtime._load("b-healthy")["cleanup_state"] == "succeeded")
        assert len(environment.attempts) == 1
        assert runtime._load("b-healthy")["status"] == "cancelled"

    async with RuntimeEngine(
        store=store, environment=environment, cleanup_retry_seconds=0.6
    ) as runtime:
        await until(lambda: runtime._load("a-bad")["cleanup_state"] == "succeeded")
        final = await runtime.runs.get("a-bad")
        assert final["status"] == "cancelled"
        assert len(environment.attempts) == 2
        assert environment.attempts[1] >= diagnostic["next_retry_at"]
        assert final["maintenance"]["stop"]["last_error"] == diagnostic["last_error"]
        assert final["maintenance"]["stop"]["recovered_at"] is not None
        events = (await runtime.events.list("a-bad"))["items"]
        assert len([event for event in events if event["type"] == "run.completed"]) == 1
        assert not runtime._workers
        assert "private host path" not in caplog.text


@pytest.mark.parametrize("phase", ["discovery", "candidate"])
async def test_maintenance_scan_and_candidate_failures_do_not_suppress_cleanup(
    store, build, monkeypatch, phase, caplog
):
    from agentloom.runtime import host
    from agentloom.runtime.cleanup import CleanupService

    failures = []
    if phase == "discovery":
        original = host.settle_orphaned_stops

        def fail_scan(runtime):
            if not failures:
                failures.append(time.time())
                raise OSError("private storage details")
            original(runtime)

        monkeypatch.setattr(host, "settle_orphaned_stops", fail_scan)
    else:
        original = CleanupService._clean_run

        def fail_candidate(service, run_id):
            if run_id == "a-bad" and not failures:
                failures.append(time.time())
                raise OSError("private storage details")
            original(service, run_id)

        monkeypatch.setattr(CleanupService, "_clean_run", fail_candidate)

    async with RuntimeEngine(store=store, cleanup_retry_seconds=0.25) as runtime:
        for run_id in ("a-bad", "b-healthy"):
            await build(runtime, lambda r: ModelResponse("done")).ainvoke("question", run_id=run_id)
        await until(lambda: runtime._load("b-healthy")["cleanup_state"] == "succeeded")
        await until(lambda: runtime._load("a-bad")["cleanup_state"] == "succeeded")
        assert not runtime._cleanup.done()
        assert len(failures) == 1
        assert "private storage details" not in caplog.text
        if phase == "candidate":
            diagnostic = (await runtime.runs.get("a-bad"))["maintenance"]["cleanup"]
            assert diagnostic["failures"] == 1
            assert diagnostic["recovered_at"] >= diagnostic["failed_at"] + 0.25


def completed_stream(adapter):
    if adapter is OpenAIResponsesAdapter:
        frames = [
            '{"type":"response.completed","response":{"status":"completed",'
            '"output":[{"type":"message","role":"assistant","content":'
            '[{"type":"output_text","text":"done"}]}]}}'
        ]
    else:
        frames = [
            '{"choices":[{"index":0,"delta":{"content":"done"},"finish_reason":null}]}',
            '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "[DONE]",
        ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join("data: " + frame + "\n\n" for frame in frames),
    )


@pytest.mark.parametrize("adapter", [OpenAIChatAdapter, OpenAIResponsesAdapter])
@pytest.mark.parametrize("kind", ["seconds", "date", "config"])
async def test_retry_after_survives_http_adapter_and_attempt_commit(store, build, adapter, kind):
    calls, scheduled = [], []
    delay = 0.15 if kind == "config" else 0.01

    def server(request):
        calls.append(time.time())
        if len(calls) == 1:
            if kind == "date":
                deadline = int(calls[0]) + 2
                header = formatdate(deadline, usegmt=True)
            else:
                seconds = 1 if kind == "seconds" else 0
                deadline = calls[0] + seconds
                header = str(seconds)
            scheduled.append(max(calls[0] + delay, deadline))
            return httpx.Response(429, headers={"Retry-After": header})
        return completed_stream(adapter)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine(store=store) as runtime,
    ):
        model = adapter(model="fixture", api_key="test", context_window_tokens=32768, client=client)
        agent = build(
            runtime,
            None,
            model=model,
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=delay)
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=4)
        assert result.status == "succeeded"
        assert len(calls) == 2
        assert calls[1] >= scheduled[0], calls
        operation = next(iter(runtime._load(result.run_id)["operations"].values()))
        first = operation["attempts"][0]
        assert first["error"]["retry_after_at"] >= calls[0] - 1
        assert operation["attempts"][1]["started_at"] >= scheduled[0]
        assert result.usage.business_model_attempts == 2
        assert result.usage.loops_consumed == 1


@pytest.mark.parametrize(
    "replacement",
    [
        {"task": ""},
        {"task": " \t\n"},
        {"task": None},
        {"task": 0},
        {"task": {}},
        {"task": []},
        {"variables": {"name": 0}},
        {"variables": "invalid"},
        {"unexpected": True},
    ],
)
async def test_invalid_delegate_patch_preserves_request_context_and_next_hook(
    store, build, replacement
):
    original = {"executor": "worker", "task": "valid task", "variables": {"name": "original"}}
    children, observed = [], []

    def model(request):
        if request.role_key == "worker":
            children.append(request)
            return ModelResponse("child result")
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("delegate_task", original),))

    def patch(event, context):
        system = next(
            record for record in context.reader.messages() if record["message"]["role"] == "system"
        )
        return HookDecision(
            payload_patch={"arguments": {**original, **replacement}},
            context_patch={system["record_id"]: "MUST_BE_DISCARDED"},
        )

    async def inspect_next(event, context):
        observed.append(await context.reader.read_payload())
        assert context.reader.messages()[0]["message"]["content"] == "SYSTEM"
        return HookDecision()

    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            model,
            cls=PlanAgent,
            executors={
                "worker": AgentDefinition("worker", "{{name}}", "worker", variables={"name": ""})
            },
            hooks=HookSet(
                (
                    HookBinding("patch", "executor.delegate", "request", "patch"),
                    HookBinding("inspect", "executor.delegate", "request", "inspect", ordinal=1),
                ),
                {
                    "patch": FunctionHookHandler(patch),
                    "inspect": FunctionHookHandler(inspect_next),
                },
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=3)
        assert result.status == "succeeded"
        assert len(children) == len(observed) == 1
        assert observed[0]["arguments"] == original
        assert children[0].messages[0]["content"] == "original"
        assert children[0].messages[-1]["content"] == "valid task"
        state = runtime._load(result.run_id)
        op = next(o for o in state["operations"].values() if o["event_type"] == "executor.delegate")
        assert op["request"]["arguments"] == original
        assert op["hooks"]["request:patch"]["diagnostic"] is not None
        assert op["hooks"]["request:inspect"]["diagnostic"] is None
        calls = next(
            m["tool_calls"] for m in agent.model.requests[-1].messages if m.get("tool_calls")
        )
        assert json.loads(calls[0]["function"]["arguments"]) == original


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_delegate_patch_respects_explicit_schema_and_deny(store, build, decision):
    children = []

    def model(request):
        if request.role_key == "worker":
            children.append(request)
            return ModelResponse("child result")
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        return ModelResponse(
            tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "valid"}),)
        )

    async with RuntimeEngine(store=store) as runtime:
        agent = build(
            runtime,
            model,
            cls=PlanAgent,
            executors={
                "worker": AgentDefinition(
                    "worker", "SYSTEM", "worker", input_schema={} if decision == "allow" else None
                )
            },
            hooks=HookSet(
                (HookBinding("patch", "executor.delegate", "request", "patch"),),
                {
                    "patch": FunctionHookHandler(
                        lambda e, c: HookDecision(
                            decision,
                            payload_patch={"arguments": {"executor": "worker", "task": None}},
                        )
                    )
                },
            ),
        )
        result = await (await agent.start("question")).wait(timeout_s=3)
        assert result.status == "succeeded"
        op = next(
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "executor.delegate"
        )
        if decision == "allow":
            assert len(children) == 1 and children[0].messages[-1]["content"] == "null"
            assert op["hooks"]["request:patch"]["diagnostic"] is None
        else:
            assert not children
            assert op["request"]["arguments"]["task"] == "valid"
            assert op["response"]["status"] == "blocked"
            assert op["hooks"]["request:patch"]["diagnostic"] is not None


@pytest.mark.parametrize("header", [None, "", "nonsense", "-1", "NaN", "inf", "1e309"])
def test_invalid_retry_after_is_ignored(header):
    assert retry_after_deadline(header) is None


def test_retry_after_accepts_seconds_and_legacy_http_dates():
    now = time.time()
    assert now <= retry_after_deadline("0.25") <= time.time() + 0.25
    for date in (
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ):
        assert retry_after_deadline(date) == 784111777


async def test_persisted_retry_deadline_is_reused_after_restart(store, build):
    saved = asyncio.Event()
    calls = []
    hooks_called = []
    policy = ExecutionPolicy(retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0))

    def hook(event, context):
        hooks_called.append(event.stage)
        return HookDecision()

    hooks = HookSet(
        (
            HookBinding("before", "model.generate", "request", "hook"),
            HookBinding("after", "model.generate", "response", "hook"),
        ),
        {"hook": FunctionHookHandler(hook)},
    )

    def checkpoint(command, state):
        if command.startswith("attempt-result:"):
            saved.set()
            raise asyncio.CancelledError

    def server(request):
        calls.append(time.time())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return completed_stream(OpenAIChatAdapter)

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        model = OpenAIChatAdapter(
            model="fixture", api_key="test", context_window_tokens=32768, client=client
        )
        async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as first:
            await build(first, None, model=model, execution_policy=policy, hooks=hooks).start(
                "question", run_id="retry-recovery"
            )
            await asyncio.wait_for(saved.wait(), 2)
        pending = next(iter(store.load("default", "retry-recovery")["operations"].values()))
        deadline = pending["next_attempt_at"]
        assert deadline == pending["attempts"][0]["next_attempt_at"]
        assert deadline >= calls[0] + 1
        await asyncio.sleep(0.4)
        async with RuntimeEngine(store=store) as second:
            handle = await build(
                second, None, model=model, execution_policy=policy, hooks=hooks
            ).start(run_id="retry-recovery")
            result = await handle.wait(timeout_s=3)
            assert result.status == "succeeded"
            assert len(calls) == 2
            assert deadline <= calls[1] < deadline + 0.3
            final = next(iter(second._load(handle.run_id)["operations"].values()))
            assert final["operation_id"] == pending["operation_id"]
            assert final["attempts"][0] == pending["attempts"][0]
            assert hooks_called == ["request", "response"]


@pytest.mark.parametrize("control", ["stop", "deadline"])
async def test_retry_after_wait_does_not_override_root_control(store, build, control):
    waiting = asyncio.Event()
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    def checkpoint(command, state):
        if command.startswith("attempt-result:"):
            waiting.set()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime,
    ):
        model = OpenAIChatAdapter(
            model="fixture", api_key="test", context_window_tokens=32768, client=client
        )
        handle = await build(
            runtime,
            None,
            model=model,
            execution_policy=ExecutionPolicy(
                run_timeout_seconds=1,
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
            ),
        ).start("question")
        await asyncio.wait_for(waiting.wait(), 2)
        if control == "stop":
            await handle.stop(command_id="stop-while-rate-limited")
        result = await handle.wait(timeout_s=3)
        assert result.status == ("cancelled" if control == "stop" else "timed_out")
        assert len(requests) == result.usage.business_model_attempts == 1
