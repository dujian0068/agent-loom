"""Public event envelopes preserve legal output without shrinking its JSON budget."""

import pytest

from agentloom import (
    AgentDefinition,
    AgentInput,
    ConfigurationError,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RunEvent,
    RuntimeEngine,
)
from agentloom.configuration.json_documents import MAX_DOCUMENT_DEPTH, canonical_document
from agentloom.configuration.json_values import MAX_JSON_DEPTH


def nested(depth, shape):
    value = "leaf"
    for index in range(depth):
        value = {"child": value} if shape == "mixed" and index % 2 else [value]
    return value


@pytest.mark.parametrize("cls", [ReactAgent, PlanAgent])
@pytest.mark.parametrize("depth", [126, 127, 128])
@pytest.mark.parametrize("shape", ["list", "mixed"])
async def test_completed_output_survives_events_and_reopened_cursor(
    runtime_store, build, cls, depth, shape
):
    value = nested(depth, shape)
    definition = AgentDefinition("deep", "Return JSON.", output_schema={})
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse(canonical_document(value)),
            cls=cls,
            definition=definition,
        )
        handle = await agent.start("go", run_id="event-depth")
        result = await handle.wait(timeout_s=3)
        assert result.status == "succeeded"
        assert canonical_document(result.output.value) == canonical_document(value)
        events = [event async for event in handle.events()]
        terminal = events[-1]
        assert terminal.type == "run.completed"
        assert canonical_document(terminal.payload["output"]["value"]) == canonical_document(value)
        with pytest.raises(TypeError):
            terminal.payload["output"]["value"] = "changed"
        assert [e async for e in handle.events(after_seq=terminal.seq)] == []
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda r: pytest.fail("Terminal replay called a model"),
            cls=cls,
            definition=definition,
        )
        replay = [
            event async for event in agent.astream(run_id="event-depth", after_seq=terminal.seq - 1)
        ]
        assert replay == [terminal]


async def test_live_astream_delivers_terminal_with_maximum_depth(runtime_store, build):
    value = nested(MAX_JSON_DEPTH, "mixed")
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse(canonical_document(value)),
            definition=AgentDefinition("a", "Return JSON.", output_schema={}),
        )
        events = [event async for event in agent.astream("go")]
        assert events[-1].type == "run.completed"
        assert canonical_document(events[-1].payload["output"]["value"]) == canonical_document(
            value
        )
        assert len(agent.model.requests) == 1


def test_event_wrappers_do_not_relax_external_material_limit():
    with pytest.raises(ConfigurationError, match="supported depth of 128"):
        AgentInput(nested(MAX_JSON_DEPTH + 1, "list"))
    value = nested(MAX_JSON_DEPTH, "mixed")
    event = RunEvent("event", "run", 1, "run.completed", {"output": {"value": value}}, 0)
    assert canonical_document(event.payload["output"]["value"]) == canonical_document(value)


def test_internal_event_document_budget_is_still_bounded():
    RunEvent("event", "run", 1, "custom", {"value": nested(MAX_DOCUMENT_DEPTH - 1, "list")}, 0)
    with pytest.raises(ConfigurationError, match="supported depth of 512"):
        RunEvent("event", "run", 1, "custom", {"value": nested(MAX_DOCUMENT_DEPTH, "list")}, 0)
