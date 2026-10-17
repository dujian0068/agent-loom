"""MCP HTTP failures share persisted retry deadlines with the model call driver."""

import asyncio
import time
from contextlib import asynccontextmanager
from email.utils import formatdate
from types import SimpleNamespace

import httpx
import pytest
from conftest import assert_finished

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    McpToolAdapter,
    ModelResponse,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
)


class LimitedMcp(McpToolAdapter):
    """Inject an HTTPStatusError at the transport seam; retain production invoke."""

    def __init__(self, header):
        super().__init__(url="https://example.invalid/mcp")
        self.header = header
        self.calls = []

    @asynccontextmanager
    async def _session(self):
        yield self

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            nextCursor=None,
            tools=[
                SimpleNamespace(name="limited", inputSchema={"type": "object"}, outputSchema=None)
            ],
        )

    async def call_tool(self, name, arguments, meta=None):
        self.calls.append(time.time())
        if len(self.calls) == 1:
            header = self.header(self.calls[0]) if callable(self.header) else self.header
            response = httpx.Response(
                429, headers={"Retry-After": header}, request=httpx.Request("POST", self.url)
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # The actual AnyIO session can also wrap this on context exit.
                raise ExceptionGroup("transport task group", [exc]) from exc
        return SimpleNamespace(
            isError=False, model_dump=lambda **kwargs: {"structuredContent": {"ok": True}}
        )


def make_agent(build, runtime, adapter, *, timeout=10, delay=0):
    def model(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("limited", {}),))

    return build(
        runtime,
        model,
        definition=AgentDefinition(
            "test",
            "SYSTEM",
            capabilities=(CapabilityBinding("limited", "mcp", "limited", "limited"),),
        ),
        capabilities=CapabilityRegistry(
            tools=(
                CapabilityRegistration(
                    "limited",
                    adapter,
                    (CapabilitySpec("limited", "limited", "limited", kind="mcp"),),
                ),
            )
        ),
        execution_policy=ExecutionPolicy(
            run_timeout_seconds=timeout,
            retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=delay),
        ),
    )


def operation(runtime, run_id):
    return next(
        (
            op
            for op in runtime._load(run_id)["operations"].values()
            if op["event_type"] == "capability.invoke"
        ),
        None,
    )


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("kind", ["seconds", "date", "config"])
async def test_mcp_retry_after_seconds_date_and_config_are_persisted(runtime_store, build, kind):
    adapter = LimitedMcp(
        (lambda now: formatdate(int(now) + 1, usegmt=True))
        if kind == "date"
        else "0"
        if kind == "config"
        else "0.2"
    )
    delay = 0.15 if kind == "config" else 0
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await make_agent(build, runtime, adapter, delay=delay).ainvoke("question")
        assert_finished(runtime, result.run_id, "succeeded")
        first, second = operation(runtime, result.run_id)["attempts"]
        expected = max(first["ended_at"] + delay, first["error"]["retry_after_at"])
        assert first["next_attempt_at"] >= expected
        assert second["started_at"] >= first["next_attempt_at"]
        assert adapter.calls[1] >= first["next_attempt_at"]
        if kind == "seconds":
            assert first["error"]["retry_after_at"] >= adapter.calls[0] + 0.2
        assert len(adapter.calls) == 2


async def test_mcp_retry_deadline_survives_runtime_restart(runtime_store, build):
    adapter = LimitedMcp("0.5")
    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await make_agent(build, runtime, adapter).start("question", run_id="retry")
        await until(
            lambda: (
                operation(runtime, handle.run_id)
                and operation(runtime, handle.run_id)["next_attempt_at"]
            )
        )
        before = operation(runtime, handle.run_id)
        due = before["next_attempt_at"]
    async with RuntimeEngine(store=runtime_store) as restarted:
        result = await make_agent(build, restarted, adapter).ainvoke(None, run_id="retry")
        assert_finished(restarted, result.run_id, "succeeded")
        after = operation(restarted, result.run_id)
        assert before["operation_id"] == after["operation_id"]
        assert after["attempts"][0]["next_attempt_at"] == due
        assert due <= adapter.calls[1] < due + 0.4
        assert len(after["attempts"]) == len(adapter.calls) == 2


@pytest.mark.parametrize("control", ["stop", "deadline"])
async def test_mcp_retry_wait_obeys_stop_and_root_deadline(runtime_store, build, control):
    adapter = LimitedMcp("2")
    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await make_agent(build, runtime, adapter, timeout=1).start("question")
        await until(
            lambda: (
                operation(runtime, handle.run_id)
                and operation(runtime, handle.run_id)["next_attempt_at"]
            )
        )
        if control == "stop":
            await handle.stop(command_id="stop")
        result = await handle.wait(timeout_s=3)
        assert_finished(runtime, result.run_id, "cancelled" if control == "stop" else "timed_out")
        assert len(adapter.calls) == 1
