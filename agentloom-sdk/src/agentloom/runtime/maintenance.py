"""Bounded, durable diagnostics for independently retryable host maintenance."""

import logging

from ..errors import error_record

logger = logging.getLogger(__name__)


def maintenance_due(state, phase, now):
    diagnostic = state.get("maintenance", {}).get(phase, {})
    return diagnostic.get("next_retry_at", 0) <= now


def maintenance_recovered(state, phase, now):
    diagnostic = state.get("maintenance", {}).get(phase)
    if diagnostic:
        # Keep the last failure visible after recovery without growing an error log.
        diagnostic.update(next_retry_at=0, recovered_at=now)


def maintenance_failed(runtime, phase, exc, *, run_id=None):
    """Report failures even when storage cannot accept the per-Run diagnostic."""
    error = error_record(exc)
    logger.warning("Maintenance failed: phase=%s run_id=%s code=%s", phase, run_id, error["code"])
    if run_id is None:
        return
    try:
        with runtime.store.transaction(runtime.namespace) as tx:
            state = tx.get("runs", run_id)
            if state is None:
                return
            diagnostics = state.setdefault("maintenance", {})
            previous = diagnostics.get(phase, {})
            diagnostics[phase] = {
                "failures": previous.get("failures", 0) + 1,
                "last_error": error,
                "failed_at": tx.now,
                "next_retry_at": tx.now + runtime.cleanup_retry_seconds,
                "recovered_at": None,
            }
            state["revision"] += 1
            tx.put("runs", run_id, state)
    except Exception as save_error:
        # Diagnostics must not take down the scan they are meant to protect.
        logger.warning(
            "Maintenance diagnostic unavailable: phase=%s run_id=%s code=%s",
            phase,
            run_id,
            error_record(save_error)["code"],
        )
