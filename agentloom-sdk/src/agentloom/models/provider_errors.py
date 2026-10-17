"""Map explicit provider error codes; never classify by remote error messages."""

from ..errors import AgentLoomError

MAX_PROVIDER_ERROR_BYTES = 64 * 1024


def provider_error(payload, fallback):
    error = payload.get("error") if isinstance(payload, dict) else None
    # Responses also emits a top-level error event, without an error envelope.
    if error is None and isinstance(payload, dict) and payload.get("type") == "error":
        error = payload
    if isinstance(error, dict) and error.get("code") == "context_length_exceeded":
        return AgentLoomError(
            "MODEL_CONTEXT_LENGTH_EXCEEDED",
            category="validation",
            phase="receive",
            external_outcome="not_started",
        )
    return fallback
