from ..configuration.json_documents import document_digest as digest
from ..errors import InfrastructureError, LeaseLost, RequestError
from .base import TERMINAL, ExecutionLease


def verify_lease(state, lease, now):
    current = state.get("lease") or {}
    if (
        state["status"] in TERMINAL
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

    def commit(self, command, change, *, fingerprint=None):
        # Retry the same local transaction command, never its external action.
        # A lost acknowledgement is resolved by its committed receipt.
        for attempt in range(3):
            try:
                return self._commit_once(command, change, fingerprint=fingerprint)
            except LeaseLost:
                raise
            except InfrastructureError:
                if attempt == 2:
                    raise

    def _commit_once(self, command, change, *, fingerprint=None):
        with self.store.transaction(self.namespace) as tx:
            state = tx.get("runs", self.lease.run_id)
            if state is None:
                raise LeaseLost()
            key = self.lease.run_id + ":" + command
            receipt = tx.get("commits", key)
            if receipt:
                if receipt.get("fingerprint") != fingerprint:
                    raise RequestError("IDEMPOTENCY_CONFLICT")
                return state
            verify_lease(state, self.lease, tx.now)
            change(state)
            state["revision"] += 1
            receipt = {
                "command": command,
                "run_id": self.lease.run_id,
                "revision": state["revision"],
                "committed_at": tx.now,
                "fingerprint": fingerprint,
                "state_hash": digest(state)
                if state["revision"] % 16 == 0 or state["status"] in TERMINAL
                else None,
            }
            tx.put("runs", self.lease.run_id, state)
            tx.put("commits", key, receipt)
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
