"""Attempt resource cleanup must not replace an already established outcome."""

import asyncio
import logging
from contextlib import asynccontextmanager

from .errors import AgentLoomError, InfrastructureError, error_record


def _control_error(exc):
    if isinstance(exc, BaseExceptionGroup):
        return next((error for child in exc.exceptions if (error := _control_error(child))), None)
    if isinstance(exc, InfrastructureError) or (
        isinstance(exc, AgentLoomError) and exc.code in {"RUN_STOPPED", "RUN_TIMEOUT"}
    ):
        return exc
    return None


def _interrupt(exc):
    if isinstance(exc, BaseExceptionGroup):
        return next((error for child in exc.exceptions if (error := _interrupt(child))), None)
    return exc if isinstance(exc, BaseException) and not isinstance(exc, Exception) else None


async def close_resource(close, report=None, *, primary=None):
    """Close in the owning task; retain the original failure or completed result.

    Cancellation and loss of execution authority still propagate. Ordinary close
    errors are diagnostic facts, never grounds to repeat the external action.
    """
    try:
        await close()
    except BaseException as exc:
        if exc is primary:
            return
        primary_control = _control_error(primary) or isinstance(
            primary, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
        )
        critical = _interrupt(exc) or _control_error(exc)
        if critical is not None and not primary_control:
            raise critical
        diagnostic = {"phase": "resource_close", "error": error_record(exc)}
        if report is not None:
            report(diagnostic)
        else:
            logging.getLogger(__name__).warning(
                "Attempt resource close failed: %s", diagnostic["error"]["code"]
            )


@asynccontextmanager
async def managed_resource(manager, report=None):
    """Preserve the body's outcome even when a context manager's exit raises."""
    resource = await manager.__aenter__()
    primary = None
    try:
        yield resource
    except BaseException as exc:
        primary = exc
        raise
    finally:
        await close_resource(
            lambda: manager.__aexit__(
                type(primary) if primary is not None else None,
                primary,
                primary.__traceback__ if primary is not None else None,
            ),
            report,
            primary=primary,
        )
