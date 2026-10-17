"""Renew and fence one cleanup job while its synchronous environment work runs."""

import threading

from ..errors import LeaseLost
from ..storage import TERMINAL


class CleanupLeaseGuard:
    def __init__(self, runtime, run_id, job_id, epoch):
        self.runtime, self.run_id, self.job_id, self.epoch = runtime, run_id, job_id, epoch
        self._stop = threading.Event()
        self._thread = None
        self._error = None

    def matches(self, state, now):
        if not state or state["status"] not in TERMINAL or state["cleanup_state"] != "running":
            return False
        job = state.get("cleanup_job") or {}
        lease = job.get("lease") or {}
        return (
            job.get("job_id") == self.job_id
            and lease.get("worker_id") == self.runtime.worker_id
            and lease.get("epoch") == self.epoch
            and lease.get("expires_at", 0) > now
        )

    def renew(self):
        with self.runtime.store.transaction(self.runtime.namespace) as tx:
            state = tx.get("runs", self.run_id)
            if not self.matches(state, tx.now):
                raise LeaseLost("Cleanup ownership has expired or changed")
            state["cleanup_job"]["lease"]["expires_at"] = tx.now + self.runtime.lease_seconds
            state["revision"] += 1
            tx.put("runs", self.run_id, state)

    def _heartbeat(self):
        while not self._stop.wait(self.runtime.lease_seconds / 3):
            try:
                self.renew()
            except Exception as exc:
                # Never revive an expired epoch or claim success after renewal failed.
                self._error = exc
                return

    def __enter__(self):
        self.renew()
        self._thread = threading.Thread(
            target=self._heartbeat, name="agentloom-cleanup-lease", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join()
        if exc_type is None and self._error is not None:
            raise self._error
