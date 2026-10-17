"""Terminal file cleanup has a separate lease and never constructs an Agent."""

from ..errors import error_record
from ..storage import TERMINAL
from .cleanup_lease import CleanupLeaseGuard
from .maintenance import maintenance_due, maintenance_failed, maintenance_recovered


class CleanupService:
    def __init__(self, runtime):
        self.runtime = runtime

    def scan_once(self):
        runtime = self.runtime
        with runtime.store.transaction(runtime.namespace) as tx:
            candidates = [
                s["run_id"]
                for s in tx.list("runs")
                if s["status"] in TERMINAL
                and s["cleanup_state"] != "succeeded"
                and maintenance_due(s, "cleanup", tx.now)
            ]
        for run_id in candidates:
            try:
                self._clean_run(run_id)
            except Exception as exc:
                maintenance_failed(runtime, "cleanup", exc, run_id=run_id)

    def _clean_run(self, run_id):
        runtime = self.runtime
        with runtime.store.transaction(runtime.namespace) as tx:
            state = tx.get("runs", run_id)
            if (
                not state
                or state["status"] not in TERMINAL
                or state["cleanup_state"] == "succeeded"
                or not maintenance_due(state, "cleanup", tx.now)
            ):
                return
            job = state["cleanup_job"]
            lease = job.get("lease") or {}
            if job["next_retry_at"] > tx.now or lease.get("expires_at", 0) > tx.now:
                return
            epoch = lease.get("epoch", 0) + 1
            job["lease"] = {
                "worker_id": runtime.worker_id,
                "epoch": epoch,
                "expires_at": tx.now + runtime.lease_seconds,
            }
            job["attempts"] += 1
            state["cleanup_state"] = "running"
            state["revision"] += 1
            tx.put("runs", run_id, state)
            scope = job["scope"]
            guard = CleanupLeaseGuard(runtime, run_id, job["job_id"], epoch)
        with guard:
            try:
                if scope:
                    runtime.environment.cleanup(scope)
                error = None
            except Exception as exc:
                error = error_record(exc)
        with runtime.store.transaction(runtime.namespace) as tx:
            state = tx.get("runs", run_id)
            if not guard.matches(state, tx.now):
                return
            job = state["cleanup_job"]
            lease = job["lease"]
            state["cleanup_state"] = "failed" if error else "succeeded"
            state["revision"] += 1
            job.update(
                error=error,
                next_retry_at=tx.now + runtime.cleanup_retry_seconds if error else 0,
            )
            if not error:
                maintenance_recovered(state, "cleanup", tx.now)
            lease["expires_at"] = 0
            tx.put("runs", run_id, state)
