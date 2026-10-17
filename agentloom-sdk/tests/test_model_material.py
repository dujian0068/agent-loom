"""Malformed response containment and attempt-local evidence across protocols."""

import asyncio
import json

import httpx
import pytest
from conftest import assert_finished, binding, tool

from agentloom import (
    AgentDefinition,
    CapabilityRegistry,
    ExecutionPolicy,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
    ProviderEvent,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
)


def sse(frames):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join(
            "data: " + (frame if isinstance(frame, str) else json.dumps(frame)) + "\n\n"
            for frame in frames
        ),
    )


def chat(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


@pytest.mark.parametrize("usage", [False, [], "", {"input_tokens": True}, {"output_tokens": -1}])
async def test_invalid_model_usage_fails_before_durable_result_commit(runtime_store, build, usage):
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(runtime, lambda r: ModelResponse("answer", usage=usage))
        result = await agent.ainvoke("question")
        state = assert_finished(runtime, result.run_id, "failed")
        assert result.error["code"] == "MODEL_PROTOCOL_ERROR"
        assert len(agent.model.requests) == 1
        assert not result.usage.token_counts_complete
        attempt = next(iter(state["operations"].values()))["attempts"][0]
        assert attempt["status"] == "failed"


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": None, "output_tokens": None}])
async def test_missing_usage_remains_unknown_and_does_not_invent_zero(runtime_store, build, usage):
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: ModelResponse("ok", usage=usage)).ainvoke(
            "question"
        )
        assert result.status == "succeeded"
        assert result.usage.input_tokens is None and result.usage.output_tokens is None
        assert not result.usage.token_counts_complete


@pytest.mark.parametrize("adapter", [OpenAIChatAdapter, OpenAIResponsesAdapter])
async def test_http_adapter_rejects_malformed_usage(runtime_store, build, adapter):
    frames = (
        [chat({"content": "answer"}), {"choices": [], "usage": True}, chat(finish="stop"), "[DONE]"]
        if adapter is OpenAIChatAdapter
        else [
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [], "usage": True},
            }
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sse(frames))
    ) as client:
        async with RuntimeEngine(store=runtime_store) as runtime:
            model = adapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            result = await build(runtime, None, model=model).ainvoke("question")
            assert_finished(runtime, result.run_id, "failed")
            assert result.error["code"] == "MODEL_PROTOCOL_ERROR"


def incomplete_frames(adapter, attempt):
    marker = f"attempt-{attempt}"
    if adapter is OpenAIChatAdapter:
        return [
            chat({"reasoning_content": marker + "-reasoning"}),
            chat(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"{marker}-call-{index}",
                            "function": {
                                "name": "echo",
                                "arguments": f'{{"value":"{marker}-{index}',
                            },
                        }
                        for index in (0, 1)
                    ]
                }
            ),
        ]
    frames = [{"type": "response.reasoning_summary_text.delta", "delta": marker + "-reasoning"}]
    for index in (0, 1):
        frames.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {
                        "type": "function_call",
                        "id": f"item-{index}",
                        "call_id": f"{marker}-call-{index}",
                        "name": "echo",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": index,
                    "item_id": f"item-{index}",
                    "delta": f'{{"value":"{marker}-{index}',
                },
            ]
        )
    return frames


@pytest.mark.parametrize("adapter", [OpenAIChatAdapter, OpenAIResponsesAdapter])
async def test_incomplete_calls_stay_with_their_attempt_and_never_execute(
    runtime_store, build, adapter
):
    requests, executed = [], []

    def server(request):
        requests.append(json.loads(request.content))
        return sse(incomplete_frames(adapter, len(requests)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        async with RuntimeEngine(store=runtime_store) as runtime:
            model = adapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            agent = build(
                runtime,
                None,
                model=model,
                definition=AgentDefinition("test", "SYSTEM", capabilities=(binding(),)),
                capabilities=CapabilityRegistry(
                    tools=(tool(lambda args, ctx: executed.append(args)),)
                ),
                execution_policy=ExecutionPolicy(
                    retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
                ),
            )
            result = await agent.ainvoke("question")
            state = assert_finished(runtime, result.run_id, "failed")
            assert len(requests) == 2 and executed == []
            assert "attempt-1" not in json.dumps(requests[1])
            operation = next(iter(state["operations"].values()))
            assert len(state["partial_results"]) == 2
            for index, attempt in enumerate(operation["attempts"], 1):
                partial = attempt["partial"]
                assert partial["attempt_id"] == attempt["attempt_id"]
                assert partial["operation_id"] == operation["operation_id"]
                assert partial["complete"] is False
                assert partial["reasoning_fragments"] == [f"attempt-{index}-reasoning"]
                assert [call["provider_index"] for call in partial["incomplete_calls"]] == [0, 1]
                assert [call["provider_call_id"] for call in partial["incomplete_calls"]] == [
                    f"attempt-{index}-call-0",
                    f"attempt-{index}-call-1",
                ]
                assert partial == state["partial_results"][index - 1]


async def test_stop_retains_partial_reasoning_and_arguments(runtime_store, build):
    entered = asyncio.Event()

    class StreamingModel(ScriptedModel):
        async def stream_once(self, request, context):
            yield ProviderEvent("reasoning_delta", "reasoning-before-stop")
            yield ProviderEvent("tool_arguments_delta", '{"unfinished":', 0, "call")
            entered.set()
            await asyncio.Event().wait()

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(runtime, None, model=StreamingModel(None))
        handle = await agent.start("question")
        await asyncio.wait_for(entered.wait(), 3)
        await handle.stop(command_id="stop")
        result = await handle.wait(timeout_s=3)
        state = assert_finished(runtime, result.run_id, "cancelled")
        assert state["partial_results"][0]["reasoning_fragments"] == ["reasoning-before-stop"]
        assert state["partial_results"][0]["incomplete_calls"][0]["arguments_fragments"] == [
            '{"unfinished":'
        ]


async def test_partial_size_limit_includes_nontext_material(runtime_store, build, monkeypatch):
    from agentloom.models import material

    monkeypatch.setattr(material, "MAX_MODEL_RESPONSE_BYTES", 20)

    class StreamingModel(ScriptedModel):
        async def stream_once(self, request, context):
            yield ProviderEvent("reasoning_delta", "accepted")
            yield ProviderEvent("tool_arguments_delta", "x" * 30, 0, "call")

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, None, model=StreamingModel(None)).ainvoke("question")
        state = assert_finished(runtime, result.run_id, "failed")
        assert result.error["code"] == "MODEL_RESPONSE_TOO_LARGE"
        partial = state["partial_results"][0]
        assert partial["reasoning_fragments"] == ["accepted"] and partial["truncated"]
        assert "x" * 30 not in json.dumps(partial)


@pytest.mark.parametrize("empty", [None, {}, {"prompt_tokens": None, "completion_tokens": None}])
async def test_chat_empty_usage_frame_does_not_erase_known_counts(runtime_store, build, empty):
    frames = [
        chat({"content": "done"}),
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        {**chat(finish="stop"), "usage": empty},
        "[DONE]",
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sse(frames))
    ) as client:
        async with RuntimeEngine(store=runtime_store) as runtime:
            model = OpenAIChatAdapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            result = await build(runtime, None, model=model).ainvoke("question")
            assert result.status == "succeeded" and result.usage.token_counts_complete
            assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 2)


async def test_chat_partial_usage_frames_merge_cumulative_fields(runtime_store, build):
    frames = [
        chat({"content": "done"}),
        {"choices": [], "usage": {"prompt_tokens": 3}},
        {"choices": [], "usage": {"prompt_tokens": None, "completion_tokens": 2}},
        chat(finish="stop"),
        "[DONE]",
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sse(frames))
    ) as client:
        async with RuntimeEngine(store=runtime_store) as runtime:
            model = OpenAIChatAdapter(
                model="test", api_key="test", context_window_tokens=32768, client=client
            )
            result = await build(runtime, None, model=model).ainvoke("question")
            assert result.status == "succeeded" and result.usage.token_counts_complete
            assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 2)


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"type": "error", "code": "context_length_exceeded"}, "MODEL_CONTEXT_LENGTH_EXCEEDED"),
        (
            {"type": "response.failed", "response": {"error": {"code": "context_length_exceeded"}}},
            "MODEL_CONTEXT_LENGTH_EXCEEDED",
        ),
        (
            {"type": "error", "code": "unrelated", "message": "context_length_exceeded"},
            "MODEL_PROVIDER_ERROR",
        ),
    ],
)
async def test_responses_error_shapes_preserve_explicit_provider_code(
    runtime_store, build, event, expected
):
    calls = []

    def server(request):
        calls.append(request)
        return sse([event])

    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        async with RuntimeEngine(store=runtime_store) as runtime:
            model = OpenAIResponsesAdapter(
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
            assert result.status == "failed" and result.error["code"] == expected
            assert len(calls) == 1


@pytest.mark.parametrize("value", [{"receipt": "side-effect"}, None, {"receipt": "凭证🙂"}])
async def test_invalid_tool_output_retains_raw_through_after_hook_and_reopen(
    runtime_store, build, value
):
    executed, observed = [], []

    def execute(arguments, context):
        executed.append(context.operation_id)
        return value

    def model(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("handled")
        return ModelResponse(tool_calls=(ToolCall("echo", {}),))

    def after(event, context):
        observed.append(event.payload)
        return HookDecision()

    from dataclasses import replace

    registered = tool(execute)
    registered = replace(
        registered, members=(replace(registered.members[0], output_schema={"type": "integer"}),)
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            model,
            definition=AgentDefinition("test", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(registered,)),
            hooks=HookSet(
                (HookBinding("after", "capability.invoke", "response", "after"),),
                {"after": FunctionHookHandler(after)},
            ),
        ).ainvoke("question")
        run_id = result.run_id
    async with RuntimeEngine(store=runtime_store) as reopened:
        await (await reopened.runs.attach(run_id)).wait()
        operation = next(
            op
            for op in reopened._load(run_id)["operations"].values()
            if op["event_type"] == "capability.invoke"
        )
        assert len(executed) == 1 and len(observed) == 1
        for material in (operation["raw"], observed[0]):
            assert material["status"] == "failed" and material["result"] == value
            assert material["execution_fact"] == "completed"
            assert material["error"]["code"] == "CAPABILITY_OUTPUT_SCHEMA_INVALID"


@pytest.mark.parametrize("adapter", [OpenAIChatAdapter, OpenAIResponsesAdapter])
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"error": {"code": "context_length_exceeded"}}, "MODEL_CONTEXT_LENGTH_EXCEEDED"),
        ({"error": {"message": "context_length_exceeded"}}, "HTTP_400"),
        ({"error": "context_length_exceeded"}, "HTTP_400"),
    ],
)
async def test_context_length_mapping_uses_explicit_code_not_remote_text(
    runtime_store, build, adapter, body, expected
):
    calls = []

    def server(request):
        calls.append(request)
        return httpx.Response(400, json=body)

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
                    retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0)
                ),
            ).ainvoke("question")
            assert_finished(runtime, result.run_id, "failed")
            assert result.error["code"] == expected and len(calls) == 1
