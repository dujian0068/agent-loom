"""Reject invalid keys before mutation and validate event cursors before reads."""

from uuid import uuid4

import pytest

from agentloom import ModelResponse, RequestError, RuntimeEngine


@pytest.mark.parametrize("invalid", [None, "", " \t\n", 0, False, [], {}])
async def test_invalid_control_identity_is_side_effect_free(runtime_store, build, invalid):
    async with RuntimeEngine(store=runtime_store) as runtime:
        handle = await build(runtime, lambda r: ModelResponse("ok")).start("question")
        await handle.wait()
        for action in (runtime.runs.stop, runtime.records.delete):
            with pytest.raises(RequestError, match="IDENTITY_INVALID"):
                await action(handle.run_id, command_id=invalid)
        with pytest.raises(RequestError, match="IDENTITY_INVALID"):
            await runtime.inputs.append(handle.run_id, input_id=invalid, content="more")
        with runtime_store.transaction(runtime.namespace) as tx:
            assert tx.list("commands") == []
            assert tx.get("admission", handle.run_id) is None
            assert len(tx.list("inputs")) == 1


async def test_uuid_and_explicit_string_keys_keep_their_identity(runtime_store):
    key, run_id = uuid4(), uuid4()
    async with RuntimeEngine(store=runtime_store) as runtime:
        first = await runtime.runs.stop(run_id, command_id=key)
        again = await runtime.runs.stop(str(run_id), command_id=str(key))
        assert first == again and first["command_id"] == str(key)
        other = await runtime.runs.stop("custom-key", command_id="command:key")
        assert other["run_id"] == "custom-key"


@pytest.mark.parametrize(
    "cursor,limit", [(None, 1), ("0", 1), (False, 1), (-1, 1), (0, 0), (0, True)]
)
async def test_bad_event_pagination_is_rejected_before_lookup(runtime_store, cursor, limit):
    async with RuntimeEngine(store=runtime_store) as runtime:
        with pytest.raises(RequestError, match="INVALID_CURSOR"):
            await runtime.events.read("does-not-exist", after_seq=cursor, limit=limit)
