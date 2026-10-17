"""Renew and fence one cleanup job while its synchronous environment work runs."""

from ..errors import LeaseLost
from ..storage import TERMINAL
from .leases import LeaseHeartbeat


class CleanupLeaseGuard(LeaseHeartbeat):
    def __init__(self, runtime, run_id, job_id, epoch):
        self.runtime, self.run_id, self.job_id, self.epoch = runtime, run_id, job_id, epoch
        super().__init__(self.renew, runtime.lease_seconds, name="agentloom-cleanup-lease")

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
