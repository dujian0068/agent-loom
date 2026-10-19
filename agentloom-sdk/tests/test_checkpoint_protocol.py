"""Common envelope, immutable versions, fallback and gap detection on both stores."""

import asyncio

import pytest

from agentloom import AgentDefinition, InfrastructureError, ModelResponse, RuntimeEngine
from agentloom.checkpoints import restore


async def checkpointed_run(store, build):
    class Cut(Exception):
        pass

    def checkpoint(command, state):
        if command.startswith("decision:") and state["tasks"][state["root_task_id"]]["round"] == 2:
            raise Cut()

    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        agent = build(
            runtime,
            lambda r: ModelResponse("bad"),
            definition=AgentDefinition("a", "SYS", output_schema={"type": "object"}),
        )
        handle = await agent.start("hello", run_id="journal-history")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=10)
    return store.load("default", "journal-history")


async def test_graph_snapshot_falls_back_and_replays_versions(runtime_store, build):
    state = await checkpointed_run(runtime_store, build)
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        snapshot = tx.get("graph_checkpoints", head["head_checkpoint_id"])
        assert snapshot["engine_kind"] == "agent" and snapshot["format_version"] == 1
        assert snapshot["graph_execution_id"] == state["graph_id"]
        assert snapshot["previous_checkpoint_id"] is not None
        assert snapshot["parent_execution_ref"] is None
        tx.delete("checkpoint_versions", snapshot["engine_state_ref"]["blob_id"])
    replay = runtime_store.load_run_consistently("default", state["run_id"])
    assert replay["tasks"] == state["tasks"] and replay["operations"] == state["operations"]
    assert replay["lease"] == state["lease"]


@pytest.mark.parametrize("damage", ["missing", "hash", "chain"])
async def test_scope_delta_gap_or_hash_error_cannot_be_skipped(runtime_store, build, damage):
    state = await checkpointed_run(runtime_store, build)
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        snapshot = tx.get("graph_checkpoints", head["head_checkpoint_id"])
        # An older attached snapshot is a valid state while aggregation lags.
        head["head_checkpoint_id"] = snapshot["previous_checkpoint_id"]
        tx.put("graph_heads", state["graph_id"], head)
        assert restore(tx, "default", state)["tasks"] == state["tasks"]
        key = f"{state['graph_id']}:{head['scope_commit_seq']}"
        delta = tx.get("scope_commits", key)
        if damage == "missing":
            tx.delete("scope_commits", key)
        elif damage == "hash":
            delta["after_refs"] = {}
            tx.put("scope_commits", key, delta)
        else:
            from agentloom.configuration.json_documents import document_digest

            delta["previous_hash"] = "broken"
            delta.pop("content_hash")
            delta["content_hash"] = document_digest(delta)
            tx.put("scope_commits", key, delta)
    with pytest.raises(InfrastructureError, match="CHECKPOINT_CORRUPT"):
        runtime_store.load_run_consistently("default", state["run_id"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("engine_kind", "workflow"),
        ("engine_protocol_version", True),
        ("engine_protocol_version", 2),
    ],
)
async def test_unknown_engine_or_typed_version_never_uses_agent_codec(
    runtime_store, build, field, value
):
    state = await checkpointed_run(runtime_store, build)
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        head[field] = value
        tx.put("graph_heads", state["graph_id"], head)
    with pytest.raises(InfrastructureError, match="CHECKPOINT_VERSION_UNSUPPORTED"):
        runtime_store.load_run_consistently("default", state["run_id"])


async def test_record_deletion_removes_owned_journal(runtime_store, build):
    async with RuntimeEngine(store=runtime_store) as runtime:
        result = await build(runtime, lambda r: ModelResponse("done")).ainvoke("hello")
        async with asyncio.timeout(3):
            while runtime._load(result.run_id)["cleanup_state"] != "succeeded":
                await asyncio.sleep(0.01)
        await runtime.records.delete(result.run_id, command_id="delete-with-checkpoints")
        with runtime_store.transaction("default") as tx:
            for kind in (
                "graph_heads",
                "graph_checkpoints",
                "node_checkpoints",
                "scope_commits",
                "checkpoint_versions",
            ):
                assert not any(row["run_id"] == result.run_id for row in tx.list(kind))


async def test_failed_snapshot_cannot_rollback_node_or_model_commits(
    runtime_store, build, monkeypatch
):
    from agentloom.checkpoints.snapshots import SnapshotWriter

    def fail(*args):
        raise InfrastructureError("INJECTED_SNAPSHOT_WRITE_FAILURE")

    monkeypatch.setattr(SnapshotWriter, "_aggregate", fail)
    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(runtime, lambda r: ModelResponse("durable"))
        result = await agent.ainvoke("hello")
        assert result.status == "succeeded" and len(agent.model.requests) == 1
        state = runtime._load(result.run_id)
        with runtime_store.transaction("default") as tx:
            head = tx.get("graph_heads", state["graph_id"])
            assert head["head_checkpoint_id"] is None
            assert head["snapshot_error"]["code"] == "GRAPH_SNAPSHOT_FAILED"
            assert len(tx.list("scope_commits")) > 0 and len(tx.list("node_checkpoints")) > 0
        replay = runtime_store.load_run_consistently("default", result.run_id)
        assert replay["tasks"] == state["tasks"] and replay["output"]["value"] == "durable"


@pytest.mark.parametrize(
    "damage",
    [
        "missing_ref",
        "list_ref",
        "null_scope",
        "missing_scope",
        "null_payload",
        "bad_refs",
        "all_bad",
    ],
)
async def test_snapshot_structure_fallback_uses_only_a_complete_valid_candidate(
    runtime_store, build, damage
):
    from agentloom.checkpoints.journal import _payload

    state = await checkpointed_run(runtime_store, build)
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        latest = tx.get("graph_checkpoints", head["head_checkpoint_id"])
        assert latest["previous_checkpoint_id"] is not None
        snapshots = tx.list("graph_checkpoints") if damage == "all_bad" else [latest]
        for snapshot in snapshots:
            if damage == "missing_ref":
                del snapshot["engine_state_ref"]
            elif damage == "list_ref":
                snapshot["engine_state_ref"] = []
            elif damage == "null_scope":
                snapshot["engine_state_ref"]["content_scope_ref"] = None
            elif damage == "missing_scope":
                del snapshot["engine_state_ref"]["content_scope_ref"]
            elif damage in {"null_payload", "bad_refs"}:
                snapshot["engine_state_ref"] = _payload(
                    tx,
                    "default",
                    state["run_id"],
                    "agent_checkpoint",
                    None
                    if damage == "null_payload"
                    else {"refs": None, "node_refs": {}, "delta_hash": "bad"},
                )
            else:
                snapshot["engine_state_ref"] = None
            tx.put("graph_checkpoints", snapshot["checkpoint_id"], snapshot)
    replay = runtime_store.load_run_consistently("default", state["run_id"])
    assert replay["tasks"] == state["tasks"] and replay["operations"] == state["operations"]
    assert replay["lease"] == state["lease"]


@pytest.mark.parametrize("version", [True, 2])
async def test_corrupt_candidate_with_unknown_protocol_is_not_skipped(
    runtime_store, build, version
):
    state = await checkpointed_run(runtime_store, build)
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        latest = tx.get("graph_checkpoints", head["head_checkpoint_id"])
        latest.update(engine_state_ref=None, engine_protocol_version=version)
        tx.put("graph_checkpoints", latest["checkpoint_id"], latest)
    with pytest.raises(InfrastructureError, match="CHECKPOINT_VERSION_UNSUPPORTED"):
        runtime_store.load_run_consistently("default", state["run_id"])


async def test_snapshot_fallback_cannot_skip_a_missing_delta(runtime_store, build):
    state = await checkpointed_run(runtime_store, build)
    with runtime_store.transaction("default") as tx:
        head = tx.get("graph_heads", state["graph_id"])
        latest = tx.get("graph_checkpoints", head["head_checkpoint_id"])
        latest["engine_state_ref"] = None
        tx.put("graph_checkpoints", latest["checkpoint_id"], latest)
        tx.delete("scope_commits", f"{state['graph_id']}:{head['scope_commit_seq']}")
    with pytest.raises(InfrastructureError, match="CHECKPOINT_CORRUPT"):
        runtime_store.load_run_consistently("default", state["run_id"])
