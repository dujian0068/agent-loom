"""Root deadlines are control facts and do not depend on an Agent or active owner."""

from ..errors import AgentLoomError
from ..storage import TERMINAL
from .control import request_stop
from .maintenance import maintenance_due, maintenance_failed


def request_expired_stops(runtime):
    with runtime.store.transaction(runtime.namespace) as tx:
        candidates = [
            state["run_id"]
            for state in tx.list("runs")
            if state["status"] not in TERMINAL | {"stopping"}
            and state["deadline_at"] <= tx.now
            and maintenance_due(state, "deadline", tx.now)
        ]
    for run_id in candidates:
        try:
            with runtime.store.transaction(runtime.namespace) as tx:
                state = tx.get("runs", run_id)
                if (
                    state
                    and state["status"] not in TERMINAL | {"stopping"}
                    and state["deadline_at"] <= tx.now
                    and maintenance_due(state, "deadline", tx.now)
                ):
                    request_stop(
                        tx, run_id, AgentLoomError("RUN_TIMEOUT", category="timeout").to_dict()
                    )
        except Exception as exc:
            maintenance_failed(runtime, "deadline", exc, run_id=run_id)
