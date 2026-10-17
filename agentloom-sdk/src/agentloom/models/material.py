"""Validate model material and retain bounded, attempt-local incomplete streams."""

import time

from ..configuration import json_copy
from ..configuration.json_values import MAX_JSON_INTEGER
from ..errors import AgentLoomError
from .limits import MAX_MODEL_RESPONSE_BYTES


def protocol_error(message="Invalid model response"):
    return AgentLoomError(
        "MODEL_PROTOCOL_ERROR",
        message,
        category="protocol",
        phase="receive",
        external_outcome="unknown",
    )


def validate_usage(usage):
    """Missing counters stay unknown; malformed statistics are protocol errors."""
    if usage is None:
        return
    if not isinstance(usage, dict):
        raise protocol_error("Model usage must be an object or null")
    for key in ("input_tokens", "output_tokens"):
        count = usage.get(key)
        if count is not None and (type(count) is not int or not 0 <= count <= MAX_JSON_INTEGER):
            raise protocol_error("Model token counts must be safe nonnegative integers or null")


def normalize_usage(value, input_key="input_tokens", output_key="output_tokens"):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise protocol_error("Model usage must be an object or null")
    usage = {"input_tokens": value.get(input_key), "output_tokens": value.get(output_key)}
    validate_usage(usage)
    return usage


def merge_usage(previous, current):
    """Merge cumulative provider counters without erasing earlier known values."""
    if current is None:
        return previous
    return {
        key: current.get(key) if current.get(key) is not None else (previous or {}).get(key)
        for key in ("input_tokens", "output_tokens")
    }


def validate_response(value):
    try:
        response = json_copy(value)
    except AgentLoomError as exc:
        raise protocol_error("Model response must contain canonical JSON") from exc
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("text"), str)
        or not isinstance(response.get("tool_calls"), list)
        or not isinstance(response.get("provider_state", {}), dict)
    ):
        raise protocol_error()
    validate_usage(response.get("usage"))
    identities = set()
    for call in response["tool_calls"]:
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("name"), str)
            or not call["name"]
            or not isinstance(call.get("call_id"), str)
            or not call["call_id"]
            or "arguments" not in call
            or call["call_id"] in identities
        ):
            raise protocol_error("Invalid or duplicate tool call identity")
        identities.add(call["call_id"])
    return response


class PartialMaterial:
    """Audit fragments only. Never interpret them as executable tool decisions."""

    def __init__(self):
        self.text = []
        self.reasoning = []
        self.calls = {}
        self.size = 0
        self.last_received_at = None
        self.truncated = False

    def add(self, frame):
        kind = frame.kind
        if kind not in {"text_delta", "reasoning_delta", "tool_name_delta", "tool_arguments_delta"}:
            return
        if not isinstance(frame.data, str):
            raise protocol_error("Model delta must be a string")
        if not frame.data:
            return
        size = len(frame.data.encode("utf-8"))
        if self.size + size > MAX_MODEL_RESPONSE_BYTES:
            self.truncated = True
            raise AgentLoomError("MODEL_RESPONSE_TOO_LARGE", category="protocol")
        self.size += size
        self.last_received_at = time.time()
        if kind == "text_delta":
            self.text.append(frame.data)
        elif kind == "reasoning_delta":
            self.reasoning.append(frame.data)
        else:
            index, call_id = frame.provider_index, frame.provider_call_id
            if index is not None and (type(index) is not int or index < 0):
                raise protocol_error("Invalid tool fragment index")
            if call_id is not None and not isinstance(call_id, str):
                raise protocol_error("Invalid tool fragment identity")
            # Unidentified fragments stay separate; do not invent an association.
            key = (
                ("index", index)
                if index is not None
                else ("id", call_id)
                if call_id
                else ("unidentified", len(self.calls))
            )
            call = self.calls.setdefault(
                key,
                {
                    "provider_index": index,
                    "provider_call_id": call_id,
                    "name_fragments": [],
                    "arguments_fragments": [],
                    "complete": False,
                },
            )
            if call_id:
                if call["provider_call_id"] not in (None, call_id):
                    raise protocol_error("Tool fragment identity changed")
                call["provider_call_id"] = call_id
            field = "name_fragments" if kind == "tool_name_delta" else "arguments_fragments"
            call[field].append(frame.data)

    def snapshot(self):
        if not (self.text or self.reasoning or self.calls or self.truncated):
            return None
        return {
            "text": "".join(self.text),
            "text_fragments": list(self.text),
            "reasoning_fragments": list(self.reasoning),
            "incomplete_calls": json_copy(list(self.calls.values())),
            "complete": False,
            "last_received_at": self.last_received_at,
            "truncated": self.truncated,
        }
