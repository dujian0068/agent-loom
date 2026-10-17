"""All adapters share cumulative usage validation at the model event boundary."""

import pytest
from conftest import assert_finished, binding, tool

from agentloom import (
    AgentDefinition,
    CapabilityRegistry,
    ModelDescriptor,
    ModelResponse,
    ProviderEvent,
    RuntimeEngine,
    ToolCall,
)
from agentloom.configuration.json_values import MAX_JSON_INTEGER
from agentloom.runtime.recovery import validate_recovery


@pytest.mark.parametrize(
    "frames,final,expected,complete",
    [
        ([{"input_tokens": 9}, {"output_tokens": 5}, {}, None], {"input_tokens": 0}, (0, 5), True),
        ([{"input_tokens": 0}], {}, (0, None), False),
        ([{"output_tokens": 0}], None, (None, 0), False),
        ([{"input_tokens": 2, "output_tokens": 3}, {"output_tokens": 4}], {}, (2, 4), True),
    ],
)
async def test_usage_merges_cumulative_fields_and_preserves_unknowns(
    runtime_store, build, frames, final, expected, complete
):
    class Model:
        descriptor = ModelDescriptor("usage-merge", 32768)

        async def stream_once(self, request, context):
            for frame in frames:
                yield ProviderEvent("usage", frame)
            yield ProviderEvent("completed", ModelResponse("done", usage=final).to_dict())

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, None, model=Model()).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "succeeded")
        assert (result.usage.input_tokens, result.usage.output_tokens) == expected
        assert result.usage.token_counts_complete is complete
        validate_recovery(state)


@pytest.mark.parametrize("channel", ["usage", "completed"])
@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("overflow", [False, True])
async def test_each_usage_channel_enforces_same_safe_integer_boundary(
    runtime_store, build, channel, field, overflow
):
    count = MAX_JSON_INTEGER + int(overflow)

    class Model:
        descriptor = ModelDescriptor("usage-boundary", 32768)

        async def stream_once(self, request, context):
            statistics = {"input_tokens": 0, "output_tokens": 0, field: count}
            response = ModelResponse("done").to_dict()
            if channel == "usage":
                yield ProviderEvent("usage", statistics)
            else:
                response["usage"] = statistics
            yield ProviderEvent("completed", response)

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, None, model=Model()).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "failed" if overflow else "succeeded")
        if overflow:
            assert result.error["code"] == "MODEL_PROTOCOL_ERROR"
        else:
            assert getattr(result.usage, field) == count
            assert result.usage.token_counts_complete
        validate_recovery(state)


async def test_new_model_operation_does_not_inherit_prior_usage_events(runtime_store, build):
    class Model:
        descriptor = ModelDescriptor("usage-scope", 32768)

        async def stream_once(self, request, context):
            if any(item["role"] == "tool" for item in request.messages):
                yield ProviderEvent("completed", ModelResponse("done").to_dict())
            else:
                yield ProviderEvent("usage", {"input_tokens": 3, "output_tokens": 2})
                yield ProviderEvent(
                    "completed", ModelResponse(tool_calls=(ToolCall("echo", {}),)).to_dict()
                )

    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(
            runtime,
            None,
            model=Model(),
            definition=AgentDefinition("test", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: "ok"),)),
        ).ainvoke("go")
        state = assert_finished(runtime, result.run_id, "succeeded")
        assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 2)
        assert not result.usage.token_counts_complete
        model_ops = [
            op for op in state["operations"].values() if op["event_type"] == "model.generate"
        ]
        final = next(op for op in model_ops if op["raw"]["result"]["text"] == "done")
        assert final["raw"]["result"]["usage"] is None
        validate_recovery(state)
