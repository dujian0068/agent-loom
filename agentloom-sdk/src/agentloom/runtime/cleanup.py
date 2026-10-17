"""Terminal file cleanup has a separate lease and never constructs an Agent."""

from ..errors import error_record
from ..storage import TERMINAL


class CleanupService:
    def __init__(self, runtime):
        self.runtime = runtime

    def scan_once(self):
        runtime = self.runtime
        with runtime.store.transaction(runtime.namespace) as tx:
            candidates = [
                s["run_id"]
                for s in tx.list("runs")
                if s["status"] in TERMINAL and s["cleanup_state"] != "succeeded"
            ]
        for run_id in candidates:
            with runtime.store.transaction(runtime.namespace) as tx:
                state = tx.get("runs", run_id)
                if not state or state["cleanup_state"] == "succeeded":
                    continue
                job = state["cleanup_job"]
                lease = job.get("lease") or {}
                if job["next_retry_at"] > tx.now or lease.get("expires_at", 0) > tx.now:
                    continue
                epoch = lease.get("epoch", 0) + 1
                job["lease"] = {
                    "worker_id": runtime.worker_id,
                    "epoch": epoch,
                    "expires_at": tx.now + runtime.lease_seconds,
                }
                job["attempts"] += 1
                state["cleanup_state"] = "running"
                tx.put("runs", run_id, state)
                scope = job["scope"]
            try:
                if scope:
                    runtime.environment.cleanup(scope)
                error = None
            except Exception as exc:
                error = error_record(exc)
            with runtime.store.transaction(runtime.namespace) as tx:
                state = tx.get("runs", run_id)
                if not state:
                    continue
                job = state["cleanup_job"]
                lease = job["lease"]
                if (
                    lease["worker_id"] != runtime.worker_id
                    or lease["epoch"] != epoch
                    or lease["expires_at"] <= tx.now
                ):
                    continue
                state["cleanup_state"] = "failed" if error else "succeeded"
                state["revision"] += 1
                job.update(
                    error=error,
                    next_retry_at=tx.now + runtime.cleanup_retry_seconds if error else 0,
                )
                lease["expires_at"] = 0
                tx.put("runs", run_id, state)
