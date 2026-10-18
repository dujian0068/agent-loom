"""Immutable graph/node versions and gap-checked replay on the shared store.

The run document is a query projection. Recovery reads a graph snapshot and its
ordered scope deltas, then checks the projection before installing that state.
Control and leases are always read from the current run, never from a snapshot.
"""

import json
from uuid import NAMESPACE_URL, uuid5

from ..configuration.json_documents import canonical_document, copy_document, document_digest
from ..errors import InfrastructureError

FORMAT_VERSION = 1
AGENT_PROTOCOL_VERSION = 1
SNAPSHOT_INTERVAL = 16


def graph_id(run_id):
    return str(uuid5(NAMESPACE_URL, run_id + ":agent-graph"))


def _require(condition):
    if not condition:
        raise InfrastructureError("CHECKPOINT_CORRUPT")


def _facts(state):
    facts = {"graph": {"root_task_id": state["root_task_id"], **state.get("graph", {})}}
    for task_id, task in state["tasks"].items():
        facts["task:" + task_id] = {k: v for k, v in task.items() if k != "context"}
        facts["context:" + task_id] = task["context"]
    facts.update({"operation:" + k: v for k, v in state["operations"].items()})
    facts.update({"batch:" + k: v for k, v in state.get("batches", {}).items()})
    facts.update({"activation:" + k: v for k, v in state.get("activations", {}).items()})
    facts.update({"result:" + k: v for k, v in state.get("task_results", {}).items()})
    return facts


def _put_version(tx, run_id, name, value):
    envelope = {"run_id": run_id, "name": name, "value": value}
    ref = document_digest(envelope)
    tx.put("checkpoint_versions", ref, envelope)
    return ref


def _read_version(tx, run_id, name, ref):
    payload_ref = ref if isinstance(ref, dict) else None
    if payload_ref is not None:
        _require(
            payload_ref["kind"] == "blob"
            and payload_ref["content_scope_ref"]["scope_key"] == run_id
            and payload_ref["sha256"] == payload_ref["blob_id"]
        )
        ref = payload_ref["blob_id"]
    version = tx.get("checkpoint_versions", ref)
    _require(version is not None and document_digest(version) == ref)
    _require(version["run_id"] == run_id and version["name"] == name)
    if payload_ref is not None:
        _require(len(canonical_document(version).encode("utf-8")) == payload_ref["size_bytes"])
    return version["value"]


def _payload(tx, namespace, run_id, name, value):
    ref = _put_version(tx, run_id, name, value)
    return {
        "kind": "blob",
        "content_scope_ref": {"namespace": namespace, "scope_key": run_id},
        "blob_id": ref,
        "sha256": ref,
        "size_bytes": len(
            canonical_document({"run_id": run_id, "name": name, "value": value}).encode("utf-8")
        ),
        "media_type": "application/json",
    }


def publish(tx, namespace, state, command_id, *, cache=None):
    """Publish scope delta, node checkpoints and receipt watermarks atomically."""
    if state.get("root_task_id") is None:
        return None
    run_id = state["run_id"]
    scope_id = state["graph_id"]
    head = tx.get("graph_heads", scope_id)
    old_refs = head["refs"] if head else {}
    # Node revisions cover continuation AND committed context changes, including
    # Hook edits made while the node is waiting for an operation.
    for task_id, task in state["tasks"].items():
        if "continuation" not in task:
            continue
        old_ref = old_refs.get("task:" + task_id)
        if old_ref:
            before = _read_version(tx, run_id, "task:" + task_id, old_ref)
            after = {k: v for k, v in task.items() if k != "context"}
            context_hash = document_digest(
                {"run_id": run_id, "name": "context:" + task_id, "value": task["context"]}
            )
            changed = (
                document_digest(before) != document_digest(after)
                or old_refs.get("context:" + task_id) != context_hash
            )
            if changed and task["node_revision"] == before.get("node_revision", 0):
                task["node_revision"] += 1
    refs, changes = {}, {}
    seq = head["scope_commit_seq"] + 1 if head else 1
    for name, value in _facts(state).items():
        # Only a serialization comparison cache, never a persisted hash format.
        # The reference must still match the transaction's authoritative head;
        # rollback or another writer makes the cache miss. JSON distinguishes
        # booleans and numbers, unlike Python container equality.
        encoded = (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
            )
            if cache is not None
            else None
        )
        cached = cache.get(name) if cache is not None else None
        if cached and cached[0] == encoded and cached[1] == old_refs.get(name):
            ref = cached[1]
        else:
            ref = document_digest({"run_id": run_id, "name": name, "value": value})
        if cache is not None:
            cache[name] = (encoded, ref)
        refs[name] = ref
        if old_refs.get(name) != ref:
            _put_version(tx, run_id, name, value)
            changes[name] = ref
    for name in old_refs.keys() - refs.keys():
        changes[name] = None
    if not changes:
        return head
    node_refs = dict(head.get("node_refs", {})) if head else {}
    for task_id, task in state["tasks"].items():
        if "task:" + task_id not in changes and "context:" + task_id not in changes:
            continue
        checkpoint_id = str(uuid5(NAMESPACE_URL, f"{scope_id}:{task_id}:{seq}"))
        checkpoint = {
            "run_id": run_id,
            "node_checkpoint_id": checkpoint_id,
            "node_task_id": task_id,
            "activation_id": task.get("activation_id"),
            "node_revision": task.get("node_revision", 0),
            "node_protocol_version": 1,
            "scope_commit_seq": seq,
            "continuation_ref": _payload(
                tx, namespace, run_id, "continuation:" + task_id, task.get("continuation")
            ),
            "input_ref": _payload(tx, namespace, run_id, "input:" + task_id, task.get("input")),
            "task_state_ref": refs["task:" + task_id],
            "context_revision_ref": {
                "context_id": task["context"]["context_id"],
                "revision": task["context"]["revision"],
                "manifest_ref": refs["context:" + task_id],
            },
            "consumed_result_ids": task.get("consumed_result_ids", []),
            "waiting_refs": [task["wait_condition"]] if task.get("wait_condition") else [],
        }
        checkpoint["content_hash"] = document_digest(checkpoint)
        tx.put("node_checkpoints", checkpoint_id, checkpoint)
        node_refs[task_id] = checkpoint_id
    delta = {
        "format_version": FORMAT_VERSION,
        "run_id": run_id,
        "graph_execution_id": scope_id,
        "scope_commit_seq": seq,
        "command_id": command_id,
        "after_refs": changes,
        "node_refs": node_refs,
        "previous_hash": head["delta_hash"] if head else None,
    }
    delta_hash = document_digest(delta)
    tx.put("scope_commits", f"{scope_id}:{seq}", {**delta, "content_hash": delta_hash})
    head = {
        "run_id": run_id,
        "graph_execution_id": scope_id,
        "engine_kind": "agent",
        "engine_protocol_version": AGENT_PROTOCOL_VERSION,
        "scope_commit_seq": seq,
        "state_revision": state["revision"],
        "refs": refs,
        "node_refs": node_refs,
        "delta_hash": delta_hash,
        "head_checkpoint_id": head["head_checkpoint_id"] if head else None,
    }
    waiting = all(
        t["status"] in {"waiting", "succeeded", "failed", "cancelled"}
        for t in state["tasks"].values()
    )
    # Waiting on a live model/tool is not a stable graph wait. Correctness is
    # already in the delta; do not rewrite a full index at every operation wait.
    stable_wait = waiting and not any(
        o["status"] == "pending" for o in state["operations"].values()
    )
    tx.put("graph_heads", scope_id, head)
    return {**head, "_snapshot_due": seq == 1 or seq % SNAPSHOT_INTERVAL == 0 or stable_wait}


def restore(tx, namespace, state):
    try:
        return _restore(tx, namespace, state)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InfrastructureError("CHECKPOINT_CORRUPT") from exc


def _restore(tx, namespace, state):
    """Restore through the agent codec at one consistent store watermark."""
    scope_id = state["graph_id"]
    head = tx.get("graph_heads", scope_id)
    if head is None:
        if state.get("graph", {}).get("protocol_version"):
            raise InfrastructureError("CHECKPOINT_CORRUPT")
        return state  # explicitly supported pre-node-protocol checkpoint
    _require(type(head["scope_commit_seq"]) is int and head["scope_commit_seq"] > 0)
    if head["engine_kind"] != "agent" or (
        type(head["engine_protocol_version"]) is not int or head["engine_protocol_version"] != 1
    ):
        raise InfrastructureError("CHECKPOINT_VERSION_UNSUPPORTED")
    checkpoint_id = head["head_checkpoint_id"]
    payload, seq, previous_hash = None, 0, None
    visited = set()
    while checkpoint_id:
        _require(checkpoint_id not in visited)
        visited.add(checkpoint_id)
        checkpoint = tx.get("graph_checkpoints", checkpoint_id)
        _require(checkpoint is not None)
        try:
            _require(
                checkpoint["namespace"] == namespace
                and checkpoint["run_id"] == state["run_id"]
                and checkpoint["graph_execution_id"] == scope_id
            )
            if (
                (
                    type(checkpoint["format_version"]) is not int
                    or checkpoint["format_version"] != FORMAT_VERSION
                )
                or checkpoint["engine_kind"] != "agent"
                or type(checkpoint["engine_protocol_version"]) is not int
                or checkpoint["engine_protocol_version"] != AGENT_PROTOCOL_VERSION
            ):
                raise InfrastructureError("CHECKPOINT_VERSION_UNSUPPORTED")
            _require(checkpoint["engine_state_ref"]["content_scope_ref"]["namespace"] == namespace)
            payload = _read_version(
                tx, state["run_id"], "agent_checkpoint", checkpoint["engine_state_ref"]
            )
            _require(
                type(checkpoint["covered_commit_seq"]) is int
                and 0 < checkpoint["covered_commit_seq"] <= head["scope_commit_seq"]
            )
            seq, previous_hash = checkpoint["covered_commit_seq"], payload["delta_hash"]
            break
        except InfrastructureError as exc:
            if exc.code != "CHECKPOINT_CORRUPT":
                raise
            checkpoint_id = checkpoint["previous_checkpoint_id"]
    refs = dict(payload["refs"]) if payload else {}
    node_refs = dict(payload["node_refs"]) if payload else {}
    for sequence in range(seq + 1, head["scope_commit_seq"] + 1):
        delta = tx.get("scope_commits", f"{scope_id}:{sequence}")
        _require(delta is not None)
        content_hash = delta.pop("content_hash")
        _require(
            document_digest(delta) == content_hash
            and type(delta["scope_commit_seq"]) is int
            and delta["run_id"] == state["run_id"]
            and type(delta["format_version"]) is int
            and delta["format_version"] == FORMAT_VERSION
            and delta["previous_hash"] == previous_hash
            and delta["scope_commit_seq"] == sequence
            and delta["graph_execution_id"] == scope_id
        )
        for name, ref in delta["after_refs"].items():
            if ref is None:
                refs.pop(name, None)
            else:
                refs[name] = ref
        node_refs, previous_hash = delta["node_refs"], content_hash
    _require(
        refs == head["refs"]
        and node_refs == head["node_refs"]
        and previous_hash == head["delta_hash"]
    )
    facts = {name: _read_version(tx, state["run_id"], name, ref) for name, ref in refs.items()}
    for task_id, ref in node_refs.items():
        node = tx.get("node_checkpoints", ref)
        if node is not None:
            saved_hash = node.pop("content_hash", None)
            _require(document_digest(node) == saved_hash)
        _require(
            node is not None
            and node["node_task_id"] == task_id
            and type(node["node_protocol_version"]) is int
            and node["node_protocol_version"] == 1
            and type(node["node_revision"]) is int
            and node["node_revision"] == facts["task:" + task_id]["node_revision"]
            and node["context_revision_ref"]["manifest_ref"] == refs["context:" + task_id]
            and node["context_revision_ref"]["context_id"]
            == facts["context:" + task_id]["context_id"]
            and node["context_revision_ref"]["revision"] == facts["context:" + task_id]["revision"]
            and node["task_state_ref"] == refs["task:" + task_id]
        )
        for field, prefix in (("continuation", "continuation:"), ("input", "input:")):
            payload_ref = node[field + "_ref"]
            _require(payload_ref["content_scope_ref"]["namespace"] == namespace)
            value = _read_version(tx, state["run_id"], prefix + task_id, payload_ref)
            _require(document_digest(value) == document_digest(facts["task:" + task_id].get(field)))
            facts["task:" + task_id][field] = value
    _require(document_digest(facts) == document_digest(_facts(state)))
    restored = copy_document(state)
    restored["tasks"] = {
        name[5:]: {**value, "context": facts["context:" + name[5:]]}
        for name, value in facts.items()
        if name.startswith("task:")
    }
    restored["operations"] = {
        name[10:]: value for name, value in facts.items() if name.startswith("operation:")
    }
    restored["batches"] = {
        name[6:]: value for name, value in facts.items() if name.startswith("batch:")
    }
    restored["graph"] = {k: v for k, v in facts["graph"].items() if k != "root_task_id"}
    restored["activations"] = {
        name[11:]: value for name, value in facts.items() if name.startswith("activation:")
    }
    restored["task_results"] = {
        name[7:]: value for name, value in facts.items() if name.startswith("result:")
    }
    return restored


def delete_owned(tx, run_id):
    """Delete all immutable execution material only with its quiescent Owner."""
    keys = {
        "graph_heads": "graph_execution_id",
        "graph_checkpoints": "checkpoint_id",
        "node_checkpoints": "node_checkpoint_id",
    }
    for kind, key in keys.items():
        for value in tx.list(kind):
            if value["run_id"] == run_id:
                tx.delete(kind, value[key])
    for value in tx.list("scope_commits"):
        if value["run_id"] == run_id:
            tx.delete("scope_commits", f"{value['graph_execution_id']}:{value['scope_commit_seq']}")
    for value in tx.list("checkpoint_versions"):
        if value["run_id"] == run_id:
            tx.delete("checkpoint_versions", document_digest(value))
