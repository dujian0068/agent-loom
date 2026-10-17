"""Validate extension outcomes before any durable or model-visible projection."""

from dataclasses import dataclass

from ..configuration import json_copy
from ..errors import AgentLoomError, ConfigurationError
from .contracts import ToolResult


def invalid(code="CAPABILITY_RESULT_INVALID"):
    return AgentLoomError(code, category="protocol", external_outcome="known")


@dataclass(frozen=True)
class ToolOutcome:
    value: object
    envelope: dict

    def raw_result(self):
        status = self.envelope["status"]
        return {
            "status": "succeeded" if status == "succeeded" else "failed",
            "result": self.value,
            "error": self.envelope["error"],
            "execution_fact": "not_started"
            if self.envelope["error"]
            and self.envelope["error"]["external_outcome"] == "not_started"
            else "unknown"
            if self.envelope["error"] and self.envelope["error"]["external_outcome"] == "unknown"
            else "completed",
            "tool_result": self.envelope,
        }


async def invoke_adapter(adapter, request, context):
    """Select the contract before calling; never retry on a TypeError or fallback after I/O."""
    once = getattr(adapter, "invoke_once", None)
    if callable(once):
        result = await once(request, context)
    else:
        # Compatibility for pre-contract user adapters. Returned dicts are values,
        # even when they contain status/error/content business fields.
        result = ToolResult.from_value(
            await adapter.invoke(request.capability_key, request.arguments, context)
        )
    try:
        return normalize_result(result)
    except AgentLoomError as exc:
        # The adapter returned, even if its result cannot be delivered. Keep
        # malformed but canonical content in audit, not in model feedback.
        exc.execution_fact = "completed"
        try:
            exc.invalid_tool_result = {
                "value": json_copy(result.to_dict() if isinstance(result, ToolResult) else result)
            }
        except ConfigurationError:
            pass
        raise


def normalize_result(result):
    try:
        envelope = json_copy(result.to_dict() if isinstance(result, ToolResult) else result)
    except ConfigurationError as exc:
        raise invalid() from exc
    required = {"status", "content", "error", "artifacts", "metadata"}
    if not isinstance(envelope, dict) or set(envelope) != required:
        raise invalid()
    status, error = envelope["status"], envelope["error"]
    if (
        not isinstance(status, str)
        or status not in {"succeeded", "failed", "unknown"}
        or not isinstance(envelope["metadata"], dict)
    ):
        raise invalid()
    if not isinstance(envelope["content"], list) or not isinstance(envelope["artifacts"], list):
        raise invalid()
    if status == "succeeded":
        if error is not None:
            raise invalid()
    else:
        if not isinstance(error, dict) or not all(
            isinstance(error.get(k), str) and error[k]
            for k in ("code", "message", "category", "phase", "external_outcome")
        ):
            raise invalid()
        if type(error.get("retryable_transport", False)) is not bool:
            raise invalid()
        if error["external_outcome"] not in {"not_started", "known", "unknown"}:
            raise invalid()
        if error["category"] not in {
            "validation",
            "permission",
            "not_found",
            "conflict",
            "transport",
            "rate_limit",
            "timeout",
            "business",
            "protocol",
            "cancelled",
            "unknown",
        } or error["phase"] not in {"prepare", "connect", "send", "receive", "execute", "after"}:
            raise invalid()
        if status == "unknown" and error["external_outcome"] != "unknown":
            raise invalid()
    if envelope["artifacts"]:
        raise invalid("CAPABILITY_ARTIFACT_UNSUPPORTED")
    values = []
    for block in envelope["content"]:
        if not isinstance(block, dict):
            raise invalid()
        if (
            block.get("type") == "text"
            and set(block) == {"type", "text"}
            and isinstance(block["text"], str)
        ):
            values.append(block["text"])
        elif block.get("type") == "json" and set(block) == {"type", "json_ref"}:
            ref = block["json_ref"]
            if not isinstance(ref, dict) or ref.get("kind") != "inline":
                raise invalid("CAPABILITY_CONTENT_REFERENCE_UNSUPPORTED")
            if set(ref) != {"kind", "value"}:
                raise invalid()
            values.append(ref["value"])
        elif block.get("type") == "artifact_ref":
            raise invalid("CAPABILITY_ARTIFACT_UNSUPPORTED")
        else:
            raise invalid()
    value = values[0] if len(values) == 1 else {"content": envelope["content"]}
    return ToolOutcome(value, envelope)
