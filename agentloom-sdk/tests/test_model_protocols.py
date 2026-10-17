import json

import httpx
import pytest
from conftest import binding, tool

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityRegistry,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    ModelRequest,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    RuntimeEngine,
)


class FragmentedSSE(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.data = b"".join(
            ("data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n").encode()
            for frame in frames
        )

    async def __aiter__(self):
        for offset in range(0, len(self.data), 7):
            yield self.data[offset : offset + 7]


def stream(frames):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=FragmentedSSE(frames)
    )


def chat(delta=None, finish=None):
    return {
        "id": "chat-1",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }


async def test_chat_fragmented_calls_usage_and_after_hook_once_per_response(build):
    requests, hook_results = [], []

    def server(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["max_completion_tokens"] > 0 and body["stream"] is True
        assert request.headers["authorization"] == "Bearer test-secret"
        if len(requests) == 1:
            return stream(
                [
                    chat(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "provider-call",
                                    "function": {"name": "ec", "arguments": '{"value":'},
                                }
                            ]
                        }
                    ),
                    chat(
                        {
                            "tool_calls": [
                                {"index": 0, "function": {"name": "ho", "arguments": '"你好"}'}}
                            ]
                        }
                    ),
                    chat(finish="tool_calls"),
                    {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
                    "[DONE]",
                ]
            )
        assert body["messages"][-1]["role"] == "tool"
        assert json.loads(body["messages"][-1]["content"])["result"] == {"value": "你好"}
        assert body["messages"][-2]["tool_calls"][0]["id"] != "provider-call"
        return stream(
            [
                chat({"content": "com"}),
                chat({"content": "plete"}),
                chat(finish="stop"),
                {"choices": [], "usage": {"prompt_tokens": 18, "completion_tokens": 2}},
                "[DONE]",
            ]
        )

    def after(event, context):
        hook_results.append(event.payload["result"])
        return HookDecision()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine() as runtime,
    ):
        model = OpenAIChatAdapter(
            model="fixture", api_key="test-secret", context_window_tokens=32768, client=client
        )
        agent = build(
            runtime,
            None,
            model=model,
            definition=AgentDefinition("a", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: a),)),
            hooks=HookSet(
                (HookBinding("after", "model.generate", "response", "h"),),
                {"h": FunctionHookHandler(after)},
            ),
        )
        result = await agent.ainvoke("question")
        assert result.output.value == "complete" and len(hook_results) == 2
        assert result.usage.input_tokens == 30 and result.usage.output_tokens == 5
        assert result.usage.token_counts_complete
        operations = runtime._load(result.run_id)["operations"].values()
        first = next(
            o
            for o in operations
            if o["event_type"] == "model.generate" and o["purpose_key"] == "model:0"
        )
        call = first["raw"]["result"]["tool_calls"][0]
        assert call["provider_call_id"] == "provider-call" and call["ordinal"] == 0
        assert "test-secret" not in json.dumps(await runtime.events.list(result.run_id))


@pytest.mark.parametrize(
    "frames,code",
    [
        ([chat({"content": "partial"}), chat(finish="stop")], "MODEL_STREAM_INTERRUPTED"),
        (["[DONE]"], "MODEL_PROTOCOL_ERROR"),
        ([chat(finish="stop"), chat({"content": "late"}), "[DONE]"], "MODEL_PROTOCOL_ERROR"),
        (
            [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
            "MODEL_RESPONSE_INCOMPLETE",
        ),
    ],
)
async def test_provider_terminal_certificate_is_required(frames, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: stream(frames))) as client:
        cls = OpenAIResponsesAdapter if code == "MODEL_RESPONSE_INCOMPLETE" else OpenAIChatAdapter
        adapter = cls(model="fixture", api_key="test", context_window_tokens=32768, client=client)
        with pytest.raises(AgentLoomError, match=code):
            _ = [event async for event in adapter.stream_once(ModelRequest((), (), "react"), None)]


@pytest.mark.parametrize("status,retryable", [(401, False), (422, False), (429, True), (503, True)])
async def test_http_transport_classification(status, retryable):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status))
    ) as client:
        model = OpenAIChatAdapter(
            model="fixture", api_key="test", context_window_tokens=32768, client=client
        )
        with pytest.raises(AgentLoomError) as raised:
            _ = [event async for event in model.stream_once(ModelRequest((), (), "react"), None)]
        assert raised.value.retryable_transport is retryable
        assert raised.value.code == f"HTTP_{status}"


async def test_responses_preserves_encrypted_reasoning_phase_and_effective_hooks(build):
    reasoning = {
        "id": "rs_1",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "opaque-continuation",
    }
    first_output = [
        reasoning,
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "status": "completed",
            "content": [{"type": "output_text", "text": "raw", "annotations": []}],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "provider-id",
            "name": "echo",
            "arguments": "{}",
            "status": "completed",
        },
    ]
    seen = []

    def server(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["store"] is False
        if len(seen) == 1:
            output = first_output
        else:
            items = body["input"]
            assert reasoning in items
            commentary = next(i for i in items if i.get("id") == "msg_1")
            assert (
                commentary["phase"] == "commentary"
                and commentary["content"][0]["text"] == "effective"
            )
            assert (
                next(i for i in items if i.get("type") == "function_call_output")["call_id"]
                == "provider-id"
            )
            call = next(i for i in items if i.get("type") == "function_call")
            assert json.loads(call["arguments"]) == {"path": "new.txt"}
            output = [
                {
                    "id": "msg_2",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "done", "annotations": []}],
                }
            ]
        return stream(
            [
                {"type": "response.created", "response": {"id": "resp"}},
                {"type": "response.output_text.delta", "delta": "raw"},
                {
                    "type": "response.completed",
                    "response": {"id": "resp", "status": "completed", "output": output},
                },
            ]
        )

    def after(event, context):
        result = json.loads(json.dumps(event.payload["result"]))
        if result["tool_calls"]:
            result["text"] = "effective"
        return HookDecision(payload_patch={"result": result})

    def before_tool(event, context):
        return HookDecision(payload_patch={"arguments": {"path": "new.txt"}})

    def echo(arguments, context):
        assert arguments == {"path": "new.txt"}
        return {"ok": True}

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(server)) as client,
        RuntimeEngine() as runtime,
    ):
        model = OpenAIResponsesAdapter(
            model="fixture", api_key="test", context_window_tokens=32768, client=client
        )
        agent = build(
            runtime,
            None,
            model=model,
            definition=AgentDefinition("a", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(echo),)),
            hooks=HookSet(
                (
                    HookBinding("after", "model.generate", "response", "h"),
                    HookBinding("rewrite", "capability.invoke", "request", "rewrite"),
                ),
                {"h": FunctionHookHandler(after), "rewrite": FunctionHookHandler(before_tool)},
            ),
        )
        result = await agent.ainvoke("question")
        assert result.output.value == "done" and len(seen) == 2
        state = runtime._load(result.run_id)
        records = state["tasks"][state["root_task_id"]]["context"]["records"]
        assistant = next(r for r in records if r["message"]["role"] == "assistant")
        assert (
            assistant["raw"]["content"] == "raw" and assistant["message"]["content"] == "effective"
        )
        assert assistant["raw"]["tool_calls"][0]["function"]["arguments"] == "{}"


async def test_invalid_json_numbers_in_arguments_remain_feedback_material():
    frames = [
        chat(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call",
                        "function": {"name": "echo", "arguments": '{"bad":NaN}'},
                    }
                ]
            }
        ),
        chat(finish="tool_calls"),
        "[DONE]",
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: stream(frames))) as client:
        adapter = OpenAIChatAdapter(
            model="fixture", api_key="test", context_window_tokens=32768, client=client
        )
        events = [e async for e in adapter.stream_once(ModelRequest((), (), "react"), None)]
        assert events[-1].data["tool_calls"][0]["arguments"] == '{"bad":NaN}'
