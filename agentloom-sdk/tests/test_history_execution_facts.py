"""Interrupted history preserves known completion without inventing delivered output."""

import asyncio
import json

from conftest import binding, tool

from agentloom import AgentDefinition, CapabilityRegistry, ModelResponse, RuntimeEngine, ToolCall


async def test_interrupted_batch_distinguishes_completed_unknown_and_unstarted(
    runtime_store, build
):
    entered = asyncio.Event()

    async def wait(arguments, context):
        entered.set()
        await asyncio.Event().wait()

    executed = []
    definition = AgentDefinition(
        "test",
        "SYSTEM",
        capabilities=tuple(binding(name) for name in ("done", "waiting", "unstarted")),
    )
    capabilities = CapabilityRegistry(
        tools=(
            tool(lambda a, c: {"receipt": "COMMITTED_SECRET"}, "done"),
            tool(wait, "waiting"),
            tool(lambda a, c: executed.append(a), "unstarted"),
        )
    )
    async with RuntimeEngine(store=runtime_store) as runtime:
        h = await build(
            runtime,
            lambda r: ModelResponse(
                tool_calls=tuple(ToolCall(name, {}) for name in ("done", "waiting", "unstarted"))
            ),
            definition=definition,
            capabilities=capabilities,
        ).start("first", session_id="history")
        await asyncio.wait_for(entered.wait(), 2)
        await h.stop(command_id="stop")
        assert (await h.wait(timeout_s=2)).status == "cancelled"
        agent = build(
            runtime,
            lambda r: ModelResponse("done"),
            definition=definition,
            capabilities=capabilities,
        )
        assert (await agent.ainvoke("next", session_id="history")).status == "succeeded"
        messages = agent.model.requests[0].messages
        feedback = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert [m["executed"] for m in feedback] == [True, "unknown", False]
        assert [m["status"] for m in feedback] == ["cancelled", "unknown", "cancelled"]
        assert "COMMITTED_SECRET" not in json.dumps(messages) and not executed
