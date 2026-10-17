"""Transport framing shared by explicit protocol adapters, with no automatic retries."""

import json

import httpx

from ..configuration import json_copy
from ..errors import AgentLoomError


def http_error(status):
    retry = status in {429, 502, 503, 504}
    return AgentLoomError(
        "HTTP_" + str(status),
        category="rate_limit" if status == 429 else "transport",
        retryable_transport=retry,
        phase="receive",
        external_outcome="unknown",
    )


async def sse_data(response):
    if response.status_code >= 400:
        raise http_error(response.status_code)
    if "text/event-stream" not in response.headers.get("content-type", ""):
        raise AgentLoomError("MODEL_PROTOCOL_ERROR", "Expected a server-sent event stream")
    lines, size = [], 0
    try:
        async for line in response.aiter_lines():
            size += len(line.encode())
            if size > 20 * 1024 * 1024:
                raise AgentLoomError("MODEL_RESPONSE_TOO_LARGE")
            if not line:
                if lines:
                    yield "\n".join(lines)
                    lines = []
            elif line.startswith("data:"):
                lines.append(line[5:].removeprefix(" "))
        # An unfinished SSE frame is not a completion certificate.
    except httpx.TransportError as exc:
        raise AgentLoomError(
            "MODEL_STREAM_INTERRUPTED",
            category="transport",
            phase="receive",
            retryable_transport=True,
            external_outcome="unknown",
        ) from exc


def frame_json(text):
    try:
        value = json_copy(json.loads(text))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, AgentLoomError) as exc:
        raise AgentLoomError("MODEL_PROTOCOL_ERROR", "Invalid event JSON") from exc


def tool_arguments(text):
    """Keep invalid argument text for structured tool feedback, never persist NaN."""
    try:
        return json_copy(json.loads(text))
    except (ValueError, TypeError, AgentLoomError):
        return text
