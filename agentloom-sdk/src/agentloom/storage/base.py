"""Transactions are synchronous, bounded local work. Never await inside one."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out", "budget_exhausted"})


@dataclass(frozen=True)
class ExecutionLease:
    run_id: str
    worker_id: str
    epoch: int


@runtime_checkable
class Transaction(Protocol):
    now: float

    def get(self, kind: str, key: str): ...
    def put(self, kind: str, key: str, value): ...
    def delete(self, kind: str, key: str): ...
    def list(self, kind: str): ...


@runtime_checkable
class RuntimeStore(Protocol):
    def transaction(self, namespace: str): ...
    def load(self, namespace: str, run_id: str): ...
    def read_path(self, namespace: str, run_id: str, path): ...


class StoreQueries:
    def load_run_consistently(self, namespace, run_id):
        from ..checkpoints import restore

        with self.transaction(namespace) as tx:
            state = tx.get("runs", str(run_id))
            return restore(tx, namespace, state) if state else None

    def get_commit_receipt(self, namespace, run_id, command_id):
        with self.transaction(namespace) as tx:
            return tx.get("commits", str(run_id) + ":" + command_id)

    def read_path(self, namespace, run_id, path):
        state = self.load(namespace, run_id)
        for key in path:
            if state is None:
                return None
            state = state.get(key)
        return state

    def load(self, namespace, run_id):
        with self.transaction(namespace) as tx:
            return tx.get("runs", str(run_id))

    def checkpoints(self, namespace, run_id):
        with self.transaction(namespace) as tx:
            return sorted(
                (v for v in tx.list("commits") if v["run_id"] == str(run_id)),
                key=lambda v: v["revision"],
            )
