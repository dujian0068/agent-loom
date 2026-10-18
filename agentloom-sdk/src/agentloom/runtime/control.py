"""Root control is independent of models, user hooks and graph scheduling."""

import time

from ..configuration.json_documents import copy_document
from ..context.history_facts import execution_fact
from ..errors import AgentLoomError
from ..events.control import settle_saved_hooks
from ..storage import TERMINAL, CommitCoordinator, claim
from ..types import new_id
from .calls import failure
from .leases import LeaseHeartbeat
from .maintenance import maintenance_due, maintenance_failed, maintenance_recovered
from .recovery import validate_recovery


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


def request_stop(tx, run_id, error):
    """Close admission and persist the first terminal reason under the same lock.

    This is a control intent, not proof of termination. A valid lease holder must
    still revoke writers before committing the terminal result.
    """
    state = tx.get("runs", run_id)
    if state and state["status"] not in TERMINAL and state["status"] != "stopping":
        state.update(status="stopping", control_error=error)
        if error["code"] == "RUN_TIMEOUT":
            maintenance_recovered(state, "deadline", tx.now)
        state["revision"] += 1
        emit(state, "run.stopping")
        tx.put("runs", run_id, state)
    tx.put("admission", run_id, {"closed": True})
    return state["status"] if state else None


def finalize_run(runtime, lease, status, *, output=None, error=None):
    coordinator = CommitCoordinator(
        runtime.store, runtime.namespace, lease, runtime.checkpoint_callback
    )
    with LeaseHeartbeat(
        lambda: coordinator.renew(runtime.lease_seconds),
        runtime.lease_seconds,
        name="agentloom-revocation-lease",
    ):
        state = runtime._load(lease.run_id)
        validate_recovery(state)
        scope = state["workspace"]
        if scope:
            runtime._files.revoke(runtime, scope)

    def finish(s):
        final_status, final_output, final_error = status, output, error
        if s["status"] == "stopping":
            final_error = s.get("control_error") or AgentLoomError("RUN_STOPPED").to_dict()
            final_status = "timed_out" if final_error["code"] == "RUN_TIMEOUT" else "cancelled"
            final_output = None
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
                control_error = final_error or AgentLoomError("RUN_ENDED").to_dict()
                denied = settle_saved_hooks(s, op, control_error)
                fact = execution_fact(op)
                if op["response"] is not None:
                    op["effective_before_control"] = copy_document(op["response"])
                response = failure(
                    AgentLoomError(
                        "HOOK_DENIED",
                        phase="prepare" if denied == "request" else "after",
                        external_outcome="known" if fact == "completed" else fact,
                    ).to_dict()
                    if denied
                    else {**control_error, "category": "cancelled"},
                    blocked=bool(denied),
                )
                response.update(execution_fact=fact, operation_id=op["operation_id"])
                op.update(
                    status=response["status"],
                    phase="completed",
                    response=response,
                    next_attempt_at=None,
                )
                emit(
                    s,
                    "operation.completed",
                    {
                        "operation_id": op["operation_id"],
                        "task_id": op["task_id"],
                        "role_key": op["role_key"],
                        "event_type": op["event_type"],
                        "status": op["status"],
                    },
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

    return coordinator.commit("terminal", finish)


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
