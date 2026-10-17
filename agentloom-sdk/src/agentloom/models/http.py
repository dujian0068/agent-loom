"""Transport framing shared by explicit protocol adapters, with no automatic retries."""

import json
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from ..configuration import json_copy
from ..errors import AgentLoomError
from .limits import MAX_MODEL_RESPONSE_BYTES
from .provider_errors import MAX_PROVIDER_ERROR_BYTES, provider_error


def retry_after_deadline(value):
    """Convert Retry-After to an absolute deadline once, when headers arrive."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            # The obsolete HTTP asctime format has no explicit GMT suffix.
            deadline = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except (ValueError, TypeError, OverflowError):
            return None
    else:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        deadline = time.time() + seconds
    return deadline if math.isfinite(deadline) else None


def http_error(status, headers=None):
    retry = status in {429, 502, 503, 504}
    return AgentLoomError(
        "HTTP_" + str(status),
        category="rate_limit" if status == 429 else "transport",
        retryable_transport=retry,
        phase="receive",
        external_outcome="unknown",
        retry_after_at=retry_after_deadline((headers or {}).get("retry-after")),
    )


async def sse_data(response):
    if response.status_code >= 400:
        error = http_error(response.status_code, response.headers)
        if response.status_code == 400:
            body = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_PROVIDER_ERROR_BYTES:
                        break
                    body.extend(chunk)
                else:
                    error = provider_error(json.loads(body), error)
            except (ValueError, UnicodeError, httpx.TransportError):
                pass
        raise error
    if "text/event-stream" not in response.headers.get("content-type", ""):
        raise AgentLoomError("MODEL_PROTOCOL_ERROR", "Expected a server-sent event stream")
    lines, size = [], 0
    try:
        async for line in response.aiter_lines():
            size += len(line.encode())
            if size > MAX_MODEL_RESPONSE_BYTES:
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
