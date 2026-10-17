"""Transport teardown cannot erase completion or turn business failure into retry."""

import asyncio
import json
from contextlib import asynccontextmanager
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
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
)
from agentloom.errors import InfrastructureError, LeaseLost
from agentloom.resources import managed_resource


class CloseFailure(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.content = "".join(
            "data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n"
            for frame in frames
        ).encode()

    async def __aiter__(self):
        yield self.content

    async def aclose(self):
        raise httpx.ReadError("close failed")


@pytest.mark.parametrize("adapter", [OpenAIChatAdapter, OpenAIResponsesAdapter])
@pytest.mark.parametrize("completed", [True, False])
async def test_model_close_keeps_complete_result_or_primary_error(
    runtime_store, build, adapter, completed
):
    error = {"code": "context_length_exceeded"}
    if adapter is OpenAIChatAdapter:
        frames = (
            [
                {
                    "choices": [
                        {"index": 0, "delta": {"content": "ANSWER"}, "finish_reason": "stop"}
                    ]
                },
                "[DONE]",
            ]
            if completed
            else [{"error": error}]
        )
    else:
        frames = (
            [
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "ANSWER"},
                                ],
                            }
                        ],
                    },
                }
            ]
            if completed
            else [{"type": "error", **error}]
        )
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=CloseFailure(frames)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        async with RuntimeEngine(store=runtime_store) as runtime:
            model = adapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            result = await build(
                runtime,
                None,
                model=model,
                execution_policy=ExecutionPolicy(
                    retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
                ),
            ).ainvoke("question")
            state = assert_finished(runtime, result.run_id, "succeeded" if completed else "failed")
            op = next(iter(state["operations"].values()))
            assert len(requests) == len(op["attempts"]) == 1
            assert op["attempts"][0]["diagnostics"][0]["phase"] == "resource_close"
            if completed:
                assert result.output.value == op["raw"]["result"]["text"] == "ANSWER"
            else:
                assert result.error["code"] == "MODEL_CONTEXT_LENGTH_EXCEEDED"


class CompletedMcp(McpToolAdapter):
    def __init__(self, is_error):
        super().__init__(url="https://example.invalid/mcp")
        self.calls, self.is_error = 0, is_error

    @asynccontextmanager
    async def _session(self):
        try:
            yield self
        finally:
            raise ExceptionGroup("session exit", [httpx.ReadError("close failed")])

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            nextCursor=None,
            tools=[
                SimpleNamespace(
                    name="work",
                    inputSchema={"type": "object"},
                    outputSchema=None,
                )
            ],
        )

    async def call_tool(self, name, arguments, meta=None):
        self.calls += 1
        return SimpleNamespace(
            isError=self.is_error,
            model_dump=lambda **kw: {
                "structuredContent": {"receipt": "WORK_COMPLETED"},
                "isError": self.is_error,
            },
        )


@pytest.mark.parametrize("is_error", [False, True])
async def test_mcp_close_preserves_known_result_and_never_repeats_call(
    runtime_store, build, is_error
):
    adapter = CompletedMcp(is_error)

    def model(request):
        return (
            ModelResponse("done")
            if any(m["role"] == "tool" for m in request.messages)
            else (ModelResponse(tool_calls=(ToolCall("work", {}),)))
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            model,
            definition=AgentDefinition(
                "test", "SYSTEM", capabilities=(CapabilityBinding("work", "mcp", "work", "work"),)
            ),
            capabilities=CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "work",
                        adapter,
                        (CapabilitySpec("work", "work", "work", kind="mcp"),),
                    ),
                )
            ),
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(
                    enabled=True,
                    max_attempts=2,
                    delay_seconds=0,
                )
            ),
        ).ainvoke("question")
        state = assert_finished(runtime, result.run_id, "succeeded")
        op = next(o for o in state["operations"].values() if o["event_type"] == "capability.invoke")
        assert adapter.calls == len(op["attempts"]) == 1
        assert op["response"]["status"] == ("failed" if is_error else "succeeded")
        assert op["raw"]["execution_fact"] == "completed"
        assert "WORK_COMPLETED" in json.dumps(op["raw"])
        assert op["attempts"][0]["diagnostics"][0]["phase"] == "resource_close"
        if is_error:
            assert op["response"]["error"]["code"] == "MCP_TOOL_ERROR"


@pytest.mark.parametrize("grouped", [False, True])
async def test_resource_close_cannot_swallow_lost_authority(grouped):
    @asynccontextmanager
    async def resource():
        yield None
        error = LeaseLost()
        raise ExceptionGroup("lost", [error]) if grouped else error

    with pytest.raises(InfrastructureError, match="LEASE_LOST"):
        async with managed_resource(resource(), lambda diagnostic: pytest.fail(str(diagnostic))):
            pass


async def test_cancelled_body_survives_close_exception_group():
    diagnostics = []
    original = asyncio.CancelledError()

    @asynccontextmanager
    async def resource():
        try:
            yield None
        finally:
            raise BaseExceptionGroup("closing", [asyncio.CancelledError(), OSError("close")])

    with pytest.raises(asyncio.CancelledError) as caught:
        async with managed_resource(resource(), diagnostics.append):
            raise original
    assert caught.value is original and diagnostics[0]["phase"] == "resource_close"
