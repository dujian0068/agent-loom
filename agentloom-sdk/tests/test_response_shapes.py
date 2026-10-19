"""Malformed Responses events/continuations fail at the protocol boundary."""

import json
from types import SimpleNamespace

import httpx
import pytest

from agentloom import AgentLoomError, OpenAIResponsesAdapter, RuntimeEngine
from agentloom.models.responses import response_inputs


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "response.output_item.added", "item": None},
        {"type": "response.output_item.added"},
        {"type": "response.created"},
        {"type": "response.created", "response": None},
        {"type": "response.created", "response": {"id": 1}},
        {"type": "response.completed", "response": None},
        {"type": "response.completed", "response": {"status": "completed", "output": None}},
        {"type": "response.completed", "response": {"status": "completed"}},
        {"type": "response.completed", "response": {"status": "completed", "output": [None]}},
        {
            "type": "response.completed",
            "response": {"status": "completed", "output": [{"type": "message", "content": None}]},
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text"}]}],
            },
        },
        {"type": "response.output_text.delta", "delta": None},
        {"type": "response.function_call_arguments.delta", "delta": "{}", "output_index": True},
        {"type": "response.failed", "response": None},
        {"type": None},
    ],
)
async def test_invalid_frames_have_protocol_errors(frame):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="data: " + json.dumps(frame) + "\n\n",
            )
        )
    ) as client:
        model = OpenAIResponsesAdapter(
            model="test", api_key="fake", context_window_tokens=32768, client=client
        )
        request = SimpleNamespace(messages=[], tools=[], parameters={}, max_output_tokens=32)
        with pytest.raises(AgentLoomError, match="MODEL_PROTOCOL_ERROR") as caught:
            [frame async for frame in model.stream_once(request, SimpleNamespace(diagnostics=[]))]
        assert caught.value.category == "protocol" and not caught.value.retryable_transport


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"protocol": "openai.responses"},
        {"protocol": "openai.responses", "output": None},
        {"protocol": "openai.responses", "output": [{"type": "function_call"}]},
    ],
)
def test_invalid_provider_state_does_not_leak_python_errors(state):
    with pytest.raises(AgentLoomError, match="MODEL_PROTOCOL_ERROR"):
        response_inputs([{"role": "assistant", "content": "x", "provider_state": state}])


async def test_protocol_error_is_persisted_without_retry(runtime_store, build):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"type":"response.created","response":null}\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        model = OpenAIResponsesAdapter(
            model="test", api_key="fake", context_window_tokens=32768, client=client
        )
        async with RuntimeEngine(store=runtime_store) as runtime:
            result = await build(runtime, None, model=model).ainvoke("go")
            assert result.status == "failed" and result.error["code"] == "MODEL_PROTOCOL_ERROR"
            assert len(calls) == 1
            op = next(iter(runtime._load(result.run_id)["operations"].values()))
            assert op["attempts"][0]["error"]["category"] == "protocol"
