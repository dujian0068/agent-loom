"""Root control is independent of models, user hooks and graph scheduling."""

import time

from ..errors import AgentLoomError
from ..storage import CommitCoordinator, claim
from ..types import new_id
from .calls import failure
from .maintenance import maintenance_due, maintenance_failed, maintenance_recovered


def emit(state, event_type, payload=None):
    state["events"].append(
        {
            "event_id": new_id(),
            "run_id": state["run_id"],
            "seq": len(state["events"]) + 1,
            "type": event_type,
            "payload": payload or {},
            "committed_at": time.time(),
            "schema_version": 1,
        }
    )


def run_sequence(state):
    """Order runs by their initial question, independently of carried supplements."""
    return next(
        item["session_seq"] for item in state["inputs"] if item["input_id"] == state["input_id"]
    )


def finalize_run(runtime, lease, status, *, output=None, error=None):
    state = runtime._load(lease.run_id)
    scope = state["workspace"]
    if scope:
        runtime.environment.revoke(scope)

    def finish(s):
        final_status, final_output, final_error = status, output, error
        if s["status"] == "stopping":
            final_status, final_output, final_error = (
                "cancelled",
                None,
                AgentLoomError("RUN_STOPPED").to_dict(),
            )
        elif time.time() >= s["deadline_at"]:
            final_status, final_output, final_error = (
                "timed_out",
                None,
                AgentLoomError("RUN_TIMEOUT", category="timeout").to_dict(),
            )
        for task in s["tasks"].values():
            if task["status"] not in {"succeeded", "failed", "cancelled"}:
                task.update(status="cancelled", error=final_error, stage="completed")
        for op in s["operations"].values():
            if op["ticket"] == "reserved":
                op["ticket"] = "released"
            if op["status"] == "pending":
                op.update(
                    status="failed",
                    phase="completed",
                    response=failure(final_error or AgentLoomError("RUN_ENDED").to_dict()),
                )
            for attempt in op["attempts"]:
                if attempt["status"] == "started":
                    attempt.update(status="cancelled", ended_at=time.time(), error=final_error)
        s["usage"]["loops_reserved"] = 0
        for item in s["inputs"]:
            if item["input_id"] != s["input_id"] and item["delivery_state"] in {
                "pending",
                "claimed",
            }:
                item["delivery_state"] = "pending_next_run"
        s.update(
            status=final_status,
            output=final_output,
            error=final_error,
            ended_at=time.time(),
            cleanup_state="pending",
        )
        s["cleanup_job"] = {
            "job_id": new_id(),
            "scope": s["workspace"],
            "lease": None,
            "attempts": 0,
            "next_retry_at": 0,
            "error": None,
        }
        maintenance_recovered(s, "stop", time.time())
        emit(
            s,
            "run.completed",
            {"status": final_status, "output": final_output, "error": final_error},
        )
        s["terminal_seq"] = len(s["events"])

    return CommitCoordinator(
        runtime.store, runtime.namespace, lease, runtime.checkpoint_callback
    ).commit("terminal", finish)


def settle_orphaned_stops(runtime):
    with runtime.store.transaction(runtime.namespace) as tx:
        ids = [
            s["run_id"]
            for s in tx.list("runs")
            if s["status"] == "stopping"
            and (s["lease"] or {}).get("expires_at", 0) <= tx.now
            and maintenance_due(s, "stop", tx.now)
        ]
    for run_id in ids:
        lease = None
        try:
            lease = claim(
                runtime.store,
                runtime.namespace,
                run_id,
                runtime.worker_id,
                runtime.lease_seconds,
                eligible=lambda state, now: (
                    state["status"] == "stopping" and maintenance_due(state, "stop", now)
                ),
            )
            if lease:
                finalize_run(
                    runtime, lease, "cancelled", error=AgentLoomError("RUN_STOPPED").to_dict()
                )
        except Exception as exc:
            maintenance_failed(runtime, "stop", exc, run_id=run_id)
        finally:
            if lease:
                try:
                    CommitCoordinator(runtime.store, runtime.namespace, lease).release()
                except Exception as exc:
                    maintenance_failed(runtime, "stop", exc, run_id=run_id)
