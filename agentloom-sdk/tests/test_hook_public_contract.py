"""Documented Hook inputs and pinned immutable reads across commit boundaries."""

import json
from dataclasses import asdict

import pytest

from agentloom import (
    AgentLoomError,
    FunctionHookHandler,
    HookBinding,
    HookContext,
    HookDecision,
    HookInput,
    HookReadPort,
    HookRecordPage,
    HookRecordView,
    HookSet,
    ModelResponse,
    RuntimeEngine,
)


async def test_documented_hook_denies_before_external_call(runtime_store, build):
    seen = []

    async def hook(event, context):
        assert isinstance(event, HookInput) and isinstance(context.metadata, HookContext)
        assert set(asdict(event)) == {"event_type", "stage", "payload_ref", "context_refs"}
        assert event.payload_ref["value"] == await context.reader.read_payload()
        page = await context.reader.read_context(event.context_refs[0], limit=1)
        assert isinstance(page, HookRecordPage) and isinstance(page.records[0], HookRecordView)
        assert page.records[0].role == "system"
        seen.append((event, context, page))
        return HookDecision("deny")

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda r: pytest.fail("denied model was called"),
            hooks=HookSet(
                (HookBinding("check", "model.generate", "request", "h"),),
                {"h": FunctionHookHandler(hook)},
            ),
        )
        result = await agent.ainvoke("go")
        assert result.status == "failed" and len(seen) == 1
        event, context, page = seen[0]
        state = runtime._load(result.run_id)
        saved = state["operations"][event.operation_id]["hooks"]["request:check"]
        assert saved["metadata"] == json.loads(json.dumps(context.metadata))
        assert context.metadata.input_context_refs == event.context_refs
        for mutation in [
            lambda: page.records[0].effective_payload.__setitem__("content", "bad"),
            lambda: event.payload_ref.__setitem__("kind", "blob"),
            lambda: context.metadata.__setitem__("role_key", "worker"),
        ]:
            with pytest.raises(TypeError):
                mutation()
        with pytest.raises(AgentLoomError, match="HOOK_READ_PORT_EXPIRED"):
            await context.reader.read_payload()
        with pytest.raises(AgentLoomError, match="HOOK_READ_PORT_EXPIRED"):
            await context.reader.read_context(event.context_refs[0])


async def test_hook_chain_reads_committed_revision_and_sequence_pages(runtime_store, build):
    snapshots = []

    async def hook(event, context):
        ref = event.context_refs[0]
        records, cursor = [], 0
        while True:
            page = await context.reader.read_context(ref, after_seq=cursor, limit=1)
            records.extend(page.records)
            if page.next_seq is None:
                break
            cursor = page.next_seq
        assert [r.context_seq for r in records] == list(range(1, len(records) + 1))
        assert all(set(r.effective_payload) == set(r.visible_fields) for r in records)
        with pytest.raises(AgentLoomError, match="HOOK_CONTEXT_FORBIDDEN"):
            await context.reader.read_context({**ref, "revision": ref["revision"] + 1})
        with pytest.raises(AgentLoomError, match="HOOK_CONTEXT_FORBIDDEN"):
            await context.reader.read_context({**ref, "context_id": "other-task"})
        snapshots.append((ref, records))
        return (
            HookDecision(context_patch={records[0].record_id: "CHANGED"})
            if len(snapshots) == 1
            else HookDecision()
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse("done"),
            hooks=HookSet(
                (
                    HookBinding("one", "model.generate", "request", "h"),
                    HookBinding("two", "model.generate", "request", "h", ordinal=1),
                ),
                {"h": FunctionHookHandler(hook)},
            ),
        )
        assert (await agent.ainvoke("go")).status == "succeeded"
        assert snapshots[0][1][0].effective_payload["content"] == "SYSTEM"
        assert snapshots[1][1][0].effective_payload["content"] == "CHANGED"
        assert snapshots[1][0]["revision"] > snapshots[0][0]["revision"]
        assert agent.model.requests[0].messages[0]["content"] == "CHANGED"


@pytest.mark.parametrize("cursor,limit", [(True, 1), (-1, 1), (0, True), (0, 0), (0, 257)])
async def test_hook_read_rejects_invalid_cursors(cursor, limit):
    port = HookReadPort([], {}, "context", 1)
    with pytest.raises(AgentLoomError, match="INVALID_CURSOR"):
        await port.read_context(port.ref, after_seq=cursor, limit=limit)


async def test_hook_record_projection_excludes_raw_and_protocol_state():
    port = HookReadPort(
        [
            {
                "record_id": "r",
                "kind": "message",
                "protected": False,
                "raw": {"secret": "RAW"},
                "message": {
                    "role": "assistant",
                    "content": "effective",
                    "provider_state": {"secret": "PROTOCOL"},
                },
            }
        ],
        {"effective": "payload"},
        "context",
        1,
    )
    page = await port.read_context(port.ref)
    assert page.records[0].context_seq == 1  # Old checkpoints without stored sequences.
    assert "RAW" not in json.dumps(page) and "PROTOCOL" not in json.dumps(page)
    with pytest.raises(AgentLoomError, match="HOOK_CONTEXT_FORBIDDEN"):
        await port.read_context({"context_id": "context", "revision": True})
