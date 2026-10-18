from ..configuration.json_documents import document_digest as digest
from ..errors import InfrastructureError, LeaseLost, RequestError
from .base import TERMINAL, ExecutionLease


def verify_lease(state, lease, now, *, allow_terminal=False):
    current = state.get("lease") or {}
    if (
        (not allow_terminal and state["status"] in TERMINAL)
        or current.get("worker_id") != lease.worker_id
        or current.get("epoch") != lease.epoch
        or current.get("expires_at", 0) <= now
    ):
        raise LeaseLost()


def claim(store, namespace, run_id, worker_id, ttl, *, eligible=None):
    with store.transaction(namespace) as tx:
        state = tx.get("runs", run_id)
        if not state or state["status"] in TERMINAL:
            return None
        if eligible is not None and not eligible(state, tx.now):
            return None
        old = state.get("lease") or {}
        if old.get("expires_at", 0) > tx.now:
            return None
        epoch = old.get("epoch", 0) + 1
        state["lease"] = {"worker_id": worker_id, "epoch": epoch, "expires_at": tx.now + ttl}
        tx.put("runs", run_id, state)
        return ExecutionLease(run_id, worker_id, epoch)


class CommitCoordinator:
    def __init__(self, store, namespace, lease, callback=None):
        self.store, self.namespace, self.lease = store, namespace, lease
        self.callback = callback
        self._journal_cache = {}
        from ..checkpoints.snapshots import SnapshotWriter

        self._snapshots = SnapshotWriter(store, namespace, lease)

    def commit(self, command, change=None, *, fingerprint=None, receipt_task_id=None):
        from ..nodes.commands import RuntimeCommit, submit
        from ..nodes.effects import apply_effect

        if isinstance(command, RuntimeCommit):
            if change is not None and change != self.lease:
                raise LeaseLost()
            return submit(self, command, apply_effect)
        # Retry the same local transaction command, never its external action.
        # A lost acknowledgement is resolved by its committed receipt.
        for attempt in range(3):
            try:
                return self._commit_once(
                    command, change, fingerprint=fingerprint, receipt_task_id=receipt_task_id
                )
            except LeaseLost:
                raise
            except InfrastructureError:
                if attempt == 2:
                    raise

    def _commit_once(self, command, change, *, fingerprint=None, receipt_task_id=None):
        with self.store.transaction(self.namespace) as tx:
            state = tx.get("runs", self.lease.run_id)
            if state is None:
                raise LeaseLost()
            key = self.lease.run_id + ":" + command
            receipt = tx.get("commits", key)
            if receipt:
                verify_lease(state, self.lease, tx.now, allow_terminal=True)
                if receipt.get("fingerprint") != fingerprint:
                    raise RequestError("IDEMPOTENCY_CONFLICT")
                return state
            verify_lease(state, self.lease, tx.now)
            event_cursor = len(state.get("events", ()))
            change(state)
            state["revision"] += 1
            from ..checkpoints import publish

            head = publish(tx, self.namespace, state, command, cache=self._journal_cache)
            receipt = {
                "command": command,
                "run_id": self.lease.run_id,
                "revision": state["revision"],
                "committed_at": tx.now,
                "fingerprint": fingerprint,
                "entity_versions": [
                    {
                        "task_id": receipt_task_id,
                        "node_revision": state["tasks"][receipt_task_id]["node_revision"],
                    }
                ]
                if receipt_task_id
                else [],
                "allocated_refs": {"task_id": receipt_task_id} if receipt_task_id else {},
                "public_event_seqs": list(
                    range(event_cursor + 1, len(state.get("events", ())) + 1)
                ),
                "scope_versions": [
                    {
                        "graph_id": head["graph_execution_id"],
                        "scope_commit_seq": head["scope_commit_seq"],
                        "state_revision": head["state_revision"],
                    }
                ]
                if head
                else [],
                "state_hash": digest(state)
                if state["revision"] % 16 == 0 or state["status"] in TERMINAL
                else None,
            }
            tx.put("runs", self.lease.run_id, state)
            tx.put("commits", key, receipt)
        self._snapshots.request(head, state)
        # This is deliberately after the real transaction, enabling process-death tests.
        if self.callback:
            self.callback(command, state)
        return state

    def renew(self, ttl):
        with self.store.transaction(self.namespace) as tx:
            state = tx.get("runs", self.lease.run_id)
            verify_lease(state, self.lease, tx.now)
            state["lease"]["expires_at"] = tx.now + ttl
            tx.put("runs", self.lease.run_id, state)

    def release(self):
        with self.store.transaction(self.namespace) as tx:
            state = tx.get("runs", self.lease.run_id)
            if (
                state
                and state.get("lease", {}).get("worker_id") == self.lease.worker_id
                and state["lease"]["epoch"] == self.lease.epoch
            ):
                state["lease"]["expires_at"] = 0
                tx.put("runs", self.lease.run_id, state)
