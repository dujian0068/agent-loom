"""Validate extension outcomes before any durable or model-visible projection."""

from dataclasses import dataclass

from ..configuration import json_copy
from ..errors import AgentLoomError, ConfigurationError, InfrastructureError
from ..operation_errors import normalize_operation_error
from .contracts import ToolResult


def invalid(code="CAPABILITY_RESULT_INVALID"):
    return AgentLoomError(code, category="protocol", external_outcome="known")


def result_from_value(value):
    """Wrap only after a value-returning function has completed successfully."""
    try:
        return ToolResult.from_value(value)
    except ConfigurationError as exc:
        error = invalid()
        error.execution_fact = "completed"
        raise error from exc


def report_completion(context, *, value=None, error=None):
    """Fix a value adapter's known response before closing its transport.

    The host callback performs the same result/Schema validation as the ordinary
    return path and commits under the active lease. Callback failures propagate;
    a storage failure must never be downgraded to a transport diagnostic.
    """
    report = getattr(context, "report_completion", None)
    if report is None:
        return
    if error is not None:
        report(error=error)
        return
    try:
        result = result_from_value(value)
    except AgentLoomError as exc:
        report(error=exc)
        raise
    report(result)


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
        value = await adapter.invoke(request.capability_key, request.arguments, context)
        result = result_from_value(value)
    return normalize_completion(result)


def normalize_completion(result):
    """Normalize an already returned envelope, retaining its completion evidence."""
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


def normalize_error_completion(error):
    """Validate a reported failure through the returned ToolResult boundary.

    Reporting an error is distinct from raising one during adapter execution.
    Its explicit external outcome determines the execution fact; private error
    attributes must not override that fact or bypass canonical JSON validation.
    """
    if isinstance(error, InfrastructureError) or (
        isinstance(error, BaseException) and not isinstance(error, Exception)
    ):
        raise error
    value = getattr(error, "result_material", None)
    block = (
        {"type": "text", "text": value}
        if isinstance(value, str)
        else {"type": "json", "json_ref": {"kind": "inline", "value": value}}
    )
    return normalize_completion(
        {
            "status": "failed",
            "error": error.to_dict() if isinstance(error, AgentLoomError) else None,
            "content": [block],
            "artifacts": [],
            "metadata": {},
        }
    )


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
        try:
            envelope["error"] = normalize_operation_error(error)
        except (ValueError, ConfigurationError) as exc:
            raise invalid() from exc
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
