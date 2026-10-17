"""Attempt outcomes and synchronous completion receipts, independent of teardown."""

from ..errors import AgentLoomError, InfrastructureError, error_record


def success(result):
    return {"status": "succeeded", "result": result, "error": None, "execution_fact": "completed"}


def failure(error, *, blocked=False):
    return {
        "status": "blocked" if blocked else "failed",
        "result": None,
        "error": error,
        "execution_fact": "not_started"
        if blocked or error.get("external_outcome") == "not_started"
        else "unknown",
    }


def failed_result(exc):
    """Preserve established execution facts without accepting malformed JSON."""
    raw = failure(error_record(exc))
    if hasattr(exc, "result_material"):
        raw["result"] = exc.result_material
        raw["execution_fact"] = getattr(exc, "execution_fact", "completed")
    elif hasattr(exc, "execution_fact"):
        raw["execution_fact"] = exc.execution_fact
    for field in ("tool_result", "invalid_tool_result"):
        if hasattr(exc, field):
            raw[field] = getattr(exc, field)
    return raw


class AttemptCompletion:
    """Save a known outcome once, under the attempt's original lease and identity.

    A receipt becomes saved only after the durable callback returns. It expires
    when dispatch exits, so a retained adapter callback cannot write a later
    attempt. Resource cleanup is still performed in the calling task.
    """

    def __init__(self, persist):
        self._persist = persist
        self.saved = False
        self._closed = False

    def record(self, raw):
        if self._closed:
            raise AgentLoomError("ATTEMPT_COMPLETION_CLOSED", category="protocol")
        if not self.saved:
            try:
                self._persist(raw)
            except InfrastructureError:
                raise
            except Exception as exc:
                # This callback is host persistence, not adapter execution. A
                # failed acknowledgement/observer must stop delivery even if the
                # transaction committed; recovery resolves the saved receipt.
                raise InfrastructureError(
                    "ATTEMPT_COMPLETION_COMMIT_FAILED", type(exc).__name__
                ) from exc
            self.saved = True

    def close(self):
        self._closed = True
