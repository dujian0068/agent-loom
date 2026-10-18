"""Coalesced, optional graph aggregation after critical scope commits succeed."""

import asyncio
from uuid import NAMESPACE_URL, uuid5

from ..errors import LeaseLost
from ..storage.base import TERMINAL
from ..storage.commit import verify_lease
from .journal import AGENT_PROTOCOL_VERSION, FORMAT_VERSION, _payload


class SnapshotWriter:
    def __init__(self, store, namespace, lease):
        self.store, self.namespace, self.lease = store, namespace, lease
        self._pending = None
        self._scheduled = False

    def request(self, head, state):
        if head is None or not head.get("_snapshot_due"):
            return
        # Retain only immutable references and the small snapshot metadata, not
        # a second full Run or live NodeContext/lease in the engine payload.
        root = state["tasks"][state["root_task_id"]]
        self._pending = (
            head,
            {
                "phase": root["status"],
                "ready_task_ids": [
                    key for key, task in state["tasks"].items() if task["status"] == "ready"
                ],
            },
        )
        if state["status"] in TERMINAL:
            self.flush()
        elif not self._scheduled:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.flush()
            else:
                self._scheduled = True
                loop.call_soon(self.flush)

    def flush(self):
        pending, self._pending = self._pending, None
        self._scheduled = False
        if pending is None:
            return
        head, metadata = pending
        try:
            self._aggregate(head, metadata)
        except LeaseLost:
            # A new owner will aggregate the same committed references.
            return
        except Exception:
            # Aggregation is an optimization; a failed snapshot is never an
            # execution rollback. Persist a bounded diagnostic when possible.
            try:
                with self.store.transaction(self.namespace) as tx:
                    state = tx.get("runs", self.lease.run_id)
                    if state is None:
                        return
                    verify_lease(state, self.lease, tx.now, allow_terminal=True)
                    current = tx.get("graph_heads", head["graph_execution_id"])
                    if current:
                        current["snapshot_error"] = {
                            "code": "GRAPH_SNAPSHOT_FAILED",
                            "scope_commit_seq": head["scope_commit_seq"],
                        }
                        tx.put("graph_heads", head["graph_execution_id"], current)
            except Exception:
                pass

    def _aggregate(self, head, metadata):
        seq, scope_id = head["scope_commit_seq"], head["graph_execution_id"]
        checkpoint_id = str(uuid5(NAMESPACE_URL, f"{scope_id}:snapshot:{seq}"))
        payload = {
            **metadata,
            "refs": head["refs"],
            "node_refs": head["node_refs"],
            "delta_hash": head["delta_hash"],
            "state_revision_ref": head["state_revision"],
        }
        with self.store.transaction(self.namespace) as tx:
            state = tx.get("runs", self.lease.run_id)
            if state is None:
                return
            verify_lease(state, self.lease, tx.now, allow_terminal=True)
            current = tx.get("graph_heads", scope_id)
            if current is None:
                return
            previous = (
                tx.get("graph_checkpoints", current["head_checkpoint_id"])
                if current["head_checkpoint_id"]
                else None
            )
            if previous and previous["covered_commit_seq"] >= seq:
                return
            payload_ref = _payload(tx, self.namespace, state["run_id"], "agent_checkpoint", payload)
            checkpoint = {
                "format_version": FORMAT_VERSION,
                "namespace": self.namespace,
                "run_id": state["run_id"],
                "graph_execution_id": scope_id,
                "checkpoint_id": checkpoint_id,
                "engine_kind": "agent",
                "engine_protocol_version": AGENT_PROTOCOL_VERSION,
                "checkpoint_seq": seq,
                "covered_commit_seq": seq,
                "previous_checkpoint_id": current["head_checkpoint_id"],
                "parent_execution_ref": None,
                "engine_state_ref": payload_ref,
                "run_control_ref": {"run_id": state["run_id"], "revision": head["state_revision"]},
                "created_at": tx.now,
            }
            tx.put("graph_checkpoints", checkpoint_id, checkpoint)
            current["head_checkpoint_id"] = checkpoint_id
            current.pop("snapshot_error", None)
            tx.put("graph_heads", scope_id, current)
